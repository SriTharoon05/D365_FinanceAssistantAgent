"""Connection lifecycle failures must terminate without exposing stale ERP data."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.metadata import EntityInfo
from app.integrations.d365.runtime import D365Runtime


TEST_SECRET = "runtime-client-secret-must-not-appear"
TEST_TOKEN = "runtime-access-token-must-not-appear"


@pytest.fixture
async def runtime(monkeypatch):
    settings = Settings(
        _env_file=None,
        d365_mock_mode=False,
        d365_base_url="https://erp.example.test",
        d365_tenant_id="tenant-id",
        d365_client_id="client-id",
        d365_client_secret=TEST_SECRET,
        d365_connection_timeout_seconds=0.5,
        d365_metadata_timeout_seconds=0.2,
    )

    def unexpected_http(request):
        raise AssertionError("Runtime lifecycle tests must not make external requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_http)) as http:
        value = D365Runtime(settings, http_client=http)
        monkeypatch.setattr(value.auth, "get_token", AsyncMock(return_value=TEST_TOKEN))
        monkeypatch.setattr(value.client, "get", AsyncMock(return_value=[]))

        async def ready_metadata(on_stage=None):
            return mark_metadata_ready(value, on_stage=on_stage)

        monkeypatch.setattr(value.metadata, "load", AsyncMock(side_effect=ready_metadata))
        try:
            yield value
        finally:
            await value.close()


def mark_metadata_ready(runtime, *, complete=True, on_stage=None):
    registry = runtime.metadata.registry
    registry.loaded = True
    registry.entities[runtime.settings.d365_customers_entity] = EntityInfo(
        runtime.settings.d365_customers_entity,
        "D365.Customer",
        {"CustomerAccount": "Edm.String", "dataAreaId": "Edm.String"},
    )
    registry.resolved.update(
        {
            "customer_transactions": "CustomerTransactions",
            "open_transactions": "CustomerOpenTransactions" if complete else None,
        }
    )
    if on_stage:
        on_stage("discovering_entities")
    return registry


def assert_terminal_failure(status):
    assert status["status"] == "disconnected"
    assert status["connection_stage"] == "failed"
    assert status["last_error_summary"]
    assert status["connection_elapsed_seconds"] >= 0
    serialized = json.dumps(status)
    assert TEST_SECRET not in serialized
    assert TEST_TOKEN not in serialized


@pytest.mark.parametrize("stalled_step", ["auth", "customers", "metadata"])
async def test_each_connection_phase_enforces_its_own_deadline(runtime, monkeypatch, stalled_step):
    runtime.settings.d365_connection_timeout_seconds = 0.03
    runtime.settings.d365_metadata_timeout_seconds = 0.03
    never_ready = asyncio.Event()

    async def stall(*args, **kwargs):
        await never_ready.wait()

    if stalled_step == "auth":
        monkeypatch.setattr(runtime.auth, "get_token", AsyncMock(side_effect=stall))
    elif stalled_step == "customers":
        monkeypatch.setattr(runtime.client, "get", AsyncMock(side_effect=stall))
    else:
        monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=stall))
    # The outer deadline bounds a broken implementation as well as the test.
    status = await asyncio.wait_for(runtime.reconnect(initial=True), timeout=0.5)
    assert_terminal_failure(status)
    assert status["connection_timeout_seconds"] == pytest.approx(0.06)
    assert "time" in status["last_error_summary"].lower()
    if stalled_step == "metadata":
        assert "metadata" in status["last_error_summary"].lower()
        assert "0.03" in status["last_error_summary"]
    else:
        runtime.metadata.load.assert_not_awaited()


async def test_slow_metadata_can_exceed_primary_budget_and_still_connect(runtime, monkeypatch):
    runtime.settings.d365_connection_timeout_seconds = 0.03
    runtime.settings.d365_metadata_timeout_seconds = 0.2

    async def slow_metadata(on_stage=None):
        status = runtime.status()
        assert status["connection_stage"] == "loading_metadata"
        assert status["connection_phase_timeout_seconds"] == 0.2
        assert status["connection_phase_elapsed_seconds"] >= 0
        await asyncio.sleep(0.06)
        return mark_metadata_ready(runtime, on_stage=on_stage)

    monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=slow_metadata))
    started = time.monotonic()
    status = await asyncio.wait_for(runtime.reconnect(initial=True), timeout=0.5)
    assert time.monotonic() - started >= 0.06
    assert status["status"] == "connected"
    assert status["connection_stage"] == "ready"
    assert status["connection_timeout_seconds"] == pytest.approx(0.23)


def test_existing_primary_timeout_configuration_gets_independent_metadata_default(tmp_path, monkeypatch):
    monkeypatch.delenv("D365_METADATA_TIMEOUT_SECONDS", raising=False)
    old_configuration = tmp_path / ".env"
    old_configuration.write_text("D365_CONNECTION_TIMEOUT_SECONDS=60\n", encoding="utf-8")
    settings = Settings(_env_file=old_configuration)
    assert settings.d365_connection_timeout_seconds == 60
    assert settings.d365_metadata_timeout_seconds == 180


async def test_explicit_metadata_refresh_bypasses_cached_schema(runtime, monkeypatch):
    mark_metadata_ready(runtime)

    async def fresh_metadata(on_stage=None, force_refresh=False):
        assert force_refresh is True
        return mark_metadata_ready(runtime, on_stage=on_stage)

    monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=fresh_metadata))
    status = await runtime.reconnect(refresh_metadata=True)
    assert status["status"] == "connected"
    assert runtime.metadata.load.await_args.kwargs["force_refresh"] is True


async def test_unexpected_metadata_error_finishes_connection_and_redacts_details(runtime, monkeypatch):
    monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=KeyError(TEST_SECRET + TEST_TOKEN)))
    status = await runtime.reconnect(initial=True)
    assert_terminal_failure(status)
    runtime.auth.get_token.assert_awaited_once()
    runtime.client.get.assert_awaited_once()


async def test_simultaneous_reconnect_returns_active_attempt_without_duplicate_work(runtime, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def authenticate(*args, **kwargs):
        entered.set()
        await release.wait()
        return TEST_TOKEN

    monkeypatch.setattr(runtime.auth, "get_token", AsyncMock(side_effect=authenticate))
    first = asyncio.create_task(runtime.reconnect(initial=True))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        active_status = await asyncio.wait_for(runtime.reconnect(), timeout=0.1)
        assert active_status["status"] == "connecting"
        assert active_status["connection_stage"] == "authenticating"
        runtime.auth.get_token.assert_awaited_once()
        runtime.metadata.load.assert_not_awaited()
        release.set()
        final_status = await first
        assert final_status["status"] == "connected"
        runtime.auth.get_token.assert_awaited_once()
        runtime.client.get.assert_awaited_once()
        runtime.metadata.load.assert_awaited_once()
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("cancelled_step", ["auth", "metadata"])
async def test_cancelled_connection_becomes_terminal_and_can_be_retried(runtime, monkeypatch, cancelled_step):
    entered = asyncio.Event()
    original_metadata_load = runtime.metadata.load

    async def authenticate(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def unfinished_metadata(on_stage=None):
        mark_metadata_ready(runtime, on_stage=on_stage)
        entered.set()
        await asyncio.Event().wait()

    if cancelled_step == "auth":
        monkeypatch.setattr(runtime.auth, "get_token", AsyncMock(side_effect=authenticate))
    else:
        monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=unfinished_metadata))
    attempt = asyncio.create_task(runtime.reconnect(initial=True))
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    attempt.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attempt
    assert_terminal_failure(runtime.status())
    with pytest.raises(AppError):
        await runtime.finance.get_customer("TEST-001", company="usmf")
    monkeypatch.setattr(runtime.auth, "get_token", AsyncMock(return_value=TEST_TOKEN))
    monkeypatch.setattr(runtime.metadata, "load", original_metadata_load)
    retried = await runtime.reconnect()
    assert retried["status"] == "connected"
    assert retried["connection_stage"] == "ready"


async def test_stale_metadata_never_permits_finance_reads_during_discovery(runtime, monkeypatch):
    mark_metadata_ready(runtime)
    entered, release = asyncio.Event(), asyncio.Event()

    async def discover(on_stage=None):
        mark_metadata_ready(runtime, on_stage=on_stage)
        entered.set()
        await release.wait()
        return runtime.metadata.registry

    monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=discover))
    attempt = asyncio.create_task(runtime.reconnect(initial=True))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        status = runtime.status()
        assert status["status"] == "connecting"
        assert status["connection_stage"] == "discovering_entities"
        assert status["connection_phase_timeout_seconds"] == 0.2
        assert status["metadata_loaded"]
        calls_before_read = runtime.client.get.await_count
        with pytest.raises(AppError) as rejected:
            await runtime.finance.get_customer("TEST-001", company="usmf")
        assert "disconnected" in rejected.value.message.lower() or "connect" in rejected.value.message.lower()
        assert runtime.client.get.await_count == calls_before_read
        release.set()
        assert (await attempt)["status"] == "connected"
    finally:
        release.set()
        if not attempt.done():
            attempt.cancel()
        await asyncio.gather(attempt, return_exceptions=True)


async def test_optional_probe_failure_does_not_disconnect_primary_customer_access(runtime, monkeypatch):
    async def restricted_metadata(on_stage=None):
        registry = mark_metadata_ready(runtime, complete=False, on_stage=on_stage)
        # The transport notifies the runtime before the resolver decides this
        # optional entity permission failure can be skipped.
        runtime._request_failed("Dynamics 365 returned HTTP 403 for an optional entity.")
        assert runtime.status()["status"] == "connecting"
        return registry

    monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=restricted_metadata))
    status = await runtime.reconnect(initial=True)
    assert status["status"] == "degraded"
    assert status["connection_stage"] == "ready"
    assert status["last_error_summary"] is None


@pytest.mark.parametrize("complete, expected_status", [(True, "connected"), (False, "degraded")])
async def test_customer_access_and_metadata_discovery_establish_terminal_readiness(
    runtime,
    monkeypatch,
    complete,
    expected_status,
):
    order = []

    async def authenticate(*args, **kwargs):
        order.append("auth")
        assert runtime.status()["connection_stage"] == "authenticating"
        return TEST_TOKEN

    async def customers(*args, **kwargs):
        order.append("customers")
        assert runtime.status()["connection_stage"] == "checking_customers"
        runtime.client.last_success_at = "2026-10-06T12:00:00+00:00"
        return []

    async def metadata(on_stage=None):
        order.append("metadata")
        assert runtime.status()["connection_stage"] == "loading_metadata"
        return mark_metadata_ready(runtime, complete=complete, on_stage=on_stage)

    monkeypatch.setattr(runtime.auth, "get_token", AsyncMock(side_effect=authenticate))
    monkeypatch.setattr(runtime.client, "get", AsyncMock(side_effect=customers))
    monkeypatch.setattr(runtime.metadata, "load", AsyncMock(side_effect=metadata))
    status = await runtime.reconnect(initial=True)
    assert order == ["auth", "customers", "metadata"]
    assert status["status"] == expected_status
    assert status["connection_stage"] == "ready"
    assert status["last_error_summary"] is None
    assert status["last_success_at"] == "2026-10-06T12:00:00+00:00"
    assert status["connection_elapsed_seconds"] >= 0
