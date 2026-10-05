"""Standard-public-entity adapter. Unknown fields and business state fail closed."""

from contextvars import ContextVar
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.errors import AppError

from .client import entity_key, escape_account_display_value, odata_literal
from .errors import D365EntityUnavailableError, UnsafeMutationError
from .finance import utcnow
from .metadata import norm


WRITE_ATTEMPTED = ContextVar("d365_write_attempted", default=False)


def decimal_string(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation()
        return str(amount)
    except (InvalidOperation, TypeError, ValueError):
        raise AppError(
            "D365_FINANCE_DATA_INVALID",
            "Dynamics 365 returned an invalid monetary value. No balance was calculated.",
            status_code=502,
        ) from None


def date_string(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise AppError(
            "D365_FINANCE_DATA_INVALID", "Dynamics 365 returned an invalid finance date.", status_code=502
        ) from None


def enum_member(value):
    text = str(value).strip()
    if "'" in text:
        parts = text.split("'")
        return parts[-2] if len(parts) >= 3 else text
    return text.rsplit(".", 1)[-1]


def posted_value(value):
    if value in (True, 1, "Yes", "true", "True", "1") or str(value).endswith("'Yes'"):
        return True
    if value in (False, 0, "No", "false", "False", "0") or str(value).endswith("'No'"):
        return False
    raise UnsafeMutationError("The posted state could not be verified. No financial mutation is allowed.")


class LiveD365Provider:
    mock = False

    def __init__(self, settings, client, registry, connected=None):
        self.settings, self.client, self.registry = settings, client, registry
        self.connected = connected

    def info(self, name):
        if not self.registry.loaded or (self.connected and not self.connected()):
            raise D365EntityUnavailableError(
                "Dynamics 365 is disconnected. Reconnect before retrieving current finance data."
            )
        info = self.registry.entities.get(name)
        if not info:
            raise D365EntityUnavailableError(
                f"The public entity {name} is unavailable. Check metadata diagnostics and entity configuration."
            )
        return info

    def field(self, info, role):
        value = info.resolve(role)
        if not value:
            raise D365EntityUnavailableError(
                f"{info.name} has no verified {role} field. Inspect entity diagnostics before using this capability."
            )
        return value

    def actual(self, info, *names):
        result = next((info.actual(name) for name in names if info.actual(name)), None)
        if not result:
            raise D365EntityUnavailableError(
                f"{info.name} does not expose required field {' / '.join(names)}. Verify the standard entity mapping."
            )
        return result

    def value(self, info, row, role, required=True):
        field = info.resolve(role)
        value = row.get(field) if field else None
        if required and (value is None or value == ""):
            raise AppError(
                "D365_FINANCE_DATA_INVALID",
                f"{info.name} returned a record without a usable {role} value. No accounting result was fabricated.",
                status_code=502,
            )
        return value

    def company_filter(self, info, company):
        return f"{self.field(info, 'company')} eq {odata_literal(company)}"

    async def rows(self, info, company, clause=None, top=None):
        where = self.company_filter(info, company)
        if clause:
            where += " and (" + clause + ")"
        return await self.client.get(info.name, filter=where, top=top, cross_company=True)

    def normalize_customer(self, info, row):
        group = next((row[key] for key in row if norm(key) in {"customergroupid", "customergroup"}), None)
        terms = next((row[key] for key in row if norm(key) in {"paymentterms", "paymenttermsname"}), None)
        return {
            "account": str(self.value(info, row, "account")),
            "name": str(self.value(info, row, "name")),
            "company": str(self.value(info, row, "company")).lower(),
            "currency": str(self.value(info, row, "currency")),
            "customer_group": group,
            "payment_terms": terms,
            "source_entity": info.name,
            "retrieved_at": utcnow().isoformat(),
        }

    async def search_customers(self, query, company):
        info = self.info(self.settings.d365_customers_entity)
        account, name = self.field(info, "account"), self.field(info, "name")
        clause = f"contains({account},{odata_literal(query)}) or contains({name},{odata_literal(query)})"
        return [self.normalize_customer(info, row) for row in await self.rows(info, company, clause, top=50)]

    async def customer_record(self, account, company):
        info = self.info(self.settings.d365_customers_entity)
        rows = await self.rows(
            info, company, f"{self.field(info, 'account')} eq {odata_literal(account)}", top=2
        )
        if not rows:
            raise AppError(
                "D365_CUSTOMER_NOT_FOUND",
                f"No customer {account} was found in {company.upper()}.",
                status_code=404,
            )
        if len(rows) != 1:
            raise AppError(
                "D365_AMBIGUOUS_RECORD",
                "Dynamics 365 returned multiple customer records for this account. No mutation is permitted.",
                status_code=409,
            )
        return info, rows[0]

    async def get_customer(self, account, company):
        info, row = await self.customer_record(account, company)
        return self.normalize_customer(info, row)

    def transaction_info(self, role):
        entity = self.registry.resolved.get(role)
        if not entity:
            env = "D365_" + role.upper() + "_ENTITY"
            raise D365EntityUnavailableError(
                f"The {role.replace('_', ' ')} capability is unavailable. Inspect entity diagnostics and configure {env}."
            )
        return self.info(entity)

    def normalize_transaction(self, info, row, is_open=True):
        amount_field, remaining_field = info.resolve("amount"), info.resolve("remaining")
        original = row.get(amount_field) if amount_field else None
        if is_open and amount_field == remaining_field:
            original = None  # Open AmountCur is remaining, not proof of the original invoice amount.
        remaining = self.value(info, row, "remaining") if is_open else None
        return {
            "account": str(self.value(info, row, "account")),
            "company": str(self.value(info, row, "company")).lower(),
            "invoice_number": str(
                self.value(info, row, "invoice", required=False)
                or self.value(info, row, "voucher", required=False)
                or ""
            ),
            "currency": str(self.value(info, row, "currency")),
            "original_amount": decimal_string(original) if original is not None else None,
            "remaining_amount": decimal_string(remaining) if remaining is not None else None,
            "due_date": date_string(self.value(info, row, "due", required=False)),
            "transaction_date": date_string(self.value(info, row, "date", required=False)),
            "voucher": self.value(info, row, "voucher", required=False),
            "source_entity": info.name,
            "retrieved_at": utcnow().isoformat(),
        }

    async def open_transactions(self, account, company):
        info = self.transaction_info("open_transactions")
        records = await self.rows(info, company, f"{self.field(info, 'account')} eq {odata_literal(account)}")
        rows = [self.normalize_transaction(info, row) for row in records]
        if any(row["original_amount"] is None for row in rows) and self.registry.resolved.get(
            "customer_transactions"
        ):
            history = self.transaction_info("customer_transactions")
            if history.resolve("invoice") and history.resolve("amount"):
                history_records = await self.rows(
                    history, company, f"{self.field(history, 'account')} eq {odata_literal(account)}"
                )
                matches = {}
                for historical in history_records:
                    key = (
                        str(self.value(history, historical, "invoice", False) or ""),
                        str(self.value(history, historical, "currency")),
                    )
                    matches.setdefault(key, []).append(historical)
                for row in rows:
                    match = matches.get((row["invoice_number"], row["currency"]), [])
                    if row["original_amount"] is None and len(match) == 1:
                        row["original_amount"] = decimal_string(self.value(history, match[0], "amount"))
        return [row for row in rows if Decimal(row["remaining_amount"]) != 0]

    async def header_record(self, identifier, company):
        info = self.info(self.settings.d365_free_text_headers_entity)
        identifier_fields = [
            info.actual(name)
            for name in ("InvoiceIdentifier", "ExternalInvoiceId", "InvoiceId", "InvoiceNumber")
            if info.actual(name)
        ]
        if not identifier_fields:
            raise D365EntityUnavailableError(
                "The free-text invoice identifier mapping is unavailable. Inspect entity diagnostics."
            )
        records = await self.rows(
            info,
            company,
            " or ".join(f"{field} eq {odata_literal(identifier)}" for field in identifier_fields),
            top=2,
        )
        if not records:
            raise AppError(
                "D365_INVOICE_NOT_FOUND",
                "No free-text invoice was found in the selected company.",
                status_code=404,
            )
        if len(records) != 1:
            raise AppError(
                "D365_AMBIGUOUS_RECORD",
                "The invoice identifier matches multiple records. Specify a unique identifier.",
                status_code=409,
            )
        return info, records[0]

    async def invoice(self, identifier, company):
        for role in ("open_transactions", "customer_transactions"):
            if self.registry.resolved.get(role):
                info = self.transaction_info(role)
                if info.resolve("invoice"):
                    rows = await self.rows(
                        info, company, f"{self.field(info, 'invoice')} eq {odata_literal(identifier)}", top=2
                    )
                    if len(rows) == 1:
                        return {
                            **self.normalize_transaction(info, rows[0], is_open=role == "open_transactions"),
                            "is_posted": True,
                        }
                    if len(rows) > 1:
                        raise AppError(
                            "D365_AMBIGUOUS_RECORD",
                            "Several transactions use this invoice identifier. Specify a unique record.",
                            status_code=409,
                        )
        info, row = await self.header_record(identifier, company)
        return {
            "account": str(self.value(info, row, "account")),
            "company": company,
            "invoice_number": identifier,
            "currency": str(self.value(info, row, "currency")),
            "original_amount": None,
            "remaining_amount": None,
            "due_date": date_string(self.value(info, row, "due", False)),
            "transaction_date": date_string(self.value(info, row, "date", False)),
            "voucher": self.value(info, row, "voucher", False),
            "is_posted": posted_value(row.get(self.actual(info, "IsPosted"))),
            "source_entity": info.name,
            "retrieved_at": utcnow().isoformat(),
        }

    async def payments(self, account, company):
        info = self.transaction_info("customer_transactions")
        type_field = info.resolve("type")
        if not type_field:
            raise D365EntityUnavailableError(
                "Payment history cannot be distinguished safely because the transaction type field is unavailable."
            )
        records = await self.rows(info, company, f"{self.field(info, 'account')} eq {odata_literal(account)}")
        result = []
        for row in records:
            text = enum_member(row.get(type_field, "")).casefold()
            if text in {"payment", "custpayment", "customerpayment"}:
                result.append(
                    {
                        "account": account,
                        "company": company,
                        "currency": str(self.value(info, row, "currency")),
                        "amount": str(abs(Decimal(decimal_string(self.value(info, row, "amount"))))),
                        "payment_date": date_string(self.value(info, row, "date", False)),
                        "voucher": self.value(info, row, "voucher", False),
                        "reference": self.value(info, row, "invoice", False),
                        "source_entity": info.name,
                        "retrieved_at": utcnow().isoformat(),
                    }
                )
        return result

    def key(self, info, row):
        if not info.keys or any(row.get(key) is None for key in info.keys):
            raise D365EntityUnavailableError(
                f"A complete verified key for {info.name} is unavailable. No mutation was executed."
            )
        return entity_key(info.name, {key: row[key] for key in info.keys})

    async def verify_master(self, entity, value, names, company=None, predicate=None):
        info = self.info(entity)
        field = self.actual(info, *names)
        clause = f"{field} eq {odata_literal(value)}"
        records = (
            await self.rows(info, company, clause, top=2)
            if company and info.resolve("company")
            else await self.client.get(entity, filter=clause, top=2)
        )
        if len(records) != 1:
            raise AppError(
                "D365_SETUP_INVALID",
                f"The configured value '{value}' could not be verified in {entity}. Check legal entity setup.",
                status_code=422,
            )
        if predicate:
            predicate(info, records[0])
        return records[0]

    def find_setup(self, role, names, required_fields):
        candidates = [
            info
            for info in self.registry.entities.values()
            if any(norm(name) in norm(info.name) for name in names)
            and all(any(info.actual(alias) for alias in aliases) for aliases in required_fields)
        ]
        if not candidates:
            raise D365EntityUnavailableError(
                f"Live {role} setup validation requires a supported public setup entity. Check D365 public entity permissions and metadata diagnostics. The operation remains disabled until setup can be verified."
            )
        return sorted(candidates, key=lambda info: (len(info.name), info.name))[0]

    async def validate_terms(self, terms, company):
        info = self.find_setup(
            "payment terms",
            ("PaymentTerms", "TermsOfPayment"),
            (("PaymentTermsName", "PaymentTerms", "TermsOfPayment"),),
        )
        await self.verify_master(
            info.name, terms, ("PaymentTermsName", "PaymentTerms", "TermsOfPayment"), company
        )

    async def journal_record(self, number, company):
        info = self.info(self.settings.d365_payment_headers_entity)
        field = self.actual(info, "JournalBatchNumber", "JournalNumber")
        records = await self.rows(info, company, f"{field} eq {odata_literal(number)}", top=2)
        if len(records) != 1:
            raise AppError(
                "D365_JOURNAL_NOT_FOUND", "A unique customer payment journal was not found.", status_code=404
            )
        if posted_value(records[0].get(self.actual(info, "IsPosted"))):
            raise UnsafeMutationError("Posted customer payment journals cannot be changed.")
        return info, records[0]

    async def payment_setup(self, data, company, include_line=False):
        journal_name = data.get("journal_name", self.settings.d365_payment_journal_name)
        names = self.find_setup(
            "customer payment journal", ("JournalNames",), (("JournalName",), ("JournalType",))
        )

        def check_journal(info, record):
            value = enum_member(record.get(info.actual("JournalType"), ""))
            if value != "CustPayment":
                raise UnsafeMutationError(
                    "The selected journal name is not a verified CustPayment journal type."
                )

        await self.verify_master(names.name, journal_name, ("JournalName",), company, check_journal)
        if not include_line:
            return
        bank = self.find_setup(
            "bank account", ("BankAccounts",), (("BankAccountId", "BankAccount", "AccountID"),)
        )
        await self.verify_master(
            bank.name,
            data.get("bank_account", self.settings.d365_payment_bank_account),
            ("BankAccountId", "BankAccount", "AccountID"),
            company,
        )
        modes = self.find_setup(
            "customer payment method",
            ("CustomerPaymentModes", "CustomerPaymentMethods"),
            (("Name", "PaymentMode", "PaymentMethodName", "PaymentModeName"),),
        )
        await self.verify_master(
            modes.name,
            data.get("payment_method", self.settings.d365_payment_method),
            ("PaymentMode", "PaymentMethodName", "PaymentModeName", "Name"),
            company,
        )
        profiles = self.find_setup(
            "customer posting profile",
            ("CustomerPostingProfiles",),
            (("PostingProfile", "PostingProfileName", "Name"),),
        )
        await self.verify_master(
            profiles.name,
            data.get("posting_profile", self.settings.d365_customer_posting_profile),
            ("PostingProfile", "PostingProfileName", "Name"),
            company,
        )

    async def validate_mutation(self, action, data, company):
        if action == "create_customer":
            try:
                await self.customer_record(data["account"], company)
            except AppError as exc:
                if exc.code != "D365_CUSTOMER_NOT_FOUND":
                    raise
            else:
                raise AppError(
                    "D365_DUPLICATE_RECORD", "That customer account already exists.", status_code=409
                )
            await self.verify_master(
                self.settings.d365_customer_groups_entity,
                data["customer_group"],
                ("CustomerGroupId", "CustomerGroup"),
                company,
            )
            await self.verify_master(
                self.settings.d365_currencies_entity, data["currency"], ("CurrencyCode", "Currency")
            )
            if data.get("payment_terms"):
                await self.validate_terms(data["payment_terms"], company)
            self.customer_payload(data, company)
            return None
        if action in {"update_customer", "delete_test_customer"}:
            info, record = await self.customer_record(data["account"], company)
            self.key(info, record)
            if action == "delete_test_customer":
                prefixes = tuple(
                    part.strip()
                    for part in self.settings.d365_delete_allowed_prefixes.split(",")
                    if part.strip()
                )
                if not prefixes or not data["account"].startswith(prefixes):
                    raise UnsafeMutationError(
                        "Only customers with an allowed test account prefix may be deleted."
                    )
                # All historical transactions, including fully settled records, are checked.
                transactions = self.transaction_info("customer_transactions")
                self.field(
                    transactions, "type"
                )  # Without transaction types, full financial history cannot be established safely.
                if await self.rows(
                    transactions,
                    company,
                    f"{self.field(transactions, 'account')} eq {odata_literal(data['account'])}",
                    top=1,
                ):
                    raise UnsafeMutationError(
                        "This test customer has financial history and cannot be deleted."
                    )
                for entity in (
                    self.settings.d365_free_text_headers_entity,
                    self.settings.d365_payment_lines_entity,
                ):
                    draft = self.info(entity)
                    account_field = draft.resolve("account") or draft.actual("AccountDisplayValue")
                    if not account_field:
                        raise D365EntityUnavailableError(
                            f"Cannot verify absence of draft customer records in {entity}; deletion is disabled."
                        )
                    display_account = (
                        escape_account_display_value(data["account"])
                        if norm(account_field) == "accountdisplayvalue"
                        else data["account"]
                    )
                    if await self.rows(
                        draft, company, f"{account_field} eq {odata_literal(display_account)}", top=1
                    ):
                        raise UnsafeMutationError(
                            "This test customer has draft financial records and cannot be deleted."
                        )
            else:
                if not any(key in data for key in ("name", "payment_terms", "customer_group")):
                    raise AppError(
                        "D365_VALIDATION_ERROR", "Specify a safe customer field to change.", status_code=422
                    )
                if data.get("payment_terms"):
                    await self.validate_terms(data["payment_terms"], company)
                if data.get("customer_group"):
                    await self.verify_master(
                        self.settings.d365_customer_groups_entity,
                        data["customer_group"],
                        ("CustomerGroupId", "CustomerGroup"),
                        company,
                    )
                self.customer_payload(data, company, update=True)
            return self.normalize_customer(info, record)
        if action == "create_draft_free_text_invoice":
            await self.get_customer(data["account"], company)
            await self.verify_master(
                self.settings.d365_currencies_entity, data["currency"], ("CurrencyCode", "Currency")
            )
            try:
                await self.header_record(data["external_id"], company)
            except AppError as exc:
                if exc.code != "D365_INVOICE_NOT_FOUND":
                    raise
            else:
                raise AppError(
                    "D365_DUPLICATE_RECORD",
                    "The external invoice identifier already exists.",
                    status_code=409,
                )
            for line in data["lines"]:

                def check_main(info, row):
                    type_field = self.actual(info, "Type", "MainAccountType")
                    if enum_member(row.get(type_field, "")) != "Revenue":
                        raise UnsafeMutationError("The main account must be a verified revenue account.")
                    for names in (
                        ("IsSuspended", "Suspended"),
                        ("DoNotAllowManualEntry", "DoNotAllowManualPosting"),
                    ):
                        flag = self.actual(info, *names)
                        if posted_value(row.get(flag)):
                            raise UnsafeMutationError(
                                "The revenue account is suspended or does not permit manual posting."
                            )

                await self.verify_master(
                    self.settings.d365_main_accounts_entity,
                    line.get("revenue_account", self.settings.d365_revenue_account),
                    ("MainAccountId", "MainAccount"),
                    predicate=check_main,
                )
            self.invoice_payloads(data, company)
            return None
        if action in {"update_draft_free_text_invoice", "delete_draft_free_text_invoice"}:
            info, record = await self.header_record(data["identifier"], company)
            if posted_value(record.get(self.actual(info, "IsPosted"))):
                raise UnsafeMutationError("Posted invoices cannot be modified or deleted.")
            self.key(info, record)
            self.actual(info, "DueDate")
            return {
                "identifier": data["identifier"],
                "is_posted": False,
                "due_date": date_string(record.get(info.resolve("due"))),
            }
        if action == "create_customer_payment_journal":
            await self.payment_setup(data, company)
            info = self.info(self.settings.d365_payment_headers_entity)
            for names in (("JournalName",), ("Description",)):
                self.actual(info, *names)
            return None
        if action == "add_customer_payment_line":
            await self.get_customer(data["account"], company)
            header, record = await self.journal_record(data["journal_number"], company)
            name = record.get(self.actual(header, "JournalName"))
            await self.payment_setup({**data, "journal_name": name}, company, include_line=True)
            info = self.info(self.settings.d365_payment_lines_entity)
            payload = self.payment_payload(data, company)
            journal_field = self.actual(info, "JournalBatchNumber", "JournalNumber")
            line_field = self.actual(info, "LineNumber")
            clause = f"{journal_field} eq {odata_literal(data['journal_number'])} and {line_field} eq {data['line_number']}"
            if await self.rows(info, company, clause, top=1):
                raise AppError(
                    "D365_DUPLICATE_RECORD", "That payment line number already exists.", status_code=409
                )
            reference_field = self.actual(info, "PaymentReference", "Document", "DocumentNumber")
            if await self.rows(
                info, company, f"{reference_field} eq {odata_literal(data['reference'])}", top=1
            ):
                raise AppError(
                    "D365_DUPLICATE_RECORD",
                    "A payment line with that reference already exists. Verify it before creating another.",
                    status_code=409,
                )
            return {"journal_number": data["journal_number"], "is_posted": False, "proposed_line": payload}
        raise UnsafeMutationError("Unsupported financial mutation.")

    def customer_payload(self, data, company, update=False):
        info = self.info(self.settings.d365_customers_entity)
        mapping = {
            "name": ("OrganizationName", "Name"),
            "customer_group": ("CustomerGroupId", "CustomerGroup"),
            "payment_terms": ("PaymentTerms", "PaymentTermsName"),
        }
        result = {}
        if not update:
            result[self.field(info, "company")] = company
            result[self.field(info, "account")] = data["account"]
            result[self.field(info, "currency")] = data["currency"]
            # CustomersV3 requires organization party type in most Finance installations.
            party = info.actual("PartyType")
            if party:
                result[party] = "Organization"
        for key, names in mapping.items():
            if key in data:
                result[self.actual(info, *names)] = data[key]
        return result

    def invoice_payloads(self, data, company):
        header = self.info(self.settings.d365_free_text_headers_entity)
        lines = self.info(self.settings.d365_free_text_lines_entity)
        identifier = self.actual(header, "InvoiceIdentifier", "ExternalInvoiceId")
        payload = {
            self.field(header, "company"): company,
            self.field(header, "account"): data["account"],
            self.field(header, "currency"): data["currency"],
            self.actual(header, "DueDate"): data["due_date"],
            self.actual(header, "InvoiceDate"): data["invoice_date"],
            identifier: data["external_id"],
        }
        line_payloads = []
        for index, line in enumerate(data["lines"], 1):
            line_payloads.append(
                {
                    self.field(lines, "company"): company,
                    self.actual(
                        lines, "InvoiceIdentifier", "ParentInvoiceIdentifier", "ExternalInvoiceId"
                    ): data["external_id"],
                    self.actual(lines, "LineNumber"): index,
                    self.actual(lines, "Description"): line["description"],
                    self.actual(lines, "MainAccountDisplayValue"): line.get(
                        "revenue_account", self.settings.d365_revenue_account
                    ),
                    self.actual(lines, "Amount", "LineAmount", "InvoiceAmount"): line["amount"],
                }
            )
        return payload, line_payloads

    def payment_payload(self, data, company):
        info = self.info(self.settings.d365_payment_lines_entity)
        mapping = {
            ("JournalBatchNumber", "JournalNumber"): data["journal_number"],
            ("LineNumber",): data["line_number"],
            ("AccountDisplayValue",): escape_account_display_value(data["account"]),
            ("AccountType",): "Cust",
            ("CurrencyCode",): data["currency"],
            ("CreditAmount",): data["amount"],
            ("DebitAmount",): "0",
            ("TransactionDate", "TransDate"): data["payment_date"],
            ("PaymentReference", "Document", "DocumentNumber"): data["reference"],
            ("OffsetAccountType",): "Bank",
            ("OffsetAccountDisplayValue",): escape_account_display_value(
                data.get("bank_account", self.settings.d365_payment_bank_account)
            ),
            ("MethodOfPayment", "PaymentMethodName", "PaymentMethod"): data.get(
                "payment_method", self.settings.d365_payment_method
            ),
            ("PostingProfile", "CustomerPostingProfile"): data.get(
                "posting_profile", self.settings.d365_customer_posting_profile
            ),
        }
        return {
            self.field(info, "company"): company,
            **{self.actual(info, *names): value for names, value in mapping.items()},
        }

    def verify_fields(self, row, requested):
        for key, value in requested.items():
            actual = row.get(key)
            if actual is None or str(actual) != str(value):
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "Dynamics 365 did not return the requested field values after the write. Verify the record before trying again.",
                    status_code=409,
                    details={"field": key},
                )

    async def verify_invoice_lines(self, data, company):
        info = self.info(self.settings.d365_free_text_lines_entity)
        link = self.actual(info, "InvoiceIdentifier", "ParentInvoiceIdentifier", "ExternalInvoiceId")
        rows = await self.rows(info, company, f"{link} eq {odata_literal(data['external_id'])}")
        amount = self.actual(info, "Amount", "LineAmount", "InvoiceAmount")
        if len(rows) != len(data["lines"]):
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The number of created invoice lines differs from the requested draft. Review the draft in Dynamics 365.",
                status_code=409,
            )
        actual_total = sum((Decimal(decimal_string(row.get(amount))) for row in rows), Decimal(0))
        expected_total = sum((Decimal(line["amount"]) for line in data["lines"]), Decimal(0))
        if actual_total != expected_total:
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The created invoice line total differs from the requested draft. Review it before trying again.",
                status_code=409,
            )

    async def write(self, method, *args):
        try:
            result = await getattr(self.client, method)(*args)
        except AppError as exc:
            if exc.code == "D365_WRITE_OUTCOME_UNKNOWN":
                WRITE_ATTEMPTED.set(True)
            raise
        except (ValueError, TypeError):
            WRITE_ATTEMPTED.set(True)
            raise
        WRITE_ATTEMPTED.set(True)
        return result

    async def execute_mutation(self, action, data, company):
        await self.validate_mutation(action, data, company)
        token = WRITE_ATTEMPTED.set(False)
        try:
            return await self._execute_validated(action, data, company)
        except AppError as exc:
            if WRITE_ATTEMPTED.get() and exc.code not in {
                "D365_WRITE_OUTCOME_UNKNOWN",
                "D365_WRITE_VERIFICATION_FAILED",
                "D365_PARTIAL_DRAFT_CREATED",
            }:
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "A Dynamics 365 write was attempted, but its final state could not be verified. Review the record before trying again.",
                    status_code=409,
                    details={
                        "identifier": data.get("account")
                        or data.get("external_id")
                        or data.get("identifier")
                        or data.get("journal_number"),
                        "verification_code": exc.code,
                    },
                ) from None
            raise
        except (ValueError, TypeError, KeyError):
            if WRITE_ATTEMPTED.get():
                raise AppError(
                    "D365_WRITE_OUTCOME_UNKNOWN",
                    "A write was attempted, but Dynamics 365 returned an unrecognized result. Verify the record before retrying.",
                    status_code=409,
                ) from None
            raise
        finally:
            WRITE_ATTEMPTED.reset(token)

    async def _execute_validated(self, action, data, company):
        if action == "create_customer":
            payload = self.customer_payload(data, company)
            await self.write("post", self.settings.d365_customers_entity, payload)
            info, record = await self.customer_record(data["account"], company)
            self.verify_fields(
                record, {key: value for key, value in payload.items() if key != info.actual("PartyType")}
            )
            return {
                "identifier": data["account"],
                "customer": self.normalize_customer(info, record),
                "verified": True,
            }
        if action in {"update_customer", "delete_test_customer"}:
            info, record = await self.customer_record(data["account"], company)
            if action == "update_customer":
                changes = self.customer_payload(data, company, update=True)
                await self.write("patch", self.key(info, record), changes)
                updated_info, updated = await self.customer_record(data["account"], company)
                self.verify_fields(updated, changes)
                return {
                    "identifier": data["account"],
                    "customer": self.normalize_customer(updated_info, updated),
                    "verified": True,
                }
            await self.write("delete", self.key(info, record))
            try:
                await self.customer_record(data["account"], company)
            except AppError as exc:
                if exc.code == "D365_CUSTOMER_NOT_FOUND":
                    return {"identifier": data["account"], "deleted": True, "verified": True}
                raise
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The customer still exists after deletion. Verify its state in Dynamics 365.",
                status_code=409,
            )
        if action == "create_draft_free_text_invoice":
            header, lines = self.invoice_payloads(data, company)
            await self.write("post", self.settings.d365_free_text_headers_entity, header)
            try:
                for line in lines:
                    await self.write("post", self.settings.d365_free_text_lines_entity, line)
            except AppError:
                # OData multi-entity writes are not assumed atomic. Do not retry or delete partially created data.
                raise AppError(
                    "D365_PARTIAL_DRAFT_CREATED",
                    "An unposted invoice header may have been created, but not all lines were confirmed. Review the draft using the external identifier in Dynamics 365 before retrying.",
                    status_code=409,
                    details={"identifier": data["external_id"]},
                ) from None
            info, record = await self.header_record(data["external_id"], company)
            if posted_value(record.get(self.actual(info, "IsPosted"))):
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "The new invoice is unexpectedly posted. Review it in Dynamics 365.",
                    status_code=409,
                )
            await self.verify_invoice_lines(data, company)
            return {
                "identifier": data["external_id"],
                "invoice": await self.invoice(data["external_id"], company),
                "verified": True,
                "posted": False,
            }
        if action in {"update_draft_free_text_invoice", "delete_draft_free_text_invoice"}:
            info, record = await self.header_record(data["identifier"], company)
            if posted_value(record.get(self.actual(info, "IsPosted"))):
                raise UnsafeMutationError(
                    "The invoice was posted after confirmation. No mutation was executed."
                )
            if action == "update_draft_free_text_invoice":
                due_field = self.actual(info, "DueDate")
                await self.write("patch", self.key(info, record), {due_field: data["due_date"]})
                _, updated = await self.header_record(data["identifier"], company)
                if date_string(updated.get(due_field)) != data["due_date"]:
                    raise AppError(
                        "D365_WRITE_VERIFICATION_FAILED",
                        "The invoice due date did not match the requested update. Review the draft in Dynamics 365.",
                        status_code=409,
                    )
                return {
                    "identifier": data["identifier"],
                    "invoice": await self.invoice(data["identifier"], company),
                    "verified": True,
                }
            await self.write("delete", self.key(info, record))
            try:
                await self.header_record(data["identifier"], company)
            except AppError as exc:
                if exc.code == "D365_INVOICE_NOT_FOUND":
                    return {"identifier": data["identifier"], "deleted": True, "verified": True}
                raise
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The draft still exists after deletion. Verify the invoice state in Dynamics 365.",
                status_code=409,
            )
        if action == "create_customer_payment_journal":
            info = self.info(self.settings.d365_payment_headers_entity)
            payload = {
                self.field(info, "company"): company,
                self.actual(info, "JournalName"): data.get(
                    "journal_name", self.settings.d365_payment_journal_name
                ),
                self.actual(info, "Description"): data["description"],
            }
            created = await self.write("post", info.name, payload)
            number = created.get(self.actual(info, "JournalBatchNumber", "JournalNumber"))
            if not number:
                raise AppError(
                    "D365_WRITE_OUTCOME_UNKNOWN",
                    "The journal creation returned no identifier. Verify the journal in Dynamics 365 before trying again.",
                    status_code=409,
                )
            _, record = await self.journal_record(number, company)
            return {
                "identifier": number,
                "journal": record,
                "verified": True,
                "posted": False,
                "manual_instructions": "Add and review payment lines, settle against invoices, then post in Dynamics 365. Automatic posting is unavailable.",
            }
        if action == "add_customer_payment_line":
            info = self.info(self.settings.d365_payment_lines_entity)
            await self.journal_record(data["journal_number"], company)
            await self.write("post", info.name, self.payment_payload(data, company))
            clause = f"{self.actual(info, 'JournalBatchNumber', 'JournalNumber')} eq {odata_literal(data['journal_number'])} and {self.actual(info, 'LineNumber')} eq {data['line_number']}"
            rows = await self.rows(info, company, clause, top=2)
            if len(rows) != 1:
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "The new payment line could not be verified. Review the journal before retrying.",
                    status_code=409,
                )
            amount_field = self.actual(info, "CreditAmount")
            if Decimal(decimal_string(rows[0].get(amount_field))) != Decimal(data["amount"]):
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "The new payment amount does not match the requested draft. Review the journal before retrying.",
                    status_code=409,
                )
            self.verify_fields(
                rows[0],
                {
                    self.actual(info, "AccountDisplayValue"): escape_account_display_value(data["account"]),
                    self.actual(info, "CurrencyCode"): data["currency"],
                },
            )
            return {
                "identifier": f"{data['journal_number']}/{data['line_number']}",
                "payment_line": rows[0],
                "verified": True,
                "posted": False,
                "manual_instructions": "Review and settle the payment against invoices, then post this unposted journal in Dynamics 365.",
            }
        raise UnsafeMutationError("Unsupported financial mutation.")
