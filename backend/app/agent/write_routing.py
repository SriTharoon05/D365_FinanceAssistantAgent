"""Resolve explicit draft-write inputs without generating or executing ERP changes."""

import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.mock import parse_date

_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,49}"
_DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4})"
_DUE_UPDATE = re.compile(
    rf"^\s*(?:change|update|set)\s+(?P<target>.+?)\s+to\s+(?P<date>{_DATE})\s*[.!]?\s*$",
    re.I,
)
_PAYMENT_LINE = re.compile(
    rf"^\s*add\s+(?:an?\s+)?payment\s+line\s+(?P<line>\d+)"
    rf"(?:\s+to\s+(?:the\s+)?journal(?:\s+(?P<journal>{_IDENTIFIER}))?)?"
    rf"\s+for\s+(?P<account>{_IDENTIFIER})\s*,?\s*"
    rf"amount\s+(?P<currency>[A-Z]{{3}})\s+(?P<amount>\d+(?:,\d{{3}})*(?:\.\d{{1,2}})?)\s*,?\s*"
    rf"payment\s+date\s+(?P<date>{_DATE})\s*,?\s*reference\s+"
    r"(?P<reference>[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9_-])?)\s*\.?\s*$",
    re.I,
)


@dataclass
class WriteRequest:
    action_type: str
    arguments: dict[str, Any] = field(default_factory=dict)
    clarification: str | None = None


def resolve_write_request(message: str, identifiers: dict[str, Any]) -> WriteRequest | None:
    """Recognize only complete, narrow forms; all other requests stay with typed model tools."""
    update = _DUE_UPDATE.fullmatch(message)
    if update:
        target = update["target"]
        explicit = re.fullmatch(
            rf"(?:the\s+)?(?:(?:draft\s+)?invoice\s+)?(?P<identifier>{_IDENTIFIER})"
            r"(?:['’]s)?\s+due\s+date",
            target,
            re.I,
        ) or re.fullmatch(
            rf"(?:the\s+)?due\s+date\s+(?:of|for)\s+(?:draft\s+)?invoice\s+(?P<identifier>{_IDENTIFIER})",
            target,
            re.I,
        )
        contextual = re.fullmatch(
            r"(?:the\s+)?due\s+date(?:\s+(?:of|for)\s+(?:that|the)\s+(?:draft\s+)?invoice)?",
            target,
            re.I,
        )
        identifier = None
        if explicit and re.search(r"[0-9_.-]", explicit["identifier"]):
            identifier = explicit["identifier"]
        elif contextual:
            identifier = identifiers.get("invoice") or identifiers.get("draft_invoice")
        else:
            return None
        due_date = parse_date(update["date"])
        if due_date is None:
            return None
        if not identifier:
            return WriteRequest(
                "update_draft_free_text_invoice",
                clarification="Which draft invoice ID should I update? No action was prepared.",
            )
        return WriteRequest(
            "update_draft_free_text_invoice", {"identifier": identifier, "due_date": due_date}
        )

    line = _PAYMENT_LINE.fullmatch(message)
    if line:
        payment_date = parse_date(line["date"])
        if payment_date is None:
            return None
        journal = line["journal"] or identifiers.get("journal_number")
        if not journal:
            return WriteRequest(
                "add_customer_payment_line",
                clarification="Which unposted journal number should I use for this payment line? No action was prepared.",
            )
        return WriteRequest(
            "add_customer_payment_line",
            {
                "journal_number": journal,
                "account": line["account"],
                "amount": line["amount"].replace(",", ""),
                "currency": line["currency"].upper(),
                "line_number": int(line["line"]),
                "payment_date": payment_date,
                "reference": line["reference"],
            },
        )
    return None
