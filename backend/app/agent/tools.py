"""A closed, typed tool catalog; models never construct OData URLs."""

import json
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from app.schemas.finance import MUTATION_SCHEMAS


class CompanyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str | None = Field(default=None, max_length=12, pattern=r"^[A-Za-z0-9_-]+$")


class SearchArguments(CompanyArguments):
    query: str = Field(min_length=1, max_length=100)


class AccountArguments(CompanyArguments):
    account: str = Field(min_length=1, max_length=40)


class DatedAccountArguments(AccountArguments):
    as_of_date: date | None = Field(
        default=None,
        description="Due-date cutoff applied to currently open amounts; not a historical balance snapshot.",
    )


class InvoiceArguments(CompanyArguments):
    identifier: str = Field(min_length=1, max_length=100)


class WriteSetupArguments(CompanyArguments):
    purpose: Literal["invoice", "payment", "customer", "all"] = "all"


WRITE_CLARIFICATION_TOOL = "request_write_clarification"
WriteOperation = Literal[tuple(MUTATION_SCHEMAS)]
WriteField = Literal[
    tuple(sorted({field for schema in MUTATION_SCHEMAS.values() for field in schema.model_fields}))
]


class WriteClarificationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: WriteOperation
    missing_fields: list[WriteField] = Field(
        min_length=1,
        max_length=20,
        description="Input fields still needed for this operation; never invent their values.",
    )

    @model_validator(mode="after")
    def fields_belong_to_operation(self):
        fields = MUTATION_SCHEMAS[self.operation].model_fields
        if any(name not in fields for name in self.missing_fields):
            raise ValueError("Missing fields must belong to the selected operation")
        return self


def write_clarification_message(
    arguments: WriteClarificationArguments, *, proposal_prepared: bool = False
) -> str:
    """Render input help without trusting model prose or claiming current ERP facts."""
    labels = {
        "account": "customer account",
        "name": "customer name",
        "identifier": "draft invoice identifier",
        "external_id": "unique external invoice identifier",
        "lines": "invoice lines with descriptions and amounts",
        "due_date": "due date (YYYY-MM-DD)",
        "invoice_date": "invoice date (YYYY-MM-DD)",
        "journal_number": "payment journal number",
        "line_number": "positive line number",
        "payment_date": "payment date (YYYY-MM-DD)",
        "reference": "unique payment reference",
    }
    fields = list(dict.fromkeys(arguments.missing_fields))
    requested = ", ".join(labels.get(name, name.replace("_", " ")) for name in fields)
    outcome = (
        "No additional action was prepared; review the existing confirmation card."
        if proposal_prepared
        else "No action was prepared."
    )
    return f"Please provide the {requested}. {outcome}"


READ_TOOLS: dict[str, tuple[type[BaseModel], str]] = {
    "get_write_setup": (
        WriteSetupArguments,
        "Read verified setup choices and configured defaults for invoice revenue accounts, payment "
        "journal names and customer payment terms in the selected company. A revenue account is a "
        "general-ledger MainAccountId, not a customer or bank account. Never invent or silently choose "
        "a different account; show valid choices if the configured default cannot be verified.",
    ),
    "search_customers": (
        SearchArguments,
        "Find customers by account or name. Ask the user to choose if multiple match.",
    ),
    "get_customer": (AccountArguments, "Retrieve a verified customer master record."),
    "get_customer_balance": (
        AccountArguments,
        "Compute current net outstanding balance by currency, including signed open credits and payments.",
    ),
    "get_customer_open_transactions": (
        AccountArguments,
        "Get invoices/open transactions supporting a customer's balance.",
    ),
    "get_overdue_invoices": (
        DatedAccountArguments,
        "Retrieve currently open invoices overdue at the specified due-date cutoff. "
        "Amounts are current; this does not reconstruct historical balances or settlements.",
    ),
    "get_invoice_details": (
        InvoiceArguments,
        "Retrieve an invoice by invoice number or external invoice identifier.",
    ),
    "get_payment_history": (AccountArguments, "Retrieve the customer's verified payment history."),
    "get_finance_summary": (
        DatedAccountArguments,
        "Get outstanding balances, overdue invoices and payments from verified ERP records.",
    ),
    "draft_collection_reminder": (
        DatedAccountArguments,
        "Draft a collection reminder using only verified finance data. Never send email.",
    ),
}

WRITE_DESCRIPTIONS = {
    "create_customer": "Validate and propose creating a customer. Returns a pending confirmation; does not execute.",
    "update_customer": "Validate and propose safe customer master changes. Does not execute.",
    "delete_test_customer": "Propose deleting an allowed test customer with no transactions. Does not execute.",
    "create_draft_free_text_invoice": "Propose an UNPOSTED draft invoice with explicit lines and a unique external_id. Does not execute.",
    "update_draft_free_text_invoice": "Validate and propose changing an UNPOSTED draft invoice's due date using its identifier and an explicit due_date (YYYY-MM-DD). This is the tool for a requested due-date change; get_invoice_details only reads. Returns a pending Confirm card; does not execute.",
    "delete_draft_free_text_invoice": "Propose deleting an UNPOSTED draft invoice. Does not execute.",
    "create_customer_payment_journal": "Propose creating an unposted customer payment journal HEADER only. A payment line is a separate confirmed action.",
    "add_customer_payment_line": "Validate and propose an unposted payment line in an existing journal_number. Requires the customer account, amount, currency, positive line_number, unique payment reference and payment_date. A confirmed journal-header reference identifies the target only. Request missing inputs; never invent them. Returns a pending Confirm card; does not settle or post.",
}

TOOL_SCHEMAS: dict[str, type[BaseModel]] = {name: value[0] for name, value in READ_TOOLS.items()}
TOOL_SCHEMAS.update(
    {
        name: create_model(f"{schema.__name__}ToolArguments", __base__=(schema, CompanyArguments))
        for name, schema in MUTATION_SCHEMAS.items()
    }
)
TOOL_SCHEMAS[WRITE_CLARIFICATION_TOOL] = WriteClarificationArguments

ToolCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def build_tools(execute: ToolCallback, writes_enabled: bool = True) -> list[StructuredTool]:
    """Bind a fixed set of schema-validated tools to one conversation's executor."""
    names = list(READ_TOOLS) + ([*MUTATION_SCHEMAS, WRITE_CLARIFICATION_TOOL] if writes_enabled else [])
    result: list[StructuredTool] = []
    for name in names:

        def make_callback(tool_name: str) -> Callable[..., Awaitable[str]]:
            async def invoke(**kwargs: Any) -> str:
                payload = await execute(tool_name, kwargs)
                return json.dumps(payload, ensure_ascii=False, default=str)

            return invoke

        result.append(
            StructuredTool.from_function(
                coroutine=make_callback(name),
                name=name,
                description=(
                    "Ask for missing inputs for a supported proposed write. Use only field names from "
                    "that operation's schema. This local tool prepares no action and reads or writes "
                    "no ERP data; it returns a fixed clarification question."
                    if name == WRITE_CLARIFICATION_TOOL
                    else READ_TOOLS[name][1]
                    if name in READ_TOOLS
                    else WRITE_DESCRIPTIONS[name]
                ),
                args_schema=TOOL_SCHEMAS[name],
            )
        )
    return result
