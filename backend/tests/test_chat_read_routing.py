"""Finance read intent regressions, independent of external AI or ERP availability."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.read_routing import format_read_result, resolve_read_request
from app.agent.tools import READ_TOOLS
from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.finance import D365FinanceService
from app.integrations.d365.mock import MockD365Provider
from app.models.entities import Base, Conversation, LocalProfile, Message, PendingAction, ToolRun
from app.services.chat import ChatService


@pytest.fixture
async def read_chat(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reads.db'}")
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
    calls = []
    for name in READ_TOOLS:
        original = getattr(finance, name)

        async def record(*args, _name=name, _original=original, **kwargs):
            calls.append((_name, kwargs))
            return await _original(*args, **kwargs)

        setattr(finance, name, record)
    runtime = SimpleNamespace(finance=finance, status=lambda: {"status": "connected"})
    service = ChatService(settings, runtime, sessions)

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Supported reads must not depend on model tool selection")

    monkeypatch.setattr("app.services.chat.run_live_agent", forbidden_model)
    yield SimpleNamespace(
        service=service,
        sessions=sessions,
        provider=provider,
        calls=calls,
        conversation_id=conversation_id,
        owner_id=owner_id,
    )
    await engine.dispose()


async def ask(chat, message):
    return [
        event
        async for event in chat.service.stream(chat.conversation_id, message, chat.owner_id, "read-test")
    ]


def answer(events):
    return "".join(payload["delta"] for name, payload in events if name == "message_delta")


@pytest.mark.parametrize(
    "message",
    [
        "Show AST-001 payment history as a chart",
        "Show Asterion’s payment history as a chart",
        "Show payment history for customer AST-001",
        "Show AST-001 payments by date as a chart",
    ],
)
async def test_payment_prompt_variants_query_fresh_records_without_ai(read_chat, message):
    events = await ask(read_chat, message)
    assert events[-1][1]["status"] == "completed"
    assert ("get_payment_history", {"account": "AST-001", "company": "usmf"}) in read_chat.calls
    assert "25,000.00" in answer(events)
    assert "couldn't verify" not in answer(events)
    assert any(name == "evidence" for name, _ in events)


async def test_screenshot_history_followup_reuses_intent_but_queries_new_amounts(read_chat):
    await ask(read_chat, "Show AST-001 payment history as a chart")
    read_chat.provider.payment_records[0]["amount"] = "123.45"
    events = await ask(read_chat, "Show AST-001 history as a chart")
    assert events[-1][1]["status"] == "completed"
    assert "123.45" in answer(events)
    assert "25,000" not in answer(events)
    assert len([call for call in read_chat.calls if call[0] == "get_payment_history"]) == 2


async def test_unqualified_history_asks_type_without_guessing(read_chat):
    events = await ask(read_chat, "Show AST-001 history as a chart")
    assert "payment history, open invoices, or a finance summary" in answer(events)
    assert not read_chat.calls


@pytest.mark.parametrize(
    "message,tool",
    [
        ("What is Asterion's outstanding balance?", "get_customer_balance"),
        ("How much does Asterion owe?", "get_customer_balance"),
        ("Show a bar chart of Asterion’s currently overdue invoices.", "get_overdue_invoices"),
        ("Show Asterion’s remaining invoice amounts as a bar chart.", "get_customer_open_transactions"),
        ("Show Asterion’s payments by date as a chart.", "get_payment_history"),
        ("Show Asterion’s overdue invoices as of 5 October 2026 as a bar chart", "get_overdue_invoices"),
        ("Show AST-001 open invoices as a chart", "get_customer_open_transactions"),
        ("Show AST-001 finance summary", "get_finance_summary"),
        ("Show details of customer AST-001", "get_customer"),
        ("Show invoice FTI-00000022 balance", "get_invoice_details"),
    ],
)
async def test_supported_read_families_query_expected_tool(read_chat, message, tool):
    events = await ask(read_chat, message)
    assert events[-1][1]["status"] == "completed"
    assert any(name == tool for name, _ in read_chat.calls)
    assert "couldn't verify" not in answer(events)


async def test_scoped_contextual_reads_and_reminder(read_chat):
    await ask(read_chat, "Show AST-001 balance")
    for message, tool in (
        ("Show this customer’s balance", "get_customer_balance"),
        ("Show the invoices behind that amount", "get_customer_open_transactions"),
        ("Draft a collection reminder for the overdue invoice", "draft_collection_reminder"),
    ):
        before = len(read_chat.calls)
        events = await ask(read_chat, message)
        assert events[-1][1]["status"] == "completed"
        assert (tool, {"account": "AST-001", "company": "usmf"}) in read_chat.calls[before:]


async def test_polite_reminder_and_invoice_details_followup_preserve_identifier(read_chat):
    events = await ask(read_chat, "Can you draft a collection reminder for Asterion")
    assert "Draft only" in answer(events)
    invoice = await ask(read_chat, "Show details of FTI-00000022")
    assert invoice[-1][1]["status"] == "completed"
    followup = await ask(read_chat, "Show invoice details of that invoice")
    assert "FTI-00000022" in answer(followup)
    assert len([call for call in read_chat.calls if call[0] == "get_invoice_details"]) == 2


@pytest.mark.parametrize(
    "query,expected", [("AbsentCompany", "No customers"), ("Solutions", "Which customer account")]
)
async def test_zero_or_multiple_customer_matches_do_not_choose_stale_account(read_chat, query, expected):
    if query == "Solutions":
        read_chat.provider.customers["BLR-001"]["name"] = "Other Solutions"
    await ask(read_chat, "Show AST-001 balance")
    read_chat.calls.clear()
    events = await ask(read_chat, f"Show {query}'s payment history as a chart")
    assert events[-1][1]["status"] == "completed"
    assert expected in answer(events)
    assert all(name == "search_customers" for name, _ in read_chat.calls)


async def test_explicit_unknown_account_keeps_actual_error_and_does_not_use_history(read_chat):
    await ask(read_chat, "Show AST-001 payment history as a chart")
    read_chat.calls.clear()
    events = await ask(read_chat, "Show MISSING-001 payment history as a chart")
    assert any(name == "error" and payload["code"] == "D365_CUSTOMER_NOT_FOUND" for name, payload in events)
    assert events[-1][1]["status"] == "error"
    assert read_chat.calls == [("get_payment_history", {"account": "MISSING-001", "company": "usmf"})]


async def test_verified_empty_payments_are_distinct_from_unavailable(read_chat):
    events = await ask(read_chat, "Show BLR-001 payment history as a chart")
    assert events[-1][1]["status"] == "completed"
    assert "No payment records were returned" in answer(events)
    assert "Unposted payment journal lines" in answer(events)
    assert "couldn't verify" not in answer(events)


@pytest.mark.parametrize("code", ["D365_CAPABILITY_UNAVAILABLE", "d365_connection_error"])
async def test_erp_errors_stay_visible_without_retrying_other_reads(read_chat, code):
    async def unavailable(**kwargs):
        raise AppError(code, "Actual ERP source error", status_code=503)

    read_chat.service.runtime.finance.get_payment_history = unavailable
    events = await ask(read_chat, "Show AST-001 payment history as a chart")
    assert any(
        name == "error" and payload["code"] == code and payload["message"] == "Actual ERP source error"
        for name, payload in events
    )
    assert events[-1][1]["status"] == "error"
    assert not read_chat.calls


async def test_company_switch_does_not_reuse_customer_or_read_intent(read_chat):
    await ask(read_chat, "Show AST-001 payment history as a chart")
    async with read_chat.sessions() as session:
        conversation = await session.get(Conversation, read_chat.conversation_id)
        conversation.selected_company = "usrt"
        await session.commit()
    read_chat.calls.clear()
    events = await ask(read_chat, "Show this customer's payment history as a chart")
    assert "Which customer" in answer(events)
    assert not read_chat.calls
    mismatch = await ask(read_chat, "Show AST-001 payment history in company USMF as a chart")
    assert any(name == "error" and payload["code"] == "company_mismatch" for name, payload in mismatch)
    assert not read_chat.calls


async def test_due_date_cutoff_is_exact_and_not_historical_balance(read_chat):
    events = await ask(read_chat, "Which Asterion invoices are overdue as of 30 September 2026?")
    assert (
        "get_overdue_invoices",
        {"account": "AST-001", "as_of_date": "2026-09-30", "company": "usmf"},
    ) in read_chat.calls
    assert "No matching open records" in answer(events)  # equal due date is not overdue
    assert "does not reconstruct historical" in answer(events)


@pytest.mark.parametrize(
    "message",
    [
        "Show AST-001 payments last month as a chart",
        "Show AST-001 payments only INR as a chart",
        "Show top 5 AST-001 payments",
        "Show Asterion’s INR payment history",
        "Show Asterion’s payment history sorted by amount",
        "Show Asterion’s overdue invoices over INR 10000",
        "Show AST-001 overdue invoices as of 2026-10-05 trailing junk",
    ],
)
async def test_unsupported_modifiers_are_not_silently_discarded(read_chat, message):
    events = await ask(read_chat, message)
    assert events[-1][1]["status"] == "completed"
    assert not read_chat.calls
    assert "supported" in answer(events) or "cutoff" in answer(events)


@pytest.mark.parametrize(
    "message",
    [
        "Show invoice TEST-INV-001 and change its due date to 2026-11-15",
        "Create an invoice for AST-001",
        "Settle AST-001 payments",
        "Post journal 25134",
        "Show AST-001 balance and payments as a chart",
        "Show available revenue accounts",
    ],
)
def test_writes_compound_requests_and_other_tools_do_not_become_single_reads(message):
    assert resolve_read_request(message, {"account": "AST-001"}, "usmf") is None


def test_missing_amounts_are_unavailable_not_zero():
    text = format_read_result(
        "get_invoice_details",
        {"invoice": {"identifier": "FTI-1", "currency": "INR", "is_posted": False}},
        "usmf",
    )
    assert "Unavailable" in text
    assert "0.00" not in text
    payment = format_read_result(
        "get_payment_history", {"payments": [{"payment_date": "2026-10-07"}]}, "usmf"
    )
    assert "Unavailable" in payment
    assert "0.00" not in payment


def test_latest_scoped_customer_wins_across_action_and_read_chronology():
    now = datetime.now(UTC)
    older_action = PendingAction(
        proposed_changes={"account": "AST-001", "company": "usmf"},
        status="executed",
        action_type="update_customer",
        created_at=now - timedelta(minutes=2),
    )
    fresh = ToolRun(
        tool_name="get_payment_history",
        safe_input={"account": "BLR-001", "company": "usmf"},
        safe_output={},
        created_at=now,
    )
    other_company = ToolRun(
        tool_name="get_payment_history",
        safe_input={"account": "NVS-001", "company": "usrt"},
        safe_output={},
        created_at=now + timedelta(minutes=1),
    )
    identifiers = ChatService._identifiers([other_company, fresh], [older_action], "usmf")
    assert identifiers["account"] == "BLR-001"
    assert identifiers["last_read_tool"] == "get_payment_history"


async def test_current_turn_evidence_and_scoped_inputs_are_saved(read_chat):
    await ask(read_chat, "Show AST-001 payment history as a chart")
    async with read_chat.sessions() as session:
        run = await session.scalar(select(ToolRun))
        assert run.safe_input["company"] == "usmf"
        message = await session.scalar(select(Message).where(Message.role == "assistant"))
        assert message.meta["evidence"][0]["account"] == "AST-001"
