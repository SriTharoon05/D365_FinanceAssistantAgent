"""Real CustomersV3 keys, company scoping, and read-only mutation verification."""

import json
import re
from contextlib import asynccontextmanager

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.auth import D365AuthManager
from app.integrations.d365.client import D365ODataClient
from app.integrations.d365.live import LiveD365Provider, enum_member
from app.integrations.d365.metadata import D365MetadataResolver, EntityInfo
from app.integrations.d365.runtime import D365Runtime
from app.integrations.d365.setup import lookup_setup


ACCOUNT = "TEST-CUSTOMER-001"
COMPANY = "demf"
CUSTOMER = {
    "dataAreaId": COMPANY,
    "CustomerAccount": ACCOUNT,
    "OrganizationName": "Original Customer",
    "SalesCurrencyCode": "EUR",
    "PartyType": "Organization",
    "PaymentTerms": "Net30",
    "CustomerGroupId": "10",
}
UPDATED = {**CUSTOMER, "OrganizationName": "Requested Customer"}
METADATA = """<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="D365">
<EntityType Name="CustomerV3"><Key><PropertyRef Name="dataAreaId"/>
<PropertyRef Name="CustomerAccount"/></Key>
<Property Name="dataAreaId" Type="Edm.String"/><Property Name="CustomerAccount" Type="Edm.String"/>
<Property Name="OrganizationName" Type="Edm.String"/><Property Name="SalesCurrencyCode" Type="Edm.String"/>
<Property Name="PartyType" Type="Edm.String"/><Property Name="PaymentTerms" Type="Edm.String"/>
<Property Name="CustomerGroupId" Type="Edm.String"/></EntityType>
<EntityType Name="History"><Property Name="dataAreaId" Type="Edm.String"/>
<Property Name="CustomerAccount" Type="Edm.String"/><Property Name="CurrencyCode" Type="Edm.String"/>
<Property Name="AmountCur" Type="Edm.Decimal"/><Property Name="TransactionType" Type="Edm.String"/>
</EntityType><EntityType Name="Draft"><Property Name="dataAreaId" Type="Edm.String"/>
<Property Name="InvoiceCustomerAccount" Type="Edm.String"/></EntityType>
<EntityType Name="PaymentLine"><Property Name="dataAreaId" Type="Edm.String"/>
<Property Name="AccountDisplayValue" Type="Edm.String"/></EntityType>
<EntityContainer Name="Public"><EntitySet Name="CustomersV3" EntityType="D365.CustomerV3"/>
<EntitySet Name="CustomerTransactions" EntityType="D365.History"/>
<EntitySet Name="CDSFreeTextInvoiceHeaders" EntityType="D365.Draft"/>
<EntitySet Name="CustomerPaymentJournalLines" EntityType="D365.PaymentLine"/>
</EntityContainer></Schema>"""


def settings():
    return Settings(
        _env_file=None,
        d365_base_url="https://erp.example.test",
        d365_tenant_id="tenant",
        d365_client_id="app",
        d365_client_secret="test-secret",
        d365_default_company="usmf",
        d365_metadata_cache_hours=0,
        d365_max_retries=0,
    )


class CustomerTransport:
    def __init__(self, *, before=None, after=None, write_status=204, write_error=None, history=None):
        self.before = [CUSTOMER] if before is None else before
        self.after = [[UPDATED]] if after is None else after
        self.write_status = write_status
        self.write_error = write_error
        self.history = [] if history is None else history
        self.requests = []
        self.writes = []
        self.verification_reads = 0

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        entity = request.url.path.rsplit("/", 1)[-1]
        if request.method in {"PATCH", "DELETE"}:
            self.writes.append(request)
            if self.write_error is not None:
                raise self.write_error("The write response was lost", request=request)
            return httpx.Response(self.write_status)
        assert request.method == "GET"
        assert request.url.params["cross-company"] == "true"
        assert f"dataAreaId eq '{COMPANY}'" in request.url.params["$filter"]
        if entity == "CustomersV3":
            assert f"CustomerAccount eq '{ACCOUNT}'" in request.url.params["$filter"]
            if self.writes:
                response = self.after[min(self.verification_reads, len(self.after) - 1)]
                self.verification_reads += 1
            else:
                response = self.before
            if isinstance(response, int):
                return httpx.Response(response)
            if isinstance(response, type) and issubclass(response, httpx.HTTPError):
                raise response("The verification read failed", request=request)
            return httpx.Response(200, json={"value": response})
        if entity == "CustomerTransactions":
            return httpx.Response(200, json={"value": self.history})
        assert entity in {"CDSFreeTextInvoiceHeaders", "CustomerPaymentJournalLines"}
        return httpx.Response(200, json={"value": []})


@asynccontextmanager
async def provider_for(transport):
    config = settings()
    resolver = D365MetadataResolver(None, config)
    resolver.parse(METADATA)
    resolver.registry.resolved["customer_transactions"] = "CustomerTransactions"
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(config, D365AuthManager(config, http), http)
        yield LiveD365Provider(config, client, resolver.registry)


@pytest.fixture
def verification_delays(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("app.integrations.d365.live.asyncio.sleep", sleep)
    return delays


def data_for(action):
    data = {"account": ACCOUNT}
    if action == "update_customer":
        data["name"] = UPDATED["OrganizationName"]
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize("action,method", [("update_customer", "PATCH"), ("delete_test_customer", "DELETE")])
async def test_customer_mutation_uses_actual_composite_keys_and_cross_company_scoping(
    action, method, verification_delays
):
    transport = CustomerTransport(after=[[UPDATED]] if action == "update_customer" else [[]])
    async with provider_for(transport) as provider:
        result = await provider.execute_mutation(action, data_for(action), COMPANY)

    assert result["verified"] is True
    assert result["identifier"] == ACCOUNT
    assert len(transport.writes) == 1
    request = transport.writes[0]
    assert request.method == method
    assert request.url.params["cross-company"] == "true"
    assert request.url.path.endswith(f"CustomersV3(dataAreaId='{COMPANY}',CustomerAccount='{ACCOUNT}')")
    if method == "PATCH":
        assert json.loads(request.content) == {"OrganizationName": UPDATED["OrganizationName"]}
        assert result["customer"]["company"] == COMPANY
        assert result["customer"]["currency"] == "EUR"
    else:
        assert result["deleted"] is True
    assert transport.verification_reads == 1
    assert verification_delays == []


@pytest.mark.asyncio
async def test_acknowledged_update_tolerates_stale_and_temporarily_missing_readback_without_rewriting(
    verification_delays,
):
    transport = CustomerTransport(after=[[CUSTOMER], [], [UPDATED]])
    async with provider_for(transport) as provider:
        result = await provider.execute_mutation("update_customer", data_for("update_customer"), COMPANY)

    assert result["verified"] is True
    assert result["customer"]["name"] == UPDATED["OrganizationName"]
    assert len(transport.writes) == 1
    assert transport.verification_reads == 3
    assert verification_delays == [0.2, 0.5]


@pytest.mark.asyncio
async def test_acknowledged_delete_tolerates_eventually_consistent_reads_without_redeleting(
    verification_delays,
):
    transport = CustomerTransport(after=[[CUSTOMER], [CUSTOMER], []])
    async with provider_for(transport) as provider:
        result = await provider.execute_mutation(
            "delete_test_customer", data_for("delete_test_customer"), COMPANY
        )

    assert result["verified"] is True
    assert result["deleted"] is True
    assert len(transport.writes) == 1
    assert transport.verification_reads == 3
    assert verification_delays == [0.2, 0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_customer", "delete_test_customer"])
async def test_acknowledged_but_unverified_mutation_stops_after_three_reads(action, verification_delays):
    transport = CustomerTransport(after=[[CUSTOMER]])
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(action, data_for(action), COMPANY)

    assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
    assert len(transport.writes) == 1
    assert transport.verification_reads == 3
    assert verification_delays == [0.2, 0.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_customer", "delete_test_customer"])
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "D365_VALIDATION_ERROR"),
        (403, "D365_PERMISSION_ERROR"),
        (404, "D365_ENTITY_UNAVAILABLE"),
        (409, "D365_VALIDATION_ERROR"),
        (412, "D365_VALIDATION_ERROR"),
        (429, "D365_RATE_LIMIT_ERROR"),
    ],
)
async def test_deterministic_write_rejection_is_not_marked_unknown_or_retried(
    action, status, code, verification_delays
):
    transport = CustomerTransport(write_status=status)
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(action, data_for(action), COMPANY)

    assert error.value.code == code
    assert error.value.details["write_outcome"] == "not_written"
    assert len(transport.writes) == 1
    assert transport.verification_reads == 0
    assert verification_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_customer", "delete_test_customer"])
@pytest.mark.parametrize("status", [500, 503])
async def test_ambiguous_server_write_failure_is_unknown_without_automatic_write_retry(
    action, status, verification_delays
):
    transport = CustomerTransport(write_status=status)
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(action, data_for(action), COMPANY)

    assert error.value.code == "D365_WRITE_OUTCOME_UNKNOWN"
    assert len(transport.writes) == 1
    assert transport.verification_reads == 0
    assert verification_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_customer", "delete_test_customer"])
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_lost_write_response_requires_manual_or_read_only_reconciliation_without_rewriting(
    action, failure, verification_delays
):
    transport = CustomerTransport(write_error=failure)
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(action, data_for(action), COMPANY)

    assert error.value.code == "D365_WRITE_OUTCOME_UNKNOWN"
    assert len(transport.writes) == 1
    assert transport.verification_reads == 0
    assert verification_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_customer", "delete_test_customer"])
@pytest.mark.parametrize(
    "read_failure,code", [(403, "D365_PERMISSION_ERROR"), (httpx.ReadTimeout, "D365_CONNECTION_ERROR")]
)
async def test_verification_permission_or_connection_failure_is_not_retried_or_treated_as_absence(
    action, read_failure, code, verification_delays
):
    transport = CustomerTransport(after=[read_failure])
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation(action, data_for(action), COMPANY)

    assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
    assert error.value.details["verification_code"] == code
    assert len(transport.writes) == 1
    assert transport.verification_reads == 1
    assert verification_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [{"dataAreaId": "usmf"}, {"CustomerAccount": "TEST-OTHER"}])
async def test_filtered_customer_read_cannot_target_a_different_company_or_account(
    wrong, verification_delays
):
    transport = CustomerTransport(before=[{**CUSTOMER, **wrong}])
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation("update_customer", data_for("update_customer"), COMPANY)

    assert error.value.code == "D365_FINANCE_DATA_INVALID"
    assert transport.writes == []
    assert verification_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong", [{"dataAreaId": "usmf"}, {"CustomerAccount": "TEST-OTHER"}])
async def test_readback_matching_changed_fields_in_wrong_scope_never_verifies_success(
    wrong, verification_delays
):
    transport = CustomerTransport(after=[[{**UPDATED, **wrong}]])
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation("update_customer", data_for("update_customer"), COMPANY)

    assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
    assert error.value.details["verification_code"] == "D365_FINANCE_DATA_INVALID"
    assert len(transport.writes) == 1
    assert transport.verification_reads == 1
    assert verification_delays == []


@pytest.mark.asyncio
async def test_settled_financial_history_still_blocks_test_customer_deletion(verification_delays):
    transport = CustomerTransport(
        history=[
            {
                "dataAreaId": COMPANY,
                "CustomerAccount": ACCOUNT,
                "AmountCur": "0",
                "TransactionType": "Payment",
            }
        ]
    )
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.execute_mutation("delete_test_customer", data_for("delete_test_customer"), COMPANY)

    assert error.value.code == "UNSAFE_MUTATION"
    assert transport.writes == []
    assert verification_delays == []


@pytest.mark.asyncio
async def test_read_only_update_reconciliation_verifies_matching_fields_without_repeating_patch(
    verification_delays,
):
    transport = CustomerTransport(before=[UPDATED])
    async with provider_for(transport) as provider:
        result = await provider.reconcile_mutation("update_customer", data_for("update_customer"), COMPANY)

    assert result["verified"] is True
    assert result["reconciled"] is True
    assert result["customer"]["name"] == UPDATED["OrganizationName"]
    assert transport.writes == []
    assert verification_delays == []


@pytest.mark.asyncio
async def test_read_only_delete_reconciliation_verifies_scoped_absence_without_repeating_delete(
    verification_delays,
):
    transport = CustomerTransport(before=[])
    async with provider_for(transport) as provider:
        result = await provider.reconcile_mutation(
            "delete_test_customer", data_for("delete_test_customer"), COMPANY
        )

    assert result["verified"] is True
    assert result["reconciled"] is True
    assert result["deleted"] is True
    assert transport.writes == []
    assert verification_delays == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["update_customer", "delete_test_customer"])
async def test_read_only_reconciliation_does_not_apply_unverified_changes(action, verification_delays):
    transport = CustomerTransport(before=[CUSTOMER])
    async with provider_for(transport) as provider:
        with pytest.raises(AppError) as error:
            await provider.reconcile_mutation(action, data_for(action), COMPANY)

    assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
    assert transport.writes == []
    assert verification_delays == [0.2, 0.5]


@pytest.mark.asyncio
async def test_runtime_write_timeout_requires_reconnection_before_read_only_reconciliation(
    verification_delays,
):
    transport = CustomerTransport(write_error=httpx.ReadTimeout, after=[[UPDATED]])
    config = settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        runtime = D365Runtime(config, http_client=http)
        runtime.metadata.parse(METADATA)
        runtime.metadata.registry.resolved["customer_transactions"] = "CustomerTransactions"
        runtime._state = "connected"

        with pytest.raises(AppError) as lost_response:
            await runtime.provider.execute_mutation("update_customer", data_for("update_customer"), COMPANY)
        assert lost_response.value.code == "D365_WRITE_OUTCOME_UNKNOWN"
        assert runtime.status()["status"] == "disconnected"
        assert len(transport.writes) == 1

        request_count = len(transport.requests)
        with pytest.raises(AppError) as disconnected:
            await runtime.provider.reconcile_mutation("update_customer", data_for("update_customer"), COMPANY)
        assert disconnected.value.code == "D365_CAPABILITY_UNAVAILABLE"
        assert len(transport.requests) == request_count

        # The guard is opened only after the caller has restored an authenticated connection.
        runtime._state = "connected"
        result = await runtime.provider.reconcile_mutation(
            "update_customer", data_for("update_customer"), COMPANY
        )
        assert result["verified"] is True
        assert result["reconciled"] is True
        assert len(transport.writes) == 1
        assert transport.verification_reads == 1
        assert verification_delays == []


def typed_provider(client=None, config=None):
    config = config or settings()
    resolver = D365MetadataResolver(None, config)
    resolver.parse(METADATA)
    namespace = "Microsoft.Dynamics.DataEntities"
    definitions = {
        "CDSFreeTextInvoiceHeaders": {
            "dataAreaId": "Edm.String",
            "ExternalInvoiceId": "Edm.String",
            "CustomerAccount": "Edm.String",
            "InvoiceAccount": "Edm.String",
            "CurrencyCode": "Edm.String",
            "InvoiceDate": "Edm.DateTimeOffset",
            "DueDate": "Edm.DateTimeOffset",
            "IsPosted": f"{namespace}.NoYes",
        },
        "CDSFreeTextInvoiceLines": {
            "dataAreaId": "Edm.String",
            "ExternalInvoiceId": "Edm.String",
            "LineNumber": "Edm.Decimal",
            "Description": "Edm.String",
            "MainAccountDisplayValue": "Edm.String",
            "TransactionCurrencyAmount": "Edm.Decimal",
            "Quantity": "Edm.Decimal",
            "UnitPrice": "Edm.Decimal",
        },
        "CustomerPaymentJournalHeaders": {
            "dataAreaId": "Edm.String",
            "JournalBatchNumber": "Edm.String",
            "JournalName": "Edm.String",
            "Description": "Edm.String",
            "IsPosted": f"{namespace}.NoYes",
        },
        "CustomerPaymentJournalLines": {
            "dataAreaId": "Edm.String",
            "JournalBatchNumber": "Edm.String",
            "LineNumber": "Edm.Decimal",
            "AccountDisplayValue": "Edm.String",
            "AccountType": f"{namespace}.LedgerJournalACType",
            "CurrencyCode": "Edm.String",
            "CreditAmount": "Edm.Decimal",
            "DebitAmount": "Edm.Decimal",
            "TransactionDate": "Edm.DateTimeOffset",
            "PaymentReference": "Edm.String",
            "OffsetAccountType": f"{namespace}.LedgerJournalACType",
            "OffsetAccountDisplayValue": "Edm.String",
            "PaymentMethodName": "Edm.String",
            "PostingProfile": "Edm.String",
        },
        "MainAccounts": {
            "ChartOfAccounts": "Edm.String",
            "MainAccountId": "Edm.String",
            "MainAccountType": f"{namespace}.DimensionLedgerAccountType",
            "IsSuspended": f"{namespace}.NoYes",
            "DoNotAllowManualEntry": f"{namespace}.NoYes",
        },
        "Ledgers": {"LegalEntityId": "Edm.String", "ChartOfAccounts": "Edm.String"},
        "JournalNames": {
            "dataAreaId": "Edm.String",
            "Name": "Edm.String",
            "Type": f"{namespace}.LedgerJournalType",
        },
        "PaymentTerms": {"dataAreaId": "Edm.String", "Name": "Edm.String"},
    }
    for entity, fields in definitions.items():
        resolver.registry.entities[entity] = EntityInfo(entity, entity, fields)
    return LiveD365Provider(config, client, resolver.registry)


def invoice_data():
    return {
        "account": ACCOUNT,
        "currency": "EUR",
        "external_id": "CHAT-DRAFT-001",
        "invoice_date": "2026-10-06",
        "due_date": "2026-11-05",
        "lines": [{"description": "Verified service", "amount": "120.50", "revenue_account": "401100"}],
    }


def payment_data():
    return {
        "account": ACCOUNT,
        "currency": "EUR",
        "journal_number": "JOURNAL-001",
        "line_number": 1,
        "amount": "120.50",
        "payment_date": "2026-10-06",
        "reference": "PAYMENT-001",
        "bank_account": "DEMF-OPER",
    }


def test_actual_free_text_invoice_payload_uses_metadata_names_types_and_account_link():
    provider = typed_provider()
    header, lines = provider.invoice_payloads(invoice_data(), COMPANY)
    assert header == {
        "dataAreaId": COMPANY,
        "CustomerAccount": ACCOUNT,
        "InvoiceAccount": ACCOUNT,
        "CurrencyCode": "EUR",
        "DueDate": "2026-11-05T00:00:00Z",
        "InvoiceDate": "2026-10-06T00:00:00Z",
        "ExternalInvoiceId": "CHAT-DRAFT-001",
    }
    assert lines == [
        {
            "dataAreaId": COMPANY,
            "ExternalInvoiceId": "CHAT-DRAFT-001",
            "LineNumber": "1",
            "Description": "Verified service",
            "MainAccountDisplayValue": "401100",
            "TransactionCurrencyAmount": "120.50",
            "Quantity": "1",
            "UnitPrice": "120.50",
        }
    ]


def test_actual_payment_line_payload_serializes_decimal_date_and_display_fields():
    provider = typed_provider()
    payload = provider.payment_payload(payment_data(), COMPANY)
    assert payload["LineNumber"] == "1"
    assert payload["CreditAmount"] == "120.50"
    assert payload["DebitAmount"] == "0"
    assert payload["TransactionDate"] == "2026-10-06T00:00:00Z"
    assert payload["AccountDisplayValue"] == "TEST\\-CUSTOMER\\-001"
    assert payload["OffsetAccountDisplayValue"] == "DEMF\\-OPER"
    assert payload["PaymentMethodName"] == provider.settings.d365_payment_method
    assert payload["AccountType"] == "Cust"
    assert payload["OffsetAccountType"] == "Bank"


def test_typed_readback_accepts_semantically_matching_numeric_date_enum_and_raw_display_values():
    provider = typed_provider()
    expected = provider.payment_payload(payment_data(), COMPANY)
    actual = {
        **expected,
        "LineNumber": 1,
        "CreditAmount": 120.5,
        "DebitAmount": 0,
        "TransactionDate": "2026-10-06T12:34:56Z",
        "AccountDisplayValue": ACCOUNT,
        "OffsetAccountDisplayValue": "DEMF-OPER",
        "AccountType": "Microsoft.Dynamics.DataEntities.LedgerJournalACType'Cust'",
        "OffsetAccountType": "Microsoft.Dynamics.DataEntities.LedgerJournalACType'Bank'",
    }
    provider.verify_fields(actual, expected, provider.registry.entities["CustomerPaymentJournalLines"])


@pytest.mark.parametrize(
    "field,bad",
    [("CreditAmount", "121.00"), ("TransactionDate", "2026-10-07T00:00:00Z"), ("AccountType", "Bank")],
)
def test_typed_readback_rejects_semantically_different_financial_values(field, bad):
    provider = typed_provider()
    expected = provider.payment_payload(payment_data(), COMPANY)
    with pytest.raises(AppError) as error:
        provider.verify_fields(
            {**expected, field: bad}, expected, provider.registry.entities["CustomerPaymentJournalLines"]
        )
    assert error.value.code == "D365_WRITE_VERIFICATION_FAILED"
    assert error.value.details["field"] == field


class SetupClient:
    def __init__(self):
        self.calls = []
        self.records = {
            "Ledgers": [{"LegalEntityId": COMPANY, "ChartOfAccounts": "DEMF-CHART"}],
            "MainAccounts": [
                {
                    "ChartOfAccounts": "DEMF-CHART",
                    "MainAccountId": "401100",
                    "MainAccountType": "Revenue",
                    "IsSuspended": "No",
                    "DoNotAllowManualEntry": "No",
                },
                {
                    "ChartOfAccounts": "OTHER-CHART",
                    "MainAccountId": "401100",
                    "MainAccountType": "Expense",
                    "IsSuspended": "No",
                    "DoNotAllowManualEntry": "No",
                },
                {
                    "ChartOfAccounts": "DEMF-CHART",
                    "MainAccountId": "111100",
                    "MainAccountType": "Expense",
                    "IsSuspended": "No",
                    "DoNotAllowManualEntry": "No",
                },
            ],
            "JournalNames": [
                {
                    "dataAreaId": COMPANY,
                    "Name": "CustPay",
                    "Type": "Microsoft.Dynamics.DataEntities.LedgerJournalType'CustPayment'",
                }
            ],
            "PaymentTerms": [{"dataAreaId": COMPANY, "Name": "Net30"}],
        }

    async def get(self, entity, **kwargs):
        self.calls.append((entity, kwargs))
        clause = kwargs.get("filter", "")
        rows = self.records.get(entity, [])
        for field, enum_type, value in re.findall(
            r"\b([A-Za-z_][A-Za-z0-9_]*) eq "
            r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)?'((?:[^']|'')*)'",
            clause,
        ):
            rows = [
                row
                for row in rows
                if (enum_member(row.get(field, "")) if enum_type else str(row.get(field, ""))).casefold()
                == value.replace("''", "'").casefold()
            ]
        return rows[: kwargs.get("top", len(rows))]

    async def post(self, *args, **kwargs):
        raise AssertionError("Setup discovery must remain read-only")

    async def patch(self, *args, **kwargs):
        raise AssertionError("Setup discovery must remain read-only")

    async def delete(self, *args, **kwargs):
        raise AssertionError("Setup discovery must remain read-only")


@pytest.mark.asyncio
async def test_main_account_id_repeated_in_other_chart_is_resolved_by_company_ledger():
    client = SetupClient()
    provider = typed_provider(client)
    info, account = await lookup_setup(client, provider.registry, "main_account", "401100", COMPANY)
    provider.verify_revenue_account(info, account)
    assert account["ChartOfAccounts"] == "DEMF-CHART"
    ledger = next(kwargs for entity, kwargs in client.calls if entity == "Ledgers")
    assert f"LegalEntityId eq '{COMPANY}'" in ledger["filter"]
    assert ledger["cross_company"] is True
    account_read = next(kwargs for entity, kwargs in client.calls if entity == "MainAccounts")
    assert "ChartOfAccounts eq 'DEMF-CHART'" in account_read["filter"]
    assert "MainAccountId eq '401100'" in account_read["filter"]


@pytest.mark.asyncio
async def test_read_only_invoice_setup_lists_real_revenue_accounts_and_verifies_default_separately():
    client = SetupClient()
    provider = typed_provider(client)
    result = await provider.get_write_setup(COMPANY, "invoice")
    section = result["setup"]["main_account"]
    assert section["source_entity"] == "MainAccounts"
    assert section["candidates"] == [{"id": "401100", "type": "Revenue"}]
    assert section["configured_default"] == {"value": "401100", "verified": True}
    account_reads = [kwargs for entity, kwargs in client.calls if entity == "MainAccounts"]
    assert any(kwargs["top"] == 50 for kwargs in account_reads)
    assert any("MainAccountId eq '401100'" in kwargs["filter"] for kwargs in account_reads)
    assert result["evidence"][0]["source_entity"] == "MainAccounts"


@pytest.mark.asyncio
async def test_invalid_configured_revenue_default_is_not_replaced_with_an_available_candidate():
    client = SetupClient()
    provider = typed_provider(client, settings().model_copy(update={"d365_revenue_account": "MISSING"}))
    section = (await provider.get_write_setup(COMPANY, "invoice"))["setup"]["main_account"]
    assert section["candidates"] == [{"id": "401100", "type": "Revenue"}]
    assert section["configured_default"]["value"] == "MISSING"
    assert section["configured_default"]["verified"] is False
    assert section["configured_default"]["diagnostic"]


@pytest.mark.asyncio
async def test_actual_payment_term_name_field_is_verified_in_selected_company():
    client = SetupClient()
    provider = typed_provider(client)
    await provider.validate_terms("Net30", COMPANY)
    assert client.calls == [
        (
            "PaymentTerms",
            {
                "filter": f"dataAreaId eq '{COMPANY}' and Name eq 'Net30'",
                "top": 2,
                "cross_company": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_reconciled_draft_update_returns_exact_unposted_header_despite_historical_invoice_collision(
    monkeypatch,
):
    provider = typed_provider()
    header, _ = provider.invoice_payloads(invoice_data(), COMPANY)
    header["IsPosted"] = "Microsoft.Dynamics.DataEntities.NoYes'No'"

    async def exact_header(identifier, company):
        assert identifier == invoice_data()["external_id"]
        assert company == COMPANY
        return provider.registry.entities["CDSFreeTextInvoiceHeaders"], header

    async def historical_lookup(*args, **kwargs):
        raise AssertionError(
            "A posted transaction sharing the identifier must not replace verified draft evidence"
        )

    monkeypatch.setattr(provider, "header_record", exact_header)
    monkeypatch.setattr(provider, "invoice", historical_lookup)
    result = await provider.reconcile_mutation(
        "update_draft_free_text_invoice",
        {
            "identifier": invoice_data()["external_id"],
            "due_date": invoice_data()["due_date"],
        },
        COMPANY,
    )
    assert result["reconciled"] is True
    assert result["verified"] is True
    assert result["invoice"]["source_entity"] == "CDSFreeTextInvoiceHeaders"
    assert result["invoice"]["is_posted"] is False
    assert result["invoice"]["account"] == ACCOUNT


@pytest.mark.asyncio
async def test_created_draft_verification_returns_exact_header_without_broad_historical_lookup(monkeypatch):
    provider = typed_provider()
    data = invoice_data()
    header, _ = provider.invoice_payloads(data, COMPANY)
    header["IsPosted"] = "No"
    verified_lines = []

    async def exact_header(identifier, company):
        return provider.registry.entities["CDSFreeTextInvoiceHeaders"], header

    async def lines_check(checked_data, company):
        verified_lines.append((checked_data, company))

    async def historical_lookup(*args, **kwargs):
        raise AssertionError("Creation verification must return the exact draft header")

    monkeypatch.setattr(provider, "header_record", exact_header)
    monkeypatch.setattr(provider, "verify_invoice_lines", lines_check)
    monkeypatch.setattr(provider, "invoice", historical_lookup)
    result = await provider.verify_created_invoice(data, COMPANY)
    assert result["verified"] is True
    assert result["posted"] is False
    assert result["invoice"]["source_entity"] == "CDSFreeTextInvoiceHeaders"
    assert result["invoice"]["is_posted"] is False
    assert verified_lines == [(data, COMPANY)]
