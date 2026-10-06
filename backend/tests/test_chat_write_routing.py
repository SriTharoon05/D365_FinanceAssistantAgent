"""Live chat routing with real action persistence and a local ERP test double."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.integrations.d365.finance import D365FinanceService
from app.integrations.d365.mock import MockD365Provider
from app.models.entities import Base, Conversation, LocalProfile, PendingAction
from app.services.chat import ChatService


@pytest.fixture
async def live_write_chat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'writes.db'}")
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
    settings = Settings(_env_file=None, d365_mock_mode=False)
    provider = MockD365Provider(settings)
    finance = D365FinanceService(provider, settings)
    for account in ("TEST-CHAT-002", "TEST-CHAT-003"):
        await finance.execute_mutation("create_customer", {"account": account, "name": "Chat test"})
    await finance.execute_mutation(
        "create_draft_free_text_invoice",
        {
            "account": "TEST-CHAT-003",
            "currency": "INR",
            "external_id": "TEST-INV-001",
            "invoice_date": "2026-10-06",
            "due_date": "2026-10-31",
            "lines": [{"description": "Integration testing", "amount": "100"}],
        },
    )
    runtime = SimpleNamespace(finance=finance, status=lambda: {"status": "connected"})
    service = ChatService(settings, runtime, sessions)

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Complete explicit writes must not rely on model tool selection")

    monkeypatch.setattr("app.services.chat.run_live_agent", forbidden_model)
    yield SimpleNamespace(
        service=service,
        provider=provider,
        sessions=sessions,
        conversation_id=conversation_id,
        owner_id=owner_id,
        forbidden_model=forbidden_model,
    )
    await engine.dispose()


async def ask(chat, message):
    return [
        event
        async for event in chat.service.stream(chat.conversation_id, message, chat.owner_id, "write-test")
    ]


def pending(events):
    assert events[-1][1]["status"] == "completed"
    return next(payload["action"] for name, payload in events if name == "pending_action")


async def confirm(chat, action):
    return await chat.service.actions.confirm(
        action["id"], chat.conversation_id, chat.owner_id, "confirmed-write"
    )


async def create_journal(chat):
    action = await chat.service.actions.propose(
        "create_customer_payment_journal",
        {"description": "Chat integration test"},
        chat.conversation_id,
        chat.owner_id,
        "usmf",
    )
    result = await confirm(chat, action)
    return result["result"]["result"]["identifier"]


@pytest.mark.parametrize("apostrophe", ["’", "'"])
async def test_explicit_invoice_update_prepares_then_applies_only_after_confirmation(
    live_write_chat, apostrophe
):
    chat = live_write_chat
    events = await ask(chat, f"Change TEST-INV-001{apostrophe}s due date to 15 November 2026.")
    action = pending(events)
    assert action["action_type"] == "update_draft_free_text_invoice"
    assert action["proposed_changes"] == {
        "identifier": "TEST-INV-001",
        "due_date": "2026-11-15",
        "company": "usmf",
    }
    assert chat.provider.invoices["TEST-INV-001"]["due_date"] == "2026-10-31"
    result = await confirm(chat, action)
    assert result["status"] == "executed"
    assert chat.provider.invoices["TEST-INV-001"]["due_date"] == "2026-11-15"


async def test_invoice_details_context_resolves_followup_without_reusing_old_facts(
    live_write_chat, monkeypatch
):
    chat = live_write_chat

    async def invoice_reader(settings, company, context, execute, emit, **kwargs):
        await execute("get_invoice_details", {"identifier": "TEST-INV-001"})
        await emit("message_delta", {"delta": "Invoice details retrieved."})

    monkeypatch.setattr("app.services.chat.run_live_agent", invoice_reader)
    await ask(chat, "give details of TEST-INV-001")
    monkeypatch.setattr("app.services.chat.run_live_agent", chat.forbidden_model)
    # The earlier read is only an identifier source: a later posted state blocks the change.
    chat.provider.invoices["TEST-INV-001"]["is_posted"] = True
    events = await ask(chat, "change the due date to 15 November 2026.")
    assert events[-1][1]["status"] == "error"
    assert any(name == "error" and "Posted invoices" in value["message"] for name, value in events)
    assert not any(name == "pending_action" for name, _ in events)
    chat.provider.invoices["TEST-INV-001"]["is_posted"] = False
    action = pending(await ask(chat, "change the due date to 15 November 2026."))
    assert action["proposed_changes"]["identifier"] == "TEST-INV-001"


@pytest.mark.parametrize("journal_phrase", [" to journal", ""])
async def test_payment_line_uses_last_confirmed_journal_and_requires_confirmation(
    live_write_chat, journal_phrase
):
    chat = live_write_chat
    journal = await create_journal(chat)
    action = pending(
        await ask(
            chat,
            f"Add payment line 1{journal_phrase} for TEST-CHAT-002, amount INR 100, "
            "payment date 6 October 2026, reference TEST-PAY-001.",
        )
    )
    changes = action["proposed_changes"]
    assert action["action_type"] == "add_customer_payment_line"
    assert changes["journal_number"] == journal
    assert changes["account"] == "TEST-CHAT-002"
    assert changes["amount"] == "100"
    assert changes["currency"] == "INR"
    assert changes["payment_date"] == "2026-10-06"
    assert changes["reference"] == "TEST-PAY-001"
    assert (journal, 1) not in chat.provider.payment_lines
    await confirm(chat, action)
    assert chat.provider.payment_lines[(journal, 1)]["reference"] == "TEST-PAY-001"


async def test_explicit_journal_overrides_last_confirmed_journal(live_write_chat):
    chat = live_write_chat
    first = await create_journal(chat)
    second = await create_journal(chat)
    assert first != second
    action = pending(
        await ask(
            chat,
            f"Add payment line 1 to journal {first} for TEST-CHAT-002, amount INR 100, "
            "payment date 6 October 2026, reference TEST-PAY-001.",
        )
    )
    assert action["proposed_changes"]["journal_number"] == first


async def test_unconfirmed_journal_does_not_become_a_payment_target(live_write_chat):
    chat = live_write_chat
    await chat.service.actions.propose(
        "create_customer_payment_journal",
        {"description": "Not confirmed"},
        chat.conversation_id,
        chat.owner_id,
        "usmf",
    )
    events = await ask(
        chat,
        "Add payment line 1 to journal for TEST-CHAT-002, amount INR 100, "
        "payment date 6 October 2026, reference TEST-PAY-001.",
    )
    text = "".join(value["delta"] for name, value in events if name == "message_delta")
    assert "Which unposted journal number" in text
    assert "No action was prepared" in text
    assert not any(name in {"pending_action", "error"} for name, _ in events)
    assert chat.provider.payment_lines == {}


async def test_missing_invoice_target_gets_specific_question(live_write_chat):
    events = await ask(live_write_chat, "change the due date to 15 November 2026.")
    text = "".join(value["delta"] for name, value in events if name == "message_delta")
    assert "Which draft invoice ID" in text
    assert "couldn't verify current finance data" not in text
    assert not any(name in {"pending_action", "error"} for name, _ in events)


async def test_confirmed_journal_from_another_company_is_not_used(live_write_chat):
    chat = live_write_chat
    await create_journal(chat)
    async with chat.sessions() as session:
        conversation = await session.get(Conversation, chat.conversation_id)
        conversation.selected_company = "usrt"
        await session.commit()
    events = await ask(
        chat,
        "Add payment line 1 to journal for TEST-CHAT-002, amount INR 100, "
        "payment date 6 October 2026, reference TEST-PAY-001.",
    )
    text = "".join(value["delta"] for name, value in events if name == "message_delta")
    assert "Which unposted journal number" in text
    assert not any(name in {"pending_action", "error"} for name, _ in events)


async def test_latest_invoice_reference_wins_across_reads_and_confirmed_creates(live_write_chat, monkeypatch):
    chat = live_write_chat

    async def read_first_invoice(settings, company, context, execute, emit, **kwargs):
        await execute("get_invoice_details", {"identifier": "TEST-INV-001"})
        await emit("message_delta", {"delta": "Invoice details retrieved."})

    monkeypatch.setattr("app.services.chat.run_live_agent", read_first_invoice)
    await ask(chat, "Show details of TEST-INV-001")
    second = await chat.service.actions.propose(
        "create_draft_free_text_invoice",
        {
            "account": "TEST-CHAT-003",
            "external_id": "TEST-INV-002",
            "invoice_date": "2026-10-06",
            "due_date": "2026-10-31",
            "lines": [{"description": "New invoice", "amount": "100"}],
        },
        chat.conversation_id,
        chat.owner_id,
        "usmf",
    )
    await confirm(chat, second)
    monkeypatch.setattr("app.services.chat.run_live_agent", chat.forbidden_model)
    action = pending(await ask(chat, "Change the due date to 15 November 2026."))
    assert action["proposed_changes"]["identifier"] == "TEST-INV-002"
    monkeypatch.setattr("app.services.chat.run_live_agent", read_first_invoice)
    await ask(chat, "Show details of TEST-INV-001")
    monkeypatch.setattr("app.services.chat.run_live_agent", chat.forbidden_model)
    action = pending(await ask(chat, "Change the due date to 15 November 2026."))
    assert action["proposed_changes"]["identifier"] == "TEST-INV-001"


async def test_invalid_payment_target_surfaces_actual_error_and_no_confirmation(live_write_chat):
    events = await ask(
        live_write_chat,
        "Add payment line 1 to journal MISSING-25135 for TEST-CHAT-002, amount INR 100, "
        "payment date 6 October 2026, reference TEST-PAY-001.",
    )
    assert any(name == "error" and value["code"] == "D365_JOURNAL_NOT_FOUND" for name, value in events)
    assert events[-1][1]["status"] == "error"
    assert not any(name == "pending_action" for name, _ in events)
    async with live_write_chat.sessions() as session:
        assert await session.scalar(select(PendingAction)) is None
