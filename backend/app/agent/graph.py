"""One bounded LangGraph assistant with typed, confirmation-gated tools."""

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import (
    READ_TOOLS,
    WRITE_CLARIFICATION_TOOL,
    ToolCallback,
    WriteClarificationArguments,
    build_tools,
    write_clarification_message,
)
from app.llm.azure import create_chat_model

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    iterations: int


async def run_live_agent(
    settings: Any,
    company: str,
    context: list[BaseMessage],
    execute: ToolCallback,
    emit: Emit,
    finance_required: bool = False,
    connection_state: str = "unknown",
) -> str:
    """Stream the graph's public answer while all ERP access stays in typed tools."""
    grounded = False
    proposal_prepared = False
    clarification_message = None

    async def grounded_execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal grounded, proposal_prepared, clarification_message
        if name == WRITE_CLARIFICATION_TOOL:
            parsed = WriteClarificationArguments.model_validate(arguments)
            clarification_message = write_clarification_message(parsed, proposal_prepared=proposal_prepared)
            return {
                "clarification": parsed.model_dump(mode="json"),
                "message": clarification_message,
                "executed": False,
            }
        result = await execute(name, arguments)
        if not result.get("error") and (name in READ_TOOLS or result.get("pending_action")):
            grounded = True
        if not result.get("error") and result.get("pending_action"):
            proposal_prepared = True
        return result

    tools = build_tools(grounded_execute, writes_enabled=settings.d365_write_actions_enabled)
    tools_by_name = {tool.name: tool for tool in tools}
    model = create_chat_model(settings).bind_tools(tools)
    maximum = settings.agent_max_iterations

    async def assistant(state: AgentState) -> dict[str, Any]:
        if clarification_message is not None:
            # Local input questions are safe without granting grounding to model-generated facts.
            return {
                "messages": [
                    AIMessage(
                        content=clarification_message,
                        response_metadata={"grounding_override": True},
                    )
                ],
                "iterations": state["iterations"] + 1,
            }
        if state["iterations"] >= maximum:
            return {
                "messages": [
                    AIMessage(
                        content=(
                            "I reached the safe tool limit for this request. Please narrow the question or retry."
                        ),
                        response_metadata={"grounding_override": True},
                    )
                ]
            }
        grounded_for_round = grounded
        answer = await model.ainvoke(
            state["messages"],
            config={"metadata": {"finance_grounded": grounded_for_round}},
        )
        answer.response_metadata["finance_grounded"] = grounded_for_round
        if finance_required and not grounded and not getattr(answer, "tool_calls", None):
            # Historical chat amounts cannot become current ERP facts without a fresh tool result.
            answer = AIMessage(
                content=(
                    "I couldn't verify current finance data from Dynamics 365 for this request. "
                    "Please retry or ask for a specific customer or invoice."
                ),
                response_metadata={"grounding_override": True},
            )
        return {"messages": [answer], "iterations": state["iterations"] + 1}

    async def tool_execution(state: AgentState) -> dict[str, Any]:
        messages = []
        calls = state["messages"][-1].tool_calls
        clarification = next((call for call in calls if call["name"] == WRITE_CLARIFICATION_TOOL), None)
        for call in calls:
            # Unknown tool names are never interpreted as arbitrary operations.
            tool = tools_by_name.get(call["name"])
            if tool is None:
                output = {
                    "error": {"code": "unsupported_tool", "message": "This operation is not supported."}
                }
            elif clarification is not None and call["id"] != clarification["id"]:
                output = {
                    "error": {
                        "code": "clarification_required",
                        "message": "No operation was prepared while required inputs need clarification.",
                    }
                }
            else:
                try:
                    output = await tool.ainvoke(call["args"])
                except ValidationError as exc:
                    output = {
                        "error": {
                            "code": "invalid_tool_arguments",
                            "message": "The requested operation has missing or invalid input fields.",
                            "fields": [".".join(str(part) for part in item["loc"]) for item in exc.errors()],
                        }
                    }
            messages.append(
                ToolMessage(
                    content=output if isinstance(output, str) else json.dumps(output, default=str),
                    tool_call_id=call["id"],
                    name=call["name"],
                )
            )
        return {"messages": messages}

    def tool_decision(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else "end"

    builder = StateGraph(AgentState)
    builder.add_node("assistant", assistant)
    builder.add_node("tools", tool_execution)
    builder.add_edge(START, "assistant")
    builder.add_conditional_edges("assistant", tool_decision, {"tools": "tools", "end": END})
    builder.add_edge("tools", "assistant")
    graph = builder.compile()
    messages = [
        SystemMessage(
            content=SYSTEM_PROMPT.format(
                company=company.upper(),
                max_iterations=maximum,
                connection_state=connection_state,
            )
        ),
        *context,
    ]
    text_parts: list[str] = []
    async for data in graph.astream(
        {"messages": messages, "iterations": 0},
        stream_mode="updates",
        config={"recursion_limit": maximum * 3 + 5},
    ):
        if "assistant" in data:
            answer = data["assistant"]["messages"][-1]
            # Wait for the whole round: prose preceding a tool call can claim a write
            # succeeded before validation or confirmation. Tool rounds are never public answers.
            if getattr(answer, "tool_calls", None):
                continue
            safe_round = (
                not finance_required
                or answer.response_metadata.get("finance_grounded", False)
                or answer.response_metadata.get("grounding_override", False)
            )
            if safe_round and isinstance(answer.content, str) and answer.content:
                text_parts.append(answer.content)
                await emit("message_delta", {"delta": answer.content})
    return "".join(text_parts)
