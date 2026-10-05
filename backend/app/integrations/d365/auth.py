"""Entra OAuth client credentials with a server-memory-only access token cache."""

import asyncio
import time

import httpx

from .errors import D365AuthenticationError


class D365AuthManager:
    def __init__(self, settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return all(
            getattr(self.settings, key, "")
            for key in ("d365_tenant_id", "d365_client_id", "d365_client_secret")
        )

    async def get_token(self, force_refresh=False) -> str:
        async with self._lock:
            if not force_refresh and self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            if not self.configured:
                raise D365AuthenticationError(
                    "Dynamics 365 credentials are not configured. Set D365_TENANT_ID, D365_CLIENT_ID, and D365_CLIENT_SECRET on the backend."
                )
            tenant = self.settings.d365_tenant_id
            if not all(ch.isalnum() or ch in "-." for ch in tenant):
                raise D365AuthenticationError("D365_TENANT_ID must be a tenant UUID or verified domain.")
            try:
                response = await self.client.post(
                    f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.d365_client_id,
                        "client_secret": self.settings.d365_client_secret,
                        "scope": self.settings.d365_base_url.rstrip("/") + "/.default",
                    },
                )
                if response.status_code != 200:
                    raise D365AuthenticationError()
                token = response.json()
                access_token = token.get("access_token")
                expires_in = float(token.get("expires_in", 3600))
                if not isinstance(access_token, str) or not access_token or expires_in <= 0:
                    raise D365AuthenticationError()
                self._token = access_token
                self._expires_at = time.monotonic() + expires_in
                return access_token
            except (httpx.HTTPError, ValueError, TypeError):
                raise D365AuthenticationError() from None

    def clear(self):
        self._token = None
        self._expires_at = 0.0
