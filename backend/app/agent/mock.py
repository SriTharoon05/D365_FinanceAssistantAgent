"""Deterministic offline conversational routing, explicitly limited to mock ERP mode."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.agent.tools import ToolCallback


def parse_date(text: str) -> str | None:
    """Accept ISO and English calendar dates without inventing a missing date."""
    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso:
        try:
            return date.fromisoformat(iso.group(1)).isoformat()
        except ValueError:
            return None
    calendar = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})\b", text)
    if calendar:
        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(" ".join(calendar.groups()), fmt).date().isoformat()
            except ValueError:
                pass
    return None


def parse_amount(text: str) -> tuple[str, str] | None:
    found = re.search(r"\b([A-Z]{3})\s+([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\b", text, re.I)
    if not found:
        reversed_match = re.search(r"\b([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s+([A-Z]{3})\b", text)
        if reversed_match:
            return reversed_match.group(2).upper(), reversed_match.group(1).replace(",", "")
        return None
    return found.group(1).upper(), found.group(2).replace(",", "")


def money(value: Any) -> str:
    try:
        return f"{Decimal(str(value)):,.2f}"
    except Exception:
        return str(value)


def format_finance(name: str, result: dict[str, Any], company: str) -> str:
    """Render provider facts and backend totals, without recomputing accounting amounts."""
    customer = result.get("customer") or {}
    account = customer.get("account") or result.get("account") or "Customer"
    customer_name = customer.get("name") or account
    label = f"**Mock data · {company.upper()}**\n\n"
    if name == "search_customers":
        customers = result.get("customers", [])
        body = (
            "No customers were found."
            if not customers
            else "\n".join(
                f"- **{item['account']}** — {item['name']} ({item.get('company', company).upper()})"
                for item in customers
            )
        )
    elif name == "get_customer":
        body = f"**{customer_name}** · {account} · {customer.get('currency', '')} · {company.upper()}"
        if customer.get("payment_terms"):
            body += f"\n\nPayment terms: {customer['payment_terms']}"
    elif name == "draft_collection_reminder":
        body = result.get("draft") or result.get("reminder") or result.get("text")
        if not body and result.get("body"):
            body = f"Subject: {result.get('subject', 'Collection reminder')}\n\n{result['body']}"
        body = body or "No overdue invoices were found for a reminder."
        if isinstance(body, dict):
            body = f"Subject: {body.get('subject', '')}\n\n{body.get('body', '')}"
        body += "\n\n**Draft only — no email was sent.**"
    elif name == "get_payment_history":
        payments = result.get("payments", [])
        body = f"Payment history for **{customer_name}** ({account}) in {company.upper()}.\n\n"
        body += (
            "\n".join(
                f"- {item.get('payment_date', 'Date unavailable')} · {item.get('currency', '')} {money(item.get('amount', '0'))} · {item.get('reference') or item.get('voucher') or 'Reference unavailable'}"
                for item in payments
            )
            if payments
            else "No payment records were found."
        )
    else:
        totals = result.get("totals_by_currency") or result.get("balance", {}).get("totals_by_currency") or {}
        transactions = result.get("transactions") or result.get("invoices") or []
        invoice = result.get("invoice")
        if invoice:
            transactions = [invoice]
        if name == "get_overdue_invoices":
            body = f"Overdue invoices for **{customer_name}** ({account}) as of **{result.get('as_of_date', date.today().isoformat())}** in {company.upper()}."
        elif totals:
            total_text = "; ".join(f"**{currency} {money(amount)}**" for currency, amount in totals.items())
            body = f"**{customer_name}** ({account}) has {total_text} outstanding in **{company.upper()}**."
        elif invoice:
            body = f"Invoice **{invoice.get('invoice_number', '')}** in **{company.upper()}**."
        else:
            body = f"Open invoices for **{customer_name}** ({account}) in **{company.upper()}**."
        if transactions:
            body += "\n\n| Invoice | Original | Remaining | Due |\n| --- | ---: | ---: | --- |\n"
            body += "\n".join(
                f"| {item.get('invoice_number', item.get('identifier', ''))} | {item.get('currency', '')} {money(item.get('original_amount', '0'))} | {item.get('currency', '')} {money(item.get('remaining_amount', '0'))} | {item.get('due_date') or 'Unavailable'} |"
                for item in transactions
            )
        elif name in {"get_overdue_invoices", "get_customer_open_transactions"}:
            body += "\n\nNo matching open invoices were found."
    evidence = result.get("evidence") or []
    retrieved_at = result.get("retrieved_at") or (evidence[0].get("retrieved_at") if evidence else None)
    if retrieved_at:
        body += f"\n\nRetrieved from mock Dynamics 365 at {retrieved_at}."
    return label + body


async def run_mock_agent(message: str, context: dict[str, Any], company: str, execute: ToolCallback) -> str:
    """A useful offline demo assistant; real mode never uses this router or seeded data."""
    lowered = message.lower().strip()
    identifiers = re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b", message, re.I)
    explicit_journal = re.search(r"\bjournal\s+([A-Z0-9-]+)", message, re.I)
    journal_identifier = explicit_journal.group(1).upper() if explicit_journal else None
    account = next(
        (
            item.upper()
            for item in identifiers
            if not item.upper().startswith(("FTI-", "ASSIST-", "PAY-", "MOCK-PAY-"))
            and item.upper() != journal_identifier
        ),
        None,
    )
    invoice_id = next(
        (item.upper() for item in identifiers if item.upper().startswith(("FTI-", "ASSIST-"))), None
    )
    as_of = parse_date(message)
    operation = (
        "create"
        if re.search(r"\b(create|add|new customer)\b", lowered)
        else "delete"
        if "delete" in lowered
        else "update"
        if any(word in lowered for word in ("change", "update", "modify"))
        else "read"
    )
    is_invoice = "draft invoice" in lowered or (
        "invoice" in lowered and operation != "read" and "payment" not in lowered
    )
    is_payment = "journal" in lowered or ("payment line" in lowered)

    async def propose(name: str, payload: dict[str, Any]) -> str:
        result = await execute(name, payload)
        action = result["pending_action"]
        return f"**Mock data · {company.upper()}**\n\n{action['title']}\n\n{action['description']}\n\nReview the confirmation card and choose **Confirm** to execute, or **Cancel**. No ERP change has been made yet."

    if lowered in {"yes", "confirm", "go ahead", "do it", "okay", "ok"}:
        return "Use **Confirm** on the action card to authorize the change. Chat messages do not execute ERP writes."
    if operation != "read":
        if is_invoice:
            if operation == "create":
                amount = parse_amount(message)
                account = account or context.get("account")
                if not account or not amount or not as_of:
                    return "To prepare the draft invoice, provide the customer account, currency, amount and due date (for example, INR 5,000 for TEST-ACME-001 due 30 October 2026)."
                return await propose(
                    "create_draft_free_text_invoice",
                    {
                        "account": account,
                        "currency": amount[0],
                        "due_date": as_of,
                        "external_id": f"ASSIST-{uuid4().hex[:12].upper()}",
                        "lines": [{"description": "Finance Assistant draft invoice", "amount": amount[1]}],
                    },
                )
            identifier = invoice_id or context.get("draft_invoice")
            if not identifier:
                return (
                    "Which unposted draft invoice should I change? Provide its external invoice identifier."
                )
            if operation == "delete":
                return await propose("delete_draft_free_text_invoice", {"identifier": identifier})
            if not as_of:
                return "What is the new due date for that unposted draft invoice? Include the day, month and year."
            return await propose(
                "update_draft_free_text_invoice", {"identifier": identifier, "due_date": as_of}
            )
        if is_payment:
            if "line" in lowered:
                amount = parse_amount(message)
                journal = re.search(r"(?:journal|in)\s+((?:MOCK-)?PAY-[A-Z0-9-]+)", message, re.I)
                journal_number = journal.group(1).upper() if journal else context.get("journal_number")
                line_number = re.search(r"line(?:\s+number)?\s+(\d+)", lowered)
                if (
                    not amount
                    or not (account or context.get("account"))
                    or not journal_number
                    or not line_number
                ):
                    return "Provide the unposted journal number, customer account, currency, amount and explicit line number to prepare a payment line."
                return await propose(
                    "add_customer_payment_line",
                    {
                        "journal_number": journal_number,
                        "account": account or context["account"],
                        "amount": amount[1],
                        "currency": amount[0],
                        "line_number": int(line_number.group(1)),
                        "payment_date": as_of or date.today().isoformat(),
                        "reference": f"ASSIST-{uuid4().hex[:12].upper()}",
                    },
                )
            amount = parse_amount(message)
            description = "Finance Assistant customer payment"
            if amount and account:
                description += f" for {account}: {amount[0]} {amount[1]} (line to be added separately)"
            return await propose("create_customer_payment_journal", {"description": description})
        if account and ("customer" in lowered or operation in {"delete", "update"}):
            if operation == "create":
                name = re.search(r"(?:named|name)\s+[\"']?([^\"']+)", message, re.I)
                return await propose(
                    "create_customer",
                    {"account": account, "name": name.group(1).strip() if name else account},
                )
            if operation == "delete":
                return await propose("delete_test_customer", {"account": account})
            terms = re.search(
                r"(?:payment\s+terms?(?:\s+(?:to|is))?|terms?\s+to)\s+[\"']?([A-Za-z0-9_-]+)", message, re.I
            )
            if not terms:
                return "What payment terms code should I set for this customer? For example: change payment terms for TEST-ACME-001 to Net30."
            # A trailing 'to CODE' is common when the account separates 'terms' from its value.
            trailing_terms = re.search(r"\bto\s+([A-Za-z0-9_-]+)\s*[.!]?$", message, re.I)
            terms_value = trailing_terms.group(1) if trailing_terms else terms.group(1)
            if terms_value.lower() in {"for", "the", "that"}:
                return "What payment terms code should I set for this customer?"
            return await propose("update_customer", {"account": account, "payment_terms": terms_value})
        return "I can prepare customer changes, unposted draft invoices, and customer payment journals. Please include the target identifier and required fields. All changes require confirmation."

    if invoice_id:
        result = await execute("get_invoice_details", {"identifier": invoice_id})
        return format_finance("get_invoice_details", result, company)
    if not any(
        word in lowered
        for word in (
            "balance",
            "outstanding",
            "invoice",
            "overdue",
            "payment",
            "reminder",
            "summary",
            "customer",
            "find",
        )
    ):
        return "**Mock assistant**\n\nI can show customer balances, open invoices, overdue amounts and payment history, draft collection reminders, and prepare confirmed ERP changes. Try asking for Asterion's outstanding balance."
    query = None
    if not account:
        patterns = (
            r"\b([A-Za-z][A-Za-z0-9 .&-]*?)[’']s\s+(?:outstanding|balance|payment|invoices|finance)",
            r"(?:for|of)\s+([A-Za-z][A-Za-z0-9 .&'-]*?)(?:\s+(?:as of|due|on|in)\b|[?.!]|$)",
            r"(?:which|show(?:\s+me)?)\s+([A-Za-z][A-Za-z0-9 .&'-]*?)(?:'s|\s+(?:invoices|payment|balance))",
            r"(?:find|search(?:\s+for)?)\s+(?:customer\s+)?(.+?)[?.!]?$",
        )
        for pattern in patterns:
            match = re.search(pattern, message, re.I)
            if match:
                candidate = match.group(1).strip().removesuffix("'s")
                candidate = re.sub(
                    r"^(?:what is|what's|show(?: me)?|tell me|give me|please(?: show me)?)\s+",
                    "",
                    candidate,
                    flags=re.I,
                )
                if candidate.lower() not in {
                    "that amount",
                    "the overdue invoice",
                    "the customer",
                    "the overdue invoices",
                    "the balance",
                    "the",
                    "my",
                    "that",
                    "those",
                    "all",
                }:
                    query = candidate
                    break
        if query:
            search = await execute("search_customers", {"query": query})
            customers = search.get("customers", [])
            if not customers:
                return f"**Mock data · {company.upper()}**\n\nNo customers were found matching '{query}'."
            if len(customers) > 1:
                return (
                    format_finance("search_customers", search, company)
                    + "\n\nWhich customer account should I use?"
                )
            account = customers[0]["account"]
        else:
            account = context.get("account")
    if not account:
        return "Which customer should I use? Provide the customer name or account."
    if "reminder" in lowered:
        name = "draft_collection_reminder"
    elif "overdue" in lowered:
        name = "get_overdue_invoices"
    elif "payment" in lowered:
        name = "get_payment_history"
    elif "summary" in lowered:
        name = "get_finance_summary"
    elif "invoice" in lowered:
        name = "get_customer_open_transactions"
    elif "balance" in lowered or "outstanding" in lowered:
        name = "get_customer_balance"
    else:
        name = "get_customer"
    payload: dict[str, Any] = {"account": account}
    if name in {"get_overdue_invoices", "get_finance_summary", "draft_collection_reminder"} and as_of:
        payload["as_of_date"] = as_of
    result = await execute(name, payload)
    return format_finance(name, result, company)
