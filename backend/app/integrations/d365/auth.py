"""Entra OAuth client credentials with a server-memory-only access token cache."""

import asyncio
import math
import re
import time

import httpx

from .errors import D365AuthenticationError

OAUTH_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
        "interaction_required",
        "consent_required",
    }
)


def provider_diagnostic(response):
    """Extract only standardized codes; never return Entra's descriptive text."""
    summary = [f"HTTP {response.status_code}"]
    try:
        payload = response.json()
    except ValueError:
        return "; ".join(summary)
    if not isinstance(payload, dict):
        return "; ".join(summary)
    oauth_code = payload.get("error")
    if isinstance(oauth_code, str) and oauth_code in OAUTH_ERROR_CODES:
        summary.append(oauth_code)
    codes = payload.get("error_codes")
    aadsts_code = (
        next((code for code in codes if type(code) is int and 10000 <= code <= 99999999), None)
        if isinstance(codes, list)
        else None
    )
    if aadsts_code is None:
        description = payload.get("error_description")
        match = (
            re.search(r"\bAADSTS(\d{5,8})\b", description[:10000]) if isinstance(description, str) else None
        )
        aadsts_code = int(match.group(1)) if match else None
    if aadsts_code is not None:
        summary.append(f"AADSTS{aadsts_code}")
    return "; ".join(summary)


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
                    raise D365AuthenticationError(
                        f"Microsoft Entra rejected Dynamics 365 authentication ({provider_diagnostic(response)}). "
                        "Check the tenant, client ID, client secret, and application access."
                    )
                token = response.json()
                if not isinstance(token, dict):
                    raise D365AuthenticationError(
                        "Microsoft Entra returned an invalid OAuth token response (HTTP 200). Reconnect and check the identity endpoint."
                    )
                access_token = token.get("access_token")
                expires_in = float(token.get("expires_in", 3600))
                if (
                    not isinstance(access_token, str)
                    or not access_token
                    or not math.isfinite(expires_in)
                    or expires_in <= 0
                ):
                    raise D365AuthenticationError(
                        "Microsoft Entra returned an invalid OAuth token response (HTTP 200). Reconnect and check the identity endpoint."
                    )
                self._token = access_token
                self._expires_at = time.monotonic() + expires_in
                return access_token
            except httpx.TimeoutException:
                raise D365AuthenticationError(
                    "Microsoft Entra authentication timed out. Check outbound HTTPS access to login.microsoftonline.com and try reconnecting."
                ) from None
            except httpx.HTTPError:
                raise D365AuthenticationError(
                    "Microsoft Entra authentication could not reach login.microsoftonline.com. Check network, proxy, and TLS settings before reconnecting."
                ) from None
            except (ValueError, TypeError):
                raise D365AuthenticationError(
                    "Microsoft Entra returned an invalid OAuth token response. Reconnect and check the identity endpoint."
                ) from None

    def clear(self):
        self._token = None
        self._expires_at = 0.0
