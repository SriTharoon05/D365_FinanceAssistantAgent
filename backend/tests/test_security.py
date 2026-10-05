"""Regressions for finance evidence and durable mutation boundaries."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.finance import D365FinanceService
from app.integrations.d365.live import LiveD365Provider
from app.integrations.d365.metadata import D365CapabilityRegistry, D365MetadataResolver, EntityInfo
from app.integrations.d365.mock import MockD365Provider
from app.models.entities import AuditEvent, Base, Conversation, LocalProfile, PendingAction
from app.services.actions import ActionsService
from app.services.chat import ChatService


def transaction_info(name="CustomerTransactions"):
    return EntityInfo(
        name,
        "D365.Transaction",
        fields={
            "CustomerAccount": "Edm.String",
            "dataAreaId": "Edm.String",
            "CurrencyCode": "Edm.String",
            "AmountCur": "Edm.Decimal",
            "InvoiceId": "Edm.String",
            "Voucher": "Edm.String",
            "TransactionType": "D365.TransactionType",
        },
    )


@pytest.mark.parametrize("payment_type", ["Payment", "CustPayment", "D365.TransactionType'Payment'"])
async def test_live_payment_history_recognizes_odata_enums(payment_type):
    """A qualified enum must not silently hide an authoritative payment."""

    class Records:
        async def get(self, entity, **kwargs):
            return [
                {
                    "CustomerAccount": "TEST-1",
                    "dataAreaId": "usmf",
                    "CurrencyCode": "USD",
                    "AmountCur": "-24.50",
                    "InvoiceId": "INV-1",
                    "Voucher": "PAY-1",
                    "TransactionType": payment_type,
                }
            ]

    registry = D365CapabilityRegistry()
    info = transaction_info()
    registry.loaded = True
    registry.entities[info.name] = info
    registry.resolved["customer_transactions"] = info.name
    provider = LiveD365Provider(Settings(_env_file=None), Records(), registry)
    payments = await provider.payments("TEST-1", "usmf")
    assert len(payments) == 1
    assert payments[0]["amount"] == "24.50"
    assert payments[0]["voucher"] == "PAY-1"


def test_open_entity_cannot_establish_absence_of_historical_transactions():
    """Settled history disappears from open entities, so deletion must not use them."""
    resolver = D365MetadataResolver(None, Settings(_env_file=None))
    info = transaction_info("CustomerOpenTransactions")
    assert resolver.score(info, "open_transactions") > 0
    assert resolver.score(info, "customer_transactions") == 0


def test_missing_remaining_amount_never_becomes_zero_balance():
    provider = LiveD365Provider(Settings(_env_file=None), None, D365CapabilityRegistry())
    info = transaction_info("CustomerOpenTransactions")
    with pytest.raises(AppError) as caught:
        provider.normalize_transaction(
            info,
            {"CustomerAccount": "TEST-1", "dataAreaId": "usmf", "CurrencyCode": "USD", "InvoiceId": "INV-1"},
        )
    assert caught.value.code == "D365_FINANCE_DATA_INVALID"


@pytest.fixture
async def confirmed_journal_setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'security.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        owner = LocalProfile()
        session.add(owner)
        await session.flush()
        conversation = Conversation(owner_id=owner.id, selected_company="usmf")
        session.add(conversation)
        await session.commit()
        owner_id, conversation_id = owner.id, conversation.id
    settings = Settings(_env_file=None, d365_mock_mode=True)
    provider = MockD365Provider(settings)
    runtime = SimpleNamespace(finance=D365FinanceService(provider, settings))
    service = ActionsService(settings, runtime, sessions)
    yield service, provider, sessions, conversation_id, owner_id
    await engine.dispose()


async def test_payment_journal_proposal_and_audit_reference_use_real_finance_contract(
    confirmed_journal_setup,
):
    """Use the common service wrapper rather than a simplified fake result shape."""
    service, provider, sessions, conversation_id, owner_id = confirmed_journal_setup
    action = await service.propose(
        "create_customer_payment_journal",
        {"description": "Confirmed test"},
        conversation_id,
        owner_id,
        "usmf",
    )
    assert action["status"] == "pending"
    assert provider.journals == {}
    result = await service.confirm(action["id"], conversation_id, owner_id, "request-journal")
    journal_result = result["result"]["result"]
    number = journal_result.get("identifier") or journal_result.get("journal_number")
    assert number
    async with sessions() as session:
        audit = await session.scalar(select(AuditEvent))
        saved_action = await session.get(PendingAction, action["id"])
        assert audit.d365_reference == number
        assert ChatService._identifiers([], [saved_action])["journal_number"] == number


async def test_partial_invoice_write_requires_verification_before_retry(confirmed_journal_setup, monkeypatch):
    service, provider, sessions, conversation_id, owner_id = confirmed_journal_setup
    action = await service.propose(
        "create_draft_free_text_invoice",
        {
            "account": "AST-001",
            "currency": "INR",
            "external_id": "TEST-PARTIAL-1",
            "due_date": "2026-10-30",
            "lines": [{"description": "Test line", "amount": "50.00"}],
        },
        conversation_id,
        owner_id,
        "usmf",
    )

    async def partial_write(action_type, payload, company):
        raise AppError(
            "D365_PARTIAL_DRAFT_CREATED",
            "The header exists but a line could not be verified.",
            status_code=409,
            details={"identifier": payload["external_id"]},
        )

    monkeypatch.setattr(provider, "execute_mutation", partial_write)
    with pytest.raises(AppError):
        await service.confirm(action["id"], conversation_id, owner_id, "request-partial")
    async with sessions() as session:
        saved_action = await session.get(PendingAction, action["id"])
        audit = await session.scalar(select(AuditEvent))
        assert saved_action.status in {"unknown", "verification_required"}
        assert audit.result_status in {"unknown", "verification_required"}
