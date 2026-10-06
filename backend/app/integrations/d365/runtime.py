"""Application lifecycle and connection state; live and mock remain strictly separate."""

import asyncio
import time

import httpx
import structlog

from app.core.errors import AppError

from .auth import D365AuthManager
from .client import D365ODataClient
from .finance import D365FinanceService, utcnow
from .live import LiveD365Provider
from .metadata import D365MetadataResolver
from .mock import MockD365Provider

logger = structlog.get_logger(__name__)
STAGE_DESCRIPTIONS = {
    "authenticating": "authenticating with Microsoft Entra",
    "checking_customers": "checking customer data access",
    "loading_metadata": "downloading tenant metadata",
    "loading_cached_metadata": "loading cached tenant metadata",
    "discovering_entities": "checking transaction entities",
}


class D365Runtime:
    def __init__(self, settings, http_client=None):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._state = "disconnected"
        self._last_error = None
        self._last_success = None
        self._last_health_check = 0.0
        self._connection_in_progress = False
        self._connection_stage = None
        self._connection_started_at = None
        self._connection_elapsed = 0.0
        self._connection_phase_started_at = None
        self._connection_phase_elapsed = 0.0
        self._connection_phase_timeout = settings.d365_connection_timeout_seconds
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=getattr(settings, "d365_timeout_seconds", 30), follow_redirects=False
        )
        self.auth = D365AuthManager(settings, self.http)
        self.client = D365ODataClient(settings, self.auth, self.http, self._request_failed)
        self.metadata = D365MetadataResolver(self.client, settings)
        self.provider = (
            MockD365Provider(settings)
            if settings.d365_mock_mode
            else LiveD365Provider(
                settings,
                self.client,
                self.metadata.registry,
                lambda: self._state in {"connected", "degraded"},
            )
        )
        self.finance = D365FinanceService(self.provider, settings)

    def _failed(self, message):
        self._state = "disconnected"
        self._last_error = message
        self._connection_stage = "failed"

    def _request_failed(self, message):
        # Optional entity permissions may fail during discovery; reconnect decides the final state.
        if not self._connection_in_progress:
            self._failed(message)

    def _set_stage(self, stage):
        self._connection_stage = stage
        logger.info("d365_connection_stage", stage=stage)

    async def start(self):
        return await self.reconnect(initial=True)

    async def reconnect(self, initial=False, refresh_metadata=False):
        # Coalesce clicks/polls during an existing attempt instead of queuing another metadata scan.
        if self._connection_in_progress or self._lock.locked():
            return self.status()
        async with self._lock:
            self._connection_in_progress = True
            self._connection_started_at = time.monotonic()
            self._connection_elapsed = 0.0
            self._connection_phase_started_at = self._connection_started_at
            self._connection_phase_elapsed = 0.0
            self._connection_phase_timeout = self.settings.d365_connection_timeout_seconds
            self._state = "connecting" if initial else "reconnecting"
            self._last_error = None
            error_code = None
            try:
                if self.settings.d365_mock_mode:
                    self._state = "connected"
                    self._last_success = utcnow().isoformat()
                else:
                    # Keep authentication and customer access bounded independently of the
                    # potentially large, cold D365 schema download.
                    async with asyncio.timeout(self._connection_phase_timeout):
                        self._set_stage("authenticating")
                        await self.auth.get_token(force_refresh=True)
                        # Verify primary data access before optional metadata discovery.
                        self._set_stage("checking_customers")
                        await self.client.get(self.settings.d365_customers_entity, top=1)
                    self._connection_phase_started_at = time.monotonic()
                    self._connection_phase_timeout = self.settings.d365_metadata_timeout_seconds
                    async with asyncio.timeout(self._connection_phase_timeout):
                        self._set_stage("loading_metadata")
                        if refresh_metadata:
                            await self.metadata.load(on_stage=self._set_stage, force_refresh=True)
                        else:
                            await self.metadata.load(on_stage=self._set_stage)
                        self._state = (
                            "connected" if all(self.metadata.registry.resolved.values()) else "degraded"
                        )
                        self._last_success = self.client.last_success_at
                        self._last_health_check = time.monotonic()
                self._last_error = None
                self._set_stage("ready")
            except TimeoutError:
                error_code = "D365_CONNECTION_TIMEOUT"
                stage = STAGE_DESCRIPTIONS.get(self._connection_stage, "checking the connection")
                self._failed(
                    f"Dynamics 365 connection timed out after {self._connection_phase_timeout:g} "
                    f"seconds while {stage}. Check network access and backend connection diagnostics, then reconnect."
                )
            except AppError as exc:
                error_code = exc.code
                self._failed(exc.message)
            except asyncio.CancelledError:
                error_code = "D365_CONNECTION_CANCELLED"
                self._failed("The Dynamics 365 connection check was cancelled. Reconnect to try again.")
                raise
            except Exception as exc:
                error_code = "D365_CONNECTION_CHECK_FAILED"
                stage = STAGE_DESCRIPTIONS.get(self._connection_stage, "checking the connection")
                logger.error(
                    "d365_connection_failed", stage=self._connection_stage, exception_type=type(exc).__name__
                )
                self._failed(
                    f"Dynamics 365 connection could not be verified while {stage}. "
                    "Check backend connection diagnostics and configuration, then reconnect."
                )
            finally:
                self._connection_elapsed = time.monotonic() - self._connection_started_at
                self._connection_phase_elapsed = time.monotonic() - self._connection_phase_started_at
                self._connection_in_progress = False
                logger.info(
                    "d365_connection_finished",
                    status=self._state,
                    stage=self._connection_stage,
                    elapsed_ms=round(self._connection_elapsed * 1000),
                    phase_elapsed_ms=round(self._connection_phase_elapsed * 1000),
                    phase_timeout_seconds=self._connection_phase_timeout,
                    error_code=error_code,
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
                async with asyncio.timeout(self.settings.d365_timeout_seconds):
                    await self.client.get(self.settings.d365_customers_entity, top=1)
                self._last_success = self.client.last_success_at
            except TimeoutError:
                self._failed(
                    "Dynamics 365 customer health check timed out. Check network access and reconnect."
                )
            except AppError as exc:
                self._failed(exc.message)
            except Exception as exc:
                logger.error("d365_health_check_failed", exception_type=type(exc).__name__)
                self._failed(
                    "Dynamics 365 health could not be verified. Check backend diagnostics and reconnect."
                )
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
            "metadata_source": "mock"
            if self.settings.d365_mock_mode
            else getattr(self.metadata, "cache_source", None),
            "capabilities": capabilities,
            "latency_ms": self.client.latency_ms,
            "mock_mode": self.settings.d365_mock_mode,
            "connection_stage": self._connection_stage,
            "connection_elapsed_seconds": round(
                time.monotonic() - self._connection_started_at
                if self._connection_in_progress
                else self._connection_elapsed,
                1,
            ),
            "connection_timeout_seconds": (
                self.settings.d365_connection_timeout_seconds + self.settings.d365_metadata_timeout_seconds
            ),
            "connection_phase_elapsed_seconds": round(
                time.monotonic() - self._connection_phase_started_at
                if self._connection_in_progress
                else self._connection_phase_elapsed,
                1,
            ),
            "connection_phase_timeout_seconds": self._connection_phase_timeout,
        }

    def diagnostics(self):
        return {
            "mock_mode": self.settings.d365_mock_mode,
            "metadata_loaded": self.settings.d365_mock_mode or self.metadata.registry.loaded,
            "metadata_source": "mock"
            if self.settings.d365_mock_mode
            else getattr(self.metadata, "cache_source", None),
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
