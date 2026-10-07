from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import run_live_agent
from app.agent.grounding import requires_finance_grounding


@pytest.mark.parametrize(
    "message",
    [
        "What is settlement?",
        "What does settlement mean?",
        "Explain a customer payment journal.",
        "Define revenue account",
        "What is a free-text invoice?",
        "What is posting?",
    ],
)
def test_exact_definitions_do_not_require_current_erp_records(message):
    assert not requires_finance_grounding(message, {"account": "AST-001"})


@pytest.mark.parametrize(
    "message",
    [
        "Explain Asterion's balance",
        "What is the balance for AST-001?",
        "What is invoice FTI-00000022?",
        "Explain the settlement of AST-PAY-001",
        "Explain settlement and show AST-001 payments",
        "What about that?",
    ],
)
def test_record_specific_explanations_and_followups_require_fresh_reads(message):
    assert requires_finance_grounding(message, {"account": "AST-001"})


async def test_live_definition_answer_is_not_replaced_with_a_finance_outage(monkeypatch):
    answer = "Settlement matches a payment with an invoice using Dynamics 365 business rules."

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, config):
            return AIMessage(content=answer)

    monkeypatch.setattr("app.agent.graph.create_chat_model", lambda settings: Model())
    events = []

    async def execute(name, arguments):
        pytest.fail("A general definition should not need ERP data.")

    async def emit(event, payload):
        events.append((event, payload))

    message = "What does settlement mean?"
    result = await run_live_agent(
        SimpleNamespace(d365_write_actions_enabled=False, agent_max_iterations=3),
        "usmf",
        [HumanMessage(content=message)],
        execute,
        emit,
        finance_required=requires_finance_grounding(message),
        connection_state="disconnected",
    )
    assert result == answer
    assert events == [("message_delta", {"delta": answer})]
