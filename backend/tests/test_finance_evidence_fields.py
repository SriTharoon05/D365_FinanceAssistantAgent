"""Finance facts keep payment details and calendar dates through JSON evidence."""

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.integrations.d365.finance import D365FinanceService, evidence
from app.schemas.finance import ToolEvidence


BASE = {
    "company": "usmf",
    "account": "TEST-CHAT-002",
    "currency": "INR",
    "source_entity": "CustTransBiEntities",
    "retrieved_at": datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc),
}
CUSTOMER = {
    **BASE,
    "name": "Verified Customer",
    "source_entity": "CustomersV3",
}
INVOICE = {
    **BASE,
    "invoice_number": "TEST-INV-001",
    "voucher": "INV-001",
    "original_amount": "100.00",
    "remaining_amount": "50.00",
    "transaction_date": "2026-09-01",
    "due_date": "2026-10-01",
    "is_invoice": True,
}


class FinanceRecords:
    mock = False

    def __init__(self, *, payments=None, transactions=None):
        self.payment_rows = payments or []
        self.transaction_rows = transactions or []

    async def get_customer(self, account, company):
        assert account == BASE["account"]
        assert company == BASE["company"]
        return CUSTOMER.copy()

    async def payments(self, account, company):
        return [row.copy() for row in self.payment_rows]

    async def open_transactions(self, account, company):
        return [row.copy() for row in self.transaction_rows]


@pytest.mark.parametrize("amount", ["100.00", "-100.00", "0.000000000000000001"])
def test_payment_evidence_retains_exact_signed_amount_reference_and_calendar_dates(amount):
    row = {
        **BASE,
        "amount": Decimal(amount),
        "payment_date": date(2026, 10, 6),
        "transaction_date": date(2026, 10, 5),
        "reference": "TEST-PAY-001",
        "voucher": "PAY-001",
    }

    proof = evidence(row, "payment", CUSTOMER)
    serialized = json.loads(json.dumps(proof))

    assert serialized["payment_date"] == "2026-10-06"
    assert serialized["transaction_date"] == "2026-10-05"
    assert serialized["reference"] == "TEST-PAY-001"
    assert isinstance(serialized["amount"], str)
    assert Decimal(serialized["amount"]) == Decimal(amount)
    assert serialized["customer_name"] == CUSTOMER["name"]
    assert serialized["source_entity"] == BASE["source_entity"]
    assert serialized["voucher"] == "PAY-001"
    assert ToolEvidence.model_validate(serialized).amount == Decimal(amount)


@pytest.mark.parametrize(
    "kind,legacy_fields",
    [
        ("customer", {"name": "Legacy Customer"}),
        ("invoice", {"invoice_number": "LEGACY-INV", "original_amount": "125.00", "due_date": "2026-11-15"}),
        ("payment", {"voucher": "LEGACY-PAY", "original_amount": "25.00"}),
    ],
)
def test_legacy_evidence_rows_remain_compatible_with_nullable_enrichment_fields(kind, legacy_fields):
    proof = evidence({**BASE, **legacy_fields}, kind)

    assert proof["kind"] == kind
    assert proof["company"] == BASE["company"]
    assert proof["account"] == BASE["account"]
    assert proof["source_entity"] == BASE["source_entity"]
    for optional in ("payment_date", "transaction_date", "reference", "amount", "days_overdue"):
        assert proof[optional] is None
    if kind == "customer":
        assert proof["customer_name"] == legacy_fields["name"]
    if "original_amount" in legacy_fields:
        assert Decimal(proof["original_amount"]) == Decimal(legacy_fields["original_amount"])
    assert ToolEvidence.model_validate(json.loads(json.dumps(proof))).kind == kind


@pytest.mark.asyncio
async def test_payment_history_evidence_keeps_actual_amount_dates_and_legacy_original_amount_alias():
    payment_rows = [
        {
            **BASE,
            "amount": "100.00",
            "payment_date": "2026-10-06",
            "transaction_date": "2026-10-05",
            "reference": "TEST-PAY-001",
            "voucher": "PAYMENT",
        },
        {
            **BASE,
            "amount": "-100.00",
            "payment_date": "2026-10-07",
            "reference": "TEST-PAY-001-REVERSAL",
            "voucher": "REVERSAL",
            "is_reversal": True,
        },
    ]
    finance = D365FinanceService(FinanceRecords(payments=payment_rows), Settings(_env_file=None))

    result = await finance.get_payment_history(BASE["account"])
    proofs = {proof["voucher"]: proof for proof in result["evidence"]}

    assert Decimal(result["totals_by_currency"]["INR"]) == 0
    assert proofs["PAYMENT"]["amount"] == "100.00"
    assert proofs["PAYMENT"]["original_amount"] == "100.00"
    assert proofs["PAYMENT"]["payment_date"] == "2026-10-06"
    assert proofs["PAYMENT"]["transaction_date"] == "2026-10-05"
    assert proofs["PAYMENT"]["reference"] == "TEST-PAY-001"
    assert proofs["REVERSAL"]["amount"] == "-100.00"
    assert proofs["REVERSAL"]["original_amount"] == "-100.00"
    assert proofs["REVERSAL"]["payment_date"] == "2026-10-07"
    assert proofs["REVERSAL"]["reference"] == "TEST-PAY-001-REVERSAL"
    assert proofs["REVERSAL"]["transaction_date"] is None
    assert all(proof["kind"] == "payment" for proof in proofs.values())
    assert json.loads(json.dumps(result["evidence"])) == result["evidence"]
    assert all("original_amount" not in row for row in payment_rows)


@pytest.mark.asyncio
async def test_overdue_evidence_preserves_days_overdue_and_both_invoice_calendar_dates():
    finance = D365FinanceService(FinanceRecords(transactions=[INVOICE]), Settings(_env_file=None))

    result = await finance.get_overdue_invoices(BASE["account"], "2026-10-07")
    proof = result["evidence"][0]

    assert result["invoices"][0]["days_overdue"] == 6
    assert proof["days_overdue"] == result["invoices"][0]["days_overdue"]
    assert isinstance(proof["days_overdue"], int)
    assert proof["due_date"] == "2026-10-01"
    assert proof["transaction_date"] == "2026-09-01"
    assert proof["invoice_number"] == INVOICE["invoice_number"]
    assert proof["original_amount"] == "100.00"
    assert proof["remaining_amount"] == "50.00"
    assert proof["kind"] == "overdue_invoice"
    assert proof["payment_date"] is None
    assert json.loads(json.dumps(proof))["days_overdue"] == 6
    assert "days_overdue" not in INVOICE
