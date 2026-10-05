"""Durable confirmation gates and auditable, at-most-once mutation attempts."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.errors import AppError
from app.models.entities import AuditEvent, Conversation, PendingAction
from app.schemas.finance import MUTATION_SCHEMAS

_SECRET_KEYS = {
    "authorization",
    "access_token",
    "refresh_token",
    "client_secret",
    "api_key",
    "password",
    "headers",
}


def safe_json(value: Any) -> Any:
    """Strip credential-bearing keys before storing public tool/audit data."""
    if isinstance(value, dict):
        return {
            str(k): safe_json(v)
            for k, v in value.items()
            if str(k).lower() not in _SECRET_KEYS
            and not str(k).lower().endswith(("_secret", "_api_key", "_token", "_password"))
        }
    if isinstance(value, (list, tuple)):
        return [safe_json(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return safe_json(value.model_dump(mode="json"))
    return str(value)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def serialize_action(action: PendingAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "action_type": action.action_type,
        "title": action.title,
        "description": action.description,
        "entity": action.entity,
        "target_identifier": action.target_identifier,
        "proposed_changes": safe_json(action.proposed_changes),
        "financial_impact": safe_json(action.financial_impact),
        "risk_level": action.risk_level,
        "expires_at": aware(action.expires_at).isoformat(),
        "status": action.status,
        "result": safe_json(action.result),
    }


class ActionsService:
    def __init__(self, settings: Any, runtime: Any, session_factory: Any):
        self.settings = settings
        self.runtime = runtime
        self.session_factory = session_factory

    async def _owned_conversation(self, session: Any, conversation_id: str, owner_id: str) -> Conversation:
        conversation = await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.owner_id == owner_id,
                Conversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            raise AppError("conversation_not_found", "Conversation was not found.", status_code=404)
        return conversation

    async def propose(
        self,
        action_type: str,
        payload: dict[str, Any],
        conversation_id: str,
        owner_id: str,
        company: str,
    ) -> dict[str, Any]:
        """A write tool only validates current ERP state and persists a preview."""
        if not self.settings.d365_write_actions_enabled:
            raise AppError(
                "writes_disabled", "ERP write actions are disabled in this environment.", status_code=403
            )
        schema = MUTATION_SCHEMAS.get(action_type)
        if schema is None:
            raise AppError("unsupported_mutation", "This write operation is not supported.")
        normalized = schema.model_validate(payload).model_dump(mode="json", exclude_none=True)
        async with self.session_factory() as session:
            await self._owned_conversation(session, conversation_id, owner_id)
        preview = await self.runtime.finance.validate_mutation(action_type, normalized, company)
        proposed = dict(preview.get("proposed_changes") or normalized)
        proposed["company"] = company.lower()
        async with self.session_factory() as session:
            await self._owned_conversation(session, conversation_id, owner_id)
            action = PendingAction(
                conversation_id=conversation_id,
                action_type=action_type,
                title=preview.get("title", action_type.replace("_", " ").capitalize()),
                description=preview.get("description", "Review the proposed ERP change before confirming."),
                entity=preview.get("entity") or "",
                target_identifier=preview.get("target_identifier") or "",
                proposed_changes=safe_json(proposed),
                financial_impact=safe_json(preview.get("financial_impact")),
                risk_level=preview.get("risk_level", "medium"),
                expires_at=datetime.now(UTC) + timedelta(minutes=self.settings.action_expiry_minutes),
                status="pending",
            )
            session.add(action)
            await session.commit()
            await session.refresh(action)
            return serialize_action(action)

    async def _get_owned_action(
        self,
        session: Any,
        action_id: str,
        conversation_id: str | None,
        owner_id: str,
    ) -> PendingAction:
        statement = (
            select(PendingAction)
            .join(Conversation, Conversation.id == PendingAction.conversation_id)
            .where(
                PendingAction.id == action_id,
                Conversation.owner_id == owner_id,
                Conversation.deleted_at.is_(None),
            )
        )
        if conversation_id:
            statement = statement.where(PendingAction.conversation_id == conversation_id)
        action = await session.scalar(statement)
        if action is None:
            raise AppError("action_not_found", "Pending action was not found.", status_code=404)
        return action

    async def confirm(
        self,
        action_id: str,
        conversation_id: str | None,
        owner_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        if not self.settings.d365_write_actions_enabled:
            raise AppError(
                "writes_disabled", "ERP write actions are disabled in this environment.", status_code=403
            )
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            action = await self._get_owned_action(session, action_id, conversation_id, owner_id)
            if action.status == "executed":
                return {"id": action.id, "status": action.status, "result": action.result}
            if action.status != "pending":
                code = "action_expired" if action.status == "expired" else "action_not_pending"
                raise AppError(
                    code, f"This action is {action.status} and cannot execute again.", status_code=409
                )
            if aware(action.expires_at) <= now:
                await session.execute(
                    update(PendingAction)
                    .where(
                        PendingAction.id == action_id,
                        PendingAction.status == "pending",
                    )
                    .values(status="expired")
                )
                await session.commit()
                raise AppError(
                    "action_expired",
                    "This confirmation has expired. Ask the assistant to prepare a fresh action.",
                    status_code=409,
                )
            claimed = await session.execute(
                update(PendingAction)
                .where(
                    PendingAction.id == action_id,
                    PendingAction.status == "pending",
                    PendingAction.expires_at > now,
                )
                .values(status="executing")
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                await session.rollback()
                raise AppError(
                    "action_not_pending", "Another request has already handled this action.", status_code=409
                )
            payload = dict(action.proposed_changes)
            company = payload.pop("company", self.settings.d365_default_company)
            audit = AuditEvent(
                conversation_id=action.conversation_id,
                action_id=action.id,
                action=action.action_type,
                company=company,
                entity=action.entity,
                record_identifier=action.target_identifier,
                safe_request_summary=safe_json(payload),
                result_status="executing",
                request_id=request_id,
            )
            session.add(audit)
            await session.commit()
            await session.refresh(audit)
            audit_id = audit.id
            action_type = action.action_type

        execution_started = False
        try:
            # ERP business state may have changed since the confirmation card was created.
            await self.runtime.finance.validate_mutation(action_type, payload, company)
            execution_started = True
            result = await self.runtime.finance.execute_mutation(action_type, payload, company)
        except asyncio.CancelledError:
            await asyncio.shield(
                self._record_result(
                    action_id,
                    audit_id,
                    "unknown" if execution_started else "failed",
                    {
                        "code": "operation_outcome_unknown" if execution_started else "operation_cancelled",
                        "message": "The operation was interrupted. Verify ERP state before preparing another action.",
                    },
                )
            )
            raise
        except Exception as exc:
            # After execution begins, even a read-after-write validation error can
            # follow a successful ERP write. Never infer that retrying is safe.
            status = "unknown" if execution_started else "failed"
            error = {
                "code": getattr(exc, "code", "operation_outcome_unknown"),
                "message": getattr(
                    exc, "message", "The write outcome requires verification in Dynamics 365."
                ),
                "details": safe_json(getattr(exc, "details", None)),
            }
            await self._record_result(action_id, audit_id, status, error)
            if execution_started:
                raise AppError(
                    "operation_outcome_unknown",
                    "The operation outcome requires verification in Dynamics 365. Check the affected record before preparing another action.",
                    status_code=502,
                    retryable=False,
                    details={"cause_code": error["code"], "verification": error["details"]},
                ) from exc
            if isinstance(exc, AppError):
                raise
            raise AppError(
                "operation_outcome_unknown",
                "The write outcome requires verification in Dynamics 365. Do not retry the same payment.",
                status_code=502,
                retryable=False,
            ) from exc
        safe_result = safe_json(result)
        await self._record_result(action_id, audit_id, "executed", safe_result)
        return {"id": action_id, "status": "executed", "result": safe_result}

    async def _record_result(
        self, action_id: str, audit_id: str, status: str, result: dict[str, Any]
    ) -> None:
        async with self.session_factory() as session:
            await session.execute(
                update(PendingAction)
                .where(PendingAction.id == action_id)
                .values(
                    status=status,
                    result=safe_json(result),
                )
            )
            record = result.get("result") or result
            reference = record.get("reference") or record.get("journal_number") or record.get("identifier")
            if not reference and isinstance(record.get("details"), dict):
                reference = record["details"].get("identifier") or record["details"].get("reference")
            await session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == audit_id)
                .values(
                    result_status=status,
                    d365_reference=str(reference) if reference else None,
                )
            )
            await session.commit()

    async def cancel(
        self,
        action_id: str,
        conversation_id: str | None,
        owner_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            action = await self._get_owned_action(session, action_id, conversation_id, owner_id)
            if action.status == "cancelled":
                return {"id": action.id, "status": "cancelled", "result": None}
            if action.status != "pending":
                raise AppError(
                    "action_not_pending",
                    f"This action is {action.status} and cannot be cancelled.",
                    status_code=409,
                )
            status = "expired" if aware(action.expires_at) <= datetime.now(UTC) else "cancelled"
            changed = await session.execute(
                update(PendingAction)
                .where(
                    PendingAction.id == action_id,
                    PendingAction.status == "pending",
                )
                .values(status=status)
            )
            if changed.rowcount != 1:
                await session.rollback()
                raise AppError(
                    "action_not_pending", "Another request has already handled this action.", status_code=409
                )
            await session.commit()
            return {"id": action_id, "status": status, "result": None}
