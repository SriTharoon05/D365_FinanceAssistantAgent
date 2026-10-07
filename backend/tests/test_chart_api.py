"""Charts use saved finance evidence and enforce message ownership before rendering."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.entities import Message


PNG = b"\x89PNG\r\n\x1a\nmock-chart-image"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/chart-api.db",
        d365_mock_mode=True,
        azure_openai_api_key="",
        groq_api_key="",
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as value:
        yield value


def completed_answer(client, prompt="What is the outstanding balance for Asterion?"):
    created = client.post("/api/conversations", json={})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    response = client.post("/api/chat/stream", json={"conversation_id": conversation_id, "message": prompt})
    assert response.status_code == 200, response.text
    events = [
        (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        for block in response.text.split("\n\n")
        if block.strip()
        for lines in [block.splitlines()]
    ]
    assert events[-1][0] == "done", events
    messages = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["status"] == "completed"
    assert messages[-1]["metadata"]["evidence"]
    return conversation_id, messages


def descriptors(client, message_id):
    response = client.get(f"/api/messages/{message_id}/charts")
    assert response.status_code == 200, response.text
    return response.json()["charts"]


def image_path(message_id, chart_id):
    return f"/api/messages/{message_id}/charts/{chart_id}.png"


def test_saved_mock_balance_exposes_verified_invoice_amounts_without_rendering(client, monkeypatch):
    _, messages = completed_answer(client)
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)

    charts = descriptors(client, messages[-1]["id"])

    assert charts
    invoice_chart = next(
        chart
        for chart in charts
        if {point["label"] for point in chart["points"]} == {"FTI-00000022", "FTI-00000021"}
    )
    assert invoice_chart["kind"] == "bar"
    assert invoice_chart["company"].lower() == "usmf"
    assert invoice_chart["currency"] == "INR"
    assert invoice_chart["source_count"] == 2
    assert {point["label"]: Decimal(point["amount"]) for point in invoice_chart["points"]} == {
        "FTI-00000022": Decimal("35000"),
        "FTI-00000021": Decimal("75000"),
    }
    assert all(isinstance(point["amount"], str) for point in invoice_chart["points"])
    render.assert_not_awaited()


@pytest.mark.parametrize("theme,size", [("light", "standard"), ("dark", "standard"), ("dark", "compact")])
def test_chart_image_returns_png_and_passes_selected_saved_chart_and_theme(client, monkeypatch, theme, size):
    _, messages = completed_answer(client)
    message_id = messages[-1]["id"]
    chart = descriptors(client, message_id)[0]
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)

    response = client.get(image_path(message_id, chart["id"]), params={"theme": theme, "size": size})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG
    expected = {**chart, "render_size": "compact"} if size == "compact" else chart
    render.assert_awaited_once_with(expected, theme)
    assert "render_size" not in descriptors(client, message_id)[0]


def test_invalid_theme_size_and_unknown_chart_never_call_renderer(client, monkeypatch):
    _, messages = completed_answer(client)
    message_id = messages[-1]["id"]
    chart = descriptors(client, message_id)[0]
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)

    invalid = client.get(image_path(message_id, chart["id"]), params={"theme": "arbitrary"})
    invalid_size = client.get(image_path(message_id, chart["id"]), params={"size": "arbitrary"})
    missing = client.get(image_path(message_id, "unknown-chart"))

    assert invalid.status_code == 422
    assert invalid_size.status_code == 422
    assert missing.status_code == 404
    render.assert_not_awaited()


def test_other_profile_cannot_list_or_render_previously_rendered_chart(client, monkeypatch):
    _, messages = completed_answer(client)
    message_id = messages[-1]["id"]
    chart = descriptors(client, message_id)[0]
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)
    assert client.get(image_path(message_id, chart["id"])).status_code == 200
    render.reset_mock()

    client.cookies.clear()

    assert client.get(f"/api/messages/{message_id}/charts").status_code == 404
    assert client.get(image_path(message_id, chart["id"])).status_code == 404
    render.assert_not_awaited()


def test_deleted_conversation_cannot_list_or_render_previously_rendered_chart(client, monkeypatch):
    conversation_id, messages = completed_answer(client)
    message_id = messages[-1]["id"]
    chart = descriptors(client, message_id)[0]
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)
    assert client.get(image_path(message_id, chart["id"])).status_code == 200
    render.reset_mock()

    assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204

    assert client.get(f"/api/messages/{message_id}/charts").status_code == 404
    assert client.get(image_path(message_id, chart["id"])).status_code == 404
    render.assert_not_awaited()


@pytest.mark.parametrize(
    "role,status",
    [("user", "completed"), ("assistant", "error"), ("assistant", "streaming"), ("assistant", "interrupted")],
)
def test_only_completed_assistant_evidence_can_be_visualized(client, monkeypatch, role, status):
    _, messages = completed_answer(client)
    message_id = messages[-1]["id"]
    chart = descriptors(client, message_id)[0]
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)

    async def change_message_state():
        async with client.app.state.sessions() as session:
            row = await session.get(Message, message_id)
            row.role = role
            row.status = status
            await session.commit()

    client.portal.call(change_message_state)

    assert descriptors(client, message_id) == []
    assert client.get(image_path(message_id, chart["id"])).status_code == 404
    render.assert_not_awaited()


def test_payment_chart_is_rebuilt_from_persisted_evidence_after_restart(settings):
    with TestClient(create_app(settings)) as first:
        _, messages = completed_answer(first, "Show Asterion's payment history.")
        message_id = messages[-1]["id"]
        before = descriptors(first, message_id)
        cookie = first.cookies.get("finance_session")
        assert before
        payment_chart = next(chart for chart in before if chart["kind"] == "line")
        assert payment_chart["company"].lower() == "usmf"
        assert payment_chart["currency"] == "INR"
        assert payment_chart["source_count"] == 1
        assert len(payment_chart["points"]) == 1
        assert payment_chart["points"][0]["label"] == "2026-09-20"
        assert Decimal(payment_chart["points"][0]["amount"]) == Decimal("25000")

    with TestClient(create_app(settings)) as second:
        second.cookies.set("finance_session", cookie)
        assert descriptors(second, message_id) == before


def test_unknown_message_is_404_without_rendering(client, monkeypatch):
    render = AsyncMock(return_value=PNG)
    monkeypatch.setattr(client.app.state.charts, "render", render)

    assert client.get("/api/messages/unknown-message/charts").status_code == 404
    assert client.get(image_path("unknown-message", "unknown-chart")).status_code == 404
    render.assert_not_awaited()
