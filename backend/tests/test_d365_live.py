from copy import deepcopy

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.auth import D365AuthManager
from app.integrations.d365.client import D365ODataClient
from app.integrations.d365.finance import D365FinanceService
from app.integrations.d365.live import LiveD365Provider
from app.integrations.d365.metadata import D365CapabilityRegistry, EntityInfo
from app.integrations.d365.runtime import D365Runtime


CUSTOMER = {
    "dataAreaId": "usmf",
    "CustomerAccount": "TEST-LIVE",
    "OrganizationName": "Verified Live Customer",
    "SalesCurrencyCode": "INR",
    "CustomerGroupId": "10",
    "PaymentTerms": "Net30",
}
TRANSACTION = {
    "dataAreaId": "usmf",
    "CustomerAccount": "TEST-LIVE",
    "CurrencyCode": "INR",
    "InvoiceNumber": "REAL-INV-1",
    "TransactionDate": "2026-09-01T12:00:00Z",
    "DueDate": "2026-09-30T00:00:00Z",
    "Voucher": "VOUCHER-1",
    "AmountCur": "123.45",
    "TransactionType": "Microsoft.Dynamics.DataEntities.TransactionType'Invoice'",
}


def registry():
    result = D365CapabilityRegistry()
    definitions = {
        "CustomersV3": (list(CUSTOMER), ["dataAreaId", "CustomerAccount"]),
        "CustomerTransactions": (list(TRANSACTION), ["dataAreaId", "Voucher"]),
        "CustomerOpenTransactions": (
            [field for field in TRANSACTION if field != "TransactionType"],
            ["dataAreaId", "Voucher"],
        ),
        "CDSFreeTextInvoiceHeaders": (
            [
                "dataAreaId",
                "InvoiceCustomerAccount",
                "CurrencyCode",
                "InvoiceIdentifier",
                "DueDate",
                "InvoiceDate",
                "IsPosted",
            ],
            ["dataAreaId", "InvoiceIdentifier"],
        ),
        "CDSFreeTextInvoiceLines": (
            [
                "dataAreaId",
                "InvoiceIdentifier",
                "LineNumber",
                "MainAccountDisplayValue",
                "Description",
                "Amount",
            ],
            ["dataAreaId", "InvoiceIdentifier", "LineNumber"],
        ),
        "CustomerPaymentJournalHeaders": (
            ["dataAreaId", "JournalBatchNumber", "JournalName", "Description", "IsPosted"],
            ["dataAreaId", "JournalBatchNumber"],
        ),
        "CustomerPaymentJournalLines": (
            ["dataAreaId", "JournalBatchNumber", "AccountDisplayValue", "CurrencyCode", "LineNumber"],
            ["dataAreaId", "JournalBatchNumber", "LineNumber"],
        ),
    }
    result.entities = {
        name: EntityInfo(name, name, {field: "Edm.String" for field in fields}, keys)
        for name, (fields, keys) in definitions.items()
    }
    result.resolved = {
        "customer_transactions": "CustomerTransactions",
        "open_transactions": "CustomerOpenTransactions",
    }
    result.loaded = True
    return result


def configured_settings():
    return Settings(
        _env_file=None,
        d365_base_url="https://erp.example.test",
        d365_tenant_id="tenant",
        d365_client_id="app",
        d365_client_secret="test-secret",
        d365_max_retries=0,
    )


def token_response():
    return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})


@pytest.mark.asyncio
async def test_live_uses_fresh_remaining_and_verified_original_transaction():
    open_reads = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        entity = request.url.path.rsplit("/", 1)[-1]
        assert request.url.params["cross-company"] == "true"
        assert "dataAreaId eq 'usmf'" in request.url.params["$filter"]
        if entity == "CustomersV3":
            return httpx.Response(200, json={"value": [CUSTOMER]})
        if entity == "CustomerTransactions":
            return httpx.Response(200, json={"value": [TRANSACTION]})
        open_reads.append(request)
        amount = "67.89" if len(open_reads) == 1 else "17.89"
        return httpx.Response(200, json={"value": [{**TRANSACTION, "AmountCur": amount}]})

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        finance = D365FinanceService(LiveD365Provider(settings, client, registry()), settings)
        first = await finance.get_customer_balance("TEST-LIVE")
        second = await finance.get_customer_balance("TEST-LIVE")
        assert first["totals_by_currency"] == {"INR": "67.89"}
        assert second["totals_by_currency"] == {"INR": "17.89"}
        assert first["transactions"][0]["original_amount"] == "123.45"
        assert first["mock_mode"] is False
        assert first["evidence"][0]["source_entity"] == "CustomerOpenTransactions"


@pytest.mark.asyncio
async def test_live_qualified_enum_payment_history():
    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        **TRANSACTION,
                        "AmountCur": "-50.10",
                        "TransactionType": "Microsoft.Dynamics.DataEntities.TransactionType'Payment'",
                    },
                    TRANSACTION,
                ]
            },
        )

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        provider = LiveD365Provider(settings, client, registry())
        payments = await provider.payments("TEST-LIVE", "usmf")
        assert len(payments) == 1
        assert payments[0]["amount"] == "50.10"


@pytest.mark.asyncio
async def test_live_deletion_checks_full_history_even_when_settled():
    writes = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        if request.method != "GET":
            writes.append(request)
        records = [CUSTOMER] if request.url.path.endswith("CustomersV3") else [TRANSACTION]
        return httpx.Response(200, json={"value": records})

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        provider = LiveD365Provider(settings, client, registry())
        with pytest.raises(AppError) as error:
            await provider.validate_mutation("delete_test_customer", {"account": "TEST-LIVE"}, "usmf")
        assert error.value.code == "UNSAFE_MUTATION"
        assert "history" in error.value.message
        assert writes == []


@pytest.mark.asyncio
async def test_live_posted_invoice_delete_is_rejected_before_write():
    calls = []

    def transport(request):
        calls.append(request)
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        return httpx.Response(
            200, json={"value": [{"dataAreaId": "usmf", "InvoiceIdentifier": "INV-1", "IsPosted": "Yes"}]}
        )

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        provider = LiveD365Provider(settings, client, registry())
        with pytest.raises(AppError) as error:
            await provider.execute_mutation("delete_draft_free_text_invoice", {"identifier": "INV-1"}, "usmf")
        assert error.value.code == "UNSAFE_MUTATION"
        assert not any(call.method == "DELETE" for call in calls)


@pytest.mark.asyncio
async def test_live_unknown_payment_setup_is_actionable_and_fail_closed():
    class Client:
        async def post(self, *args):
            raise AssertionError("Must not write without verified setup")

    settings = configured_settings()
    provider = LiveD365Provider(settings, Client(), registry())
    with pytest.raises(AppError) as error:
        await provider.validate_mutation(
            "create_customer_payment_journal", {"description": "Test payment"}, "usmf"
        )
    assert error.value.code == "D365_CAPABILITY_UNAVAILABLE"
    assert "setup" in error.value.message


@pytest.mark.asyncio
async def test_live_update_readback_compares_requested_values():
    writes = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        if request.method == "PATCH":
            writes.append(request)
            return httpx.Response(204)
        return httpx.Response(200, json={"value": [deepcopy(CUSTOMER)]})

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        provider = LiveD365Provider(settings, client, registry())
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(
                "update_customer", {"account": "TEST-LIVE", "name": "Requested Change"}, "usmf"
            )
        assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
        assert len(writes) == 1


@pytest.mark.asyncio
async def test_live_post_write_missing_record_is_unknown_not_safe_failure():
    wrote = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        if request.method == "PATCH":
            wrote.append(request)
            return httpx.Response(204)
        return httpx.Response(200, json={"value": [] if wrote else [CUSTOMER]})

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        provider = LiveD365Provider(settings, client, registry())
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(
                "update_customer", {"account": "TEST-LIVE", "name": "Requested Change"}, "usmf"
            )
        assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
        assert error.value.details["verification_code"] == "D365_CUSTOMER_NOT_FOUND"


@pytest.mark.asyncio
async def test_health_poll_is_throttled_then_marks_disconnected():
    calls = []

    def transport(request):
        if request.url.host == "login.microsoftonline.com":
            return token_response()
        calls.append(request)
        return httpx.Response(200, json={"value": []}) if len(calls) == 1 else httpx.Response(503)

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = D365Runtime(settings, http_client=http)
        runtime._state = "connected"
        assert (await runtime.check_health())["status"] == "connected"
        assert (await runtime.check_health())["status"] == "connected"
        assert len(calls) == 1
        runtime._last_health_check = 0
        assert (await runtime.check_health())["status"] == "disconnected"
        assert len(calls) == 2
