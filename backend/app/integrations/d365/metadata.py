"""Namespace-independent metadata parsing and deterministic capability selection."""

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

import structlog

from app.core.errors import AppError

logger = structlog.get_logger(__name__)
MAX_AUTO_PROBES = 3
PROBE_TIMEOUT_SECONDS = 5.0
MAX_INHERITANCE_DEPTH = 128
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
        self.cache_source: str | None = None

    @property
    def max_metadata_bytes(self):
        return self.settings.d365_metadata_max_mb * 1024**2

    @property
    def cache_path(self):
        """Scope structural metadata to the endpoint and authenticated application."""
        scope = json.dumps(
            [
                self.settings.d365_base_url.rstrip("/"),
                self.settings.d365_tenant_id,
                self.settings.d365_client_id,
            ],
            separators=(",", ":"),
        )
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        database_path = self.settings.database_path()
        data_path = database_path.parent if database_path is not None else Path("data")
        return data_path / "metadata-cache" / f"v1-{digest}.xml"

    def _read_cached_xml(self):
        """Read only a current, privately owned, bounded regular cache file."""
        if not self.settings.d365_metadata_cache_hours:
            return None
        try:
            path = self.cache_path
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                return None
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            with os.fdopen(descriptor, "rb") as cached:
                info = os.fstat(cached.fileno())
                getuid = getattr(os, "getuid", None)
                now = time.time()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                    or (getuid is not None and info.st_uid != getuid())
                    or (getuid is not None and stat.S_IMODE(info.st_mode) != 0o600)
                    or not math.isfinite(info.st_mtime)
                    or info.st_mtime > now
                    or now - info.st_mtime >= self.settings.d365_metadata_cache_hours * 3600
                    or info.st_size > self.max_metadata_bytes
                ):
                    return None
                body = cached.read(self.max_metadata_bytes + 1)
            if len(body) > self.max_metadata_bytes:
                return None
            return body.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            return None

    @staticmethod
    def _discard_cache_file(temporary):
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _stage_cached_xml(self, xml, cancelled, state):
        """A worker may stage XML, but only the active load may publish the cache."""
        temporary = None
        completed = False
        try:
            if cancelled.is_set():
                return None
            path = self.cache_path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = path.parent.lstat()
            getuid = getattr(os, "getuid", None)
            if (
                not stat.S_ISDIR(directory.st_mode)
                or (getuid is not None and directory.st_uid != getuid())
                or (getuid is not None and directory.st_mode & 0o022)
            ):
                raise OSError("Metadata cache directory is not private")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".metadata-", suffix=".tmp", dir=path.parent, delete=False
            ) as cached:
                temporary = Path(cached.name)
                state["temporary"] = temporary
                fchmod = getattr(os, "fchmod", None)
                if fchmod is not None:
                    fchmod(cached.fileno(), 0o600)
                else:
                    os.chmod(temporary, 0o600)
                cached.write(xml.encode("utf-8"))
                cached.flush()
                os.fsync(cached.fileno())
            if cancelled.is_set():
                return None
            completed = True
            return temporary, path
        except (OSError, UnicodeError, ValueError) as exc:
            logger.warning("d365_metadata_cache_write_failed", error_type=type(exc).__name__)
            return None
        finally:
            if not completed or cancelled.is_set():
                self._discard_cache_file(temporary)

    async def _write_cached_xml(self, xml):
        """Atomic persistence is best effort and never stores probe records or tokens."""
        if not self.settings.d365_metadata_cache_hours:
            return
        cancelled, state = threading.Event(), {"temporary": None}
        worker = asyncio.create_task(asyncio.to_thread(self._stage_cached_xml, xml, cancelled, state))
        try:
            staged = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            self._discard_cache_file(state["temporary"])

            def discard_result(task):
                if task.cancelled():
                    return
                failure = task.exception()
                if failure is not None:
                    logger.warning("d365_metadata_cache_write_failed", error_type=type(failure).__name__)
                    return
                staged = task.result()
                if staged is not None:
                    self._discard_cache_file(staged[0])

            worker.add_done_callback(discard_result)
            raise
        if staged is not None:
            temporary, path = staged
            try:
                os.replace(temporary, path)
            except OSError as exc:
                logger.warning("d365_metadata_cache_write_failed", error_type=type(exc).__name__)
            finally:
                self._discard_cache_file(temporary)

    def parse(self, xml: str):
        entities = self._parse_entities(xml)
        self.registry.entities = entities
        self.registry.loaded = True
        return entities

    def _parse_entities(self, xml: str):
        """Parse locally so a cancelled worker cannot mutate the shared registry."""
        try:
            oversized = (
                len(xml) > self.max_metadata_bytes or len(xml.encode("utf-8")) > self.max_metadata_bytes
            )
        except (TypeError, AttributeError, UnicodeError):
            oversized = True
        if oversized or "\x00" in xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise AppError(
                "D365_METADATA_INVALID",
                "Unsafe or excessively large D365 metadata was rejected.",
                status_code=502,
            )
        try:
            root = ElementTree.fromstring(xml)
        except (ElementTree.ParseError, ValueError):
            raise AppError(
                "D365_METADATA_INVALID", "Dynamics 365 returned malformed OData metadata.", status_code=502
            ) from None
        types, aliases, sets = {}, {}, []

        def local(tag):
            return tag.rsplit("}", 1)[-1]

        schemas = [item for item in root.iter() if local(item.tag) == "Schema"]
        try:
            for schema in schemas:
                namespace = schema.attrib.get("Namespace", "")
                if schema.attrib.get("Alias"):
                    aliases[schema.attrib["Alias"]] = namespace
                for item in schema:
                    if local(item.tag) == "EntityType":
                        types[namespace + "." + item.attrib["Name"]] = item
                    if local(item.tag) == "EntityContainer":
                        sets.extend(child for child in item if local(child.tag) == "EntitySet")
        except KeyError:
            raise AppError(
                "D365_METADATA_INVALID", "Dynamics 365 returned malformed OData metadata.", status_code=502
            ) from None

        def qualify(name):
            prefix, dot, suffix = name.rpartition(".")
            return aliases.get(prefix, prefix) + dot + suffix

        resolved_types, resolved_depths = {}, {}

        def fields_for(name):
            seen, chain = set(), []
            name = qualify(name)
            while name in types and name not in resolved_types:
                if name in seen or len(chain) >= MAX_INHERITANCE_DEPTH:
                    raise ValueError("Unsafe metadata inheritance")
                seen.add(name)
                chain.append(name)
                name = qualify(types[name].attrib.get("BaseType", ""))
            fields, keys = resolved_types.get(name, ({}, []))
            depth = resolved_depths.get(name, 0)
            for type_name in reversed(chain):
                depth += 1
                if depth > MAX_INHERITANCE_DEPTH:
                    raise ValueError("Unsafe metadata inheritance")
                fields, keys = fields.copy(), keys.copy()
                for child in types[type_name]:
                    if local(child.tag) == "Property":
                        fields[child.attrib["Name"]] = child.attrib.get("Type", "")
                    elif local(child.tag) == "Key":
                        keys = [ref.attrib["Name"] for ref in child if local(ref.tag) == "PropertyRef"]
                resolved_types[type_name] = fields, keys
                resolved_depths[type_name] = depth
            return fields, keys

        entities = {}
        try:
            for entity in sets:
                name, type_name = entity.attrib["Name"], entity.attrib.get("EntityType", "")
                fields, keys = fields_for(type_name)
                entities[name] = EntityInfo(name, type_name, fields, keys)
        except (KeyError, ValueError):
            raise AppError(
                "D365_METADATA_INVALID", "Dynamics 365 returned malformed OData metadata.", status_code=502
            ) from None
        if not entities:
            raise AppError(
                "D365_METADATA_INVALID",
                "No public entity sets were found in Dynamics 365 metadata.",
                status_code=502,
            )
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

    async def load(self, on_stage=None, *, force_refresh=False):
        self.registry.entities = {}
        self.registry.loaded = False
        self.registry.resolved = {"customer_transactions": None, "open_transactions": None}
        self.registry.candidates = {}
        self.registry.messages = []
        self.cache_source = None
        xml = None if force_refresh else await asyncio.to_thread(self._read_cached_xml)
        if xml is not None:
            try:
                entities = await asyncio.to_thread(self._parse_entities, xml)
            except AppError:
                xml = None
            else:
                self.cache_source = "cache"
                if on_stage is not None:
                    on_stage("loading_cached_metadata")
        if xml is None:
            if on_stage is not None:
                on_stage("loading_metadata")
            response = await self.client.request(
                "GET",
                "$metadata",
                timeout_seconds=self.settings.d365_metadata_timeout_seconds,
                max_response_bytes=self.max_metadata_bytes,
                retry_reads=False,
            )
            entities = await asyncio.to_thread(self._parse_entities, response.text)
            self.cache_source = "network"
            await self._write_cached_xml(response.text)
        self.registry.entities = entities
        self.registry.loaded = True
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
