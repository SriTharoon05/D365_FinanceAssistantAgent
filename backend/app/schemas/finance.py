"""Typed finance facts and mutation inputs. Decimal amounts serialize as strings."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FinanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolEvidence(FinanceModel):
    kind: str
    company: str
    currency: str | None = None
    account: str | None = None
    customer_name: str | None = None
    invoice_number: str | None = None
    original_amount: Decimal | None = None
    remaining_amount: Decimal | None = None
    due_date: date | None = None
    voucher: str | None = None
    source_entity: str
    retrieved_at: datetime
    payment_date: date | None = None
    transaction_date: date | None = None
    reference: str | None = None
    amount: Decimal | None = None
    days_overdue: int | None = None


class Customer(FinanceModel):
    account: str
    name: str
    company: str
    currency: str
    customer_group: str | None = None
    payment_terms: str | None = None
    source_entity: str
    retrieved_at: datetime


class OpenTransaction(FinanceModel):
    account: str
    company: str
    invoice_number: str
    currency: str
    original_amount: Decimal | None = None
    remaining_amount: Decimal
    due_date: date | None = None
    transaction_date: date | None = None
    voucher: str | None = None
    source_entity: str
    retrieved_at: datetime


class Invoice(OpenTransaction):
    is_posted: bool = True


class Payment(FinanceModel):
    account: str
    company: str
    currency: str
    amount: Decimal
    voucher: str | None = None
    payment_date: date | None = None
    reference: str | None = None
    source_entity: str
    retrieved_at: datetime


class OutstandingBalance(FinanceModel):
    company: str
    account: str
    totals_by_currency: dict[str, Decimal]
    retrieved_at: datetime


class OverdueInvoice(OpenTransaction):
    days_overdue: int


class CreateCustomer(FinanceModel):
    account: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=100)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    customer_group: str = Field(default="10", max_length=40)
    payment_terms: str | None = Field(default=None, max_length=40)


class UpdateCustomer(FinanceModel):
    account: str = Field(min_length=1, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    payment_terms: str | None = Field(default=None, min_length=1, max_length=40)
    customer_group: str | None = Field(default=None, min_length=1, max_length=40)


class DeleteCustomer(FinanceModel):
    account: str = Field(min_length=1, max_length=40)


class DraftInvoiceLine(FinanceModel):
    description: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    revenue_account: str | None = Field(default=None, max_length=40)

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("Amount must be finite")
        return value


class CreateDraftInvoice(FinanceModel):
    account: str = Field(min_length=1, max_length=40)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    due_date: date
    invoice_date: date = Field(default_factory=date.today)
    external_id: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    lines: list[DraftInvoiceLine] = Field(min_length=1, max_length=50)


class UpdateDraftInvoice(FinanceModel):
    identifier: str = Field(min_length=1, max_length=50)
    due_date: date


class DeleteDraftInvoice(FinanceModel):
    identifier: str = Field(min_length=1, max_length=50)


class CreatePaymentJournal(FinanceModel):
    journal_name: str | None = Field(default=None, max_length=40)
    description: str = Field(default="Finance Assistant customer payment", max_length=200)


class AddPaymentLine(FinanceModel):
    journal_number: str = Field(min_length=1, max_length=50)
    account: str = Field(min_length=1, max_length=40)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    line_number: int = Field(gt=0, le=2147483647)
    payment_date: date = Field(default_factory=date.today)
    reference: str = Field(min_length=1, max_length=100)
    bank_account: str | None = Field(default=None, max_length=40)
    payment_method: str | None = Field(default=None, max_length=40)
    posting_profile: str | None = Field(default=None, max_length=40)
    offset_account_type: Literal["Bank"] = "Bank"


MUTATION_SCHEMAS = {
    "create_customer": CreateCustomer,
    "update_customer": UpdateCustomer,
    "delete_test_customer": DeleteCustomer,
    "create_draft_free_text_invoice": CreateDraftInvoice,
    "update_draft_free_text_invoice": UpdateDraftInvoice,
    "delete_draft_free_text_invoice": DeleteDraftInvoice,
    "create_customer_payment_journal": CreatePaymentJournal,
    "add_customer_payment_line": AddPaymentLine,
}
