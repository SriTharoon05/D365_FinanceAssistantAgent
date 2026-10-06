import json

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.auth import D365AuthManager
from app.integrations.d365.client import (
    D365ODataClient,
    entity_key,
    escape_account_display_value,
    odata_literal,
)
from app.integrations.d365.runtime import D365Runtime


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        d365_base_url="https://erp.example.test",
        d365_tenant_id="tenant-id",
        d365_client_id="client-id",
        d365_client_secret="not-a-real-secret",
        d365_max_retries=2,
    )


def token_response():
    return httpx.Response(200, json={"access_token": "in-memory-only-test-token", "expires_in": 3600})


@pytest.mark.asyncio
async def test_oauth_scope_and_memory_cache(settings):
    calls = []

    def transport(request):
        calls.append(request)
        assert request.url == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
        assert b"scope=https%3A%2F%2Ferp.example.test%2F.default" in request.content
        return token_response()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        auth = D365AuthManager(settings, http)
        assert await auth.get_token() == await auth.get_token()
        assert len(calls) == 1
        auth._expires_at = 0
        await auth.get_token()
        assert len(calls) == 2
        auth.clear()
        assert auth._token is None


@pytest.mark.asyncio
async def test_401_refreshes_once_and_retries(settings):
    auth_calls, read_calls = [], []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            auth_calls.append(request)
            return token_response()
        read_calls.append(request)
        return (
            httpx.Response(401)
            if len(read_calls) == 1
            else httpx.Response(200, json={"value": [{"CustomerAccount": "AST-001"}]})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        rows = await client.get("CustomersV3")
        assert rows[0]["CustomerAccount"] == "AST-001"
        assert len(read_calls) == len(auth_calls) == 2


@pytest.mark.asyncio
async def test_repeated_401_disconnects_runtime(settings):
    def transport(request):
        return token_response() if request.url.host == "login.microsoftonline.com" else httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = D365Runtime(settings, http_client=http)
        status = await runtime.reconnect()
        assert status["status"] == "disconnected"
        assert "authentication" in status["last_error_summary"]
        assert settings.d365_client_secret not in json.dumps(status)
        await runtime.close()


@pytest.mark.asyncio
async def test_rate_limit_read_retries_honors_retry_after(settings, monkeypatch):
    calls, delays = [], []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.integrations.d365.client.asyncio.sleep", sleep)

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "3"})
            if len(calls) == 1
            else httpx.Response(200, json={"value": []})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        assert await client.get("CustomersV3") == []
        assert delays == [3]
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_safe_filter_encoding_preserves_apostrophes(settings):
    captured = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        captured.append(request)
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        await client.get(
            "CustomersV3", filter="OrganizationName eq " + odata_literal("O'Brien & Co"), cross_company=True
        )
        assert captured[0].url.params["$filter"] == "OrganizationName eq 'O''Brien & Co'"
        assert captured[0].url.params["cross-company"] == "true"
        assert "%26" in str(captured[0].url)


@pytest.mark.asyncio
async def test_pagination_follows_safe_nextlink_and_rejects_external(settings):
    reads = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        reads.append(request)
        if len(reads) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [{"id": 1}],
                    "@odata.nextLink": "https://erp.example.test/data/CustomersV3?$skiptoken=page2",
                },
            )
        return httpx.Response(
            200, json={"value": [{"id": 2}], "@odata.nextLink": "https://attacker.example/data/CustomersV3"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        with pytest.raises(AppError) as error:
            await client.get("CustomersV3")
        assert error.value.code == "D365_UNSAFE_URL"
        assert len(reads) == 2


@pytest.mark.asyncio
async def test_ambiguous_write_is_never_retried(settings):
    writes = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        writes.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        with pytest.raises(AppError) as error:
            await client.post("CustomerPaymentJournalLines", {"LineNumber": 1})
        assert error.value.code == "D365_WRITE_OUTCOME_UNKNOWN"
        assert len(writes) == 1


@pytest.mark.asyncio
async def test_timeout_after_write_requires_verification(settings):
    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        raise httpx.ReadTimeout("simulated timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        with pytest.raises(AppError) as error:
            await client.post("CustomerPaymentJournalHeaders", {"JournalName": "CustPay"})
        assert error.value.code == "D365_WRITE_OUTCOME_UNKNOWN"


def test_account_display_escaping_and_safe_keys():
    assert escape_account_display_value("AST-001") == r"AST\-001"
    assert escape_account_display_value(r"A\B-C") == r"A\\B\-C"
    key = entity_key("CustomersV3", {"dataAreaId": "usmf", "CustomerAccount": "O'Brien?x#z"})
    assert "O''Brien%3Fx%23z" in key
    assert odata_literal("a' or 1 eq 1") == "'a'' or 1 eq 1'"


@pytest.mark.asyncio
async def test_401_then_429_only_refreshes_token_once(settings, monkeypatch):
    token_calls, reads = [], []

    async def sleep(delay):
        return None

    monkeypatch.setattr("app.integrations.d365.client.asyncio.sleep", sleep)

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            token_calls.append(request)
            return token_response()
        reads.append(request)
        return (
            httpx.Response(401 if len(reads) == 1 else 429)
            if len(reads) <= 2
            else httpx.Response(200, json={"value": []})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        await client.get("CustomersV3")
        assert len(token_calls) == 2
        assert len(reads) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_read_retry_can_be_disabled_for_connection_probes(settings, monkeypatch, status):
    calls = []

    async def sleep(delay):
        raise AssertionError("Retries were disabled")

    monkeypatch.setattr("app.integrations.d365.client.asyncio.sleep", sleep)

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        with pytest.raises(AppError):
            await client.get("CustomersV3", top=1, retry_reads=False)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "not-a-token-object",
        {},
        {"access_token": "test-token", "expires_in": None},
        {"access_token": "test-token", "expires_in": "NaN"},
    ],
)
async def test_malformed_oauth_response_is_structured_without_echoing_payload(settings, payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        auth = D365AuthManager(settings, http)
        with pytest.raises(AppError) as error:
            await auth.get_token()
        assert error.value.code == "D365_AUTHENTICATION_ERROR"
        assert "invalid OAuth token response" in error.value.message
        assert "test-token" not in error.value.message
        assert auth._token is None


@pytest.mark.asyncio
async def test_oauth_diagnostic_exposes_only_status_and_standard_codes(settings):
    secret = "this-is-the-client-secret"
    body = {
        "error": "invalid_client",
        "error_description": f"AADSTS7000215: secret {secret} client-id sensitive-identifier",
        "error_codes": [7000215],
        "access_token": "sensitive-token",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body))
    ) as http:
        auth = D365AuthManager(settings, http)
        with pytest.raises(AppError) as error:
            await auth.get_token()
        message = error.value.message
        assert "HTTP 400" in message
        assert "invalid_client" in message
        assert "AADSTS7000215" in message
        assert all(value not in message for value in (secret, "sensitive-identifier", "sensitive-token"))


@pytest.mark.asyncio
async def test_unknown_oauth_error_and_description_are_redacted(settings):
    body = {
        "error": "my-secret-code",
        "error_description": "AADSTS700016: private tenant context and a secret",
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json=body))
    ) as http:
        with pytest.raises(AppError) as error:
            await D365AuthManager(settings, http).get_token()
        assert "AADSTS700016" in error.value.message
        assert "my-secret-code" not in error.value.message
        assert "private tenant context" not in error.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception_type,expected", [(httpx.ReadTimeout, "timed out"), (httpx.ConnectError, "could not reach")]
)
async def test_oauth_network_errors_have_safe_specific_diagnostics(settings, exception_type, expected):
    def transport(request):
        raise exception_type("secret-text-in-provider-exception", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        with pytest.raises(AppError) as error:
            await D365AuthManager(settings, http).get_token()
        assert expected in error.value.message
        assert "login.microsoftonline.com" in error.value.message
        assert "secret-text" not in error.value.message
