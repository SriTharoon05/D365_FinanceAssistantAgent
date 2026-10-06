from copy import deepcopy

import pytest

from app.core.errors import AppError
from app.integrations.d365.metadata import D365CapabilityRegistry, EntityInfo
from app.integrations.d365.setup import (
    list_setup,
    lookup_main_account,
    lookup_setup,
    setup_aliases,
    setup_field,
    setup_info,
)


def registry():
    result = D365CapabilityRegistry()
    definitions = {
        "JournalNames": {
            "dataAreaId": "Edm.String",
            "Name": "Edm.String",
            "Type": "Microsoft.Dynamics.DataEntities.LedgerJournalType",
        },
        "PaymentTerms": {"dataAreaId": "Edm.String", "Name": "Edm.String"},
        "CustomerPaymentMethods": {"dataAreaId": "Edm.String", "Name": "Edm.String"},
        "CustomerPostingProfiles": {"dataAreaId": "Edm.String", "PostingProfile": "Edm.String"},
        "BankAccounts": {"dataAreaId": "Edm.String", "BankAccountId": "Edm.String"},
        "Ledgers": {"LegalEntityId": "Edm.String", "ChartOfAccounts": "Edm.String"},
        "MainAccounts": {
            "ChartOfAccounts": "Edm.String",
            "MainAccountId": "Edm.String",
            "MainAccountType": "Microsoft.Dynamics.DataEntities.DimensionLedgerAccountType",
            "IsSuspended": "Microsoft.Dynamics.DataEntities.NoYes",
            "DoNotAllowManualEntry": "Microsoft.Dynamics.DataEntities.NoYes",
        },
    }
    result.entities = {name: EntityInfo(name, name, fields) for name, fields in definitions.items()}
    result.loaded = True
    return result


class Client:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def get(self, entity, **kwargs):
        self.calls.append((entity, kwargs))
        return deepcopy(self.records.get(entity, []))


@pytest.mark.parametrize(
    ("role", "entity", "identifier"),
    [
        ("journal_name", "JournalNames", "Name"),
        ("payment_terms", "PaymentTerms", "Name"),
        ("payment_method", "CustomerPaymentMethods", "Name"),
        ("posting_profile", "CustomerPostingProfiles", "PostingProfile"),
        ("bank_account", "BankAccounts", "BankAccountId"),
    ],
)
async def test_actual_tenant_setup_aliases_are_readable_and_company_scoped(role, entity, identifier):
    client = Client({entity: [{"dataAreaId": "usmf", identifier: "TEST"}]})
    info, row = await lookup_setup(client, registry(), role, "TEST", "USMF")
    assert info.name == entity
    assert row[identifier] == "TEST"
    assert client.calls == [
        (
            entity,
            {
                "filter": f"dataAreaId eq 'usmf' and {identifier} eq 'TEST'",
                "top": 2,
                "cross_company": True,
            },
        )
    ]


def test_journal_type_uses_actual_type_field_and_terms_use_name():
    entities = registry()
    assert setup_field(setup_info(entities, "journal_name"), "journal_name", "type") == "Type"
    assert "Name" in setup_aliases("payment_terms", "id")


def test_existing_journal_records_do_not_substitute_for_setup():
    entities = registry()
    entities.entities = {
        "CustomerPaymentJournalHeaders": EntityInfo(
            "CustomerPaymentJournalHeaders",
            "Header",
            {"dataAreaId": "Edm.String", "JournalName": "Edm.String", "JournalType": "Edm.String"},
        )
    }
    with pytest.raises(AppError, match="Cannot verify journal name setup") as error:
        setup_info(entities, "journal_name")
    assert error.value.code == "D365_CAPABILITY_UNAVAILABLE"


@pytest.mark.parametrize("missing", ["dataAreaId", "Name", "Type"])
def test_journal_schema_missing_scope_or_required_fields_fails_closed(missing):
    entities = registry()
    del entities.entities["JournalNames"].fields[missing]
    with pytest.raises(AppError):
        setup_info(entities, "journal_name")


def test_setup_with_numeric_identifier_is_not_treated_as_string_identifier():
    entities = registry()
    entities.entities["JournalNames"].fields["Name"] = "Edm.Int64"
    with pytest.raises(AppError):
        setup_info(entities, "journal_name")


@pytest.mark.parametrize(
    "records",
    [
        [],
        [{"dataAreaId": "usmf", "Name": "TEST"}] * 2,
        [{"dataAreaId": "demf", "Name": "TEST"}],
        [{"Name": "TEST"}],
        [{"dataAreaId": "usmf", "Name": "OTHER"}],
    ],
)
async def test_unverified_ambiguous_or_cross_company_setup_is_rejected(records):
    client = Client({"JournalNames": records})
    with pytest.raises(AppError) as error:
        await lookup_setup(client, registry(), "journal_name", "TEST", "usmf")
    assert error.value.code == "D365_SETUP_INVALID"


async def test_lookup_escapes_identifiers_without_changing_company_scope():
    client = Client({"PaymentTerms": [{"dataAreaId": "usmf", "Name": "O'Brien"}]})
    await lookup_setup(client, registry(), "payment_terms", "O'Brien", "USMF")
    assert client.calls[0][1]["filter"] == "dataAreaId eq 'usmf' and Name eq 'O''Brien'"


def account_records(chart="Shared", account="401100"):
    return {
        "Ledgers": [{"LegalEntityId": "USMF", "ChartOfAccounts": "Shared"}],
        "MainAccounts": [
            {
                "ChartOfAccounts": chart,
                "MainAccountId": account,
                "MainAccountType": "Revenue",
                "IsSuspended": "No",
                "DoNotAllowManualEntry": "No",
            }
        ],
    }


async def test_main_account_is_verified_in_legal_entity_ledger_chart():
    client = Client(account_records())
    info, row = await lookup_main_account(client, registry(), "401100", "usmf")
    assert info.name == "MainAccounts"
    assert row["ChartOfAccounts"] == "Shared"
    assert client.calls[-1] == (
        "MainAccounts",
        {"filter": "ChartOfAccounts eq 'Shared' and MainAccountId eq '401100'", "top": 2},
    )
    assert client.calls[0][0] == "Ledgers"


async def test_generic_main_account_lookup_also_enforces_chart():
    client = Client(account_records())
    await lookup_setup(client, registry(), "main_account", "401100", "usmf")
    assert [name for name, _ in client.calls] == ["Ledgers", "MainAccounts"]


async def test_main_account_from_other_chart_cannot_validate_revenue_account():
    client = Client(account_records(chart="Other"))
    with pytest.raises(AppError, match="outside the verified chart scope"):
        await lookup_main_account(client, registry(), "401100", "usmf")


@pytest.mark.parametrize(
    "ledgers",
    [
        [],
        [{"LegalEntityId": "usmf", "ChartOfAccounts": "Shared"}] * 2,
        [{"LegalEntityId": "demf", "ChartOfAccounts": "Shared"}],
        [{"LegalEntityId": "usmf", "ChartOfAccounts": ""}],
    ],
)
async def test_missing_or_ambiguous_ledger_disables_account_lookup(ledgers):
    records = account_records()
    records["Ledgers"] = ledgers
    client = Client(records)
    with pytest.raises(AppError):
        await lookup_main_account(client, registry(), "401100", "usmf")
    assert all(entity != "MainAccounts" for entity, _ in client.calls)


async def test_unavailable_public_ledger_cannot_fall_back_to_global_main_account():
    entities = registry()
    del entities.entities["Ledgers"]
    client = Client(account_records())
    with pytest.raises(AppError) as error:
        await lookup_main_account(client, entities, "401100", "usmf")
    assert error.value.code == "D365_CAPABILITY_UNAVAILABLE"
    assert client.calls == []


async def test_read_only_setup_discovery_is_bounded_and_chart_scoped():
    client = Client(account_records())
    info, rows = await list_setup(client, registry(), "main_account", "usmf", top=30)
    assert info.name == "MainAccounts"
    assert rows[0]["MainAccountId"] == "401100"
    assert client.calls[-1] == (
        "MainAccounts",
        {"filter": "ChartOfAccounts eq 'Shared'", "top": 30},
    )


async def test_read_only_setup_discovery_rejects_cross_company_data():
    client = Client({"PaymentTerms": [{"dataAreaId": "demf", "Name": "Net30"}]})
    with pytest.raises(AppError):
        await list_setup(client, registry(), "payment_terms", "usmf")


async def test_revenue_discovery_filters_typed_enum_in_chart_before_limit():
    client = Client(account_records())
    await list_setup(client, registry(), "main_account", "usmf", top=30, type_member="Revenue")
    assert client.calls[-1] == (
        "MainAccounts",
        {
            "filter": "ChartOfAccounts eq 'Shared' and MainAccountType eq "
            "Microsoft.Dynamics.DataEntities.DimensionLedgerAccountType'Revenue'",
            "top": 30,
        },
    )


async def test_customer_payment_discovery_supports_string_type_with_company_scope():
    entities = registry()
    entities.entities["JournalNames"].fields["Type"] = "Edm.String"
    client = Client({"JournalNames": [{"dataAreaId": "usmf", "Name": "CustPay", "Type": "CustPayment"}]})
    await list_setup(client, entities, "journal_name", "usmf", top=20, type_member="CustPayment")
    assert client.calls == [
        (
            "JournalNames",
            {
                "filter": "dataAreaId eq 'usmf' and Type eq 'CustPayment'",
                "top": 20,
                "cross_company": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("role", "member"),
    [("main_account", "Expense"), ("journal_name", "Daily"), ("payment_terms", "CustPayment")],
)
async def test_setup_type_discovery_rejects_unapproved_role_member_combinations(role, member):
    client = Client({})
    with pytest.raises(ValueError):
        await list_setup(client, registry(), role, "usmf", type_member=member)
    assert client.calls == []


@pytest.mark.parametrize("returned_type", ["Expense", None, 1])
async def test_discovered_records_must_match_requested_member(returned_type):
    records = account_records()
    records["MainAccounts"][0]["MainAccountType"] = returned_type
    with pytest.raises(AppError, match="outside the requested Revenue type"):
        await list_setup(Client(records), registry(), "main_account", "usmf", type_member="Revenue")


@pytest.mark.parametrize("top", [0, -1, 101, True, "50"])
async def test_setup_discovery_rejects_unbounded_or_invalid_limits(top):
    client = Client({})
    with pytest.raises(ValueError):
        await list_setup(client, registry(), "payment_terms", "usmf", top=top)
    assert client.calls == []


async def test_upstream_permission_errors_remain_actionable():
    class Denied:
        async def get(self, *args, **kwargs):
            raise AppError("D365_PERMISSION_ERROR", "Permission denied.", status_code=403)

    with pytest.raises(AppError) as error:
        await lookup_setup(Denied(), registry(), "journal_name", "CustPay", "usmf")
    assert error.value.code == "D365_PERMISSION_ERROR"
