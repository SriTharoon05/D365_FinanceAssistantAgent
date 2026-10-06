import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.errors import AppError
from app.models.entities import AuditEvent, Base, Conversation, LocalProfile, PendingAction
from app.services.actions import ActionsService


class FakeFinance:
    def __init__(self):
        self.executions = 0
        self.validations = 0
        self.blocked = False
        self.unknown = False
        self.delay = False
        self.not_written_error = None
        self.reconciliations = 0
        self.verification_error = None

    async def validate_mutation(self, action_type, payload, company):
        self.validations += 1
        if self.blocked:
            raise AppError("unsafe_mutation", "The current record is no longer safe to change.")
        return {
            "title": "Create test customer",
            "description": "Create a customer",
            "entity": "CustomersV3",
            "target_identifier": payload.get("account", ""),
            "proposed_changes": payload,
            "risk_level": "low",
        }

    async def execute_mutation(self, action_type, payload, company):
        self.executions += 1
        if self.not_written_error:
            raise self.not_written_error
        if self.delay:
            await asyncio.sleep(0.03)
        if self.unknown:
            raise AppError("operation_outcome_unknown", "Verify ERP state before retrying.", status_code=502)
        return {"reference": payload.get("account"), "verified": True}

    async def reconcile_mutation(self, action_type, payload, company):
        self.reconciliations += 1
        if self.verification_error:
            raise self.verification_error
        return {"company": company, "result": {"identifier": payload["account"], "verified": True}}


@pytest.fixture
async def action_setup(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'actions.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        owner = LocalProfile()
        other = LocalProfile()
        session.add_all([owner, other])
        await session.flush()
        conversation = Conversation(owner_id=owner.id, selected_company="usmf")
        session.add(conversation)
        await session.commit()
        owner_id, other_id, conversation_id = owner.id, other.id, conversation.id
    finance = FakeFinance()
    service = ActionsService(Settings(_env_file=None), SimpleNamespace(finance=finance), sessions)
    yield service, finance, sessions, conversation_id, owner_id, other_id
    await engine.dispose()


async def proposal(setup):
    service, _, _, conversation_id, owner_id, _ = setup
    return await service.propose(
        "create_customer", {"account": "TEST-001", "name": "Test"}, conversation_id, owner_id, "usmf"
    )


async def test_proposal_never_executes_and_confirm_is_idempotent(action_setup):
    service, finance, sessions, conversation_id, owner_id, _ = action_setup
    action = await proposal(action_setup)
    assert finance.executions == 0
    assert action["status"] == "pending"
    result = await service.confirm(action["id"], conversation_id, owner_id, "request-1")
    repeat = await service.confirm(action["id"], conversation_id, owner_id, "request-2")
    assert result == repeat
    assert finance.executions == 1
    assert finance.validations == 2  # proposal and fresh execution check
    async with sessions() as session:
        audits = list((await session.scalars(select(AuditEvent))).all())
    assert len(audits) == 1
    assert audits[0].result_status == "executed"
    assert audits[0].company == "usmf"


async def test_simultaneous_confirm_claims_once(action_setup):
    service, finance, _, conversation_id, owner_id, _ = action_setup
    finance.delay = True
    action = await proposal(action_setup)
    results = await asyncio.gather(
        service.confirm(action["id"], conversation_id, owner_id, "request-a"),
        service.confirm(action["id"], conversation_id, owner_id, "request-b"),
        return_exceptions=True,
    )
    assert finance.executions == 1
    assert any(isinstance(result, dict) and result["status"] == "executed" for result in results)


async def test_expiry_and_ownership_prevent_execution(action_setup):
    service, finance, sessions, conversation_id, owner_id, other_id = action_setup
    action = await proposal(action_setup)
    with pytest.raises(AppError, match="not found"):
        await service.confirm(action["id"], conversation_id, other_id, "other")
    async with sessions() as session:
        await session.execute(
            update(PendingAction)
            .where(PendingAction.id == action["id"])
            .values(
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()
    with pytest.raises(AppError, match="expired"):
        await service.confirm(action["id"], conversation_id, owner_id, "expired")
    assert finance.executions == 0
    async with sessions() as session:
        stored = await session.get(PendingAction, action["id"])
        assert stored.status == "expired"


async def test_cancelled_action_cannot_execute(action_setup):
    service, finance, _, conversation_id, owner_id, _ = action_setup
    action = await proposal(action_setup)
    assert (await service.cancel(action["id"], conversation_id, owner_id, "cancel"))["status"] == "cancelled"
    with pytest.raises(AppError, match="cancelled"):
        await service.confirm(action["id"], conversation_id, owner_id, "confirm")
    assert finance.executions == 0


async def test_revalidation_blocks_changed_business_state_and_audits_failure(action_setup):
    service, finance, sessions, conversation_id, owner_id, _ = action_setup
    action = await proposal(action_setup)
    finance.blocked = True
    with pytest.raises(AppError, match="no longer safe"):
        await service.confirm(action["id"], conversation_id, owner_id, "revalidate")
    assert finance.executions == 0
    async with sessions() as session:
        stored = await session.get(PendingAction, action["id"])
        audit = await session.scalar(select(AuditEvent))
    assert stored.status == "failed"
    assert audit.result_status == "failed"


async def test_unknown_outcome_is_audited_and_never_retried(action_setup):
    service, finance, sessions, conversation_id, owner_id, _ = action_setup
    action = await proposal(action_setup)
    finance.unknown = True
    with pytest.raises(AppError, match="requires verification"):
        await service.confirm(action["id"], conversation_id, owner_id, "unknown")
    with pytest.raises(AppError, match="unknown"):
        await service.confirm(action["id"], conversation_id, owner_id, "retry")
    assert finance.executions == 1
    async with sessions() as session:
        audit = await session.scalar(select(AuditEvent))
    assert audit.result_status == "unknown"


async def uncertain_update(setup):
    service, finance, _, conversation_id, owner_id, _ = setup
    action = await service.propose(
        "update_customer", {"account": "TEST-001", "name": "Updated"}, conversation_id, owner_id, "usmf"
    )
    finance.unknown = True
    with pytest.raises(AppError, match="requires verification"):
        await service.confirm(action["id"], conversation_id, owner_id, "uncertain-update")
    return action


async def test_known_refusal_is_failed_and_retains_specific_error(action_setup):
    service, finance, sessions, conversation_id, owner_id, _ = action_setup
    action = await proposal(action_setup)
    finance.not_written_error = AppError(
        "D365_PERMISSION_ERROR",
        "The mapped D365 user cannot update this entity.",
        status_code=403,
        details={"upstream_status": 403, "write_outcome": "not_written"},
    )
    with pytest.raises(AppError) as error:
        await service.confirm(action["id"], conversation_id, owner_id, "refused")
    assert error.value.code == "D365_PERMISSION_ERROR"
    assert error.value.status_code == 403
    async with sessions() as session:
        stored = await session.get(PendingAction, action["id"])
        audit = await session.scalar(select(AuditEvent))
    assert stored.status == "failed"
    assert stored.result["message"] == "The mapped D365 user cannot update this entity."
    assert audit.result_status == "failed"


async def test_read_only_reconciliation_resolves_unknown_without_reexecution(action_setup):
    service, finance, sessions, conversation_id, owner_id, _ = action_setup
    action = await uncertain_update(action_setup)
    result = await service.verify(action["id"], conversation_id, owner_id, "verify")
    repeat = await service.verify(action["id"], conversation_id, owner_id, "verify-again")
    assert result == repeat
    assert result["status"] == "executed"
    assert result["result"]["reconciled"] is True
    assert result["result"]["verification_basis"] == "current_record_state"
    assert result["result"]["previous_error"]["code"] == "operation_outcome_unknown"
    assert finance.executions == 1
    assert finance.reconciliations == 1
    async with sessions() as session:
        audits = list((await session.scalars(select(AuditEvent))).all())
    assert len(audits) == 2
    assert all(audit.result_status == "verified" for audit in audits)
    assert any(
        audit.action == "verify_operation" and audit.safe_request_summary["read_only"] for audit in audits
    )
    with pytest.raises(AppError):
        # An observed success also prevents the original confirmation from running again.
        await service.cancel(action["id"], conversation_id, owner_id, "cancel-after-verification")


async def test_reconciliation_failure_leaves_original_uncertainty_and_audit(action_setup):
    service, finance, sessions, conversation_id, owner_id, _ = action_setup
    action = await uncertain_update(action_setup)
    finance.verification_error = AppError(
        "D365_WRITE_VERIFICATION_FAILED",
        "The current name does not match the requested change.",
        status_code=409,
    )
    with pytest.raises(AppError, match="does not match"):
        await service.verify(action["id"], conversation_id, owner_id, "verify-mismatch")
    assert finance.executions == 1
    async with sessions() as session:
        stored = await session.get(PendingAction, action["id"])
        audits = list((await session.scalars(select(AuditEvent))).all())
    assert stored.status == "unknown"
    assert len(audits) == 1 and audits[0].result_status == "unknown"


async def test_verification_checks_ownership_and_uncertain_status(action_setup):
    service, finance, _, conversation_id, owner_id, other_id = action_setup
    action = await uncertain_update(action_setup)
    with pytest.raises(AppError, match="not found"):
        await service.verify(action["id"], conversation_id, other_id, "other-owner")
    assert finance.reconciliations == 0
    pending = await proposal(action_setup)
    with pytest.raises(AppError, match="uncertain"):
        await service.verify(pending["id"], conversation_id, owner_id, "pending")
    assert finance.executions == 1


async def test_unknown_creation_requires_manual_verification_without_repeating_write(action_setup):
    service, finance, _, conversation_id, owner_id, _ = action_setup
    action = await proposal(action_setup)
    finance.unknown = True
    with pytest.raises(AppError):
        await service.confirm(action["id"], conversation_id, owner_id, "unknown-create")
    with pytest.raises(AppError, match="directly"):
        await service.verify(action["id"], conversation_id, owner_id, "unsupported-verify")
    assert finance.executions == 1 and finance.reconciliations == 0
