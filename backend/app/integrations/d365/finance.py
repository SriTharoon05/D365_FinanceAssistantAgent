"""Deterministic finance calculations shared by live and mock adapters."""

from datetime import date, datetime, timezone
from decimal import Decimal, localcontext

from pydantic import ValidationError

from app.core.errors import AppError
from app.schemas.finance import MUTATION_SCHEMAS, ToolEvidence


def utcnow():
    return datetime.now(timezone.utc)


def as_date(value):
    if value is None:
        return date.today()
    return value if isinstance(value, date) else date.fromisoformat(value)


def totals(rows, amount_key="remaining_amount"):
    result = {}
    for row in rows:
        currency = row["currency"]
        previous = result.get(currency, Decimal("0"))
        amount = Decimal(str(row[amount_key]))
        with localcontext() as context:
            context.prec = max(
                context.prec,
                max(previous.adjusted(), amount.adjusted())
                - min(previous.as_tuple().exponent, amount.as_tuple().exponent)
                + 2,
            )
            result[currency] = previous + amount
    return {key: str(value) for key, value in sorted(result.items())}


def evidence(row, kind, customer=None):
    fields = {key: row.get(key) for key in ToolEvidence.model_fields if key not in {"kind", "customer_name"}}
    fields["customer_name"] = (customer or {}).get("name") or row.get("name")
    fields["kind"] = kind
    return ToolEvidence(**fields).model_dump(mode="json")


def open_evidence_kind(row):
    transaction_type = str(row.get("transaction_type") or "").casefold()
    if transaction_type in {"payment", "custpayment", "customerpayment"}:
        return "payment"
    if Decimal(str(row["remaining_amount"])) < 0:
        return "credit"
    return "invoice" if row.get("is_invoice", True) else "customer_transaction"


class D365FinanceService:
    def __init__(self, provider, settings):
        self.provider, self.settings = provider, settings

    def company(self, company):
        company = company or self.settings.d365_default_company
        if not company or len(company) > 12 or not company.replace("-", "").replace("_", "").isalnum():
            raise AppError(
                "D365_COMPANY_INVALID", "Select a valid Dynamics 365 legal entity.", status_code=422
            )
        return company.lower()

    async def search_customers(self, query, company=None):
        customers = await self.provider.search_customers(str(query), self.company(company))
        return {
            "customers": customers,
            "evidence": [evidence(row, "customer") for row in customers],
            "mock_mode": self.provider.mock,
        }

    async def get_customer(self, account, company=None):
        customer = await self.provider.get_customer(account, self.company(company))
        return {
            "customer": customer,
            "evidence": [evidence(customer, "customer")],
            "mock_mode": self.provider.mock,
        }

    async def get_customer_open_transactions(self, account, company=None):
        company = self.company(company)
        customer = await self.provider.get_customer(account, company)
        rows = await self.provider.open_transactions(account, company)
        return {
            "customer": customer,
            "company": company,
            "transactions": rows,
            "evidence": [evidence(row, open_evidence_kind(row), customer) for row in rows],
            "balance_basis": "current",
            "mock_mode": self.provider.mock,
        }

    async def get_customer_balance(self, account, company=None):
        result = await self.get_customer_open_transactions(account, company)
        result["totals_by_currency"] = totals(result["transactions"])
        result["debit_totals_by_currency"] = totals(
            [row for row in result["transactions"] if Decimal(row["remaining_amount"]) > 0]
        )
        result["credit_totals_by_currency"] = totals(
            [row for row in result["transactions"] if Decimal(row["remaining_amount"]) < 0]
        )
        result["retrieved_at"] = utcnow().isoformat()
        return result

    async def get_overdue_invoices(self, account, as_of_date=None, company=None):
        day = as_date(as_of_date)
        result = await self.get_customer_open_transactions(account, company)
        eligible = [
            row
            for row in result["transactions"]
            if row.get("is_invoice", True)
            and Decimal(row["remaining_amount"]) > 0
            and (not row.get("transaction_date") or date.fromisoformat(row["transaction_date"]) <= day)
        ]
        rows = [
            dict(row, days_overdue=(day - date.fromisoformat(row["due_date"])).days)
            for row in eligible
            if row.get("due_date") and date.fromisoformat(row["due_date"]) < day
        ]
        unknown = sum(1 for row in eligible if not row.get("due_date"))
        return {
            "customer": result["customer"],
            "company": result["company"],
            "as_of_date": day.isoformat(),
            "balance_basis": "current",
            "date_basis": "current_open_amounts_due_date_cutoff",
            "balance_note": (
                "Remaining amounts reflect the current ERP snapshot. The selected date is a due-date "
                "cutoff; this result does not reconstruct historical settlements or balances."
            ),
            "invoices": rows,
            "totals_by_currency": totals(rows),
            "unknown_due_date_count": unknown,
            "evidence": [evidence(row, "overdue_invoice", result["customer"]) for row in rows],
            "mock_mode": self.provider.mock,
        }

    async def get_invoice_details(self, identifier, company=None):
        invoice = await self.provider.invoice(identifier, self.company(company))
        customer = await self.provider.get_customer(invoice["account"], self.company(company))
        return {
            "invoice": invoice,
            "customer": customer,
            "evidence": [evidence(invoice, "invoice", customer)],
            "mock_mode": self.provider.mock,
        }

    async def get_payment_history(self, account, company=None):
        company = self.company(company)
        customer = await self.provider.get_customer(account, company)
        payments = await self.provider.payments(account, company)
        return {
            "customer": customer,
            "company": company,
            "payments": payments,
            "totals_by_currency": totals(payments, "amount"),
            "evidence": [
                evidence({**row, "original_amount": row["amount"]}, "payment", customer) for row in payments
            ],
            "mock_mode": self.provider.mock,
        }

    async def get_finance_summary(self, account, as_of_date=None, company=None):
        balance = await self.get_customer_balance(account, company)
        overdue = await self.get_overdue_invoices(account, as_of_date, company)
        try:
            payments = await self.get_payment_history(account, company)
            payment_error = None
        except AppError as exc:
            payments = {"payments": [], "evidence": []}
            payment_error = {"code": exc.code, "message": exc.message}
        return {
            **balance,
            "overdue_invoices": overdue["invoices"],
            "overdue_totals_by_currency": overdue["totals_by_currency"],
            "as_of_date": overdue["as_of_date"],
            "date_basis": overdue["date_basis"],
            "balance_note": overdue["balance_note"],
            "unknown_due_date_count": overdue["unknown_due_date_count"],
            "payments": payments["payments"],
            "payment_history_diagnostic": payment_error,
            "evidence": balance["evidence"] + payments["evidence"],
        }

    async def draft_collection_reminder(self, account, as_of_date=None, company=None):
        overdue = await self.get_overdue_invoices(account, as_of_date, company)
        customer = overdue["customer"]
        invoices = overdue["invoices"]
        subject = "Outstanding invoice reminder" + (
            " - " + ", ".join(row["invoice_number"] for row in invoices) if invoices else ""
        )
        details = "\n".join(
            f"- {row['invoice_number']}: {row['currency']} {row['remaining_amount']} remaining, due {row['due_date']} ({row['company'].upper()})."
            for row in invoices
        )
        body = (
            f"Dear {customer['name']},\n\nOur current records show the following open invoices with due dates before {overdue['as_of_date']}:\n{details}\n\nPlease arrange payment or contact the finance team if you require clarification.\n\nRegards,\nFinance Team"
            if invoices
            else f"No currently open invoices with verified due dates before {overdue['as_of_date']} were found for {customer['name']}. No payment reminder is needed."
        )
        if not invoices and overdue["unknown_due_date_count"]:
            body = f"No currently open invoices with verified due dates before {overdue['as_of_date']} were found for {customer['name']}. {overdue['unknown_due_date_count']} open invoice(s) have no verified due date, so overdue status cannot be fully determined. Verify those dates in Dynamics 365 before drafting a reminder."
        return {
            "draft_only": True,
            "unknown_due_date_count": overdue["unknown_due_date_count"],
            "subject": subject,
            "body": body,
            "customer": customer,
            "evidence": overdue["evidence"],
            "mock_mode": self.provider.mock,
            "balance_basis": overdue["balance_basis"],
            "date_basis": overdue["date_basis"],
            "balance_note": overdue["balance_note"],
        }

    def parse_mutation(self, action_type, payload):
        schema = MUTATION_SCHEMAS.get(action_type)
        if not schema:
            raise AppError(
                "UNSAFE_MUTATION",
                "This operation is not a supported draft or master-data action. Posting and settlement require manual Dynamics 365 processing.",
                status_code=422,
            )
        try:
            return schema.model_validate(payload).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            # Error inputs may contain arbitrary user data: expose locations and text only.
            details = [
                {"field": ".".join(str(piece) for piece in item["loc"]), "message": item["msg"]}
                for item in exc.errors()
            ]
            raise AppError(
                "D365_VALIDATION_ERROR",
                "The proposed action is missing required fields or contains invalid values.",
                status_code=422,
                details=details,
            ) from None

    async def validate_mutation(self, action_type, payload, company=None):
        company = self.company(company)
        if not getattr(self.settings, "d365_write_actions_enabled", True):
            raise AppError(
                "UNSAFE_MUTATION",
                "Dynamics 365 write actions are disabled by backend configuration.",
                status_code=403,
            )
        safe = self.parse_mutation(action_type, payload)
        state = await self.provider.validate_mutation(action_type, safe, company)
        entities = {
            "customer": self.settings.d365_customers_entity,
            "draft": self.settings.d365_free_text_headers_entity,
            "journal": self.settings.d365_payment_headers_entity,
            "line": self.settings.d365_payment_lines_entity,
        }
        kind = (
            "customer"
            if "customer" in action_type and "payment" not in action_type
            else "draft"
            if "invoice" in action_type
            else "line"
            if "line" in action_type
            else "journal"
        )
        identifier = (
            safe.get("account")
            if kind == "customer"
            else safe.get("identifier") or safe.get("external_id") or safe.get("journal_number")
        )
        impact = None
        if "lines" in safe:
            impact = {
                "currency": safe["currency"],
                "amount": str(sum((Decimal(line["amount"]) for line in safe["lines"]), Decimal(0))),
                "posted": False,
            }
        elif "amount" in safe:
            impact = {"currency": safe["currency"], "amount": safe["amount"], "posted": False}
        return {
            "action_type": action_type,
            "title": action_type.replace("_", " ").capitalize(),
            "description": "Confirm this Dynamics 365 operation. Financial records remain unposted drafts.",
            "company": company,
            "entity": entities[kind],
            "target_identifier": identifier,
            "proposed_changes": safe,
            "financial_impact": impact,
            "risk_level": "high" if action_type.startswith("delete") else "medium",
            "current_state": state,
            "mock_mode": self.provider.mock,
        }

    async def execute_mutation(self, action_type, payload, company=None):
        # Revalidate immediately before execution: a confirmation cannot override changed ERP state.
        try:
            preview = await self.validate_mutation(action_type, payload, company)
        except AppError as exc:
            exc.details = {
                **(exc.details if isinstance(exc.details, dict) else {}),
                "write_outcome": "not_written",
            }
            raise
        result = await self.provider.execute_mutation(
            action_type, preview["proposed_changes"], preview["company"]
        )
        return {
            "action_type": action_type,
            "company": preview["company"],
            "entity": preview["entity"],
            "result": result,
            "mock_mode": self.provider.mock,
            "evidence": result.get("evidence", []),
        }

    async def reconcile_mutation(self, action_type, payload, company=None):
        reconcile = getattr(self.provider, "reconcile_mutation", None)
        if not callable(reconcile):
            raise AppError(
                "D365_RECONCILIATION_UNSUPPORTED",
                "Check the affected record directly in Dynamics 365; automatic verification is unavailable.",
                status_code=422,
            )
        selected_company = self.company(company)
        result = await reconcile(action_type, self.parse_mutation(action_type, payload), selected_company)
        return {
            "action_type": action_type,
            "company": selected_company,
            "result": result,
            "reconciled": True,
            "verification_basis": "current_record_state",
            "mock_mode": self.provider.mock,
        }

    async def get_write_setup(self, purpose="all", company=None):
        lookup = getattr(self.provider, "get_write_setup", None)
        if not callable(lookup):
            raise AppError(
                "D365_CAPABILITY_UNAVAILABLE",
                "Setup discovery is unavailable. Check the configured revenue account or journal name in Dynamics 365.",
                status_code=422,
            )
        return await lookup(self.company(company), purpose)
