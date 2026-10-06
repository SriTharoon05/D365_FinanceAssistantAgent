"""Standard-public-entity adapter. Unknown fields and business state fail closed."""

import asyncio
from contextvars import ContextVar
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from functools import partial

from app.core.errors import AppError

from .client import entity_key, escape_account_display_value, odata_literal
from .errors import D365EntityUnavailableError, UnsafeMutationError
from .finance import utcnow
from .metadata import norm
from .setup import list_setup, lookup_main_account, lookup_setup, setup_field


WRITE_ATTEMPTED = ContextVar("d365_write_attempted", default=False)
PAYMENT_TYPES = {"payment", "custpayment", "customerpayment"}
INVOICE_TYPES = {
    "invoice",
    "custinvoice",
    "customerinvoice",
    "freetextinvoice",
    "sales",
    "cust",
    "project",
}


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

    def wire_value(self, info, field, value):
        field_type = info.fields.get(field)
        if field_type == "Edm.Decimal":
            return decimal_string(value)
        if field_type == "Edm.Int64":
            amount = Decimal(decimal_string(value))
            if amount != amount.to_integral_value() or not -(2**63) <= amount < 2**63:
                raise AppError("D365_VALIDATION_ERROR", "The Int64 field value is invalid.", status_code=422)
            return str(int(amount))
        if field_type == "Edm.DateTimeOffset" and len(str(value)) == 10:
            return date_string(value) + "T00:00:00Z"
        return value

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
        records = await self.client.get(info.name, filter=where, top=top, cross_company=True)
        for row in records:
            if str(self.value(info, row, "company")).casefold() != company.casefold():
                raise AppError(
                    "D365_FINANCE_DATA_INVALID",
                    "Dynamics 365 returned a record outside the requested legal entity. No mutation is allowed.",
                    status_code=502,
                )
        return records

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

        # Exact account match first (cheap, works reliably via `eq`).
        exact = await self.rows(info, company, f"{account} eq {odata_literal(query)}", top=5)
        if exact:
            return [self.normalize_customer(info, row) for row in exact]

        # Fallback: this tenant's OData layer cannot execute string functions
        # (contains/startswith/substringof) on CustomersV3, so filter client-side
        # over a bounded page instead of relying on server-side pattern matching.
        candidates = await self.rows(info, company, top=2000)
        needle = query.casefold()
        matches = [
            row
            for row in candidates
            if needle in str(row.get(account, "")).casefold() or needle in str(row.get(name, "")).casefold()
        ][:50]
        return [self.normalize_customer(info, row) for row in matches]

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
        if str(self.value(info, rows[0], "account")) != account:
            raise AppError(
                "D365_FINANCE_DATA_INVALID",
                "Dynamics 365 returned a different customer account. No mutation is allowed.",
                status_code=502,
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
        if info.balance_strategy() == "derived_custtrans":
            return self.normalize_custtrans(info, row)
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

    def normalize_custtrans(self, info, row):
        # The exact standard BI entity exposes cumulative settlement in transaction currency.
        # Missing or malformed settlement values never mean zero settlement.
        original = Decimal(decimal_string(row.get("AmountCur")))
        settled = Decimal(decimal_string(row.get("SettleAmountCur")))
        with localcontext() as context:
            context.prec = max(
                context.prec,
                max(original.adjusted(), settled.adjusted())
                - min(original.as_tuple().exponent, settled.as_tuple().exponent)
                + 2,
            )
            remaining = original - settled
        invoice = str(self.value(info, row, "invoice", required=False) or "").strip()
        raw_type = self.value(info, row, "type", required=False)
        transaction_type = enum_member(raw_type).casefold() if raw_type not in (None, "") else None
        numeric_type = isinstance(raw_type, (int, float, bool)) or (
            transaction_type is not None and transaction_type.lstrip("+-").replace(".", "", 1).isdigit()
        )
        if numeric_type:
            transaction_type = None
        is_invoice = bool(invoice) and (not info.resolve("type") or transaction_type in INVOICE_TYPES)
        due = date_string(self.value(info, row, "due", required=False))
        if due in {"1900-01-01", "0001-01-01"}:
            due = None
        return {
            "account": str(self.value(info, row, "account")),
            "company": str(self.value(info, row, "company")).lower(),
            "invoice_number": invoice,
            "currency": str(self.value(info, row, "currency")),
            "original_amount": str(original),
            "remaining_amount": str(remaining),
            "due_date": due,
            "transaction_date": date_string(self.value(info, row, "date", required=False)),
            "voucher": self.value(info, row, "voucher", required=False),
            "source_entity": info.name,
            "retrieved_at": utcnow().isoformat(),
            "transaction_type": transaction_type,
            "is_invoice": is_invoice,
            "balance_basis": "current",
            "balance_strategy": "derived_custtrans",
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
                    derived = info.balance_strategy() == "derived_custtrans"
                    rows = await self.rows(
                        info,
                        company,
                        f"{self.field(info, 'invoice')} eq {odata_literal(identifier)}",
                        top=None if derived else 2,
                    )
                    if derived:
                        normalized = [self.normalize_transaction(info, row) for row in rows]
                        rows = [row for row in normalized if row["is_invoice"]]
                    if len(rows) == 1:
                        return {
                            **(
                                rows[0]
                                if derived
                                else self.normalize_transaction(
                                    info, rows[0], is_open=role == "open_transactions"
                                )
                            ),
                            "is_posted": True,
                        }
                    if len(rows) > 1:
                        raise AppError(
                            "D365_AMBIGUOUS_RECORD",
                            "Several transactions use this invoice identifier. Specify a unique record.",
                            status_code=409,
                        )
        info, row = await self.header_record(identifier, company)
        return self.normalize_invoice_header(info, row, identifier, company)

    def normalize_invoice_header(self, info, row, identifier, company):
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
            if text in PAYMENT_TYPES:
                amount = Decimal(decimal_string(self.value(info, row, "amount")))
                derived = info.balance_strategy() == "derived_custtrans"
                result.append(
                    {
                        "account": account,
                        "company": company,
                        "currency": str(self.value(info, row, "currency")),
                        "amount": str(-amount if derived else abs(amount)),
                        **({"transaction_amount": str(amount), "is_reversal": amount > 0} if derived else {}),
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
        await lookup_setup(self.client, self.registry, "payment_terms", terms, company)

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
        self.info(self.settings.d365_payment_headers_entity)
        journal_name = data.get("journal_name", self.settings.d365_payment_journal_name)
        names, record = await lookup_setup(self.client, self.registry, "journal_name", journal_name, company)
        if (
            enum_member(record.get(setup_field(names, "journal_name", "type"), "")).casefold()
            != "custpayment"
        ):
            raise UnsafeMutationError("The selected journal name is not a verified CustPayment journal type.")
        if not include_line:
            return
        bank, record = await lookup_setup(
            self.client,
            self.registry,
            "bank_account",
            data.get("bank_account", self.settings.d365_payment_bank_account),
            company,
        )
        self.verify_bank_account(bank, record)
        await lookup_setup(
            self.client,
            self.registry,
            "payment_method",
            data.get("payment_method", self.settings.d365_payment_method),
            company,
        )
        await lookup_setup(
            self.client,
            self.registry,
            "posting_profile",
            data.get("posting_profile", self.settings.d365_customer_posting_profile),
            company,
        )

    def verify_revenue_account(self, info, row):
        if enum_member(row.get(setup_field(info, "main_account", "type"), "")).casefold() != "revenue":
            raise UnsafeMutationError("The main account must be a verified revenue account.")
        for field in ("suspended", "manual_blocked"):
            if posted_value(row.get(setup_field(info, "main_account", field))):
                raise UnsafeMutationError(
                    "The revenue account is suspended or does not permit manual posting."
                )

    def verify_bank_account(self, info, row):
        status = info.actual("BankAccountStatus")
        if status and enum_member(row.get(status, "")).casefold() != "activeforalltransactions":
            raise UnsafeMutationError("The bank account is not verified as active for new transactions.")

    async def get_write_setup(self, company, purpose="all"):
        self.info(self.settings.d365_customers_entity)
        if purpose not in {"all", "invoice", "payment", "customer"}:
            raise AppError(
                "D365_VALIDATION_ERROR", "Choose a supported write setup purpose.", status_code=422
            )
        roles = []
        if purpose in {"all", "invoice"}:
            roles.append(
                ("main_account", self.settings.d365_revenue_account, self.settings.d365_main_accounts_entity)
            )
        if purpose in {"all", "payment"}:
            roles.extend(
                (
                    ("journal_name", self.settings.d365_payment_journal_name, None),
                    ("bank_account", self.settings.d365_payment_bank_account, None),
                    ("payment_method", self.settings.d365_payment_method, None),
                    ("posting_profile", self.settings.d365_customer_posting_profile, None),
                )
            )
        if purpose in {"all", "customer"}:
            roles.append(("payment_terms", None, None))
        result = {
            "company": company.lower(),
            "purpose": purpose,
            "mock_mode": False,
            "setup": {},
            "evidence": [],
        }
        for role, default, configured in roles:
            section = {"candidates": [], "configured_default": {"value": default, "verified": False}}
            try:
                info, rows = await list_setup(
                    self.client,
                    self.registry,
                    role,
                    company,
                    configured,
                    type_member={"main_account": "Revenue", "journal_name": "CustPayment"}.get(role),
                )
                section["source_entity"] = info.name
                for row in rows:
                    identifier = row.get(setup_field(info, role, "id"))
                    if not isinstance(identifier, str) or not identifier.strip():
                        continue
                    if role in {"main_account", "bank_account"}:
                        try:
                            if role == "main_account":
                                self.verify_revenue_account(info, row)
                            else:
                                self.verify_bank_account(info, row)
                        except AppError:
                            continue
                    candidate = {"id": identifier}
                    if role in {"main_account", "journal_name"}:
                        kind = enum_member(row.get(setup_field(info, role, "type"), ""))
                        if role == "journal_name" and kind.casefold() != "custpayment":
                            continue
                        candidate["type"] = kind
                    section["candidates"].append(candidate)
                result["evidence"].append(
                    {
                        "kind": "write_setup",
                        "company": company.lower(),
                        "source_entity": info.name,
                        "retrieved_at": utcnow().isoformat(),
                    }
                )
                section["listing_limit"] = 50
                if default:
                    try:
                        verified_info, record = await lookup_setup(
                            self.client, self.registry, role, default, company, configured
                        )
                        if role == "main_account":
                            self.verify_revenue_account(verified_info, record)
                        if role == "bank_account":
                            self.verify_bank_account(verified_info, record)
                        if (
                            role == "journal_name"
                            and enum_member(
                                record.get(setup_field(verified_info, role, "type"), "")
                            ).casefold()
                            != "custpayment"
                        ):
                            raise UnsafeMutationError(
                                "The configured journal name is not a CustPayment journal."
                            )
                        section["configured_default"]["verified"] = True
                    except AppError as exc:
                        if exc.code in {
                            "D365_CONNECTION_ERROR",
                            "D365_AUTHENTICATION_ERROR",
                            "D365_RATE_LIMIT_ERROR",
                        }:
                            raise
                        section["configured_default"]["diagnostic"] = exc.message
            except AppError as exc:
                if exc.code in {
                    "D365_CONNECTION_ERROR",
                    "D365_AUTHENTICATION_ERROR",
                    "D365_RATE_LIMIT_ERROR",
                }:
                    raise
                section["diagnostic"] = exc.message
            result["setup"][role] = section
        if purpose in {"all", "customer"}:
            for role, entity, names in (
                (
                    "customer_group",
                    self.settings.d365_customer_groups_entity,
                    ("CustomerGroupId", "CustomerGroup"),
                ),
                ("currency", self.settings.d365_currencies_entity, ("CurrencyCode", "Currency")),
            ):
                section = {"candidates": []}
                try:
                    info = self.info(entity)
                    identifier = self.actual(info, *names)
                    rows = (
                        await self.rows(info, company, top=50)
                        if info.resolve("company")
                        else await self.client.get(entity, top=50)
                    )
                    section.update(
                        {
                            "source_entity": entity,
                            "listing_limit": 50,
                            "candidates": [
                                {"id": row[identifier]}
                                for row in rows
                                if isinstance(row.get(identifier), str) and row[identifier].strip()
                            ],
                        }
                    )
                    result["evidence"].append(
                        {
                            "kind": "write_setup",
                            "company": company.lower(),
                            "source_entity": entity,
                            "retrieved_at": utcnow().isoformat(),
                        }
                    )
                except AppError as exc:
                    if exc.code in {
                        "D365_CONNECTION_ERROR",
                        "D365_AUTHENTICATION_ERROR",
                        "D365_RATE_LIMIT_ERROR",
                    }:
                        raise
                    section["diagnostic"] = exc.message
                result["setup"][role] = section
        return result

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
                info, row = await lookup_main_account(
                    self.client,
                    self.registry,
                    line.get("revenue_account", self.settings.d365_revenue_account),
                    company,
                    self.settings.d365_main_accounts_entity,
                )
                self.verify_revenue_account(info, row)
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
            self.actual(header, "DueDate"): self.wire_value(
                header, self.actual(header, "DueDate"), data["due_date"]
            ),
            self.actual(header, "InvoiceDate"): self.wire_value(
                header, self.actual(header, "InvoiceDate"), data["invoice_date"]
            ),
            identifier: data["external_id"],
        }
        invoice_account = header.actual("InvoiceAccount")
        if invoice_account:
            payload[invoice_account] = data["account"]
        line_payloads = []
        for index, line in enumerate(data["lines"], 1):
            line_payload = {
                self.field(lines, "company"): company,
                self.actual(lines, "InvoiceIdentifier", "ParentInvoiceIdentifier", "ExternalInvoiceId"): data[
                    "external_id"
                ],
                self.actual(lines, "LineNumber"): self.wire_value(
                    lines, self.actual(lines, "LineNumber"), index
                ),
                self.actual(lines, "Description"): line["description"],
                self.actual(lines, "MainAccountDisplayValue"): escape_account_display_value(
                    line.get("revenue_account", self.settings.d365_revenue_account)
                ),
                self.actual(
                    lines, "TransactionCurrencyAmount", "Amount", "LineAmount", "InvoiceAmount"
                ): line["amount"],
            }
            if lines.actual("Quantity") and lines.actual("UnitPrice"):
                line_payload[lines.actual("Quantity")] = "1"
                line_payload[lines.actual("UnitPrice")] = line["amount"]
            line_payloads.append(line_payload)
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
            **{
                self.actual(info, *names): self.wire_value(info, self.actual(info, *names), value)
                for names, value in mapping.items()
            },
        }

    def verify_fields(self, row, requested, info=None):
        for key, value in requested.items():
            actual = row.get(key)
            matches = str(actual) == str(value)
            if norm(key) in {"dataareaid", "legalentityid", "company"}:
                matches = str(actual).casefold() == str(value).casefold()
            if info and info.fields.get(key) in {"Edm.Decimal", "Edm.Int64"} and actual is not None:
                matches = Decimal(decimal_string(actual)) == Decimal(decimal_string(value))
            if info and info.fields.get(key) == "Edm.DateTimeOffset" and actual is not None:
                matches = date_string(actual) == date_string(value)
            if (
                info
                and info.fields.get(key)
                and not info.fields[key].startswith("Edm.")
                and actual is not None
            ):
                matches = enum_member(actual).casefold() == enum_member(value).casefold()
            if norm(key) in {
                "accountdisplayvalue",
                "offsetaccountdisplayvalue",
                "mainaccountdisplayvalue",
            } and isinstance(actual, str):
                matches = matches or escape_account_display_value(actual) == str(value)
            if actual is None or not matches:
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
        amount = self.actual(info, "TransactionCurrencyAmount", "Amount", "LineAmount", "InvoiceAmount")
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
        _, expected_lines = self.invoice_payloads(data, company)
        number = self.actual(info, "LineNumber")
        by_number = {}
        for row in rows:
            key = Decimal(decimal_string(row.get(number)))
            if key in by_number:
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "The created draft has duplicate invoice line numbers. Review it in Dynamics 365.",
                    status_code=409,
                )
            by_number[key] = row
        for expected in expected_lines:
            key = Decimal(decimal_string(expected[number]))
            if key not in by_number:
                raise AppError(
                    "D365_WRITE_VERIFICATION_FAILED",
                    "The created draft line numbers differ from the requested draft. Review it in Dynamics 365.",
                    status_code=409,
                )
            self.verify_fields(by_number[key], expected, info)

    async def write(self, method, *args, **kwargs):
        try:
            result = await getattr(self.client, method)(*args, **kwargs)
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
        token = WRITE_ATTEMPTED.set(False)
        try:
            await self.validate_mutation(action, data, company)
            return await self._execute_validated(action, data, company)
        except AppError as exc:
            if not WRITE_ATTEMPTED.get():
                exc.details = {**(exc.details or {}), "write_outcome": "not_written"}
                raise
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

    async def verify_after_write(self, verify):
        """Retry only read observations of an acknowledged write, never the write itself."""
        pending = {
            "D365_WRITE_VERIFICATION_FAILED",
            "D365_CUSTOMER_NOT_FOUND",
            "D365_INVOICE_NOT_FOUND",
            "D365_JOURNAL_NOT_FOUND",
        }
        for attempt in range(3):
            try:
                return await verify()
            except AppError as exc:
                if exc.code not in pending or attempt == 2:
                    raise
                await asyncio.sleep((0.2, 0.5)[attempt])

    async def verify_customer(self, account, company, changes):
        info, record = await self.customer_record(account, company)
        self.verify_fields(record, changes)
        return {
            "identifier": account,
            "customer": self.normalize_customer(info, record),
            "verified": True,
        }

    async def verify_deleted_customer(self, account, company):
        try:
            await self.customer_record(account, company)
        except AppError as exc:
            if exc.code == "D365_CUSTOMER_NOT_FOUND":
                return {"identifier": account, "deleted": True, "verified": True}
            raise
        raise AppError(
            "D365_WRITE_VERIFICATION_FAILED",
            "The customer still exists after deletion. Verify its state in Dynamics 365.",
            status_code=409,
        )

    async def verify_updated_invoice(self, identifier, company, due_date):
        info, record = await self.header_record(identifier, company)
        if posted_value(record.get(self.actual(info, "IsPosted"))):
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The invoice is now posted. Review the record in Dynamics 365 before making another change.",
                status_code=409,
            )
        if date_string(record.get(self.actual(info, "DueDate"))) != due_date:
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The invoice due date did not match the requested update. Review the draft in Dynamics 365.",
                status_code=409,
            )
        return {
            "identifier": identifier,
            "invoice": self.normalize_invoice_header(info, record, identifier, company),
            "verified": True,
        }

    async def verify_deleted_invoice(self, identifier, company):
        try:
            await self.header_record(identifier, company)
        except AppError as exc:
            if exc.code == "D365_INVOICE_NOT_FOUND":
                return {"identifier": identifier, "deleted": True, "verified": True}
            raise
        raise AppError(
            "D365_WRITE_VERIFICATION_FAILED",
            "The draft still exists after deletion. Verify the invoice state in Dynamics 365.",
            status_code=409,
        )

    async def verify_created_invoice(self, data, company):
        info, record = await self.header_record(data["external_id"], company)
        if posted_value(record.get(self.actual(info, "IsPosted"))):
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The new invoice is unexpectedly posted. Review it in Dynamics 365.",
                status_code=409,
            )
        header, _ = self.invoice_payloads(data, company)
        self.verify_fields(record, header, info)
        await self.verify_invoice_lines(data, company)
        return {
            "identifier": data["external_id"],
            "invoice": self.normalize_invoice_header(info, record, data["external_id"], company),
            "verified": True,
            "posted": False,
        }

    async def verify_created_journal(self, number, company, payload):
        info, record = await self.journal_record(number, company)
        self.verify_fields(record, payload, info)
        return {
            "identifier": number,
            "journal": record,
            "verified": True,
            "posted": False,
            "manual_instructions": "Add and review payment lines, settle against invoices, then post in Dynamics 365. Automatic posting is unavailable.",
        }

    async def verify_created_payment_line(self, data, company):
        info = self.info(self.settings.d365_payment_lines_entity)
        clause = f"{self.actual(info, 'JournalBatchNumber', 'JournalNumber')} eq {odata_literal(data['journal_number'])} and {self.actual(info, 'LineNumber')} eq {data['line_number']}"
        rows = await self.rows(info, company, clause, top=2)
        if len(rows) != 1:
            raise AppError(
                "D365_WRITE_VERIFICATION_FAILED",
                "The new payment line could not be uniquely verified. Review the journal before retrying.",
                status_code=409,
            )
        self.verify_fields(rows[0], self.payment_payload(data, company), info)
        await self.journal_record(data["journal_number"], company)
        return {
            "identifier": f"{data['journal_number']}/{data['line_number']}",
            "payment_line": rows[0],
            "verified": True,
            "posted": False,
            "manual_instructions": "Review and settle the payment against invoices, then post this unposted journal in Dynamics 365.",
        }

    async def reconcile_mutation(self, action, data, company):
        """Observe the intended result of an earlier uncertain action without another write."""
        if action == "update_customer":
            changes = self.customer_payload(data, company, update=True)
            verify = partial(self.verify_customer, data["account"], company, changes)
        elif action == "delete_test_customer":
            verify = partial(self.verify_deleted_customer, data["account"], company)
        elif action == "update_draft_free_text_invoice":
            verify = partial(self.verify_updated_invoice, data["identifier"], company, data["due_date"])
        elif action == "delete_draft_free_text_invoice":
            verify = partial(self.verify_deleted_invoice, data["identifier"], company)
        else:
            raise UnsafeMutationError("This uncertain action requires manual verification in Dynamics 365.")
        result = await self.verify_after_write(verify)
        return {**result, "reconciled": True}

    async def _execute_validated(self, action, data, company):
        if action == "create_customer":
            payload = self.customer_payload(data, company)
            await self.write("post", self.settings.d365_customers_entity, payload)
            info = self.info(self.settings.d365_customers_entity)
            changes = {key: value for key, value in payload.items() if key != info.actual("PartyType")}
            return await self.verify_after_write(
                lambda: self.verify_customer(data["account"], company, changes)
            )
        if action in {"update_customer", "delete_test_customer"}:
            info, record = await self.customer_record(data["account"], company)
            if action == "update_customer":
                changes = self.customer_payload(data, company, update=True)
                await self.write("patch", self.key(info, record), changes, cross_company=True)
                return await self.verify_after_write(
                    lambda: self.verify_customer(data["account"], company, changes)
                )
            await self.write("delete", self.key(info, record), cross_company=True)
            return await self.verify_after_write(
                lambda: self.verify_deleted_customer(data["account"], company)
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
            return await self.verify_after_write(lambda: self.verify_created_invoice(data, company))
        if action in {"update_draft_free_text_invoice", "delete_draft_free_text_invoice"}:
            info, record = await self.header_record(data["identifier"], company)
            if posted_value(record.get(self.actual(info, "IsPosted"))):
                raise UnsafeMutationError(
                    "The invoice was posted after confirmation. No mutation was executed."
                )
            if action == "update_draft_free_text_invoice":
                due_field = self.actual(info, "DueDate")
                await self.write(
                    "patch",
                    self.key(info, record),
                    {due_field: self.wire_value(info, due_field, data["due_date"])},
                    cross_company=True,
                )
                return await self.verify_after_write(
                    lambda: self.verify_updated_invoice(data["identifier"], company, data["due_date"])
                )
            await self.write("delete", self.key(info, record), cross_company=True)
            return await self.verify_after_write(
                lambda: self.verify_deleted_invoice(data["identifier"], company)
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
            return await self.verify_after_write(
                lambda: self.verify_created_journal(number, company, payload)
            )
        if action == "add_customer_payment_line":
            info = self.info(self.settings.d365_payment_lines_entity)
            await self.journal_record(data["journal_number"], company)
            await self.write("post", info.name, self.payment_payload(data, company))
            return await self.verify_after_write(lambda: self.verify_created_payment_line(data, company))
        raise UnsafeMutationError("Unsupported financial mutation.")
