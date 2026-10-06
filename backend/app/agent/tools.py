"""A closed, typed tool catalog; models never construct OData URLs."""

import json
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model

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


READ_TOOLS: dict[str, tuple[type[BaseModel], str]] = {
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
    "update_draft_free_text_invoice": "Propose changing an UNPOSTED invoice's due date. Does not execute.",
    "delete_draft_free_text_invoice": "Propose deleting an UNPOSTED draft invoice. Does not execute.",
    "create_customer_payment_journal": "Propose creating an unposted customer payment journal HEADER only. A payment line is a separate confirmed action.",
    "add_customer_payment_line": "Propose an unposted payment line with an explicit line_number and reference. Does not settle or post.",
}

TOOL_SCHEMAS: dict[str, type[BaseModel]] = {name: value[0] for name, value in READ_TOOLS.items()}
TOOL_SCHEMAS.update(
    {
        name: create_model(f"{schema.__name__}ToolArguments", __base__=(schema, CompanyArguments))
        for name, schema in MUTATION_SCHEMAS.items()
    }
)

ToolCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def build_tools(execute: ToolCallback, writes_enabled: bool = True) -> list[StructuredTool]:
    """Bind a fixed set of schema-validated tools to one conversation's executor."""
    names = list(READ_TOOLS) + (list(MUTATION_SCHEMAS) if writes_enabled else [])
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
                description=READ_TOOLS[name][1] if name in READ_TOOLS else WRITE_DESCRIPTIONS[name],
                args_schema=TOOL_SCHEMAS[name],
            )
        )
    return result
