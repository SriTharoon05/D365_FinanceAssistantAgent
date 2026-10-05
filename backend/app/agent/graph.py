"""One bounded LangGraph assistant with typed, confirmation-gated tools."""

import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import READ_TOOLS, ToolCallback, build_tools
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

    async def grounded_execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal grounded
        result = await execute(name, arguments)
        if not result.get("error") and (name in READ_TOOLS or result.get("pending_action")):
            grounded = True
        return result

    tools = build_tools(grounded_execute, writes_enabled=settings.d365_write_actions_enabled)
    tools_by_name = {tool.name: tool for tool in tools}
    model = create_chat_model(settings).bind_tools(tools)
    maximum = settings.agent_max_iterations

    async def assistant(state: AgentState) -> dict[str, Any]:
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
        for call in state["messages"][-1].tool_calls:
            # Unknown tool names are never interpreted as arbitrary operations.
            tool = tools_by_name.get(call["name"])
            if tool is None:
                output = {
                    "error": {"code": "unsupported_tool", "message": "This operation is not supported."}
                }
            else:
                output = await tool.ainvoke(call["args"])
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
    streamed_round = False
    async for mode, data in graph.astream(
        {"messages": messages, "iterations": 0},
        stream_mode=["messages", "updates"],
        config={"recursion_limit": maximum * 3 + 5},
    ):
        if mode == "messages":
            chunk, metadata = data
            if (
                metadata.get("langgraph_node") == "assistant"
                and isinstance(chunk.content, str)
                and chunk.content
                and (not finance_required or metadata.get("finance_grounded", False))
            ):
                streamed_round = True
                text_parts.append(chunk.content)
                await emit("message_delta", {"delta": chunk.content})
        elif mode == "updates" and "assistant" in data:
            answer = data["assistant"]["messages"][-1]
            # Also supports models returning a non-streamed response and the bounded terminal node.
            safe_round = (
                not finance_required
                or answer.response_metadata.get("finance_grounded", False)
                or answer.response_metadata.get("grounding_override", False)
            )
            if not streamed_round and safe_round and isinstance(answer.content, str) and answer.content:
                text_parts.append(answer.content)
                await emit("message_delta", {"delta": answer.content})
            streamed_round = False
    return "".join(text_parts)
