"""Application lifecycle and connection state; live and mock remain strictly separate."""

import asyncio
import time

import httpx

from app.core.errors import AppError

from .auth import D365AuthManager
from .client import D365ODataClient
from .finance import D365FinanceService, utcnow
from .live import LiveD365Provider
from .metadata import D365MetadataResolver
from .mock import MockD365Provider


class D365Runtime:
    def __init__(self, settings, http_client=None):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._state = "disconnected"
        self._last_error = None
        self._last_success = None
        self._last_health_check = 0.0
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=getattr(settings, "d365_timeout_seconds", 30), follow_redirects=False
        )
        self.auth = D365AuthManager(settings, self.http)
        self.client = D365ODataClient(settings, self.auth, self.http, self._failed)
        self.metadata = D365MetadataResolver(self.client, settings)
        self.provider = (
            MockD365Provider(settings)
            if settings.d365_mock_mode
            else LiveD365Provider(
                settings,
                self.client,
                self.metadata.registry,
                lambda: self._state in {"connected", "degraded", "connecting", "reconnecting"},
            )
        )
        self.finance = D365FinanceService(self.provider, settings)

    def _failed(self, message):
        self._state = "disconnected"
        self._last_error = message

    async def start(self):
        return await self.reconnect(initial=True)

    async def reconnect(self, initial=False):
        async with self._lock:
            self._state = "connecting" if initial else "reconnecting"
            self._last_error = None
            if self.settings.d365_mock_mode:
                self._state = "connected"
                self._last_success = utcnow().isoformat()
                return self.status()
            try:
                await self.auth.get_token(force_refresh=True)
                await self.metadata.load()
                # Metadata access alone does not prove access to the primary customer entity.
                await self.client.get(self.settings.d365_customers_entity, top=1)
                self._state = "connected" if all(self.metadata.registry.resolved.values()) else "degraded"
                self._last_success = self.client.last_success_at
                self._last_health_check = time.monotonic()
            except AppError as exc:
                self._failed(exc.message)
            except (httpx.HTTPError, ValueError):
                self._failed(
                    "Dynamics 365 connection could not be verified. Check backend configuration and network access."
                )
            return self.status()

    async def check_health(self):
        if self.settings.d365_mock_mode or self._state not in {"connected", "degraded"}:
            return self.status()
        if time.monotonic() - self._last_health_check < 30:
            return self.status()
        async with self._lock:
            if time.monotonic() - self._last_health_check < 30:
                return self.status()
            self._last_health_check = time.monotonic()
            try:
                await self.client.get(self.settings.d365_customers_entity, top=1)
                self._last_success = self.client.last_success_at
            except AppError as exc:
                self._failed(exc.message)
        return self.status()

    def status(self):
        if self.settings.d365_mock_mode:
            capabilities = {
                "customers": True,
                "customer_transactions": True,
                "open_transactions": True,
                "resolved_entities": {
                    "customer_transactions": "MockCustomerTransactions",
                    "open_transactions": "MockCustomerOpenTransactions",
                },
                "draft_invoices": True,
                "customer_payment_journals": True,
                "posting": False,
                "settlement": False,
                "diagnostics": [],
            }
        else:
            capabilities = self.metadata.registry.public()
            capabilities["customers"] = self.settings.d365_customers_entity in self.metadata.registry.entities
            capabilities["draft_invoices"] = all(
                entity in self.metadata.registry.entities
                for entity in (
                    self.settings.d365_free_text_headers_entity,
                    self.settings.d365_free_text_lines_entity,
                )
            )
            capabilities["customer_payment_journals"] = all(
                entity in self.metadata.registry.entities
                for entity in (
                    self.settings.d365_payment_headers_entity,
                    self.settings.d365_payment_lines_entity,
                )
            )
        return {
            "status": self._state,
            "company": self.settings.d365_default_company.upper(),
            "last_success_at": self.client.last_success_at or self._last_success,
            "last_error_summary": self._last_error,
            "metadata_loaded": self.settings.d365_mock_mode or self.metadata.registry.loaded,
            "capabilities": capabilities,
            "latency_ms": self.client.latency_ms,
            "mock_mode": self.settings.d365_mock_mode,
        }

    def diagnostics(self):
        return {
            "mock_mode": self.settings.d365_mock_mode,
            "metadata_loaded": self.settings.d365_mock_mode or self.metadata.registry.loaded,
            "resolved_entities": self.status()["capabilities"]["resolved_entities"],
            "candidates": self.metadata.registry.candidates,
            "entities": [info.diagnostic() for _, info in sorted(self.metadata.registry.entities.items())],
            "messages": self.metadata.registry.messages,
            "write_limits": [
                "Only verified unposted draft records may be changed or deleted.",
                "Test customer deletion requires historical transactions and drafts to be absent.",
                "Payment setup must be verified through public standard setup entities.",
                "Posting and settlement are manual Dynamics 365 operations.",
            ],
        }

    async def close(self):
        self.auth.clear()
        if self._owns_client:
            await self.http.aclose()
