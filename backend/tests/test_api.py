import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/finance.db",
        d365_mock_mode=True,
        azure_openai_api_key="",
        groq_api_key="",
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def conversation(client):
    response = client.post("/api/conversations", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def parse_events(response):
    assert response.status_code == 200, response.text
    result = []
    for block in response.text.split("\n\n"):
        if block.strip():
            lines = block.splitlines()
            result.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
    return result


def test_health_capabilities_and_safe_status(client):
    assert client.get("/api/health").json()["database"] == "healthy"
    caps = client.get("/api/settings/capabilities").json()
    assert caps["mock_mode"] and not caps["voice_input"]
    status = client.get("/api/integrations/d365/status").json()
    assert status["mock_mode"] and status["company"].lower() == "usmf"
    assert "client_secret" not in json.dumps(status)
    assert client.post("/api/integrations/d365/reconnect").status_code == 200


def test_conversation_lifecycle_search_archive_export(client):
    identifier = conversation(client)
    assert (
        client.patch(f"/api/conversations/{identifier}", json={"title": "Asterion collections"}).status_code
        == 200
    )
    assert len(client.get("/api/conversations?q=Asterion").json()) == 1
    assert client.get("/api/conversations?q=%25").json() == []
    for format_name in ["markdown", "json"]:
        exported = client.post(f"/api/conversations/{identifier}/export", json={"format": format_name})
        assert exported.status_code == 200
        assert "Asterion collections" in exported.text
    client.patch(f"/api/conversations/{identifier}", json={"archived": True})
    assert client.get("/api/conversations").json() == []
    assert len(client.get("/api/conversations?archived=true").json()) == 1
    assert client.delete("/api/conversations?archived=true").status_code == 204
    assert client.get(f"/api/conversations/{identifier}").status_code == 404


def test_mock_balance_stream_and_history(client):
    identifier = conversation(client)
    events = parse_events(
        client.post(
            "/api/chat/stream",
            json={"conversation_id": identifier, "message": "What is the outstanding balance for Asterion?"},
        )
    )
    names = [x[0] for x in events]
    assert "tool_result" in names and "evidence" in names and names[-1] == "done"
    content = "".join(p["delta"] for name, p in events if name == "message_delta")
    assert "110" in content and "INR" in content
    messages = client.get(f"/api/conversations/{identifier}/messages").json()
    assert len(messages) == 2
    assert messages[1]["status"] == "completed"
    assert messages[1]["metadata"]["evidence"]
    assert client.post(f"/api/messages/{messages[1]['id']}/feedback", json={"value": 1}).status_code == 201


def test_history_survives_backend_restart(settings):
    with TestClient(create_app(settings)) as first:
        identifier = conversation(first)
        parse_events(
            first.post(
                "/api/chat/stream",
                json={"conversation_id": identifier, "message": "Show Asterion's payment history."},
            )
        )
        cookie = first.cookies.get("finance_session")
    with TestClient(create_app(settings)) as second:
        second.cookies.set("finance_session", cookie)
        rows = second.get(f"/api/conversations/{identifier}/messages").json()
        assert len(rows) == 2 and rows[0]["role"] == "user"


def test_restart_requires_verification_for_interrupted_write(settings):
    from sqlalchemy import select
    from app.models.entities import AuditEvent, PendingAction

    with TestClient(create_app(settings)) as first:
        identifier = conversation(first)
        events = parse_events(
            first.post(
                "/api/chat/stream",
                json={
                    "conversation_id": identifier,
                    "message": "Create a test customer called TEST-INTERRUPTED.",
                },
            )
        )
        card = next(p["action"] for name, p in events if name == "pending_action")
        message_id = next(p["message_id"] for name, p in events if name == "message_start")

        async def simulate_crash():
            async with first.app.state.sessions() as session:
                action = await session.get(PendingAction, card["id"])
                action.status = "executing"
                session.add(
                    AuditEvent(
                        conversation_id=identifier,
                        action_id=action.id,
                        action=action.action_type,
                        company="usmf",
                        entity=action.entity,
                        record_identifier=action.target_identifier,
                        safe_request_summary={},
                        result_status="executing",
                        request_id="interrupted-request",
                    )
                )
                await session.commit()

        first.portal.call(simulate_crash)
        cookie = first.cookies.get("finance_session")
    with TestClient(create_app(settings)) as second:
        second.cookies.set("finance_session", cookie)
        audit = second.get("/api/audit/recent").json()
        assert audit[0]["result_status"] == "verification_required"
        assert (
            second.post(
                f"/api/actions/{card['id']}/confirm", json={"conversation_id": identifier}
            ).status_code
            == 409
        )
        retried = parse_events(
            second.post(
                "/api/chat/stream",
                json={"conversation_id": identifier, "message": "retry", "retry_message_id": message_id},
            )
        )
        cards = [p["action"] for name, p in retried if name == "pending_action"]
        assert cards and cards[0]["id"] == card["id"]

        async def count_actions():
            async with second.app.state.sessions() as session:
                return len((await session.scalars(select(PendingAction))).all())

        assert second.portal.call(count_actions) == 1


def test_conversation_ownership_and_csrf(settings):
    with TestClient(create_app(settings)) as first:
        identifier = conversation(first)
        first.cookies.clear()
        assert first.get(f"/api/conversations/{identifier}").status_code == 404
        assert first.get(f"/api/conversations/{identifier}/messages").status_code == 404
        response = first.post("/api/conversations", json={}, headers={"Origin": "https://untrusted.invalid"})
        assert response.status_code == 403


def test_validation_voice_and_request_limits(client, settings):
    identifier = conversation(client)
    assert (
        client.post("/api/chat/stream", json={"conversation_id": identifier, "message": " "}).status_code
        == 422
    )
    response = client.post("/api/voice/transcribe", files={"file": ("voice.webm", b"audio", "audio/webm")})
    assert response.status_code == 503 and response.json()["code"] == "voice_not_configured"
    assert client.post("/api/conversations", json={"selected_company": "other"}).status_code == 422
    response = client.post(
        "/api/conversations",
        content=b"x",
        headers={"Content-Length": str(settings.max_request_mb * 1024 * 1024 + 1)},
    )
    assert response.status_code == 413


def test_pending_action_needs_confirmation_and_audits(client):
    identifier = conversation(client)
    events = parse_events(
        client.post(
            "/api/chat/stream",
            json={"conversation_id": identifier, "message": "Create a test customer called TEST-ACME-001."},
        )
    )
    actions = [p["action"] for name, p in events if name == "pending_action"]
    assert len(actions) == 1, events
    action = actions[0]
    assert action["status"] == "pending"
    result = client.post(f"/api/actions/{action['id']}/confirm", json={"conversation_id": identifier})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "executed"
    assert (
        client.post(f"/api/actions/{action['id']}/confirm", json={"conversation_id": identifier}).json()
        == result.json()
    )
    audit = client.get("/api/audit/recent").json()
    assert len(audit) == 1 and audit[0]["result_status"] == "executed"
    messages = client.get(f"/api/conversations/{identifier}/messages").json()
    assert messages[1]["metadata"]["pending_actions"][0]["status"] == "executed"


def test_config_loading_no_secret_representation(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("D365_MOCK_MODE=true\nGROQ_WHISPER_MODEL=whisper-large-v3\nGROQ_API_KEY=sensitive\n")
    settings = Settings(_env_file=path)
    assert settings.groq_whisper_model == "whisper-large-v3"
    assert "sensitive" not in repr(settings)
    assert settings.d365_mock_mode


def test_session_secret_not_shared_between_instances(tmp_path):
    from app.main import session_secret

    first = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path}/one/finance.db")
    second = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{tmp_path}/two/finance.db")
    assert session_secret(first) == session_secret(first)
    assert session_secret(first) != session_secret(second)


@pytest.mark.asyncio
async def test_voice_adapter_uses_configured_model(monkeypatch):
    import httpx
    from app.services.voice import GroqTranscriber

    original_client = httpx.AsyncClient

    def handler(request):
        assert b"replaceable-model" in request.content
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(200, json={"text": "Hello finance", "duration": 2})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    settings = Settings(_env_file=None, groq_api_key="test-key", groq_whisper_model="replaceable-model")
    result = await GroqTranscriber(settings).transcribe(b"webm-data", "audio/webm")
    assert result == {"text": "Hello finance"}


@pytest.mark.asyncio
async def test_voice_rejects_type_size_and_duration():
    from app.core.errors import AppError
    from app.services.voice import GroqTranscriber

    service = GroqTranscriber(Settings(_env_file=None, groq_api_key="test-key"))
    for data, mime, duration, code in [
        (b"x", "text/html", None, "voice_invalid_type"),
        (b"", "audio/webm", None, "voice_invalid_size"),
        (b"x", "audio/webm", 999, "voice_duration_exceeded"),
    ]:
        with pytest.raises(AppError) as caught:
            await service.transcribe(data, mime, duration)
        assert caught.value.code == code
