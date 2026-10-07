import asyncio
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import structlog
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.session import build_engine, migrate
from app.models.entities import (
    AuditEvent,
    Conversation,
    Feedback,
    LocalProfile,
    Message,
    PendingAction,
    utcnow,
)
from app.schemas.api import (
    ActionRequest,
    ChatRequest,
    ConversationCreate,
    ConversationUpdate,
    ExportRequest,
    FeedbackRequest,
)
from app.services.voice import GroqTranscriber
from app.services.charts import QuickChartRenderer, build_charts

logger = structlog.get_logger()


def serialize(row):
    result = {}
    for column in row.__mapper__.column_attrs:
        name = column.key
        value = getattr(row, name)
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        result["metadata" if name == "meta" else name] = value
    return jsonable_encoder(result)


def session_secret(settings):
    if settings.app_session_secret not in {"", "change-this-locally"}:
        return settings.app_session_secret
    if settings.app_env == "production":
        raise RuntimeError("APP_SESSION_SECRET must be configured in production")
    parent = settings.database_path().parent if settings.database_path() else Path("data")
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / ".session_secret"
    if not path.exists():
        try:
            with path.open("x") as stream:
                stream.write(secrets.token_urlsafe(48))
            path.chmod(0o600)
        except FileExistsError:
            pass
    return path.read_text().strip()


def create_app(settings=None, runtime=None):
    settings = settings or Settings()
    configure_logging(settings.log_level)
    engine = build_engine(settings)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app):
        from app.integrations.d365.runtime import D365Runtime
        from app.services.actions import ActionsService
        from app.services.chat import ChatService

        await migrate(settings)
        app.state.runtime = runtime or D365Runtime(settings)
        app.state.sessions = factory
        app.state.settings = settings
        app.state.chat = ChatService(settings, app.state.runtime, factory)
        app.state.actions = ActionsService(settings, app.state.runtime, factory)
        app.state.charts = QuickChartRenderer()
        connection_task = None
        if settings.d365_mock_mode:
            await app.state.runtime.start()
        else:

            async def connect():
                try:
                    await app.state.runtime.start()
                except Exception as exc:
                    logger.error("initial_connection_failed", exception_type=type(exc).__name__)

            connection_task = asyncio.create_task(connect())
        # Interrupted writes must not be retried automatically after a process restart.
        async with factory() as session:
            await session.execute(
                update(AuditEvent)
                .where(AuditEvent.result_status == "executing")
                .values(result_status="verification_required")
            )
            await session.execute(
                update(PendingAction)
                .where(PendingAction.status == "executing")
                .values(
                    status="verification_required",
                    result={"message": "Server restarted during this operation. Verify in D365."},
                )
            )
            await session.execute(
                update(Message).where(Message.status == "streaming").values(status="interrupted")
            )
            await session.commit()
        yield
        if connection_task and not connection_task.done():
            connection_task.cancel()
            try:
                await connection_task
            except asyncio.CancelledError:
                pass
        await app.state.charts.close()
        await app.state.runtime.close()
        await engine.dispose()

    app = FastAPI(title="D365 Finance Assistant", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret(settings),
        session_cookie="finance_session",
        same_site="lax",
        https_only=settings.session_cookie_secure,
        max_age=365 * 24 * 3600,
    )
    app.add_middleware(RequestContextMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(exc.public(getattr(request.state, "request_id", "")), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        # Validation input may contain credentials or arbitrary text; return paths and reasons only.
        details = [{"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(
            {
                "code": "validation_error",
                "message": "Please check the request fields.",
                "request_id": getattr(request.state, "request_id", ""),
                "retryable": False,
                "details": details,
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logger.error(
            "unhandled_exception",
            request_id=getattr(request.state, "request_id", ""),
            exception_type=type(exc).__name__,
        )
        return JSONResponse(
            {
                "code": "internal_error",
                "message": "The request could not be completed. Retry later.",
                "request_id": getattr(request.state, "request_id", ""),
                "retryable": True,
            },
            status_code=500,
        )

    async def owner(request: Request):
        profile_id = request.session.get("profile_id")
        async with factory() as session:
            profile = await session.get(LocalProfile, profile_id) if profile_id else None
            if not profile:
                profile = LocalProfile()
                session.add(profile)
                await session.commit()
                request.session["profile_id"] = profile.id
            return profile.id

    async def find_conversation(session, identifier, owner_id):
        row = await session.scalar(
            select(Conversation).where(
                Conversation.id == identifier,
                Conversation.owner_id == owner_id,
                Conversation.deleted_at.is_(None),
            )
        )
        if not row:
            raise AppError("conversation_not_found", "Conversation was not found.", 404)
        return row

    @app.get("/api/health")
    async def health(request: Request):
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        return {
            "api": "healthy",
            "database": "healthy",
            "azure_openai": "configured" if settings.azure_openai_api_key else "not_configured",
            "d365": request.app.state.runtime.status()["status"],
            "groq": "configured" if settings.groq_api_key else "not_configured",
        }

    @app.get("/api/settings/capabilities")
    async def capabilities():
        return {
            "model": settings.azure_openai_deployment,
            "ai_configured": bool(settings.azure_openai_api_key),
            "voice_input": bool(settings.groq_api_key),
            "voice_model": settings.groq_whisper_model,
            "mock_mode": settings.d365_mock_mode,
            "company": settings.d365_default_company.upper(),
            "write_actions_enabled": settings.d365_write_actions_enabled,
            "voice_max_duration_seconds": settings.voice_max_duration_seconds,
            "voice_max_upload_mb": settings.voice_max_upload_mb,
        }

    @app.get("/api/integrations/d365/status")
    async def status(request: Request):
        return await request.app.state.runtime.check_health()

    @app.post("/api/integrations/d365/reconnect")
    async def reconnect(request: Request, refresh_metadata: bool = False, owner_id=Depends(owner)):
        return await request.app.state.runtime.reconnect(refresh_metadata=refresh_metadata)

    @app.get("/api/integrations/d365/diagnostics/entities")
    async def diagnostics(request: Request, owner_id=Depends(owner)):
        return request.app.state.runtime.diagnostics()

    @app.get("/api/conversations")
    async def conversations(q: str = "", archived: bool = False, owner_id=Depends(owner)):
        async with factory() as session:
            query = select(Conversation).where(
                Conversation.owner_id == owner_id, Conversation.deleted_at.is_(None)
            )
            query = query.where(
                Conversation.archived_at.is_not(None) if archived else Conversation.archived_at.is_(None)
            )
            if q:
                query = query.where(Conversation.title.contains(q[:200], autoescape=True))
            rows = (await session.scalars(query.order_by(Conversation.updated_at.desc()).limit(200))).all()
            return [serialize(row) for row in rows]

    @app.post("/api/conversations", status_code=201)
    async def new_conversation(body: ConversationCreate, owner_id=Depends(owner)):
        if body.selected_company and body.selected_company.lower() != settings.d365_default_company:
            raise AppError("company_not_allowed", "This application is configured for one legal entity.", 422)
        async with factory() as session:
            row = Conversation(
                owner_id=owner_id, title=body.title, selected_company=settings.d365_default_company
            )
            session.add(row)
            await session.commit()
            return serialize(row)

    @app.get("/api/conversations/{identifier}")
    async def conversation(identifier: str, owner_id=Depends(owner)):
        async with factory() as session:
            return serialize(await find_conversation(session, identifier, owner_id))

    @app.patch("/api/conversations/{identifier}")
    async def edit_conversation(identifier: str, body: ConversationUpdate, owner_id=Depends(owner)):
        async with factory() as session:
            row = await find_conversation(session, identifier, owner_id)
            if body.title is not None:
                row.title = body.title
            if body.archived is not None:
                row.archived_at = utcnow() if body.archived else None
            row.updated_at = utcnow()
            await session.commit()
            return serialize(row)

    @app.delete("/api/conversations/{identifier}", status_code=204)
    async def delete_conversation(identifier: str, owner_id=Depends(owner)):
        async with factory() as session:
            row = await find_conversation(session, identifier, owner_id)
            row.deleted_at = utcnow()
            await session.execute(
                update(PendingAction)
                .where(PendingAction.conversation_id == identifier, PendingAction.status == "pending")
                .values(status="cancelled")
            )
            await session.commit()
        return Response(status_code=204)

    @app.delete("/api/conversations", status_code=204)
    async def clear_archived(archived: bool = False, owner_id=Depends(owner)):
        if not archived:
            raise AppError("archive_required", "Only archived conversations can be cleared in bulk.")
        async with factory() as session:
            ids = select(Conversation.id).where(
                Conversation.owner_id == owner_id, Conversation.archived_at.is_not(None)
            )
            await session.execute(
                update(PendingAction)
                .where(PendingAction.conversation_id.in_(ids), PendingAction.status == "pending")
                .values(status="cancelled")
            )
            await session.execute(
                update(Conversation).where(Conversation.id.in_(ids)).values(deleted_at=utcnow())
            )
            await session.commit()
        return Response(status_code=204)

    @app.get("/api/conversations/{identifier}/messages")
    async def messages(identifier: str, owner_id=Depends(owner)):
        async with factory() as session:
            await find_conversation(session, identifier, owner_id)
            rows = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == identifier)
                    .order_by(Message.created_at, Message.id)
                )
            ).all()
            actions = (
                await session.scalars(
                    select(PendingAction).where(PendingAction.conversation_id == identifier)
                )
            ).all()
            by_id = {a.id: serialize(a) for a in actions}
            result = []
            for row in rows:
                item = serialize(row)
                metadata = item.get("metadata") or {}
                ids = metadata.get("pending_action_ids", []) or [
                    card["id"]
                    for card in metadata.get("pending_actions", [])
                    if isinstance(card, dict) and "id" in card
                ]
                metadata["pending_actions"] = [by_id[x] for x in ids if x in by_id]
                item["metadata"] = metadata
                result.append(item)
            return result

    rate_windows = defaultdict(deque)

    async def find_message_charts(identifier, owner_id):
        async with factory() as session:
            message = await session.get(Message, identifier)
            if not message:
                raise AppError("message_not_found", "Message was not found.", 404)
            await find_conversation(session, message.conversation_id, owner_id)
            if message.role != "assistant" or message.status != "completed":
                return []
            records = (message.meta or {}).get("evidence", [])
            return build_charts(records if isinstance(records, list) else [])

    @app.get("/api/messages/{identifier}/charts")
    async def charts(identifier: str, owner_id=Depends(owner)):
        return {"charts": await find_message_charts(identifier, owner_id)}

    @app.get("/api/messages/{identifier}/charts/{chart_id}.png")
    async def chart_image(
        identifier: str,
        chart_id: str,
        request: Request,
        theme: Literal["light", "dark"] = "light",
        size: Literal["standard", "compact"] = "standard",
        owner_id=Depends(owner),
    ):
        descriptors = await find_message_charts(identifier, owner_id)
        chart = next((item for item in descriptors if item["id"] == chart_id), None)
        if chart is None:
            raise AppError("chart_not_found", "Chart was not found for this response.", 404)
        if size == "compact":
            chart = {**chart, "render_size": "compact"}
        image = await request.app.state.charts.render(chart, theme)
        return Response(image, media_type="image/png")

    @app.post("/api/chat/stream")
    async def stream_chat(body: ChatRequest, request: Request, owner_id=Depends(owner)):
        now = time.monotonic()
        for key in list(rate_windows):
            if not rate_windows[key] or rate_windows[key][-1] < now - 60:
                del rate_windows[key]
        window = rate_windows[owner_id]
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= settings.chat_rate_limit_per_minute:
            raise AppError("rate_limited", "Chat rate limit reached. Please wait a minute.", 429, True)
        async with factory() as session:
            row = await find_conversation(session, body.conversation_id, owner_id)
            if row.archived_at:
                raise AppError(
                    "conversation_archived", "Restore this conversation before sending messages.", 409
                )
        window.append(now)

        async def events():
            try:
                async for name, payload in request.app.state.chat.stream(
                    body.conversation_id,
                    body.message,
                    owner_id,
                    request.state.request_id,
                    retry_message_id=body.retry_message_id,
                ):
                    yield f"event: {name}\ndata: {json.dumps(jsonable_encoder(payload), ensure_ascii=False)}\n\n"
            except AppError as exc:
                yield f"event: error\ndata: {json.dumps(exc.public(request.state.request_id))}\n\n"
                yield 'event: done\ndata: {"status":"error"}\n\n'

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/actions/{identifier}/confirm")
    async def confirm(identifier: str, body: ActionRequest, request: Request, owner_id=Depends(owner)):
        return await request.app.state.actions.confirm(
            identifier, body.conversation_id, owner_id, request.state.request_id
        )

    @app.post("/api/actions/{identifier}/cancel")
    async def cancel(identifier: str, body: ActionRequest, request: Request, owner_id=Depends(owner)):
        return await request.app.state.actions.cancel(
            identifier, body.conversation_id, owner_id, request.state.request_id
        )

    @app.post("/api/actions/{identifier}/verify")
    async def verify_action(identifier: str, body: ActionRequest, request: Request, owner_id=Depends(owner)):
        return await request.app.state.actions.verify(
            identifier, body.conversation_id, owner_id, request.state.request_id
        )

    @app.post("/api/voice/transcribe")
    async def transcribe(
        file: UploadFile = File(...),
        duration_seconds: float | None = Form(default=None),
        owner_id=Depends(owner),
    ):
        try:
            data = await file.read(settings.voice_max_upload_mb * 1024 * 1024 + 1)
            return await GroqTranscriber(settings).transcribe(data, file.content_type or "", duration_seconds)
        finally:
            await file.close()

    @app.get("/api/audit/recent")
    async def audit(owner_id=Depends(owner)):
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(AuditEvent)
                    .join(Conversation)
                    .where(Conversation.owner_id == owner_id)
                    .order_by(AuditEvent.created_at.desc())
                    .limit(50)
                )
            ).all()
            return [serialize(row) for row in rows]

    @app.post("/api/messages/{identifier}/feedback", status_code=201)
    async def feedback(identifier: str, body: FeedbackRequest, owner_id=Depends(owner)):
        async with factory() as session:
            message = await session.get(Message, identifier)
            if not message:
                raise AppError("message_not_found", "Message was not found.", 404)
            await find_conversation(session, message.conversation_id, owner_id)
            if message.role != "assistant":
                raise AppError("feedback_invalid", "Feedback is available for assistant responses only.")
            row = await session.scalar(
                select(Feedback).where(Feedback.message_id == identifier, Feedback.owner_id == owner_id)
            )
            if not row:
                row = Feedback(message_id=identifier, owner_id=owner_id, value=body.value)
                session.add(row)
            else:
                row.value = body.value
            await session.commit()
            return {"message_id": identifier, "value": row.value}

    @app.post("/api/conversations/{identifier}/export")
    async def export(identifier: str, body: ExportRequest, owner_id=Depends(owner)):
        async with factory() as session:
            row = await find_conversation(session, identifier, owner_id)
            rows = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == identifier)
                    .order_by(Message.created_at, Message.id)
                )
            ).all()
            if body.format == "json":
                content = json.dumps(
                    {"conversation": serialize(row), "messages": [serialize(x) for x in rows]},
                    ensure_ascii=False,
                    indent=2,
                )
                mime = "application/json"
            else:
                content = f"# {row.title}\n\n" + "\n\n".join(f"## {m.role}\n\n{m.content}" for m in rows)
                mime = "text/markdown"
            return Response(
                content,
                media_type=mime,
                headers={
                    "Content-Disposition": f'attachment; filename="conversation-{row.id}.{"json" if body.format == "json" else "md"}"'
                },
            )

    return app


app = create_app()
