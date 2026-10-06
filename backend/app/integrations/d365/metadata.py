"""Namespace-independent metadata parsing and deterministic capability selection."""

import asyncio
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

from app.core.errors import AppError

MAX_AUTO_PROBES = 3
PROBE_TIMEOUT_SECONDS = 5.0
SKIPPABLE_PROBE_ERRORS = {"D365_ENTITY_UNAVAILABLE", "D365_VALIDATION_ERROR", "D365_PERMISSION_ERROR"}


FIELD_ALIASES = {
    "company": ("dataAreaId", "DataAreaId", "LegalEntityId", "Company"),
    "account": (
        "CustomerAccount",
        "CustomerAccountNumber",
        "AccountNum",
        "AccountNumber",
        "CustomerAccountId",
        "InvoiceCustomerAccount",
        "InvoiceAccount",
    ),
    "name": ("OrganizationName", "CustomerName", "Name", "PartyName"),
    "currency": ("CurrencyCode", "TransactionCurrencyCode", "Currency", "SalesCurrencyCode"),
    "invoice": ("InvoiceId", "InvoiceNumber", "Invoice", "InvoiceIdentifier", "ExternalInvoiceId"),
    "date": ("TransactionDate", "TransDate", "InvoiceDate", "Date"),
    "due": ("DueDate", "PaymentDueDate"),
    "amount": (
        "TransactionCurrencyAmount",
        "AmountCur",
        "AmountInTransactionCurrency",
        "Amount",
        "InvoiceAmount",
        "InvoiceAmountCur",
    ),
    "remaining": (
        "RemainingAmount",
        "RemainingAmountCur",
        "OpenAmount",
        "OutstandingAmount",
        "AmountCur",
        "Balance",
        "AmountRemaining",
    ),
    "voucher": ("Voucher", "VoucherId", "VoucherNumber"),
    "type": ("TransactionType", "TransType", "Type"),
}


def norm(value):
    return re.sub(r"[^a-z0-9]", "", value.casefold())


@dataclass
class EntityInfo:
    name: str
    type_name: str
    fields: dict[str, str] = field(default_factory=dict)
    keys: list[str] = field(default_factory=list)

    def resolve(self, role):
        names = {norm(name): name for name in self.fields}
        return next(
            (names[norm(alias)] for alias in FIELD_ALIASES.get(role, ()) if norm(alias) in names), None
        )

    def actual(self, name):
        return next((key for key in self.fields if norm(key) == norm(name)), None)

    def diagnostic(self):
        return {
            "entity": self.name,
            "fields": sorted(self.fields),
            "keys": self.keys,
            "mapping": {role: value for role in FIELD_ALIASES if (value := self.resolve(role))},
        }


class D365CapabilityRegistry:
    def __init__(self):
        self.entities: dict[str, EntityInfo] = {}
        self.resolved: dict[str, str | None] = {"customer_transactions": None, "open_transactions": None}
        self.candidates: dict[str, list] = {}
        self.messages: list[str] = []
        self.loaded = False

    def public(self):
        return {
            "customers": any(name.lower().startswith("customers") for name in self.entities),
            "customer_transactions": self.resolved["customer_transactions"] is not None,
            "open_transactions": self.resolved["open_transactions"] is not None,
            "resolved_entities": self.resolved.copy(),
            "posting": False,
            "settlement": False,
            "diagnostics": self.messages.copy(),
        }


class D365MetadataResolver:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self.registry = D365CapabilityRegistry()

    def parse(self, xml: str):
        if len(xml) > 10_000_000 or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise AppError(
                "D365_METADATA_INVALID",
                "Unsafe or excessively large D365 metadata was rejected.",
                status_code=502,
            )
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            raise AppError(
                "D365_METADATA_INVALID", "Dynamics 365 returned malformed OData metadata.", status_code=502
            ) from None
        types, aliases, sets = {}, {}, []

        def local(tag):
            return tag.rsplit("}", 1)[-1]

        schemas = [item for item in root.iter() if local(item.tag) == "Schema"]
        for schema in schemas:
            namespace = schema.attrib.get("Namespace", "")
            if schema.attrib.get("Alias"):
                aliases[schema.attrib["Alias"]] = namespace
            for item in schema:
                if local(item.tag) == "EntityType":
                    types[namespace + "." + item.attrib["Name"]] = item
                if local(item.tag) == "EntityContainer":
                    sets.extend(child for child in item if local(child.tag) == "EntitySet")

        def qualify(name):
            prefix, dot, suffix = name.rpartition(".")
            return aliases.get(prefix, prefix) + dot + suffix

        def fields_for(name, seen=None):
            seen = set() if seen is None else seen
            name = qualify(name)
            if name in seen or name not in types:
                return {}, []
            seen.add(name)
            element = types[name]
            fields, keys = (
                fields_for(element.attrib["BaseType"], seen) if "BaseType" in element.attrib else ({}, [])
            )
            for child in element:
                if local(child.tag) == "Property":
                    fields[child.attrib["Name"]] = child.attrib.get("Type", "")
                elif local(child.tag) == "Key":
                    keys = [ref.attrib["Name"] for ref in child if local(ref.tag) == "PropertyRef"]
            return fields, keys

        entities = {}
        for entity in sets:
            name, type_name = entity.attrib["Name"], entity.attrib.get("EntityType", "")
            fields, keys = fields_for(type_name)
            entities[name] = EntityInfo(name, type_name, fields, keys)
        if not entities:
            raise AppError(
                "D365_METADATA_INVALID",
                "No public entity sets were found in Dynamics 365 metadata.",
                status_code=502,
            )
        self.registry.entities = entities
        self.registry.loaded = True
        return entities

    def score(self, info, role):
        name = norm(info.name)
        if "vendor" in name or "supplier" in name or "project" in name:
            return 0
        account, company, currency = (info.resolve(key) for key in ("account", "company", "currency"))
        if not all((account, company, currency)):
            return 0
        if role == "open_transactions" and not info.resolve("remaining"):
            return 0
        if role == "customer_transactions" and (
            not info.resolve("amount")
            or "open" in name
            or "invoice" in name
            or ("transaction" not in name and "custtrans" not in name)
        ):
            return 0
        if "customer" not in name and "custtrans" not in name:
            return 0
        score = 30
        score += 15 if "transaction" in name or "custtrans" in name else 0
        score += 8 if info.resolve("invoice") else 0
        score += 6 if info.resolve("due") else 0
        score += 4 if info.resolve("date") else 0
        score += 4 if info.resolve("voucher") else 0
        if role == "open_transactions":
            score += 30 if "open" in name else 0
            remaining = norm(info.resolve("remaining"))
            score += 20 if remaining not in {"amountcur", "balance"} else 0
            # An ordinary transaction amount is not an outstanding amount.
            if "open" not in name and remaining == "amountcur":
                return 0
        elif "open" in name:
            score -= 25
        return score

    async def load(self, on_stage=None):
        response = await self.client.request("GET", "$metadata")
        self.parse(response.text)
        self.registry.messages = []
        if on_stage is not None:
            on_stage("discovering_entities")
        for role in ("customer_transactions", "open_transactions"):
            setting = getattr(self.settings, "d365_" + role + "_entity", "auto")
            ranked = sorted(
                ((self.score(info, role), name) for name, info in self.registry.entities.items()),
                key=lambda pair: (-pair[0], pair[1]),
            )
            candidates = [
                {"score": score, **self.registry.entities[name].diagnostic()}
                for score, name in ranked
                if score > 0
            ]
            self.registry.candidates[role] = candidates
            self.registry.resolved[role] = None
            chosen = (
                [setting]
                if setting != "auto"
                else [candidate["entity"] for candidate in candidates if candidate["score"] >= 45]
            )
            limited = setting == "auto" and len(chosen) > MAX_AUTO_PROBES
            if setting == "auto":
                chosen = chosen[:MAX_AUTO_PROBES]
            for name in chosen:
                info = self.registry.entities.get(name)
                if info is None or self.score(info, role) <= 0:
                    continue
                try:
                    async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                        await self.client.get(name, top=1, retry_reads=False)
                except TimeoutError:
                    self.registry.messages.append(
                        f"The {name} entity probe exceeded {PROBE_TIMEOUT_SECONDS:g} seconds. "
                        f"It was skipped while checking {role.replace('_', ' ')}; reconnect or set "
                        f"D365_{role.upper()}_ENTITY to a verified entity."
                    )
                    continue
                except AppError as exc:
                    if exc.code not in SKIPPABLE_PROBE_ERRORS:
                        raise
                    continue
                self.registry.resolved[role] = name
                break
            if not self.registry.resolved[role]:
                if limited:
                    self.registry.messages.append(
                        f"Automatic {role.replace('_', ' ')} discovery checked only the top "
                        f"{MAX_AUTO_PROBES} candidates to keep connection setup bounded. Inspect "
                        f"diagnostics and set D365_{role.upper()}_ENTITY for another verified candidate."
                    )
                self.registry.messages.append(
                    f"{role.replace('_', ' ').capitalize()} unavailable. Inspect entity diagnostics and set D365_{role.upper()}_ENTITY to a public entity with matching company, customer, currency, and amount fields."
                )
        return self.registry
