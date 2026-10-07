"""Resolve ordinary finance reads independently of model tool-selection compliance.

Only current, single-customer requests use this path. Unrecognised or compound
requests remain with the typed agent; unsupported filters receive a question
instead of silently returning a broader dataset.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agent.mock import parse_date
from app.agent.tools import ToolCallback
from app.core.errors import AppError

_WRITES = re.compile(
    r"\b(create|add|change|update|modify|set|delete|remove|post|posting|settle|settlement|send|execute|confirm)\b",
    re.I,
)
_VISUAL = re.compile(r"\b(?:chart|charts|graph|graphs|plot|visuali[sz]e|visuali[sz]ation)\b", re.I)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_REQUEST = re.compile(
    r"^(?:(?:please|can you|could you|would you)\s+)*(?:show(?: me)?|give(?: me)?|get|list|display|"
    r"tell me|what (?:is|are)|what['’]s|which|find|search(?: for)?|plot|chart|graph|visuali[sz]e|draft)\s+",
    re.I,
)
_INTENT = re.compile(
    r"\b(?:payment history|payments?|overdue(?: invoices?)?|outstanding(?: invoices?| balance)?|"
    r"open (?:invoices?|transactions?)|unpaid invoices?|remaining invoice amounts?|balance|finance summary|financial summary|"
    r"summary|collection reminder|reminder|customer details|customer information|customers?|"
    r"invoice details|invoice information|invoices?|details|history)\b",
    re.I,
)
_FILTERS = re.compile(
    r"\b(?:between|since|until|before|after|from|last|next|today|yesterday|tomorrow|"
    r"week|month|quarter|year|monthly|weekly|yearly|daily|group(?:ed)? by|by (?:month|week|year)|"
    r"top|largest|smallest|highest|lowest|above|below|greater|less|only|excluding|exclude|"
    r"paid|unposted|draft|posted|debit|credit)\b|[<>]",
    re.I,
)
_CONTEXTUAL = re.compile(
    r"^(?:the |that |this )?(?:customer|account|amount|balance|invoices?|transactions?|payments?|it|them|those)?$|^(?:behind that amount|overdue invoices?)$",
    re.I,
)


@dataclass
class ReadRequest:
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    query: str | None = None
    clarification: str | None = None


def _intent(text: str) -> str | None:
    lowered = text.casefold()
    if re.search(r"\b(?:collection )?reminder\b", lowered):
        return "draft_collection_reminder"
    if re.search(r"\b(?:finance|financial)?\s*summary\b", lowered):
        return "get_finance_summary"
    if re.search(r"\boverdue\b", lowered):
        return "get_overdue_invoices"
    if re.search(r"\bpayments?\b", lowered):
        return "get_payment_history"
    if re.search(r"\b(?:open|outstanding|unpaid|remaining)\s+(?:invoices?|transactions?)\b", lowered):
        return "get_customer_open_transactions"
    if re.search(r"\b(?:balance|outstanding)\b", lowered):
        return "get_customer_balance"
    if re.search(r"\binvoices?\b", lowered):
        return (
            "get_invoice_details"
            if re.search(r"\b(?:details|information)\b", lowered)
            else "get_customer_open_transactions"
        )
    if re.search(r"\bcustomer\b", lowered):
        return "get_customer"
    if re.search(r"\b(?:details|information)\b", lowered) and re.search(
        r"\b(?:FTI|INV|TEST-INV|ASSIST)-[A-Za-z0-9_.-]+\b", text, re.I
    ):
        return "get_invoice_details"
    return None


def resolve_read_request(message: str, identifiers: dict[str, Any], company: str) -> ReadRequest | None:
    """Recognise bounded forms; never turn a requested write into a read."""
    text = message.strip().rstrip(".!?").strip()
    owed = re.fullmatch(r"(?:how much does|what does)\s+(.+?)\s+owe(?:\s+us)?", text, re.I)
    if owed:
        text = f"Show {owed[1]}'s balance"
    if _WRITES.search(text) or not _REQUEST.match(text):
        return None
    # Multiple operations/customer comparisons are outside this single-read grammar.
    if re.search(r"\b(?:compare|versus|vs|and|or|both|all customers|each customer|across)\b", text, re.I):
        return None
    is_visual = bool(_VISUAL.search(text))
    if not _INTENT.search(text):
        return None
    if re.search(r"\bjournal\b", text, re.I):
        return None
    if (
        re.search(r"\bpayments?\b", text, re.I)
        and re.search(r"\b(?:balance|outstanding|open|overdue|invoices?)\b", text, re.I)
        and not re.search(r"\bsummary\b", text, re.I)
    ):
        return None

    requested_company = re.search(
        r"\b(?:in|for)\s+(?:company|legal entity)\s+([A-Za-z0-9_-]{1,12})\b", text, re.I
    )
    if not requested_company:
        requested_company = re.search(r"\bin\s+([A-Z][A-Z0-9_-]{2,11})\b", text)
        if requested_company and requested_company[1] in {"INR", "USD", "EUR", "GBP", "JPY", "CAD", "AUD"}:
            requested_company = None
    if not requested_company:
        requested_company = re.search(r"\bin\s+(" + re.escape(company) + r")\b", text, re.I)
    if requested_company:
        if requested_company[1].casefold() != company.casefold():
            raise AppError(
                "company_mismatch",
                "Select the requested legal entity in this conversation before querying or changing it.",
            )
        text = text[: requested_company.start()] + text[requested_company.end() :]

    tool_name = _intent(text)
    if is_visual and tool_name == "get_payment_history":
        text = re.sub(r"\bby date\b", "", text, flags=re.I)
    as_of = re.search(
        r"\bas of\s+(.+?)(?=\s+(?:as|in)\s+(?:a\s+)?(?:bar |line |pie )?(?:chart|graph|plot)\b|$)", text, re.I
    )
    arguments: dict[str, Any] = {}
    if as_of:
        cutoff_text = as_of[1].strip()
        cutoff = (
            parse_date(cutoff_text)
            if re.fullmatch(
                r"\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}", cutoff_text, re.I
            )
            else None
        )
        if (
            tool_name not in {"get_overdue_invoices", "get_finance_summary", "draft_collection_reminder"}
            or not cutoff
        ):
            return ReadRequest(
                clarification="This read uses current ERP records. For an overdue due-date cutoff, specify 'overdue as of YYYY-MM-DD'. Date-range or historical balance requests need a separate supported report."
            )
        arguments["as_of_date"] = cutoff
        text = text[: as_of.start()] + text[as_of.end() :]
    filter_text = _REQUEST.sub("", text, count=1) if tool_name == "draft_collection_reminder" else text
    if _FILTERS.search(filter_text) or re.search(
        r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b|\b(?:in|currency)\s+[A-Z]{3}\b", text
    ):
        return ReadRequest(
            clarification="Please specify a supported current balance, open invoices, overdue invoices, or payment history request. This quick read cannot apply the requested date, amount, status, or grouping filter; I haven't returned an unfiltered result."
        )

    body = _REQUEST.sub("", text, count=1).strip()
    body = re.sub(
        r"\b(?:as|in)\s+(?:an?\s+)?(?:bar |line |pie )?(?:chart|graph|plot)\s*$", "", body, flags=re.I
    )
    body = re.sub(
        r"^(?:a |the )?(?:bar |line |pie )?(?:chart|graph|plot)\s+(?:of|for)\s+", "", body, flags=re.I
    )
    body = re.sub(r"\s+(?:as|in)\s+(?:a |the )?(?:chart|graph|plot)\s*$", "", body, flags=re.I).strip()
    # Possessive and 'for/of ACCOUNT' forms preserve full names and account casing.
    possessive = re.search(r"^(.+?)['’]s\s+", body)
    connector = re.search(r"\b(?:for|of)\s+(.+)$", body, re.I)
    if possessive:
        target = possessive[1]
    elif connector:
        target = connector[1]
    else:
        target = _INTENT.sub("", body).strip()
    target = re.sub(r"^(?:the|current|currently|available|all)\s+", "", target, flags=re.I)
    target = re.sub(r"^customer\s+(?:(?:account|named)\s+)?", "", target, flags=re.I)
    target = re.sub(r"\s+(?:are|is)$", "", target, flags=re.I)
    target = " ".join(target.split()).strip(" ,'’\"")

    # Validate the complete phrasing, including text after a possessive target.
    # This prevents e.g. 'Asterion’s payments sorted by amount' from dropping the
    # requested modifier merely because the customer name was easy to extract.
    if not re.search(r"\b(?:find|search)\b", message, re.I):
        remainder = body.replace(target, "", 1) if target else body
        remainder = re.sub(r"['’]s\b", "", remainder)
        remainder = _INTENT.sub("", remainder)
        remainder = re.sub(
            r"\b(?:for|of|the|a|an|this|that|all|current|currently|available|are|is|customer|account|behind|amount)\b",
            "",
            remainder,
            flags=re.I,
        )
        if remainder.strip(" ,'’\""):
            return ReadRequest(
                clarification="I need a specific supported read without extra filters: current balance, open invoices, overdue invoices, payment history, or invoice details. I haven't ignored the extra conditions or returned a broader result."
            )

    # Bare 'history' means payment history only when the same scoped account's
    # preceding request/read supplies that intent. Otherwise it needs a question.
    if tool_name is None and re.search(r"\bhistory\b", text, re.I):
        same_account = not target or target.casefold() == str(identifiers.get("account", "")).casefold()
        previous = _intent(str(identifiers.get("last_user_message", "")))
        if same_account and (
            previous == "get_payment_history" or identifiers.get("last_read_tool") == "get_payment_history"
        ):
            tool_name = "get_payment_history"
        else:
            return ReadRequest(
                clarification="Do you want payment history, open invoices, or a finance summary? Please specify the customer and type of history."
            )
    if not tool_name:
        return None
    if re.search(r"\b(?:find|search)\b", message, re.I):
        target = re.sub(r"^customer\s+", "", body, flags=re.I).strip()
        if target:
            return ReadRequest("search_customers", {"query": target})

    # An invoice reference explicitly named as an invoice is not a customer.
    invoice_id = re.search(
        r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]*(?:FTI|INV|ASSIST)-[A-Za-z0-9_.-]+)\b", body, re.I
    )
    explicit_invoice = re.search(r"\binvoice\s+([A-Za-z0-9][A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]*)\b", body, re.I)
    if (invoice_id or explicit_invoice) and tool_name in {
        "get_invoice_details",
        "get_customer_open_transactions",
        "get_customer_balance",
    }:
        reference = (invoice_id or explicit_invoice)[1]
        return ReadRequest("get_invoice_details", {"identifier": reference})
    if invoice_id or explicit_invoice:
        return None
    if tool_name == "get_invoice_details":
        reference = (
            (None if _CONTEXTUAL.fullmatch(target) else target)
            or identifiers.get("invoice")
            or identifiers.get("draft_invoice")
        )
        if reference and _IDENTIFIER.fullmatch(reference) and re.search(r"[0-9_.-]", reference):
            return ReadRequest(tool_name, {"identifier": reference})
        return ReadRequest(
            clarification="Which invoice should I retrieve? Provide its invoice number or external invoice identifier."
        )

    if not target or _CONTEXTUAL.fullmatch(target):
        account = identifiers.get("account")
        if not account:
            return ReadRequest(
                clarification="Which customer should I use? Provide the customer name or account."
            )
        return ReadRequest(tool_name, {**arguments, "account": account})
    if _IDENTIFIER.fullmatch(target) and re.search(r"[0-9_.-]", target):
        if len(target) > 40:
            return ReadRequest(clarification="Provide a customer account of at most 40 characters.")
        return ReadRequest(tool_name, {**arguments, "account": target})
    return ReadRequest(tool_name, arguments, query=target)


def _text(value: Any) -> str:
    return str(value).replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def _amount(value: Any) -> str:
    if value is None or value == "":
        return "Unavailable"
    try:
        number = Decimal(str(value))
        return f"{number:,.2f}" if number.is_finite() else "Unavailable"
    except (InvalidOperation, ValueError):
        return "Unavailable"


def format_read_result(name: str, result: dict[str, Any], company: str) -> str:
    """Render fresh provider records; do not invent missing amounts or recalculate totals."""
    customer = result.get("customer") or {}
    account = customer.get("account") or result.get("account")
    customer_label = _text(customer.get("name") or account or "the selected customer")
    if account:
        customer_label += f" ({_text(account)})"
    label = f"**Mock data · {company.upper()}**\n\n" if result.get("mock_mode") else ""
    if name == "search_customers":
        customers = result.get("customers", [])
        body = (
            "\n".join(
                f"- **{_text(row['account'])}** — {_text(row.get('name') or row['account'])}"
                for row in customers
            )
            or "No matching customers were found."
        )
    elif name == "get_customer":
        body = f"**{customer_label}** · {company.upper()}"
        for field, title in (("currency", "Currency"), ("payment_terms", "Payment terms")):
            if customer.get(field):
                body += f"\n\n{title}: {_text(customer[field])}"
    elif name == "draft_collection_reminder":
        body = f"Subject: {_text(result.get('subject') or 'Collection reminder')}\n\n{result.get('body') or 'No reminder text was returned.'}\n\n**Draft only — no email was sent.**"
    elif name == "get_payment_history":
        rows = result.get("payments", [])
        body = f"Payment history for **{customer_label}** in **{company.upper()}**.\n\n"
        body += (
            "\n".join(
                f"- {_text(row.get('payment_date') or 'Date unavailable')} · {_text(row.get('currency') or 'Currency unavailable')} {_amount(row.get('amount'))} · {_text(row.get('reference') or row.get('voucher') or 'Reference unavailable')}"
                for row in rows[:20]
            )
            if rows
            else "No payment records were returned for this customer. Unposted payment journal lines are not posted payment history."
        )
        if len(rows) > 20:
            body += f"\n\nShowing 20 of {len(rows)} returned payment records; the evidence contains the returned records."
    else:
        invoice = result.get("invoice")
        rows = [invoice] if invoice else result.get("invoices") or result.get("transactions") or []
        if name == "get_invoice_details":
            body = (
                f"Invoice **{_text(invoice.get('invoice_number') or invoice.get('identifier') or invoice.get('external_id') or 'Identifier unavailable')}** for **{customer_label}** in **{company.upper()}**."
                if invoice
                else "No matching invoice was returned."
            )
            if invoice and isinstance(invoice.get("is_posted"), bool):
                body += " This invoice is posted." if invoice["is_posted"] else " This invoice is unposted."
        elif name == "get_overdue_invoices":
            body = f"Currently open overdue invoices for **{customer_label}** in **{company.upper()}**, using the due-date cutoff **{_text(result.get('as_of_date') or 'Date unavailable')}**."
        elif name == "get_finance_summary":
            body = f"Current finance summary for **{customer_label}** in **{company.upper()}**."
        else:
            body = f"Current open amounts for **{customer_label}** in **{company.upper()}**."
        totals = result.get("totals_by_currency")
        if isinstance(totals, dict) and totals:
            body += (
                "\n\n"
                + "; ".join(
                    f"**{_text(currency)} {_amount(amount)}** outstanding"
                    for currency, amount in totals.items()
                )
                + "."
            )
        if rows:
            body += "\n\n| Invoice / reference | Original | Remaining | Due |\n| --- | ---: | ---: | --- |\n"
            body += "\n".join(
                f"| {_text(row.get('invoice_number') or row.get('identifier') or row.get('voucher') or 'Identifier unavailable')} | {_text(row.get('currency') or '')} {_amount(row.get('original_amount'))} | {_text(row.get('currency') or '')} {_amount(row.get('remaining_amount'))} | {_text(row.get('due_date') or 'Unavailable')} |"
                for row in rows[:20]
            )
            if len(rows) > 20:
                body += f"\n\nShowing 20 of {len(rows)} returned records; the evidence contains the returned records."
        elif name in {
            "get_customer_balance",
            "get_customer_open_transactions",
            "get_overdue_invoices",
            "get_finance_summary",
        }:
            body += "\n\nNo matching open records were returned."
        if result.get("unknown_due_date_count"):
            body += f"\n\n{result['unknown_due_date_count']} open invoice(s) have no verified due date; overdue status could not be determined for them."
        if result.get("balance_note"):
            body += "\n\n" + _text(result["balance_note"])
        if name == "get_finance_summary":
            if diagnostic := result.get("payment_history_diagnostic"):
                body += "\n\nPayment history is unavailable: " + _text(
                    diagnostic.get("message") or "the payment source could not be verified"
                )
            else:
                body += f"\n\n{len(result.get('overdue_invoices') or [])} overdue invoice(s) and {len(result.get('payments') or [])} payment record(s) returned."
    evidence = result.get("evidence") or []
    retrieved_at = result.get("retrieved_at") or (evidence[0].get("retrieved_at") if evidence else None)
    if retrieved_at:
        body += f"\n\nRetrieved from {'mock ' if result.get('mock_mode') else ''}Dynamics 365 at {_text(retrieved_at)}."
    return label + body


async def execute_read_request(request: ReadRequest, company: str, execute: ToolCallback) -> str:
    if request.clarification:
        return request.clarification
    arguments = dict(request.arguments)
    if request.query:
        search = await execute("search_customers", {"query": request.query})
        if search.get("error"):
            error = search["error"]
            raise AppError(
                error.get("code") or "d365_read_error",
                error.get("message") or "The Dynamics 365 customer lookup failed.",
            )
        customers = search.get("customers") or []
        if not customers:
            return f"No customers were found matching '{_text(request.query)}' in {company.upper()}."
        if len(customers) != 1:
            return (
                format_read_result("search_customers", search, company)
                + "\n\nWhich customer account should I use?"
            )
        arguments["account"] = customers[0]["account"]
    result = await execute(request.tool_name, arguments)
    if result.get("error"):
        error = result["error"]
        raise AppError(
            error.get("code") or "d365_read_error",
            error.get("message") or "The requested Dynamics 365 read failed.",
        )
    return format_read_result(request.tool_name, result, company)
