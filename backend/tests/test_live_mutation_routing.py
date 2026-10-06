"""Live graph regressions for input clarification and confirmation-only mutations."""

from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent import graph
from app.agent.tools import build_tools


def tool_call(name, arguments, identifier="call-1"):
    return {"name": name, "args": arguments, "id": identifier, "type": "tool_call"}


async def run_graph(monkeypatch, responses, *, output=None, outputs=None, writes_enabled=True):
    bound = []

    class ScriptedModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            bound.extend(tools)
            return self

    model = ScriptedModel(responses=responses)
    monkeypatch.setattr(graph, "create_chat_model", lambda settings: model)
    calls, events = [], []

    async def execute(name, arguments):
        calls.append((name, arguments))
        if outputs is not None:
            return outputs[name]
        return output or {"pending_action": {"id": "confirmation-only"}, "executed": False}

    async def emit(name, payload):
        events.append((name, payload))

    text = await graph.run_live_agent(
        SimpleNamespace(d365_write_actions_enabled=writes_enabled, agent_max_iterations=4),
        "usmf",
        [HumanMessage(content="Prepare the requested draft change.")],
        execute,
        emit,
        finance_required=True,
        connection_state="connected",
    )
    return text, calls, events, model, bound


@pytest.mark.parametrize(
    "operation,missing,expected",
    [
        ("update_draft_free_text_invoice", ["due_date"], ["due date"]),
        (
            "add_customer_payment_line",
            ["line_number", "reference", "payment_date"],
            ["line number", "payment reference", "payment date"],
        ),
    ],
)
async def test_missing_mutation_inputs_get_a_fixed_question_without_erp_facts_or_execution(
    monkeypatch, operation, missing, expected
):
    responses = [
        AIMessage(
            content="Unverified invoice balance INR 999999.",
            tool_calls=[
                tool_call("request_write_clarification", {"operation": operation, "missing_fields": missing})
            ],
        ),
        AIMessage(content="The write succeeded and the payment was posted."),
    ]
    text, calls, events, model, _ = await run_graph(monkeypatch, responses)
    assert all(field in text.lower() for field in expected)
    assert "No action was prepared" in text
    assert "couldn't verify current finance data" not in text
    assert "999999" not in text
    assert "posted" not in text
    assert calls == []
    assert model.i == 1  # The model cannot add arbitrary facts after the local clarification.
    assert text == "".join(payload["delta"] for name, payload in events if name == "message_delta")


async def test_clarification_prevents_proposing_other_mutations_in_the_same_tool_round(monkeypatch):
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                tool_call(
                    "update_draft_free_text_invoice",
                    {"identifier": "TEST-INV-001", "due_date": "2026-10-31"},
                    "propose",
                ),
                tool_call(
                    "request_write_clarification",
                    {"operation": "update_draft_free_text_invoice", "missing_fields": ["due_date"]},
                    "clarify",
                ),
            ],
        ),
        AIMessage(content="The invoice was updated."),
    ]
    text, calls, _, _, _ = await run_graph(monkeypatch, responses)
    assert "due date" in text.lower()
    assert "No action was prepared" in text
    assert calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": "post_customer_payment", "missing_fields": ["reference"]},
        {"operation": "update_draft_free_text_invoice", "missing_fields": ["odata_url"]},
        {"operation": "update_draft_free_text_invoice", "missing_fields": ["journal_number"]},
        {"operation": "update_draft_free_text_invoice", "missing_fields": []},
    ],
)
async def test_invalid_clarification_cannot_bypass_grounding_or_execute(monkeypatch, arguments):
    responses = [
        AIMessage(content="", tool_calls=[tool_call("request_write_clarification", arguments)]),
        AIMessage(content="The invoice was updated and its balance is INR 999999."),
    ]
    text, calls, _, _, _ = await run_graph(monkeypatch, responses)
    assert calls == []
    assert "999999" not in text
    assert "invoice was updated" not in text
    assert "couldn't verify current finance data" in text


@pytest.mark.parametrize(
    "name,arguments",
    [
        (
            "update_draft_free_text_invoice",
            {"identifier": "TEST-INV-001", "due_date": "2026-10-31"},
        ),
        (
            "add_customer_payment_line",
            {
                "journal_number": "25135",
                "account": "TEST-001",
                "amount": "100.00",
                "currency": "INR",
                "line_number": 1,
                "reference": "TEST-PAY-001",
                "payment_date": "2026-10-06",
            },
        ),
    ],
)
async def test_complete_mutation_calls_only_the_existing_confirmation_boundary(monkeypatch, name, arguments):
    responses = [
        AIMessage(content="Unverified successful write.", tool_calls=[tool_call(name, arguments)]),
        AIMessage(content="Review the proposed change and select Confirm."),
    ]
    text, calls, _, _, _ = await run_graph(monkeypatch, responses)
    assert len(calls) == 1 and calls[0][0] == name
    assert "select Confirm" in text
    assert "Unverified successful write" not in text
    assert "couldn't verify" not in text


async def test_unverified_finance_answer_stays_blocked(monkeypatch):
    text, calls, _, _, _ = await run_graph(
        monkeypatch, [AIMessage(content="The invoice balance is INR 999999.")]
    )
    assert calls == []
    assert "999999" not in text
    assert "couldn't verify current finance data" in text


async def test_missing_typed_mutation_argument_can_clarify_without_an_ai_outage(monkeypatch):
    responses = [
        AIMessage(
            content="",
            tool_calls=[tool_call("update_draft_free_text_invoice", {"identifier": "TEST-INV-001"})],
        ),
        AIMessage(
            content="",
            tool_calls=[
                tool_call(
                    "request_write_clarification",
                    {"operation": "update_draft_free_text_invoice", "missing_fields": ["due_date"]},
                    "clarify-2",
                )
            ],
        ),
        AIMessage(content="The invoice was updated."),
    ]
    text, calls, _, _, _ = await run_graph(monkeypatch, responses)
    assert "due date" in text.lower()
    assert "No action was prepared" in text
    assert calls == []
    assert "couldn't verify" not in text


async def test_clarification_does_not_deny_an_existing_confirmation_card(monkeypatch):
    responses = [
        AIMessage(content="", tool_calls=[tool_call("create_customer_payment_journal", {})]),
        AIMessage(
            content="",
            tool_calls=[
                tool_call(
                    "request_write_clarification",
                    {"operation": "add_customer_payment_line", "missing_fields": ["journal_number"]},
                    "clarify-line",
                )
            ],
        ),
        AIMessage(content="The payment was posted."),
    ]
    text, calls, _, _, _ = await run_graph(monkeypatch, responses)
    assert [name for name, _ in calls] == ["create_customer_payment_journal"]
    assert "payment journal number" in text.lower()
    assert "No additional action was prepared" in text
    assert "review the existing confirmation card" in text
    assert "posted" not in text


@pytest.mark.parametrize("clarify", [True, False])
async def test_read_then_tool_round_never_streams_premature_write_success(monkeypatch, clarify):
    next_tool = (
        tool_call(
            "request_write_clarification",
            {"operation": "update_draft_free_text_invoice", "missing_fields": ["due_date"]},
            "clarify-date",
        )
        if clarify
        else tool_call(
            "update_draft_free_text_invoice",
            {"identifier": "TEST-INV-001", "due_date": "2026-10-31"},
            "propose-date",
        )
    )
    responses = [
        AIMessage(
            content="",
            tool_calls=[tool_call("get_invoice_details", {"identifier": "TEST-INV-001"}, "read")],
        ),
        AIMessage(
            content="The invoice was updated successfully. Its balance is INR 999999.",
            tool_calls=[next_tool],
        ),
        AIMessage(content="Review the proposed change and select Confirm."),
    ]
    outputs = {
        "get_invoice_details": {
            "invoice": {"invoice_number": "TEST-INV-001", "due_date": "2026-10-30", "is_posted": False}
        },
        "update_draft_free_text_invoice": {"pending_action": {"id": "confirmation-only"}, "executed": False},
    }
    text, calls, events, _, _ = await run_graph(monkeypatch, responses, outputs=outputs)
    assert "updated successfully" not in text
    assert "999999" not in text
    assert all(
        "updated successfully" not in payload["delta"] and "999999" not in payload["delta"]
        for name, payload in events
        if name == "message_delta"
    )
    if clarify:
        assert [name for name, _ in calls] == ["get_invoice_details"]
        assert "due date" in text.lower()
        assert "No action was prepared" in text
    else:
        assert [name for name, _ in calls] == ["get_invoice_details", "update_draft_free_text_invoice"]
        assert text == "Review the proposed change and select Confirm."


def test_write_clarification_is_not_offered_when_writes_are_disabled():
    async def execute(name, arguments):
        raise AssertionError("No tool is invoked during catalog construction")

    names = {tool.name for tool in build_tools(execute, writes_enabled=False)}
    assert "request_write_clarification" not in names
    assert "update_draft_free_text_invoice" not in names
    assert "add_customer_payment_line" not in names
