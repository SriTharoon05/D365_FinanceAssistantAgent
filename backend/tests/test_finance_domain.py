from decimal import Decimal

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.finance import totals
from app.integrations.d365.runtime import D365Runtime


@pytest.fixture
async def runtime():
    runtime = D365Runtime(Settings(_env_file=None, d365_mock_mode=True))
    await runtime.start()
    yield runtime
    await runtime.close()


@pytest.mark.asyncio
async def test_mock_balance_and_evidence_match_authoritative_fixture(runtime):
    result = await runtime.finance.get_customer_balance("AST-001")
    assert result["totals_by_currency"] == {"INR": "110000"}
    assert len(result["transactions"]) == 2
    assert result["mock_mode"] is True
    assert result["evidence"][0]["account"] == "AST-001"
    assert result["evidence"][0]["source_entity"].startswith("MOCK:")
    assert result["evidence"][0]["retrieved_at"].endswith(("+00:00", "Z"))


@pytest.mark.asyncio
async def test_mixed_currency_totals_are_separate_and_decimal_exact(runtime):
    runtime.provider.invoices["USD-1"] = {
        "account": "AST-001",
        "company": "usmf",
        "invoice_number": "USD-1",
        "currency": "USD",
        "original_amount": "0.1",
        "remaining_amount": "0.1",
        "due_date": "2026-09-30",
        "transaction_date": "2026-09-01",
        "voucher": "TEST",
        "is_posted": True,
    }
    runtime.provider.invoices["USD-2"] = {
        **runtime.provider.invoices["USD-1"],
        "invoice_number": "USD-2",
        "original_amount": "0.2",
        "remaining_amount": "0.2",
    }
    result = await runtime.finance.get_customer_balance("AST-001")
    assert result["totals_by_currency"] == {"INR": "110000", "USD": "0.3"}
    assert Decimal(result["totals_by_currency"]["USD"]) == Decimal("0.3")


@pytest.mark.asyncio
async def test_overdue_uses_remaining_balance_and_strict_due_date(runtime):
    result = await runtime.finance.get_overdue_invoices("AST-001", "2026-10-05")
    assert result["totals_by_currency"] == {"INR": "35000"}
    assert [row["invoice_number"] for row in result["invoices"]] == ["FTI-00000022"]
    assert result["invoices"][0]["days_overdue"] == 5
    on_due_date = await runtime.finance.get_overdue_invoices("AST-001", "2026-09-30")
    assert not on_due_date["invoices"]


@pytest.mark.asyncio
async def test_collection_reminder_is_draft_with_verified_invoice_references(runtime):
    reminder = await runtime.finance.draft_collection_reminder("AST-001", "2026-10-05")
    assert reminder["draft_only"]
    assert "FTI-00000022" in reminder["body"]
    assert "35000" in reminder["body"]
    assert "FTI-00000021" not in reminder["body"]
    assert reminder["evidence"]


@pytest.mark.asyncio
async def test_customer_typed_crud_and_test_prefix_gate(runtime):
    finance = runtime.finance
    payload = {"account": "TEST-ACME-001", "name": "Acme Test Ltd", "currency": "INR"}
    preview = await finance.validate_mutation("create_customer", payload)
    assert preview["proposed_changes"]["customer_group"] == "10"
    assert "TEST-ACME-001" not in runtime.provider.customers
    created = await finance.execute_mutation("create_customer", payload)
    assert created["result"]["verified"]
    await finance.execute_mutation("update_customer", {"account": "TEST-ACME-001", "payment_terms": "Net60"})
    assert (await finance.get_customer("TEST-ACME-001"))["customer"]["payment_terms"] == "Net60"
    deleted = await finance.execute_mutation("delete_test_customer", {"account": "TEST-ACME-001"})
    assert deleted["result"]["deleted"]
    with pytest.raises(AppError) as error:
        await finance.validate_mutation("delete_test_customer", {"account": "AST-001"})
    assert error.value.code == "UNSAFE_MUTATION"


@pytest.mark.asyncio
async def test_unposted_invoice_crud_never_affects_posted_balance(runtime):
    finance = runtime.finance
    payload = {
        "account": "AST-001",
        "external_id": "TEST-INV-1",
        "currency": "INR",
        "due_date": "2026-10-30",
        "lines": [{"description": "Consulting", "amount": "5000.00"}],
    }
    preview = await finance.validate_mutation("create_draft_free_text_invoice", payload)
    assert preview["financial_impact"]["amount"] == "5000.00"
    created = await finance.execute_mutation("create_draft_free_text_invoice", payload)
    assert created["result"]["invoice"]["is_posted"] is False
    assert (await finance.get_customer_balance("AST-001"))["totals_by_currency"] == {"INR": "110000"}
    await finance.execute_mutation(
        "update_draft_free_text_invoice", {"identifier": "TEST-INV-1", "due_date": "2026-11-01"}
    )
    assert (await finance.get_invoice_details("TEST-INV-1"))["invoice"]["due_date"] == "2026-11-01"
    await finance.execute_mutation("delete_draft_free_text_invoice", {"identifier": "TEST-INV-1"})
    with pytest.raises(AppError) as error:
        await finance.validate_mutation("delete_draft_free_text_invoice", {"identifier": "FTI-00000022"})
    assert error.value.code == "UNSAFE_MUTATION"


@pytest.mark.asyncio
async def test_customer_with_fully_settled_historical_invoice_cannot_be_deleted(runtime):
    await runtime.finance.execute_mutation(
        "create_customer", {"account": "TEST-HISTORY", "name": "Test history"}
    )
    runtime.provider.invoices["SETTLED"] = {
        **runtime.provider.invoices["FTI-00000022"],
        "account": "TEST-HISTORY",
        "invoice_number": "SETTLED",
        "remaining_amount": "0",
    }
    assert not (await runtime.finance.get_customer_open_transactions("TEST-HISTORY"))["transactions"]
    with pytest.raises(AppError) as error:
        await runtime.finance.validate_mutation("delete_test_customer", {"account": "TEST-HISTORY"})
    assert error.value.code == "UNSAFE_MUTATION"


@pytest.mark.asyncio
async def test_payment_journal_and_explicit_line_number_are_unposted(runtime):
    finance = runtime.finance
    created = await finance.execute_mutation("create_customer_payment_journal", {})
    number = created["result"]["identifier"]
    payload = {
        "journal_number": number,
        "account": "AST-001",
        "amount": "10000",
        "currency": "INR",
        "payment_date": "2026-10-05",
        "reference": "TEST-PAYMENT-1",
        "line_number": 1,
    }
    line = await finance.execute_mutation("add_customer_payment_line", payload)
    assert line["result"]["posted"] is False
    assert "post" in line["result"]["manual_instructions"]
    assert (await finance.get_customer_balance("AST-001"))["totals_by_currency"] == {"INR": "110000"}
    with pytest.raises(AppError):
        await finance.validate_mutation(
            "add_customer_payment_line",
            {key: value for key, value in payload.items() if key != "line_number"},
        )
    with pytest.raises(AppError) as error:
        await finance.execute_mutation("add_customer_payment_line", payload)
    assert error.value.code == "D365_DUPLICATE_RECORD"


@pytest.mark.asyncio
async def test_execution_revalidates_posted_state_after_preview(runtime):
    payload = {
        "account": "AST-001",
        "external_id": "TEST-RACE",
        "due_date": "2026-10-30",
        "lines": [{"description": "Example", "amount": "10"}],
    }
    await runtime.finance.execute_mutation("create_draft_free_text_invoice", payload)
    update = {"identifier": "TEST-RACE", "due_date": "2026-11-01"}
    await runtime.finance.validate_mutation("update_draft_free_text_invoice", update)
    runtime.provider.invoices["TEST-RACE"]["is_posted"] = True
    with pytest.raises(AppError) as error:
        await runtime.finance.execute_mutation("update_draft_free_text_invoice", update)
    assert error.value.code == "UNSAFE_MUTATION"


@pytest.mark.asyncio
async def test_live_without_credentials_does_not_fall_back_to_mock():
    runtime = D365Runtime(Settings(_env_file=None, d365_mock_mode=False))
    try:
        assert (await runtime.start())["status"] == "disconnected"
        with pytest.raises(AppError):
            await runtime.finance.get_customer_balance("AST-001")
        assert runtime.provider.mock is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_unknown_due_date_reminder_does_not_claim_no_payment_needed(runtime):
    for row in runtime.provider.invoices.values():
        row["due_date"] = None
    reminder = await runtime.finance.draft_collection_reminder("AST-001", "2026-10-05")
    assert reminder["unknown_due_date_count"] == 2
    assert "cannot be fully determined" in reminder["body"]
    assert "No payment reminder is needed" not in reminder["body"]


@pytest.mark.asyncio
async def test_net_balance_preserves_open_credits_and_payment_reversals(runtime):
    base = runtime.provider.invoices["FTI-00000022"]
    records = {
        "UNAPPLIED": {"remaining_amount": "-5000", "transaction_type": "payment", "is_invoice": False},
        "REVERSAL": {"remaining_amount": "2500", "transaction_type": "payment", "is_invoice": False},
        "CREDIT": {"remaining_amount": "-300", "transaction_type": "cust", "is_invoice": True},
    }
    for key, fields in records.items():
        runtime.provider.invoices[key] = {
            **base,
            **fields,
            "invoice_number": key if fields["is_invoice"] else "",
            "voucher": key,
        }
    balance = await runtime.finance.get_customer_balance("AST-001")
    assert balance["totals_by_currency"] == {"INR": "107200"}
    assert balance["debit_totals_by_currency"] == {"INR": "112500"}
    assert balance["credit_totals_by_currency"] == {"INR": "-5300"}
    kinds = {item["voucher"]: item["kind"] for item in balance["evidence"]}
    assert kinds["UNAPPLIED"] == "payment"
    assert kinds["REVERSAL"] == "payment"
    assert kinds["CREDIT"] == "credit"
    overdue = await runtime.finance.get_overdue_invoices("AST-001", "2026-10-05")
    assert overdue["totals_by_currency"] == {"INR": "35000"}
    assert [row["invoice_number"] for row in overdue["invoices"]] == ["FTI-00000022"]


@pytest.mark.asyncio
async def test_overdue_cutoff_excludes_noninvoice_and_later_transactions(runtime):
    base = runtime.provider.invoices["FTI-00000022"]
    runtime.provider.invoices["UNKNOWN-PAYMENT"] = {
        **base,
        "invoice_number": "",
        "is_invoice": False,
        "remaining_amount": "400",
        "due_date": None,
    }
    runtime.provider.invoices["UNKNOWN-INVOICE"] = {
        **base,
        "invoice_number": "UNKNOWN-INVOICE",
        "is_invoice": True,
        "remaining_amount": "500",
        "due_date": None,
    }
    runtime.provider.invoices["LATER"] = {
        **base,
        "invoice_number": "LATER",
        "is_invoice": True,
        "remaining_amount": "900",
        "transaction_date": "2026-10-07",
    }
    overdue = await runtime.finance.get_overdue_invoices("AST-001", "2026-10-05")
    assert overdue["totals_by_currency"] == {"INR": "35000"}
    assert overdue["unknown_due_date_count"] == 1
    assert overdue["balance_basis"] == "current"
    assert overdue["date_basis"] == "current_open_amounts_due_date_cutoff"
    assert "does not reconstruct historical" in overdue["balance_note"]
    reminder = await runtime.finance.draft_collection_reminder("AST-001", "2026-10-05")
    assert "Our current records" in reminder["body"]
    assert "due dates before 2026-10-05" in reminder["body"]
    assert "Our records as of" not in reminder["body"]


def test_currency_totals_preserve_cents_beyond_default_decimal_precision():
    rows = [
        {"currency": "USD", "remaining_amount": "100000000000000000000000000.01"},
        {"currency": "USD", "remaining_amount": "0.02"},
        {"currency": "EUR", "remaining_amount": "-0.30"},
    ]
    assert totals(rows) == {"EUR": "-0.30", "USD": "100000000000000000000000000.03"}
