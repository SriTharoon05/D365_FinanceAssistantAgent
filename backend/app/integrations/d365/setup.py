"""Read-only, schema-verified setup lookup with legal entity and ledger chart scoping."""

import re

from app.core.errors import AppError

from .client import odata_literal
from .errors import D365EntityUnavailableError
from .metadata import FIELD_ALIASES


# These are setup entities, never existing transaction/journal records used as setup proof.
SETUP_ENTITIES = {
    "payment_terms": ("PaymentTerms", "TermsOfPayment"),
    "journal_name": ("JournalNames",),
    "bank_account": ("BankAccounts",),
    "payment_method": ("CustomerPaymentMethods", "CustomerPaymentModes"),
    "posting_profile": ("CustomerPostingProfiles",),
    "ledger": ("Ledgers",),
    "main_account": ("MainAccounts",),
}
SETUP_FIELDS = {
    "payment_terms": {"id": ("PaymentTermsName", "PaymentTerms", "TermsOfPayment", "Name")},
    "journal_name": {"id": ("JournalName", "Name"), "type": ("JournalType", "Type")},
    "bank_account": {"id": ("BankAccountId", "BankAccount", "AccountID")},
    "payment_method": {"id": ("PaymentMode", "PaymentMethodName", "PaymentModeName", "Name")},
    "posting_profile": {"id": ("PostingProfile", "PostingProfileName", "Name")},
    "ledger": {"id": ("LegalEntityId",), "chart": ("ChartOfAccounts",)},
    "main_account": {
        "id": ("MainAccountId", "MainAccount"),
        "chart": ("ChartOfAccounts",),
        "type": ("MainAccountType", "Type"),
        "suspended": ("IsSuspended", "Suspended"),
        "manual_blocked": ("DoNotAllowManualEntry", "DoNotAllowManualPosting"),
    },
}


def setup_aliases(role, field):
    if field == "company":
        return FIELD_ALIASES["company"]
    return SETUP_FIELDS[role][field]


def setup_field(info, role, field):
    actual = next((info.actual(alias) for alias in setup_aliases(role, field) if info.actual(alias)), None)
    if actual is None:
        raise D365EntityUnavailableError(
            f"{info.name} has no verified {role.replace('_', ' ')} {field} field. "
            "Check public entity metadata and permissions before preparing this action."
        )
    return actual


def setup_info(registry, role, configured_entity=None):
    if role not in SETUP_ENTITIES:
        raise ValueError("Unsupported setup role")
    names = (configured_entity,) if configured_entity else SETUP_ENTITIES[role]
    if registry.loaded:
        for name in names:
            info = registry.entities.get(name)
            if info is None:
                continue
            fields = tuple(SETUP_FIELDS[role])
            if role != "main_account":
                fields += ("company",)
            try:
                resolved = {field: setup_field(info, role, field) for field in fields}
            except D365EntityUnavailableError:
                continue
            # Identifiers and scope must be strings, not loosely interpreted numeric fields.
            if any(
                info.fields[resolved[field]] != "Edm.String"
                for field in ("id", "company", "chart")
                if field in resolved
            ):
                continue
            return info
    required = ", ".join("/".join(aliases) for aliases in SETUP_FIELDS[role].values())
    raise D365EntityUnavailableError(
        f"Cannot verify {role.replace('_', ' ')} setup. A readable public entity "
        f"{', '.join(names)} with {required} and the applicable company/chart scope is required. "
        "Check the mapped D365 user's permissions and metadata diagnostics."
    )


def _required_value(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AppError(
            "D365_SETUP_INVALID",
            f"Specify a nonempty {label} from verified Dynamics 365 setup.",
            status_code=422,
        )
    return value


def _scope_filter(info, role, company):
    company = _required_value(company, "legal entity")
    field = setup_field(info, role, "company")
    return f"{field} eq {odata_literal(company.lower())}"


def _verify_scope(info, role, rows, expected, scope="company"):
    field = setup_field(info, role, scope)
    for row in rows:
        actual = row.get(field)
        matches = (
            isinstance(actual, str) and actual.casefold() == expected.casefold()
            if scope == "company"
            else isinstance(actual, str) and actual == expected
        )
        if not matches:
            raise AppError(
                "D365_SETUP_INVALID",
                f"{info.name} returned setup outside the verified {scope} scope. No action was prepared.",
                status_code=422,
            )


async def lookup_setup(client, registry, role, value, company, configured_entity=None):
    """Return one exact, verified setup record; missing/ambiguous records never select a default."""
    if role == "main_account":
        return await lookup_main_account(client, registry, value, company, configured_entity)
    info = setup_info(registry, role, configured_entity)
    value = _required_value(value, role.replace("_", " "))
    identifier = setup_field(info, role, "id")
    clause = f"{_scope_filter(info, role, company)} and {identifier} eq {odata_literal(value)}"
    rows = await client.get(info.name, filter=clause, top=2, cross_company=True)
    _verify_scope(info, role, rows, company)
    matches = (
        isinstance(rows[0].get(identifier), str) and rows[0][identifier].casefold() == value.casefold()
        if rows and role == "ledger"
        else bool(rows) and rows[0].get(identifier) == value
    )
    if len(rows) != 1 or not matches:
        raise AppError(
            "D365_SETUP_INVALID",
            f"The configured {role.replace('_', ' ')} '{value}' could not be uniquely verified "
            f"in {info.name} for {company.upper()}. Inspect setup or choose an existing valid identifier.",
            status_code=422,
        )
    return info, rows[0]


async def _ledger_chart(client, registry, company):
    info, row = await lookup_setup(client, registry, "ledger", company.lower(), company)
    return _required_value(row.get(setup_field(info, "ledger", "chart")), "ledger chart of accounts")


async def lookup_main_account(client, registry, account, company, configured_entity=None):
    """Main account IDs can repeat across charts: always resolve the legal entity's ledger first."""
    info = setup_info(registry, "main_account", configured_entity)
    account = _required_value(account, "revenue main account")
    chart = await _ledger_chart(client, registry, company)
    identifier = setup_field(info, "main_account", "id")
    chart_field = setup_field(info, "main_account", "chart")
    clause = f"{chart_field} eq {odata_literal(chart)} and {identifier} eq {odata_literal(account)}"
    rows = await client.get(info.name, filter=clause, top=2)
    _verify_scope(info, "main_account", rows, chart, "chart")
    if len(rows) != 1 or rows[0].get(identifier) != account:
        raise AppError(
            "D365_SETUP_INVALID",
            f"Main account '{account}' could not be uniquely verified in chart '{chart}' "
            f"for {company.upper()}. Choose a revenue account in that company's ledger chart.",
            status_code=422,
        )
    return info, rows[0]


async def list_setup(client, registry, role, company, configured_entity=None, top=50, type_member=None):
    """Bounded read-only discovery for explaining real setup choices to a user."""
    if not isinstance(top, int) or isinstance(top, bool) or not 1 <= top <= 100:
        raise ValueError("Setup discovery limit must be between 1 and 100")
    info = setup_info(registry, role, configured_entity)
    type_clause = None
    if type_member is not None:
        if {"main_account": "Revenue", "journal_name": "CustPayment"}.get(role) != type_member:
            raise ValueError("Unsupported setup type filter")
        type_field = setup_field(info, role, "type")
        type_name = info.fields[type_field]
        if type_name == "Edm.String":
            type_literal = odata_literal(type_member)
        elif not type_name.startswith("Edm.") and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", type_name
        ):
            type_literal = f"{type_name}{odata_literal(type_member)}"
        else:
            raise D365EntityUnavailableError(
                f"{info.name} has no supported metadata type for its {type_field} filter. "
                "Check entity diagnostics before discovering setup choices."
            )
        type_clause = f"{type_field} eq {type_literal}"
    if role == "main_account":
        chart = await _ledger_chart(client, registry, company)
        clause = f"{setup_field(info, role, 'chart')} eq {odata_literal(chart)}"
        if type_clause:
            clause += " and " + type_clause
        rows = await client.get(info.name, filter=clause, top=top)
        _verify_scope(info, role, rows, chart, "chart")
    else:
        clause = _scope_filter(info, role, company)
        if type_clause:
            clause += " and " + type_clause
        rows = await client.get(info.name, filter=clause, top=top, cross_company=True)
        _verify_scope(info, role, rows, company)
    if type_member is not None:
        accepted = {type_member}
        if type_name != "Edm.String":
            accepted |= {f"{type_name}'{type_member}'", f"{type_name}.{type_member}"}
        if any(not isinstance(row.get(type_field), str) or row[type_field] not in accepted for row in rows):
            raise AppError(
                "D365_SETUP_INVALID",
                f"{info.name} returned setup outside the requested {type_member} type. "
                "No setup choices were inferred from those records.",
                status_code=422,
            )
    return info, rows
