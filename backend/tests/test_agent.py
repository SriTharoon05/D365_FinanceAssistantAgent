from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.mock import parse_date
from app.agent.tools import TOOL_SCHEMAS
from app.core.config import Settings
from app.core.errors import AppError
from app.models.entities import Base, Conversation, LocalProfile, Message, ToolRun
from app.services.chat import ChatService


class OfflineFinance:
    def __init__(self):
        self.calls = []
        self.disconnected = False

    async def search_customers(self, query, company=None):
        self.calls.append(("search_customers", query))
        if self.disconnected:
            raise AppError(
                "d365_connection_error",
                "Dynamics 365 is currently unavailable, so I can't safely retrieve the requested finance data. Reconnect to continue.",
                status_code=503,
            )
        customers = [{"account": "AST-001", "name": "Asterion", "company": "usmf", "currency": "INR"}]
        if query.lower() == "ambiguous":
            customers.append(
                {"account": "AST-002", "name": "Asterion 2", "company": "usmf", "currency": "INR"}
            )
        return {"customers": customers, "evidence": []}

    async def get_customer_balance(self, account, company=None):
        self.calls.append(("get_customer_balance", account))
        if self.disconnected:
            raise AppError(
                "d365_connection_error",
                "Dynamics 365 is currently unavailable. Reconnect to continue.",
                status_code=503,
            )
        return {
            "customer": {"account": account, "name": "Asterion"},
            "totals_by_currency": {"INR": "42.50"},
            "transactions": [
                {
                    "invoice_number": "FTI-001",
                    "currency": "INR",
                    "original_amount": "50.00",
                    "remaining_amount": "42.50",
                    "due_date": "2026-09-30",
                }
            ],
            "evidence": [
                {
                    "kind": "invoice",
                    "company": "usmf",
                    "currency": "INR",
                    "account": account,
                    "remaining_amount": "42.50",
                    "source_entity": "MockOpen",
                    "retrieved_at": "2026-10-05T00:00:00Z",
                }
            ],
        }

    async def get_customer_open_transactions(self, account, company=None):
        self.calls.append(("get_customer_open_transactions", account))
        return await self.get_customer_balance(account, company)

    async def get_overdue_invoices(self, account, as_of_date=None, company=None):
        self.calls.append(("get_overdue_invoices", account, as_of_date))
        balance = await self.get_customer_balance(account, company)
        return {**balance, "as_of_date": as_of_date, "invoices": balance["transactions"]}


@pytest.fixture
async def chat_setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'chat.db'}")
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
    finance = OfflineFinance()
    settings = Settings(_env_file=None, d365_mock_mode=True)
    service = ChatService(settings, SimpleNamespace(finance=finance), sessions)
    yield service, finance, sessions, conversation_id, owner_id
    await engine.dispose()


async def turn(setup, message):
    service, _, _, conversation_id, owner_id = setup
    return [event async for event in service.stream(conversation_id, message, owner_id, "request-id")]


async def test_mock_routing_persists_turn_evidence_and_followups(chat_setup):
    _, finance, sessions, conversation_id, _ = chat_setup
    first = await turn(chat_setup, "What is the outstanding balance for Asterion?")
    assert first[0][0] == "message_start"
    assert first[-1][1]["status"] == "completed"
    assert any(name == "evidence" for name, _ in first)
    content = "".join(payload["delta"] for name, payload in first if name == "message_delta")
    assert "Mock data" in content
    assert "42.50" in content
    assert "110,000" not in content  # render provider facts, never acceptance constants
    await turn(chat_setup, "Show me the invoices behind that amount.")
    assert ("get_customer_open_transactions", "AST-001") in finance.calls
    async with sessions() as session:
        messages = list(
            (await session.scalars(select(Message).where(Message.conversation_id == conversation_id))).all()
        )
        tools = list((await session.scalars(select(ToolRun))).all())
    assert len(messages) == 4
    assert all(message.status == "completed" for message in messages)
    assert len(tools) >= 3
    assert any(message.meta.get("evidence") for message in messages if message.role == "assistant")


async def test_disconnected_does_not_invent_financial_answer(chat_setup):
    _, finance, sessions, _, _ = chat_setup
    finance.disconnected = True
    events = await turn(chat_setup, "What is the outstanding balance for Asterion?")
    errors = [payload for name, payload in events if name == "error"]
    assert errors[0]["code"] == "d365_connection_error"
    assert events[-1][1]["status"] == "error"
    assert not any(name == "evidence" for name, _ in events)
    async with sessions() as session:
        answer = await session.scalar(select(Message).where(Message.role == "assistant"))
    assert "unavailable" in answer.content
    assert "110000" not in answer.content


async def test_ambiguous_customer_requires_clarification(chat_setup):
    _, finance, _, _, _ = chat_setup
    events = await turn(chat_setup, "What is the outstanding balance for ambiguous?")
    content = "".join(payload["delta"] for name, payload in events if name == "message_delta")
    assert "Which customer account" in content
    assert not any(call[0] == "get_customer_balance" for call in finance.calls)


async def test_missing_azure_key_in_live_mode_saves_user_and_allows_retry(chat_setup):
    service, finance, sessions, conversation_id, owner_id = chat_setup
    service.settings.d365_mock_mode = False
    events = await turn(chat_setup, "What is the outstanding balance for Asterion?")
    assert any(name == "error" and payload["code"] == "azure_openai_unavailable" for name, payload in events)
    assert not finance.calls
    async with sessions() as session:
        messages = list((await session.scalars(select(Message))).all())
    assert len(messages) == 2
    assert next(item for item in messages if item.role == "user").content.startswith("What is")
    assistant = next(item for item in messages if item.role == "assistant")
    assert assistant.status == "error"
    retried = [
        event async for event in service.stream(conversation_id, "", owner_id, "retry-id", assistant.id)
    ]
    assert retried[-1][1]["status"] == "error"
    async with sessions() as session:
        users = list((await session.scalars(select(Message).where(Message.role == "user"))).all())
    assert len(users) == 1


async def test_explicit_overdue_date_routes_to_backend(chat_setup):
    _, finance, _, _, _ = chat_setup
    await turn(chat_setup, "Which Asterion invoices are overdue as of 5 October 2026?")
    assert ("get_overdue_invoices", "AST-001", "2026-10-05") in finance.calls


async def test_invoice_sample_placeholder_gets_actionable_help_without_erp_claim(chat_setup):
    _, finance, _, _, _ = chat_setup
    events = await turn(
        chat_setup,
        "Create an unposted invoice for TEST-002, INR 100, using revenue account [verified account].",
    )
    text = "".join(payload["delta"] for name, payload in events if name == "message_delta")
    assert "general-ledger main account" in text
    assert "configured revenue account" in text
    assert "No action was prepared" in text
    assert "couldn't verify current finance data" not in text
    assert events[-1][1]["status"] == "completed"
    assert finance.calls == []
    assert not any(name in {"pending_action", "error"} for name, _ in events)


def test_tool_schema_rejects_arbitrary_odata_and_dates_are_not_guessed():
    with pytest.raises(ValueError):
        TOOL_SCHEMAS["get_customer_balance"].model_validate({"account": "AST-001", "$filter": "1 eq 1"})
    assert parse_date("30 October 2026") == "2026-10-30"
    assert parse_date("tomorrow") is None


async def test_full_mock_mutation_context_and_retry_safety(chat_setup):
    from app.integrations.d365.runtime import D365Runtime
    from app.models.entities import PendingAction
    from app.services.actions import ActionsService

    service, _, sessions, conversation_id, owner_id = chat_setup
    runtime = D365Runtime(service.settings)
    await runtime.start()
    service.runtime = runtime
    service.actions = ActionsService(service.settings, runtime, sessions)

    async def ask(text):
        return [event async for event in service.stream(conversation_id, text, owner_id, "flow")]

    async def confirm_card(events):
        card = next(payload["action"] for name, payload in events if name == "pending_action")
        return await service.actions.confirm(card["id"], conversation_id, owner_id, "confirmed")

    try:
        balance = await ask("What is the outstanding balance for Asterion?")
        assert balance[-1][1]["status"] == "completed"
        followup = await ask("Show me the invoices behind that amount.")
        assert any(
            name == "tool_start" and payload["name"] == "get_customer_open_transactions"
            for name, payload in followup
        )
        reminder = await ask("Draft a collection reminder for the overdue invoice.")
        reminder_text = "".join(payload["delta"] for name, payload in reminder if name == "message_delta")
        assert "Dear Asterion" in reminder_text
        assert "Draft only" in reminder_text
        invoice = await ask("Create a draft INR 5,000 invoice for AST-001 due on 30 October 2026.")
        result = await confirm_card(invoice)
        identifier = result["result"]["result"]["identifier"]
        update = await ask("Change the due date of that draft invoice.")
        assert not any(name == "pending_action" for name, _ in update)
        update_with_date = await ask("31 October 2026")
        updated = await confirm_card(update_with_date)
        assert updated["result"]["result"]["invoice"]["due_date"] == "2026-10-31"
        assert updated["result"]["result"]["identifier"] == identifier
        journal_events = await ask("Create a INR 10,000 customer payment journal for AST-001.")
        journal = await confirm_card(journal_events)
        journal_number = journal["result"]["result"]["identifier"]
        line_events = await ask("Add payment line 1 for AST-001 INR 10,000.")
        line = next(payload["action"] for name, payload in line_events if name == "pending_action")
        assert line["proposed_changes"]["journal_number"] == journal_number
        await confirm_card(line_events)
        assistant_id = journal_events[0][1]["message_id"]
        retry = [
            event async for event in service.stream(conversation_id, "", owner_id, "retry", assistant_id)
        ]
        retry_text = "".join(payload["delta"] for name, payload in retry if name == "message_delta")
        assert "already confirmed and executed" in retry_text
        async with sessions() as session:
            journals = list(
                (
                    await session.scalars(
                        select(PendingAction).where(
                            PendingAction.action_type == "create_customer_payment_journal",
                        )
                    )
                ).all()
            )
        assert len(journals) == 1
    finally:
        await runtime.close()


async def test_restart_verification_required_retry_does_not_make_another_proposal(chat_setup):
    from datetime import UTC, datetime, timedelta

    from app.models.entities import PendingAction

    service, finance, sessions, conversation_id, owner_id = chat_setup
    async with sessions() as session:
        action = PendingAction(
            conversation_id=conversation_id,
            action_type="create_customer",
            title="Create test",
            description="Test",
            entity="CustomersV3",
            target_identifier="TEST-001",
            proposed_changes={"account": "TEST-001", "name": "Test", "company": "usmf"},
            risk_level="low",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            status="verification_required",
        )
        session.add(action)
        user = Message(conversation_id=conversation_id, role="user", content="Create TEST-001 customer.")
        session.add(user)
        await session.flush()
        assistant = Message(
            conversation_id=conversation_id,
            role="assistant",
            content="Confirm the action.",
            parent_message_id=user.id,
            meta={"pending_actions": [{"id": action.id}]},
        )
        session.add(assistant)
        await session.commit()
        assistant_id = assistant.id
    events = [event async for event in service.stream(conversation_id, "", owner_id, "retry", assistant_id)]
    text = "".join(payload["delta"] for name, payload in events if name == "message_delta")
    assert "unknown outcome" in text
    assert not finance.calls


async def test_stopped_stream_persists_partial_answer(chat_setup):
    service, _, sessions, conversation_id, owner_id = chat_setup
    stream = service.stream(
        conversation_id, "What is the outstanding balance for Asterion?", owner_id, "stop"
    )
    first = await anext(stream)
    async for name, _ in stream:
        if name == "message_delta":
            break
    await stream.aclose()
    async with sessions() as session:
        answer = await session.get(Message, first[1]["message_id"])
    assert answer.status in {"interrupted", "completed"}
    assert answer.content
    assert answer.meta["evidence"]


async def test_live_langgraph_routes_typed_tools_and_stops_at_limit(chat_setup, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from app.agent import graph

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    service, finance, _, _, _ = chat_setup
    service.settings.d365_mock_mode = False
    service.settings.azure_openai_api_key = "test-not-a-real-secret"
    service.settings.agent_max_iterations = 2
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_customer_balance",
                    "args": {"account": "AST-001"},
                    "id": "tool-1",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_customer_balance",
                    "args": {"account": "AST-001"},
                    "id": "tool-2",
                    "type": "tool_call",
                }
            ],
        ),
    ]
    monkeypatch.setattr(graph, "create_chat_model", lambda settings: ToolModel(responses=responses))
    events = await turn(chat_setup, "Show AST-001 balance.")
    text = "".join(payload["delta"] for name, payload in events if name == "message_delta")
    assert "safe tool limit" in text
    assert events[-1][1]["status"] == "completed"
    assert len([call for call in finance.calls if call[0] == "get_customer_balance"]) == 2


async def test_no_records_keeps_mock_integration_connected(chat_setup):
    from app.integrations.d365.runtime import D365Runtime
    from app.services.actions import ActionsService

    service, _, sessions, _, _ = chat_setup
    runtime = D365Runtime(service.settings)
    await runtime.start()
    service.runtime = runtime
    service.actions = ActionsService(service.settings, runtime, sessions)
    try:
        events = await turn(chat_setup, "Show MISSING-001 outstanding balance.")
        assert any(name == "error" and "NOT_FOUND" in payload["code"] for name, payload in events)
        assert any(
            name == "integration_status" and payload["status"] == "connected" for name, payload in events
        )
        assert runtime.status()["status"] == "connected"
    finally:
        await runtime.close()


async def test_disconnected_live_finance_never_invokes_model_even_with_cached_context(
    chat_setup, monkeypatch
):
    service, _, _, _, _ = chat_setup
    await turn(chat_setup, "What is the outstanding balance for Asterion?")
    service.settings.d365_mock_mode = False
    service.settings.azure_openai_api_key = "test-not-a-real-secret"
    service.runtime.status = lambda: {"status": "disconnected"}

    async def forbidden(*args, **kwargs):
        raise AssertionError("The model must not see a disconnected financial request")

    monkeypatch.setattr("app.services.chat.run_live_agent", forbidden)
    events = await turn(chat_setup, "What about that amount?")
    assert any(name == "error" and payload["code"] == "d365_connection_error" for name, payload in events)
    assert not any(name == "message_delta" for name, _ in events)
    assert events[-1][1]["status"] == "error"


async def test_disconnected_general_help_strips_historical_erp_context(chat_setup, monkeypatch):
    service, _, _, _, _ = chat_setup
    await turn(chat_setup, "What is the outstanding balance for Asterion?")
    service.settings.d365_mock_mode = False
    service.settings.azure_openai_api_key = "test-not-a-real-secret"
    service.runtime.status = lambda: {"status": "disconnected"}
    captured = []

    async def help_agent(settings, company, context, execute, emit, **kwargs):
        captured.extend(context)
        assert kwargs["connection_state"] == "disconnected"
        await emit("message_delta", {"delta": "Hello. I can explain how to reconnect."})

    monkeypatch.setattr("app.services.chat.run_live_agent", help_agent)
    events = await turn(chat_setup, "Hello")
    assert events[-1][1]["status"] == "completed"
    assert len(captured) == 1
    assert captured[0].content == "Hello"


async def test_connected_model_cannot_answer_finance_without_fresh_tools(chat_setup, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from app.agent import graph

    class UngroundedModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    service, finance, _, _, _ = chat_setup
    service.settings.d365_mock_mode = False
    service.settings.azure_openai_api_key = "test-not-a-real-secret"
    service.runtime.status = lambda: {"status": "connected"}
    model = UngroundedModel(responses=[AIMessage(content="Asterion has INR 999999 outstanding in USMF.")])
    monkeypatch.setattr(graph, "create_chat_model", lambda settings: model)
    events = await turn(chat_setup, "What is the outstanding balance for Asterion?")
    text = "".join(payload["delta"] for name, payload in events if name == "message_delta")
    assert "999999" not in text
    assert "couldn't verify current finance data" in text
    assert not finance.calls


async def test_live_stream_emits_only_after_fresh_tool_grounding(chat_setup, monkeypatch):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from app.agent import graph

    class GroundedModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    service, _, _, _, _ = chat_setup
    service.settings.d365_mock_mode = False
    service.settings.azure_openai_api_key = "test-not-a-real-secret"
    model = GroundedModel(
        responses=[
            AIMessage(
                content="Unverified INR 999999.",
                tool_calls=[
                    {
                        "name": "get_customer_balance",
                        "args": {"account": "AST-001"},
                        "id": "fresh-balance",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Verified balance: INR 42.50 in USMF."),
        ]
    )
    monkeypatch.setattr(graph, "create_chat_model", lambda settings: model)
    events = await turn(chat_setup, "Show AST-001 outstanding balance.")
    text = "".join(payload["delta"] for name, payload in events if name == "message_delta")
    assert "999999" not in text
    assert "Verified balance: INR 42.50" in text
    first_delta = next(index for index, (name, _) in enumerate(events) if name == "message_delta")
    first_tool_result = next(index for index, (name, _) in enumerate(events) if name == "tool_result")
    assert first_tool_result < first_delta


async def test_frontend_suggested_balance_prompt_resolves_customer(chat_setup):
    _, finance, _, _, _ = chat_setup
    events = await turn(chat_setup, "What is Asterion's outstanding balance?")
    assert ("search_customers", "Asterion") in finance.calls
    assert ("get_customer_balance", "AST-001") in finance.calls
    assert events[-1][1]["status"] == "completed"
