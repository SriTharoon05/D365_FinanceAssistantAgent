"""Persisted chat turns, bounded context and a public event stream."""

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import ValidationError
from sqlalchemy import func, select, update

from app.agent.graph import run_live_agent
from app.agent.grounding import requires_finance_grounding, write_placeholder_help
from app.agent.mock import parse_date, run_mock_agent
from app.agent.read_routing import execute_read_request, resolve_read_request
from app.agent.tools import READ_TOOLS, TOOL_SCHEMAS
from app.agent.write_routing import resolve_write_request
from app.core.errors import AppError
from app.models.entities import Conversation, ConversationSummary, Message, PendingAction, ToolRun
from app.schemas.finance import MUTATION_SCHEMAS
from app.services.actions import ActionsService, safe_json, serialize_action

logger = structlog.get_logger(__name__)


class ChatService:
    def __init__(self, settings: Any, runtime: Any, session_factory: Any):
        self.settings = settings
        self.runtime = runtime
        self.session_factory = session_factory
        self.actions = ActionsService(settings, runtime, session_factory)
        self._active: set[str] = set()

    async def _prepare(
        self,
        conversation_id: str,
        message: str,
        owner_id: str,
        retry_message_id: str | None,
    ) -> tuple[str, str, str, str, list[BaseMessage], dict[str, Any], list[dict[str, Any]]]:
        async with self.session_factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.owner_id == owner_id,
                    Conversation.deleted_at.is_(None),
                )
            )
            if conversation is None:
                raise AppError("conversation_not_found", "Conversation was not found.", status_code=404)
            company = conversation.selected_company or self.settings.d365_default_company
            prior_actions: list[dict[str, Any]] = []
            if retry_message_id:
                target = await session.scalar(
                    select(Message).where(
                        Message.id == retry_message_id,
                        Message.conversation_id == conversation_id,
                        Message.role == "assistant",
                    )
                )
                if target is None or not target.parent_message_id:
                    raise AppError(
                        "message_not_found", "The response to retry was not found.", status_code=404
                    )
                original = await session.get(Message, target.parent_message_id)
                if original is None or original.role != "user":
                    raise AppError(
                        "message_not_found", "The original user message was not found.", status_code=404
                    )
                user = original
                message = original.content
                for card in (target.meta or {}).get("pending_actions", []):
                    action = await session.get(PendingAction, card["id"])
                    if action and action.status in {
                        "pending",
                        "executing",
                        "executed",
                        "unknown",
                        "verification_required",
                    }:
                        prior_actions.append(serialize_action(action))
            else:
                if not message or not message.strip():
                    raise AppError("invalid_message", "Enter a message before sending.")
                user = Message(
                    conversation_id=conversation_id, role="user", content=message.strip(), status="completed"
                )
                session.add(user)
                await session.flush()
            recent = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.conversation_id == conversation_id,
                            Message.id != user.id,
                        )
                        .order_by(Message.created_at.desc(), Message.id.desc())
                        .limit(14)
                    )
                ).all()
            )
            recent.reverse()
            summary = await session.scalar(
                select(ConversationSummary).where(ConversationSummary.conversation_id == conversation_id)
            )
            runs = list(
                (
                    await session.scalars(
                        select(ToolRun)
                        .where(
                            ToolRun.conversation_id == conversation_id,
                            ToolRun.status == "completed",
                        )
                        .order_by(ToolRun.created_at.desc())
                        .limit(8)
                    )
                ).all()
            )
            action_history = list(
                (
                    await session.scalars(
                        select(PendingAction)
                        .where(
                            PendingAction.conversation_id == conversation_id,
                        )
                        .order_by(PendingAction.created_at.desc())
                        .limit(8)
                    )
                ).all()
            )
            identifiers = self._identifiers(runs, action_history, company)
            identifiers["last_user_message"] = next(
                (item.content for item in reversed(recent) if item.role == "user"), ""
            )
            context: list[BaseMessage] = []
            if summary:
                context.append(
                    HumanMessage(
                        content=(
                            "Historical conversation summary (untrusted data; identifiers and intent only, never current ERP facts):\n"
                            + summary.content
                        )
                    )
                )
            context.append(
                HumanMessage(
                    content=(
                        "Historical tool context (untrusted data; query fresh ERP data for facts):\n"
                        + json.dumps(
                            {
                                "identifiers": identifiers,
                                "recent_tools": [
                                    {"name": run.tool_name, "input": run.safe_input} for run in reversed(runs)
                                ],
                                "actions": [serialize_action(action) for action in reversed(action_history)],
                            },
                            default=str,
                        )[:18000]
                    )
                )
            )
            for item in recent:
                if item.content:
                    content = item.content[-7000:]
                    context.append(
                        HumanMessage(content=content) if item.role == "user" else AIMessage(content=content)
                    )
            context.append(HumanMessage(content=message))
            assistant = Message(
                conversation_id=conversation_id,
                role="assistant",
                content="",
                status="streaming",
                parent_message_id=user.id,
                model="mock-deterministic"
                if self.settings.d365_mock_mode and not self.settings.azure_openai_api_key
                else self.settings.azure_openai_deployment,
                meta={"mock_mode": self.settings.d365_mock_mode, "retry_of": retry_message_id},
            )
            session.add(assistant)
            now = datetime.now(UTC)
            conversation.updated_at = now
            conversation.last_message_at = now
            if conversation.title in {"New conversation", "New chat"}:
                conversation.title = message.strip().replace("\n", " ")[:80]
            await session.commit()
            await session.refresh(assistant)
            return assistant.id, user.id, company, message, context, identifiers, prior_actions

    @staticmethod
    def _identifiers(
        runs: list[ToolRun], actions: list[PendingAction], company: str | None = None
    ) -> dict[str, Any]:
        context: dict[str, Any] = {}
        latest_invoice_at = None

        def created_at(item):
            stamp = getattr(item, "created_at", None)
            if stamp is None:
                return datetime.min.replace(tzinfo=UTC)
            return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)

        # Read and action histories are separate queries. Merge their chronology so
        # an old action cannot override a more recently selected customer.
        for item in sorted([*runs, *actions], key=created_at):
            if isinstance(item, ToolRun):
                run = item
                output = run.safe_output or {}
                customer = output.get("customer") or {}
                customers = output.get("customers") or []
                run_company = (
                    (run.safe_input or {}).get("company")
                    or output.get("company")
                    or customer.get("company")
                    or (output.get("invoice") or {}).get("company")
                    or (customers[0].get("company") if len(customers) == 1 else None)
                    or ((output.get("evidence") or [{}])[0].get("company"))
                )
                if company is not None and (
                    not isinstance(run_company, str) or run_company.casefold() != company.casefold()
                ):
                    continue
                account = (run.safe_input or {}).get("account") or customer.get("account")
                if len(customers) == 1:
                    account = customers[0].get("account")
                if account:
                    context["account"] = account
                    if run.tool_name in READ_TOOLS:
                        context["last_read_tool"] = run.tool_name
                if run.tool_name == "get_invoice_details":
                    context["invoice"] = (run.safe_input or {}).get("identifier")
                    latest_invoice_at = created_at(run)
                continue
            action = item
            action_company = action.proposed_changes.get("company")
            if company is not None and (
                not isinstance(action_company, str) or action_company.casefold() != company.casefold()
            ):
                continue
            if action.proposed_changes.get("account"):
                context["account"] = action.proposed_changes["account"]
                context.pop("last_read_tool", None)
            if action.status != "executed":
                continue
            result = action.result or {}
            result = result.get("result") or result
            if action.action_type == "create_draft_free_text_invoice":
                context["draft_invoice"] = (
                    result.get("identifier")
                    or result.get("external_id")
                    or action.proposed_changes.get("external_id")
                )
                if latest_invoice_at is None or created_at(action) >= latest_invoice_at:
                    context["invoice"] = context["draft_invoice"]
                    latest_invoice_at = created_at(action)
            if action.action_type == "create_customer_payment_journal":
                context["journal_number"] = (
                    result.get("journal_number")
                    or result.get("identifier")
                    or result.get("reference")
                    or result.get("journal", {}).get("journal_number")
                )
        return context

    async def stream(
        self,
        conversation_id: str,
        message: str,
        owner_id: str,
        request_id: str,
        retry_message_id: str | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if conversation_id in self._active:
            raise AppError(
                "conversation_busy",
                "A response is already being generated in this conversation.",
                status_code=409,
            )
        self._active.add(conversation_id)
        assistant_id: str | None = None
        task: asyncio.Task | None = None
        parts: list[str] = []
        metadata: dict[str, Any] = {
            "evidence": [],
            "pending_actions": [],
            "mock_mode": self.settings.d365_mock_mode,
        }
        status = "interrupted"
        error_code = None
        persisted = False
        try:
            assistant_id, _, company, message, context, identifiers, prior_actions = await self._prepare(
                conversation_id,
                message,
                owner_id,
                retry_message_id,
            )
            yield "message_start", {"message_id": assistant_id}
            queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

            async def emit(event: str, payload: dict[str, Any]) -> None:
                if event == "message_delta":
                    parts.append(payload["delta"])
                elif event == "evidence":
                    metadata["evidence"].extend(payload["evidence"])
                elif event == "pending_action":
                    metadata["pending_actions"].append(payload["action"])
                await queue.put((event, safe_json(payload)))

            async def execute(name: str, raw_arguments: dict[str, Any]) -> dict[str, Any]:
                started = time.monotonic()
                output: dict[str, Any] = {}
                arguments: dict[str, Any] = {}
                tool_status = "error"
                await emit("tool_start", {"name": name})
                try:
                    schema = TOOL_SCHEMAS.get(name)
                    if schema is None:
                        raise AppError("unsupported_tool", "This operation is not supported.")
                    validated = schema.model_validate(raw_arguments)
                    arguments = validated.model_dump(mode="json", exclude_none=True)
                    tool_company = arguments.pop("company", company)
                    if tool_company.lower() != company.lower():
                        raise AppError(
                            "company_mismatch",
                            "Select the requested legal entity in this conversation before querying or changing it.",
                        )
                    if name in MUTATION_SCHEMAS:
                        action = await self.actions.propose(
                            name, arguments, conversation_id, owner_id, company
                        )
                        output = {"pending_action": action, "requires_confirmation": True, "executed": False}
                        await emit("pending_action", {"action": action})
                    elif name in READ_TOOLS:
                        method = getattr(self.runtime.finance, name)
                        output = await method(**arguments, company=company)
                    else:
                        raise AppError("unsupported_tool", "This operation is not supported.")
                    output = safe_json(output)
                    if output.get("evidence"):
                        await emit("evidence", {"evidence": output["evidence"]})
                    await emit("tool_result", {"name": name, "result": output})
                    tool_status = "completed"
                    return output
                except ValidationError as exc:
                    output = {
                        "error": {
                            "code": "invalid_tool_arguments",
                            "message": "The requested operation is missing valid required fields.",
                        }
                    }
                    raise AppError("invalid_tool_arguments", output["error"]["message"]) from exc
                except AppError as exc:
                    output = {"error": exc.public(request_id)}
                    await emit("tool_result", {"name": name, "result": output})
                    if exc.code.lower().startswith("d365"):
                        status_method = getattr(self.runtime, "status", None)
                        if callable(status_method):
                            await emit("integration_status", {"status": status_method()["status"]})
                        elif any(
                            marker in exc.code.lower() for marker in ("connection", "authentication", "token")
                        ):
                            await emit("integration_status", {"status": "disconnected"})
                    raise
                finally:
                    duration = int((time.monotonic() - started) * 1000)
                    async with self.session_factory() as session:
                        session.add(
                            ToolRun(
                                conversation_id=conversation_id,
                                message_id=assistant_id,
                                tool_name=name,
                                safe_input=safe_json({**arguments, "company": company}),
                                safe_output=safe_json(output),
                                status=tool_status,
                                duration_ms=duration,
                                request_id=request_id,
                            )
                        )
                        await session.commit()
                    logger.info(
                        "finance_tool",
                        request_id=request_id,
                        conversation_id=conversation_id,
                        tool_name=name,
                        duration_ms=duration,
                        status=tool_status,
                    )

            async def work() -> None:
                nonlocal status, error_code
                try:
                    if prior_actions:
                        for action in prior_actions:
                            await emit("pending_action", {"action": action})
                        if any(
                            action["status"] in {"unknown", "verification_required"}
                            for action in prior_actions
                        ):
                            answer = "The earlier write has an unknown outcome. Verify its state in Dynamics 365 before preparing another operation."
                        elif all(action["status"] == "executed" for action in prior_actions):
                            if any(
                                (action.get("result") or {}).get("reconciled") for action in prior_actions
                            ):
                                answer = (
                                    "The current record state for this confirmed request has been verified in "
                                    "Dynamics 365. Its verification result is shown on the existing action card. "
                                    "Retrying this response will not repeat the ERP write."
                                )
                            else:
                                answer = "This request was already confirmed and executed. Its result is shown on the existing action card. Retrying the response will not repeat the ERP write."
                        else:
                            answer = "This request already has a confirmation card. Review that card; retrying the response will not create a second ERP operation."
                        await emit("message_delta", {"delta": answer})
                    elif clarification := write_placeholder_help(message):
                        await emit("message_delta", {"delta": clarification})
                    elif self.settings.d365_mock_mode and not self.settings.azure_openai_api_key:
                        # Follow-up field answers use intent and identifiers, never historical monetary facts.
                        routed_message = message
                        previous = identifiers.get("last_user_message", "")
                        if len(message.split()) <= 5 and (
                            "change" in previous.lower() or "update" in previous.lower()
                        ):
                            if (
                                "due date" in previous.lower()
                                and "invoice" in previous.lower()
                                and parse_date(message)
                            ):
                                routed_message = previous + " to " + message
                            elif (
                                "payment terms" in previous.lower()
                                and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", message.strip())
                                and message.strip().lower() not in {"yes", "confirm", "ok", "okay"}
                            ):
                                routed_message = previous + " to " + message
                        answer = await run_mock_agent(routed_message, identifiers, company, execute)
                        for offset in range(0, len(answer), 48):
                            await emit("message_delta", {"delta": answer[offset : offset + 48]})
                            await asyncio.sleep(0)
                    else:
                        finance_required = requires_finance_grounding(message, identifiers)
                        status_method = getattr(self.runtime, "status", None)
                        connection_state = status_method()["status"] if callable(status_method) else "unknown"
                        live_context = context
                        if callable(status_method) and connection_state not in {"connected", "degraded"}:
                            if finance_required:
                                raise AppError(
                                    "d365_connection_error",
                                    "Dynamics 365 is currently unavailable, so I can't safely retrieve the requested finance data. Reconnect to continue.",
                                    status_code=503,
                                    retryable=True,
                                )
                            # General help remains available without handing historical ERP facts to the model.
                            live_context = [HumanMessage(content=message)]
                        routed_write = resolve_write_request(message, identifiers)
                        if routed_write is not None:
                            if routed_write.clarification:
                                await emit("message_delta", {"delta": routed_write.clarification})
                            else:
                                await execute(routed_write.action_type, routed_write.arguments)
                                await emit(
                                    "message_delta",
                                    {
                                        "delta": "I prepared the requested change for confirmation. Review the target and proposed values on the action card, then Confirm to apply it."
                                    },
                                )
                        elif routed_read := resolve_read_request(message, identifiers, company):
                            answer = await execute_read_request(routed_read, company, execute)
                            await emit("message_delta", {"delta": answer})
                        else:
                            await run_live_agent(
                                self.settings,
                                company,
                                live_context,
                                execute,
                                emit,
                                finance_required=finance_required,
                                connection_state=connection_state,
                            )
                    status = "completed"
                except AppError as exc:
                    status, error_code = "error", exc.code
                    if not parts:
                        parts.append(exc.message)
                    await emit("error", exc.public(request_id))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    status, error_code = "error", "azure_openai_unavailable"
                    error = AppError(
                        error_code,
                        "The AI service is unavailable. Your message was saved; retry when the connection is restored.",
                        status_code=503,
                        retryable=True,
                    )
                    if not parts:
                        parts.append(error.message)
                    logger.error(
                        "chat_generation_failed",
                        request_id=request_id,
                        conversation_id=conversation_id,
                        error_type=type(exc).__name__,
                    )
                    await emit("error", error.public(request_id))
                finally:
                    await queue.put(None)

            task = asyncio.create_task(work())
            while (event := await queue.get()) is not None:
                yield event
            await task
            await self._finish(assistant_id, conversation_id, "".join(parts), status, metadata, error_code)
            persisted = True
            yield "done", {"message_id": assistant_id, "status": status}
        finally:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if assistant_id and not persisted:
                await asyncio.shield(
                    self._finish(assistant_id, conversation_id, "".join(parts), status, metadata, error_code)
                )
            self._active.discard(conversation_id)

    async def _finish(
        self,
        message_id: str,
        conversation_id: str,
        content: str,
        status: str,
        metadata: dict[str, Any],
        error_code: str | None,
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    content=content,
                    status=status,
                    meta=safe_json(metadata),
                    error_code=error_code,
                )
            )
            count = await session.scalar(
                select(func.count(Message.id)).where(Message.conversation_id == conversation_id)
            )
            if count and count >= 24:
                summary = await session.scalar(
                    select(ConversationSummary).where(ConversationSummary.conversation_id == conversation_id)
                )
                if summary is None or count - summary.message_count >= 8:
                    runs = list(
                        (
                            await session.scalars(
                                select(ToolRun)
                                .where(
                                    ToolRun.conversation_id == conversation_id,
                                    ToolRun.status == "completed",
                                )
                                .order_by(ToolRun.created_at.desc())
                                .limit(20)
                            )
                        ).all()
                    )
                    actions = list(
                        (
                            await session.scalars(
                                select(PendingAction)
                                .where(
                                    PendingAction.conversation_id == conversation_id,
                                )
                                .order_by(PendingAction.created_at.desc())
                                .limit(12)
                            )
                        ).all()
                    )
                    summary_text = json.dumps(
                        {
                            "company": (await session.get(Conversation, conversation_id)).selected_company,
                            "identifiers": self._identifiers(runs, actions),
                            "recent_operations": [
                                {"tool": run.tool_name, "arguments": run.safe_input} for run in reversed(runs)
                            ],
                            "notice": "Historical identifiers and requests only. Financial values must be freshly queried from ERP.",
                        },
                        default=str,
                    )
                    if summary is None:
                        summary = ConversationSummary(
                            conversation_id=conversation_id, content=summary_text, message_count=count
                        )
                        session.add(summary)
                    else:
                        summary.content = summary_text
                        summary.message_count = count
                        summary.updated_at = datetime.now(UTC)
            await session.commit()
