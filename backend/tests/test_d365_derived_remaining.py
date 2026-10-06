"""CustTrans BI balances use signed, current settlement amounts without guessing."""

from contextlib import asynccontextmanager
from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.auth import D365AuthManager
from app.integrations.d365.client import D365ODataClient
from app.integrations.d365.finance import D365FinanceService
from app.integrations.d365.live import LiveD365Provider
from app.integrations.d365.metadata import D365MetadataResolver


ENTITY = "CustTransBiEntities"
NAMESPACE = "Microsoft.Dynamics.DataEntities"
FIELDS = {
    "dataAreaId": "Edm.String",
    "SourceKey": "Edm.Int64",
    "AccountNum": "Edm.String",
    "CurrencyCode": "Edm.String",
    "AmountCur": "Edm.Decimal",
    "SettleAmountCur": "Edm.Decimal",
    "Invoice": "Edm.String",
    "Voucher": "Edm.String",
    "TransDate": "Edm.DateTimeOffset",
    "DueDate": "Edm.DateTimeOffset",
    "Closed": "Edm.DateTimeOffset",
    "TransType": f"{NAMESPACE}.LedgerTransType",
}
CUSTOMER = {
    "dataAreaId": "usmf",
    "CustomerAccount": "CUST-001",
    "OrganizationName": "Verified Customer",
    "CurrencyCode": "INR",
}
INVOICE = {
    "dataAreaId": "usmf",
    "SourceKey": "5637144576",
    "AccountNum": "CUST-001",
    "CurrencyCode": "INR",
    "AmountCur": "60000.00",
    "SettleAmountCur": "25000.00",
    "Invoice": "POSTED-INV-001",
    "Voucher": "INV-VOUCHER-001",
    "TransDate": "2026-09-01T00:00:00Z",
    "DueDate": "2026-09-30T00:00:00Z",
    "Closed": "1900-01-01T00:00:00Z",
    "TransType": f"{NAMESPACE}.LedgerTransType'Sales'",
}


def metadata_xml(fields=None, entity=ENTITY, direct=False):
    properties = "".join(
        f'<Property Name="{name}" Type="{kind}"/>'
        for name, kind in (FIELDS if fields is None else fields).items()
    )
    direct_type = (
        f'<EntityType Name="OpenCustomer" BaseType="{NAMESPACE}.CustTransBiEntity">'
        '<Property Name="RemainingAmount" Type="Edm.Decimal"/></EntityType>'
        if direct
        else ""
    )
    direct_set = (
        f'<EntitySet Name="CustomerOpenTransactions" EntityType="{NAMESPACE}.OpenCustomer"/>'
        if direct
        else ""
    )
    return (
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx"><edmx:DataServices>'
        f'<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="{NAMESPACE}">'
        '<EntityType Name="CustTransBiEntity"><Key><PropertyRef Name="dataAreaId"/>'
        '<PropertyRef Name="SourceKey"/></Key>' + properties + '</EntityType><EntityType Name="Customer">'
        '<Property Name="dataAreaId" Type="Edm.String"/>'
        '<Property Name="CustomerAccount" Type="Edm.String"/>'
        '<Property Name="OrganizationName" Type="Edm.String"/>'
        '<Property Name="CurrencyCode" Type="Edm.String"/></EntityType>'
        + direct_type
        + '<EntityContainer Name="Public">'
        f'<EntitySet Name="{entity}" EntityType="{NAMESPACE}.CustTransBiEntity"/>'
        f'<EntitySet Name="CustomersV3" EntityType="{NAMESPACE}.Customer"/>'
        + direct_set
        + "</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"
    )


def configured_settings(**overrides):
    return Settings(
        _env_file=None,
        d365_base_url="https://erp.example.test",
        d365_tenant_id="tenant",
        d365_client_id="app",
        d365_client_secret="test-secret",
        d365_metadata_cache_hours=0,
        d365_max_retries=0,
        **overrides,
    )


@asynccontextmanager
async def connected(records=None, *, settings=None, xml=None, direct_records=None, direct_probe_status=200):
    records = [INVOICE] if records is None else records
    settings = settings or configured_settings()
    requests = []

    def transport(request):
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        assert request.method == "GET"
        entity = request.url.path.rsplit("/", 1)[-1]
        if entity == "$metadata":
            return httpx.Response(200, text=xml or metadata_xml())
        if entity == "CustomersV3":
            return httpx.Response(200, json={"value": [CUSTOMER]})
        if entity == "CustomerOpenTransactions":
            if request.url.params.get("$top") == "1" and direct_probe_status != 200:
                return httpx.Response(direct_probe_status)
            rows = direct_records or [{**INVOICE, "RemainingAmount": "123.00"}]
        else:
            assert entity == ENTITY
            rows = records
        if request.url.params.get("$top") != "1":
            assert request.url.params["cross-company"] == "true"
            assert "dataAreaId eq 'usmf'" in request.url.params["$filter"]
        return httpx.Response(200, json={"value": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, D365AuthManager(settings, http), http)
        resolver = D365MetadataResolver(client, settings)
        await resolver.load()
        provider = LiveD365Provider(settings, client, resolver.registry)
        yield D365FinanceService(provider, settings), provider, resolver, requests


def transaction_reads(requests, entity=ENTITY):
    return [
        request
        for request in requests
        if request.url.path.endswith("/" + entity) and request.url.params.get("$top") != "1"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["auto", ENTITY])
async def test_custtrans_bi_discovery_selects_verified_derived_balance_strategy(selection):
    settings = configured_settings(d365_open_transactions_entity=selection)
    async with connected(settings=settings) as (_, provider, resolver, _):
        assert resolver.registry.resolved == {"customer_transactions": ENTITY, "open_transactions": ENTITY}
        info = resolver.registry.entities[ENTITY]
        assert info.balance_strategy() == "derived_custtrans"
        assert info.diagnostic()["balance_strategy"] == "derived_custtrans"
        assert resolver.registry.public()["open_transactions_strategy"] == "derived_custtrans"
        assert resolver.registry.candidates["open_transactions"][0]["balance_strategy"] == "derived_custtrans"
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert Decimal(rows[0]["original_amount"]) == Decimal("60000")
        assert Decimal(rows[0]["remaining_amount"]) == Decimal("35000")
        assert rows[0]["balance_strategy"] == "derived_custtrans"
        assert rows[0]["balance_basis"] == "current"
        assert rows[0]["source_entity"] == ENTITY


@pytest.mark.asyncio
async def test_direct_open_entity_is_preferred_over_derived_custtrans():
    async with connected(xml=metadata_xml(direct=True)) as (_, provider, resolver, _):
        assert resolver.registry.resolved["open_transactions"] == "CustomerOpenTransactions"
        assert resolver.registry.entities["CustomerOpenTransactions"].balance_strategy() == "direct"
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert Decimal(rows[0]["remaining_amount"]) == Decimal("123")
        assert not rows[0].get("remaining_derived", False)
        assert rows[0]["source_entity"] == "CustomerOpenTransactions"


@pytest.mark.asyncio
async def test_failed_direct_permission_probe_can_fall_back_to_verified_derived_entity():
    async with connected(xml=metadata_xml(direct=True), direct_probe_status=403) as (_, _, resolver, _):
        assert resolver.registry.resolved["open_transactions"] == ENTITY
        assert resolver.registry.entities[ENTITY].balance_strategy() == "derived_custtrans"


@pytest.mark.parametrize("entity", ["CustomerTransactions", "CustTransBiEntitiesCopy", "custtransbientities"])
def test_derivation_is_not_enabled_for_ordinary_or_similarly_named_entities(entity):
    resolver = D365MetadataResolver(None, configured_settings())
    info = resolver.parse(metadata_xml(entity=entity))[entity]
    assert info.balance_strategy() == "unavailable"
    assert resolver.score(info, "open_transactions") == 0


@pytest.mark.parametrize(
    "missing", ["AmountCur", "SettleAmountCur", "AccountNum", "CurrencyCode", "dataAreaId"]
)
def test_derived_strategy_requires_all_verified_fields(missing):
    resolver = D365MetadataResolver(None, configured_settings())
    fields = {name: kind for name, kind in FIELDS.items() if name != missing}
    info = resolver.parse(metadata_xml(fields=fields))[ENTITY]
    assert info.balance_strategy() == "unavailable"
    assert resolver.score(info, "open_transactions") == 0


@pytest.mark.parametrize("field", ["AmountCur", "SettleAmountCur"])
@pytest.mark.parametrize("kind", ["Edm.String", "Edm.Double", "Edm.Int64"])
def test_derived_strategy_requires_decimal_schema_types(field, kind):
    resolver = D365MetadataResolver(None, configured_settings())
    info = resolver.parse(metadata_xml(fields={**FIELDS, field: kind}))[ENTITY]
    assert info.balance_strategy() == "unavailable"
    assert resolver.score(info, "open_transactions") == 0


def test_settlement_amount_never_becomes_a_global_remaining_amount_alias():
    fields = {name: kind for name, kind in FIELDS.items() if name != "AmountCur"}
    fields["TransactionCurrencyAmount"] = "Edm.Decimal"
    resolver = D365MetadataResolver(None, configured_settings())
    info = resolver.parse(metadata_xml(fields=fields))[ENTITY]
    assert info.resolve("remaining") is None
    assert info.balance_strategy() == "unavailable"
    assert resolver.score(info, "open_transactions") == 0


@pytest.mark.asyncio
async def test_signed_current_balances_keep_currencies_separate_and_discard_only_zero_rows():
    records = [
        INVOICE,
        {**INVOICE, "Invoice": "FULLY-SETTLED", "AmountCur": "1000", "SettleAmountCur": "1000"},
        {
            **INVOICE,
            "Invoice": "",
            "Voucher": "SETTLED-PAYMENT",
            "AmountCur": "-25000",
            "SettleAmountCur": "-25000",
            "TransType": f"{NAMESPACE}.LedgerTransType'Payment'",
        },
        {
            **INVOICE,
            "Invoice": "",
            "Voucher": "UNAPPLIED-PAYMENT",
            "AmountCur": "-10000",
            "SettleAmountCur": "0",
            "TransType": f"{NAMESPACE}.LedgerTransType'Payment'",
        },
        {
            **INVOICE,
            "Invoice": "USD-INV",
            "CurrencyCode": "USD",
            "AmountCur": "10.25",
            "SettleAmountCur": "0.25",
        },
        {**INVOICE, "Invoice": "CREDIT-NOTE", "AmountCur": "-500", "SettleAmountCur": "-100"},
    ]
    async with connected(records) as (finance, _, _, requests):
        result = await finance.get_customer_balance("CUST-001")
        assert {key: Decimal(value) for key, value in result["totals_by_currency"].items()} == {
            "INR": Decimal("24600"),
            "USD": Decimal("10"),
        }
        assert len(result["transactions"]) == 4
        payment = next(row for row in result["transactions"] if row["voucher"] == "UNAPPLIED-PAYMENT")
        assert payment["invoice_number"] == ""
        assert payment["transaction_type"] == "payment"
        assert payment["is_invoice"] is False
        assert Decimal(payment["remaining_amount"]) == Decimal("-10000")
        assert len(transaction_reads(requests)) == 1
        assert "AccountNum eq 'CUST-001'" in transaction_reads(requests)[0].url.params["$filter"]
        assert all(row["source_entity"] == ENTITY for row in result["evidence"])
        payment_evidence = next(row for row in result["evidence"] if row["voucher"] == "UNAPPLIED-PAYMENT")
        assert payment_evidence["kind"] != "invoice"


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["1900-01-01T00:00:00Z", "2026-10-01T00:00:00Z", None])
async def test_closed_date_is_not_used_as_zero_balance_proof(closed):
    async with connected([{**INVOICE, "Closed": closed}]) as (_, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert len(rows) == 1
        assert Decimal(rows[0]["remaining_amount"]) == Decimal("35000")


@pytest.mark.asyncio
async def test_each_balance_read_uses_current_settlement_amounts():
    row = {**INVOICE}
    async with connected([row]) as (_, provider, _, requests):
        first = await provider.open_transactions("CUST-001", "usmf")
        row["SettleAmountCur"] = "45000.00"
        second = await provider.open_transactions("CUST-001", "usmf")
        assert Decimal(first[0]["remaining_amount"]) == Decimal("35000")
        assert Decimal(second[0]["remaining_amount"]) == Decimal("15000")
        assert len(transaction_reads(requests)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["AmountCur", "SettleAmountCur"])
@pytest.mark.parametrize("bad", [None, "", "not-a-decimal", "NaN", "Infinity", "-Infinity"])
async def test_invalid_monetary_values_fail_closed_without_fabricating_remaining_amount(field, bad):
    async with connected([{**INVOICE, field: bad, "Closed": "2026-10-01T00:00:00Z"}]) as (_, provider, _, _):
        with pytest.raises(AppError) as error:
            await provider.open_transactions("CUST-001", "usmf")
        assert error.value.code == "D365_FINANCE_DATA_INVALID"


@pytest.mark.asyncio
async def test_missing_settlement_value_is_not_assumed_zero():
    row = {name: value for name, value in INVOICE.items() if name != "SettleAmountCur"}
    async with connected([row]) as (_, provider, _, _):
        with pytest.raises(AppError) as error:
            await provider.open_transactions("CUST-001", "usmf")
        assert error.value.code == "D365_FINANCE_DATA_INVALID"


@pytest.mark.asyncio
async def test_derived_decimal_subtraction_preserves_small_remainder_of_large_amount():
    row = {
        **INVOICE,
        "AmountCur": "99999999999999999999.99",
        "SettleAmountCur": "99999999999999999999.98",
    }
    async with connected([row]) as (_, provider, _, requests):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert Decimal(rows[0]["original_amount"]) == Decimal(row["AmountCur"])
        assert Decimal(rows[0]["remaining_amount"]) == Decimal("0.01")
        assert len(transaction_reads(requests)) == 1


@pytest.mark.asyncio
async def test_over_settlement_remains_signed_instead_of_being_clamped_to_zero():
    async with connected([{**INVOICE, "AmountCur": "100", "SettleAmountCur": "140"}]) as (_, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert Decimal(rows[0]["remaining_amount"]) == Decimal("-40")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payment_type", ["Payment", "CustPayment", "CustomerPayment", f"{NAMESPACE}.LedgerTransType'pAyMeNt'"]
)
async def test_payment_type_overrides_a_populated_invoice_number(payment_type):
    row = {**INVOICE, "AmountCur": "100", "SettleAmountCur": "0", "TransType": payment_type}
    async with connected([row]) as (finance, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["is_invoice"] is False
        overdue = await finance.get_overdue_invoices("CUST-001", "2026-10-06")
        assert overdue["invoices"] == []
        reminder = await finance.draft_collection_reminder("CUST-001", "2026-10-06")
        assert reminder["evidence"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("transaction_type", ["Settlement", "Interest", "Ledger", "Bank", "Tax"])
async def test_explicit_noninvoice_type_is_not_used_for_invoice_collection(transaction_type):
    async with connected([{**INVOICE, "TransType": transaction_type}]) as (_, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["is_invoice"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_type", [None, "", "UnknownType", "GeneralJournal", "WriteOff", 15, "15"])
async def test_exposed_transaction_type_must_verify_invoice_classification(raw_type):
    async with connected([{**INVOICE, "TransType": raw_type}]) as (finance, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["invoice_number"] == INVOICE["Invoice"]
        assert rows[0]["is_invoice"] is False
        result = await finance.get_overdue_invoices("CUST-001", "2026-10-06")
        assert result["invoices"] == []


@pytest.mark.asyncio
async def test_missing_record_type_is_not_an_invoice_when_metadata_exposes_transaction_type():
    row = {name: value for name, value in INVOICE.items() if name != "TransType"}
    async with connected([row]) as (_, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["transaction_type"] is None
        assert rows[0]["is_invoice"] is False


@pytest.mark.asyncio
async def test_authoritative_invoice_field_can_classify_when_metadata_has_no_transaction_type_field():
    fields = {name: kind for name, kind in FIELDS.items() if name != "TransType"}
    row = {name: value for name, value in INVOICE.items() if name != "TransType"}
    async with connected([row], xml=metadata_xml(fields=fields)) as (_, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["transaction_type"] is None
        assert rows[0]["is_invoice"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("numeric_type", [15, "15"])
async def test_numeric_transaction_type_does_not_guess_an_enum_member(numeric_type):
    async with connected([{**INVOICE, "Invoice": "", "TransType": numeric_type}]) as (_, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["transaction_type"] is None
        assert rows[0]["is_invoice"] is False
        assert rows[0]["invoice_number"] == ""
        assert rows[0]["voucher"] == INVOICE["Voucher"]


@pytest.mark.asyncio
async def test_invoice_lookup_skips_payment_references_before_counting_genuine_invoices():
    records = [
        {
            **INVOICE,
            "Voucher": "PAYMENT-1",
            "AmountCur": "-10",
            "SettleAmountCur": "0",
            "TransType": "Payment",
        },
        {
            **INVOICE,
            "Voucher": "PAYMENT-2",
            "AmountCur": "-20",
            "SettleAmountCur": "0",
            "TransType": "Payment",
        },
        INVOICE,
    ]
    async with connected(records) as (finance, _, _, requests):
        result = await finance.get_invoice_details(INVOICE["Invoice"])
        invoice = result["invoice"]
        assert invoice["is_invoice"] is True
        assert invoice["voucher"] == INVOICE["Voucher"]
        assert invoice["invoice_number"] == INVOICE["Invoice"]
        assert Decimal(invoice["remaining_amount"]) == Decimal("35000")
        assert result["evidence"][0]["kind"] == "invoice"
        reads = transaction_reads(requests)
        assert len(reads) == 1
        assert "$top" not in reads[0].url.params


@pytest.mark.asyncio
async def test_invoice_lookup_cannot_return_a_payment_referencing_the_invoice_identifier():
    record = {**INVOICE, "AmountCur": "-10", "SettleAmountCur": "0", "TransType": "Payment"}
    async with connected([record]) as (_, provider, _, _):
        with pytest.raises(AppError) as error:
            await provider.invoice(INVOICE["Invoice"], "usmf")
        assert error.value.code in {"D365_CAPABILITY_UNAVAILABLE", "D365_INVOICE_NOT_FOUND"}


@pytest.mark.asyncio
async def test_invoice_lookup_still_rejects_two_genuine_invoices_with_one_identifier():
    records = [INVOICE, {**INVOICE, "Voucher": "SECOND-GENUINE-INVOICE"}]
    async with connected(records) as (_, provider, _, _):
        with pytest.raises(AppError) as error:
            await provider.invoice(INVOICE["Invoice"], "usmf")
        assert error.value.code == "D365_AMBIGUOUS_RECORD"


@pytest.mark.asyncio
@pytest.mark.parametrize("due", ["1900-01-01T00:00:00Z", "0001-01-01T00:00:00Z"])
async def test_derived_due_date_sentinels_are_unknown_and_never_fabricate_overdue_status(due):
    async with connected([{**INVOICE, "DueDate": due}]) as (finance, provider, _, _):
        rows = await provider.open_transactions("CUST-001", "usmf")
        assert rows[0]["due_date"] is None
        result = await finance.get_overdue_invoices("CUST-001", "2026-10-06")
        assert result["invoices"] == []
        assert result["unknown_due_date_count"] == 1


@pytest.mark.asyncio
async def test_genuine_invoice_retains_original_and_remaining_evidence_and_overdue_date():
    async with connected() as (finance, _, _, _):
        result = await finance.get_overdue_invoices("CUST-001", "2026-10-06")
        assert result["invoices"][0]["is_invoice"] is True
        assert result["invoices"][0]["days_overdue"] == 6
        proof = result["evidence"][0]
        assert Decimal(proof["original_amount"]) == Decimal("60000")
        assert Decimal(proof["remaining_amount"]) == Decimal("35000")
        assert proof["source_entity"] == ENTITY


@pytest.mark.asyncio
async def test_payment_history_preserves_sign_and_identifies_reversals():
    records = [
        {
            **INVOICE,
            "Invoice": "",
            "Voucher": "PAYMENT",
            "AmountCur": "-100.00",
            "SettleAmountCur": "0",
            "TransType": f"{NAMESPACE}.LedgerTransType'Payment'",
        },
        {
            **INVOICE,
            "Invoice": "",
            "Voucher": "PAYMENT-REVERSAL",
            "AmountCur": "100.00",
            "SettleAmountCur": "0",
            "TransType": f"{NAMESPACE}.LedgerTransType'Payment'",
        },
    ]
    async with connected(records) as (finance, provider, _, _):
        payments = await provider.payments("CUST-001", "usmf")
        received = next(row for row in payments if row["voucher"] == "PAYMENT")
        reversal = next(row for row in payments if row["voucher"] == "PAYMENT-REVERSAL")
        assert Decimal(received["amount"]) == Decimal("100")
        assert Decimal(received["transaction_amount"]) == Decimal("-100")
        assert received["is_reversal"] is False
        assert Decimal(reversal["amount"]) == Decimal("-100")
        assert Decimal(reversal["transaction_amount"]) == Decimal("100")
        assert reversal["is_reversal"] is True
        result = await finance.get_payment_history("CUST-001")
        assert Decimal(result["totals_by_currency"]["INR"]) == 0
