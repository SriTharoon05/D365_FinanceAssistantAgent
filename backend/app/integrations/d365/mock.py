"""Explicit offline fixture adapter. This module is never used as a live fallback."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

from app.core.errors import AppError

from .errors import UnsafeMutationError
from .finance import utcnow


class MockD365Provider:
    mock = True

    def __init__(self, settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self.customers = {
            "AST-001": {
                "account": "AST-001",
                "name": "Asterion Digital Solutions Pvt Ltd",
                "company": "usmf",
                "currency": "INR",
                "customer_group": "10",
                "payment_terms": "Net30",
            },
            "BLR-001": {
                "account": "BLR-001",
                "name": "BlueRiver Technology Services Pvt Ltd",
                "company": "usmf",
                "currency": "INR",
                "customer_group": "10",
                "payment_terms": "Net30",
            },
            "NVS-001": {
                "account": "NVS-001",
                "name": "NovaSphere Consulting Pvt Ltd",
                "company": "usmf",
                "currency": "USD",
                "customer_group": "10",
                "payment_terms": "Net30",
            },
        }
        self.invoices = {
            "FTI-00000022": {
                "account": "AST-001",
                "company": "usmf",
                "invoice_number": "FTI-00000022",
                "currency": "INR",
                "original_amount": "60000",
                "remaining_amount": "35000",
                "due_date": "2026-09-30",
                "transaction_date": "2026-08-31",
                "voucher": "MOCK-FTI-22",
                "is_posted": True,
            },
            "FTI-00000021": {
                "account": "AST-001",
                "company": "usmf",
                "invoice_number": "FTI-00000021",
                "currency": "INR",
                "original_amount": "75000",
                "remaining_amount": "75000",
                "due_date": "2026-10-15",
                "transaction_date": "2026-09-15",
                "voucher": "MOCK-FTI-21",
                "is_posted": True,
            },
        }
        self.payment_records = [
            {
                "account": "AST-001",
                "company": "usmf",
                "currency": "INR",
                "amount": "25000",
                "voucher": "MOCK-PAY-1",
                "payment_date": "2026-09-20",
                "reference": "FTI-00000022",
            }
        ]
        self.journals = {}
        self.payment_lines = {}

    def stamp(self, row, entity):
        return {**deepcopy(row), "source_entity": "MOCK:" + entity, "retrieved_at": utcnow().isoformat()}

    async def search_customers(self, query, company):
        text = query.casefold()
        return [
            self.stamp(row, self.settings.d365_customers_entity)
            for row in self.customers.values()
            if row["company"] == company
            and (text in row["account"].casefold() or text in row["name"].casefold())
        ]

    async def get_customer(self, account, company):
        row = self.customers.get(account)
        if not row or row["company"] != company:
            raise AppError(
                "D365_CUSTOMER_NOT_FOUND",
                f"No customer {account} was found in {company.upper()}.",
                status_code=404,
            )
        return self.stamp(row, self.settings.d365_customers_entity)

    async def open_transactions(self, account, company):
        return [
            self.stamp(row, "MockCustomerOpenTransactions")
            for row in self.invoices.values()
            if row["account"] == account
            and row["company"] == company
            and row["is_posted"]
            and Decimal(row["remaining_amount"]) != 0
        ]

    async def invoice(self, identifier, company):
        row = self.invoices.get(identifier)
        if not row or row["company"] != company:
            raise AppError(
                "D365_INVOICE_NOT_FOUND",
                "No invoice was found with that identifier in the selected company.",
                status_code=404,
            )
        return self.stamp(row, self.settings.d365_free_text_headers_entity)

    async def payments(self, account, company):
        return [
            self.stamp(row, "MockCustomerTransactions")
            for row in self.payment_records
            if row["account"] == account and row["company"] == company
        ]

    def allowed_prefixes(self):
        return tuple(
            part.strip() for part in self.settings.d365_delete_allowed_prefixes.split(",") if part.strip()
        )

    async def validate_mutation(self, action, data, company):
        if action == "create_customer":
            if data["account"] in self.customers:
                raise AppError(
                    "D365_DUPLICATE_RECORD", "That customer account already exists.", status_code=409
                )
            if data["customer_group"] not in {"10", "20", "30"}:
                raise AppError(
                    "D365_VALIDATION_ERROR", "Mock customer group must be 10, 20, or 30.", status_code=422
                )
            return None
        if action in {"update_customer", "delete_test_customer"}:
            customer = await self.get_customer(data["account"], company)
            if action == "update_customer" and not any(
                field in data for field in ("name", "payment_terms", "customer_group")
            ):
                raise AppError(
                    "D365_VALIDATION_ERROR",
                    "Specify at least one safe customer field to change.",
                    status_code=422,
                )
            if action == "delete_test_customer":
                if not self.allowed_prefixes() or not data["account"].startswith(self.allowed_prefixes()):
                    raise UnsafeMutationError(
                        "Only customer accounts with an allowed test prefix can be deleted."
                    )
                if (
                    any(
                        row["account"] == data["account"] and row["company"] == company
                        for row in self.invoices.values()
                    )
                    or any(
                        row["account"] == data["account"] and row["company"] == company
                        for row in self.payment_records
                    )
                    or any(
                        row["account"] == data["account"] and row["company"] == company
                        for row in self.payment_lines.values()
                    )
                ):
                    raise UnsafeMutationError(
                        "This test customer has financial history or draft financial records and cannot be deleted."
                    )
            return customer
        if action == "create_draft_free_text_invoice":
            await self.get_customer(data["account"], company)
            if data["external_id"] in self.invoices:
                raise AppError(
                    "D365_DUPLICATE_RECORD",
                    "That external invoice identifier already exists.",
                    status_code=409,
                )
            return None
        if action in {"update_draft_free_text_invoice", "delete_draft_free_text_invoice"}:
            row = await self.invoice(data["identifier"], company)
            if row["is_posted"]:
                raise UnsafeMutationError(
                    "Posted invoices cannot be changed or deleted. Use approved Dynamics 365 accounting corrections."
                )
            return row
        if action == "create_customer_payment_journal":
            name = data.get("journal_name", self.settings.d365_payment_journal_name)
            if name != self.settings.d365_payment_journal_name:
                raise AppError(
                    "D365_VALIDATION_ERROR",
                    "Select the configured customer payment journal name.",
                    status_code=422,
                )
            return None
        if action == "add_customer_payment_line":
            await self.get_customer(data["account"], company)
            journal = self.journals.get(data["journal_number"])
            if not journal or journal["company"] != company:
                raise AppError(
                    "D365_JOURNAL_NOT_FOUND",
                    "The unposted customer payment journal was not found.",
                    status_code=404,
                )
            if journal["is_posted"]:
                raise UnsafeMutationError("Posted payment journals cannot be modified.")
            if (data["journal_number"], data["line_number"]) in self.payment_lines:
                raise AppError(
                    "D365_DUPLICATE_RECORD",
                    "That payment journal line number already exists.",
                    status_code=409,
                )
            if any(
                line["reference"] == data["reference"] and line["company"] == company
                for line in self.payment_lines.values()
            ):
                raise AppError(
                    "D365_DUPLICATE_RECORD",
                    "A payment draft with that reference already exists. Verify it before creating another.",
                    status_code=409,
                )
            return deepcopy(journal)
        raise UnsafeMutationError("Unsupported financial mutation.")

    async def execute_mutation(self, action, data, company):
        async with self._lock:
            await self.validate_mutation(action, data, company)
            if action == "create_customer":
                row = {**data, "company": company}
                self.customers[data["account"]] = row
                return {
                    "customer": self.stamp(row, self.settings.d365_customers_entity),
                    "identifier": data["account"],
                    "verified": True,
                }
            if action == "update_customer":
                self.customers[data["account"]].update(data)
                return {
                    "customer": await self.get_customer(data["account"], company),
                    "identifier": data["account"],
                    "verified": True,
                }
            if action == "delete_test_customer":
                del self.customers[data["account"]]
                return {"identifier": data["account"], "deleted": True, "verified": True}
            if action == "create_draft_free_text_invoice":
                identifier = data["external_id"]
                row = {
                    "account": data["account"],
                    "company": company,
                    "invoice_number": identifier,
                    "external_id": identifier,
                    "currency": data["currency"],
                    "original_amount": str(
                        sum((Decimal(line["amount"]) for line in data["lines"]), Decimal(0))
                    ),
                    "remaining_amount": "0",
                    "due_date": data["due_date"],
                    "transaction_date": data["invoice_date"],
                    "voucher": None,
                    "is_posted": False,
                    "lines": data["lines"],
                }
                self.invoices[identifier] = row
                return {
                    "invoice": await self.invoice(identifier, company),
                    "identifier": identifier,
                    "verified": True,
                    "posted": False,
                }
            if action == "update_draft_free_text_invoice":
                self.invoices[data["identifier"]]["due_date"] = data["due_date"]
                return {
                    "invoice": await self.invoice(data["identifier"], company),
                    "identifier": data["identifier"],
                    "verified": True,
                }
            if action == "delete_draft_free_text_invoice":
                del self.invoices[data["identifier"]]
                return {"identifier": data["identifier"], "deleted": True, "verified": True}
            if action == "create_customer_payment_journal":
                identifier = "MOCK-PAY-" + uuid4().hex[:8].upper()
                row = {
                    "journal_number": identifier,
                    "company": company,
                    "journal_name": data.get("journal_name", self.settings.d365_payment_journal_name),
                    "description": data["description"],
                    "is_posted": False,
                }
                self.journals[identifier] = row
                return {
                    "journal": deepcopy(row),
                    "identifier": identifier,
                    "verified": True,
                    "posted": False,
                    "manual_instructions": "Review, settle, and post this unposted journal in Dynamics 365 Finance. Automated settlement and posting are unavailable.",
                }
            if action == "add_customer_payment_line":
                row = {**data, "company": company, "is_posted": False}
                self.payment_lines[(data["journal_number"], data["line_number"])] = row
                return {
                    "payment_line": deepcopy(row),
                    "identifier": f"{data['journal_number']}/{data['line_number']}",
                    "verified": True,
                    "posted": False,
                    "manual_instructions": "Review and settle the payment against invoices, then post the customer payment journal in Dynamics 365 Finance.",
                }
        raise UnsafeMutationError("Unsupported financial mutation.")
