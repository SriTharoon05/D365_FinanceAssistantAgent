"""Constrained OData transport, including safe pagination and read-only retry policy."""

import asyncio
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urljoin, urlsplit
import posixpath
from uuid import uuid4

import httpx
import structlog

from app.core.errors import AppError

from .errors import D365ConnectionError, map_http_error

logger = structlog.get_logger(__name__)
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def odata_literal(value: str) -> str:
    """OData string literals escape apostrophes by doubling, then httpx URL-encodes."""
    return "'" + str(value).replace("'", "''") + "'"


def escape_account_display_value(account: str) -> str:
    return account.replace("\\", "\\\\").replace("-", "\\-")


def entity_key(entity: str, fields: dict) -> str:
    if not IDENTIFIER.fullmatch(entity) or not fields or any(not IDENTIFIER.fullmatch(k) for k in fields):
        raise ValueError("Invalid OData entity or key field")
    literals = (
        f"{key}={quote(odata_literal(value), safe="'") if isinstance(value, str) else value}"
        for key, value in fields.items()
    )
    return entity + "(" + ",".join(literals) + ")"


class D365ODataClient:
    def __init__(self, settings, auth, client: httpx.AsyncClient, on_failure=None):
        self.settings, self.auth, self.client = settings, auth, client
        self.base_url = settings.d365_base_url.rstrip("/")
        self.on_failure = on_failure
        self.last_success_at = None
        self.latency_ms = None

    def _safe_url(self, path: str) -> str:
        url = urljoin(self.base_url + "/data/", path)
        parsed, root = urlsplit(url), urlsplit(self.base_url)
        if (
            (parsed.scheme, parsed.netloc) != (root.scheme, root.netloc)
            or not parsed.path.startswith("/data/")
            or not posixpath.normpath(unquote(parsed.path)).startswith("/data/")
            or parsed.fragment
            or parsed.username
        ):
            raise AppError(
                "D365_UNSAFE_URL", "An unsafe OData continuation URL was rejected.", status_code=502
            )
        return url

    async def request(self, method, path, *, params=None, payload=None, retry_reads=True):
        url = self._safe_url(path)
        method = method.upper()
        retries, refreshed, force_refresh = 0, False, False
        while True:
            try:
                token = await self.auth.get_token(force_refresh=force_refresh)
            except AppError as exc:
                self._failed(exc.message)
                raise
            force_refresh = False
            request_id = str(uuid4())
            started = time.monotonic()
            try:
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/xml"
                        if urlsplit(url).path.endswith("$metadata")
                        else "application/json;IEEE754Compatible=true",
                        "client-request-id": request_id,
                        "return-client-request-id": "true",
                        **(
                            {"Content-Type": "application/json;IEEE754Compatible=true"}
                            if payload is not None
                            else {}
                        ),
                    },
                )
            except httpx.HTTPError:
                self._failed("Dynamics 365 network request failed.")
                if method != "GET":
                    raise AppError(
                        "D365_WRITE_OUTCOME_UNKNOWN",
                        "The financial write outcome requires verification in Dynamics 365 before trying again. The write was not automatically retried.",
                        status_code=409,
                    ) from None
                raise D365ConnectionError() from None
            self.latency_ms = round((time.monotonic() - started) * 1000)
            logger.info(
                "d365_request",
                entity=urlsplit(url).path.split("/")[-1].split("(")[0],
                http_status=response.status_code,
                duration_ms=self.latency_ms,
                retry_count=retries,
                request_id=request_id,
            )
            if response.status_code == 401 and not refreshed:
                refreshed, force_refresh = True, True
                continue
            if (
                method == "GET"
                and retry_reads
                and (response.status_code == 429 or response.status_code >= 500)
                and retries < getattr(self.settings, "d365_max_retries", 2)
            ):
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (
                            parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                        ).total_seconds()
                    except (ValueError, TypeError):
                        delay = 0.25 * 2**retries
                await asyncio.sleep(max(0, min(delay, 30)))
                retries += 1
                continue
            if response.status_code >= 400:
                if response.status_code in (401, 403, 429) or response.status_code >= 500:
                    self._failed(f"Dynamics 365 returned HTTP {response.status_code}.")
                if method != "GET" and response.status_code >= 500:
                    raise AppError(
                        "D365_WRITE_OUTCOME_UNKNOWN",
                        "Dynamics 365 returned a server error after a write. Verify the outcome before retrying.",
                        status_code=409,
                    )
                raise map_http_error(response.status_code)
            self.last_success_at = datetime.now(timezone.utc).isoformat()
            return response

    def _failed(self, message):
        if self.on_failure:
            self.on_failure(message)

    async def get(self, entity, *, filter=None, select=None, top=None, cross_company=False, retry_reads=True):
        if not IDENTIFIER.fullmatch(entity):
            raise ValueError("Invalid entity collection")
        params = {}
        if filter:
            params["$filter"] = filter
        if select:
            params["$select"] = ",".join(select)
        if top is not None:
            params["$top"] = str(top)
        if cross_company:
            params["cross-company"] = "true"
        records, next_path, seen = [], entity, set()
        while next_path:
            if next_path in seen:
                raise AppError(
                    "D365_PAGINATION_ERROR",
                    "Dynamics 365 returned a repeated continuation link.",
                    status_code=502,
                )
            seen.add(next_path)
            response = await self.request("GET", next_path, params=params, retry_reads=retry_reads)
            try:
                body = response.json()
                page = body["value"]
                if not isinstance(page, list):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise AppError(
                    "D365_RESPONSE_INVALID", "Dynamics 365 returned invalid OData records.", status_code=502
                ) from None
            records.extend(page)
            if len(records) > 100000:
                raise AppError(
                    "D365_RESULT_LIMIT",
                    "The result exceeds the safe record limit. Narrow the finance query.",
                    status_code=422,
                )
            if top is not None and len(records) >= top:
                return records[:top]
            next_path = body.get("@odata.nextLink")
            params = None
        return records

    async def post(self, entity, payload):
        if not IDENTIFIER.fullmatch(entity):
            raise ValueError("Invalid entity collection")
        response = await self.request("POST", entity, payload=payload)
        return response.json() if response.content else {}

    async def patch(self, key, payload):
        response = await self.request("PATCH", key, payload=payload)
        return response.json() if response.content else {}

    async def delete(self, key):
        await self.request("DELETE", key)
        return {"deleted": True}
