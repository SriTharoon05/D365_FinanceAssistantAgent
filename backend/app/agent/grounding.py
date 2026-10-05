"""Deterministic finance grounding independent of conversational model compliance."""

import re
from typing import Any

_FINANCE = re.compile(
    r"\b(balance|outstanding|invoice|invoices|overdue|payment|payments|finance|financial|customer|customers|"
    r"receivable|receivables|voucher|journal|journals|reminder|cash|credit|debit|collection|collections|"
    r"amount|currency|currencies|account|accounts|settle|settlement|posted|posting|due|owed)\b",
    re.I,
)
_NON_ERP = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you|good morning|good afternoon|good evening|how are you|"
    r"what can you do|who are you|help|how do i use (?:this|the app)|explain (?:your )?capabilities)[.!?\s]*$",
    re.I,
)


def requires_finance_grounding(message: str, identifiers: dict[str, Any] | None = None) -> bool:
    if _FINANCE.search(message):
        return True
    if _NON_ERP.fullmatch(message.strip()):
        return False
    # Short contextual follow-ups such as 'what about that?' refer to the selected customer.
    return bool((identifiers or {}).get("account") or (identifiers or {}).get("draft_invoice"))
