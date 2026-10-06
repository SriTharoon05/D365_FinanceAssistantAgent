import asyncio
import os
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.metadata import D365MetadataResolver


METADATA = """<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
<edmx:DataServices><Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="D365">
<EntityType Name="CustomerTransaction">
<Key><PropertyRef Name="dataAreaId"/><PropertyRef Name="Voucher"/></Key>
<Property Name="dataAreaId" Type="Edm.String"/>
<Property Name="CustomerAccount" Type="Edm.String"/>
<Property Name="CurrencyCode" Type="Edm.String"/>
<Property Name="Voucher" Type="Edm.String"/>
<Property Name="AmountCur" Type="Edm.Decimal"/>
</EntityType>
<EntityType Name="CustomerOpenTransaction" BaseType="D365.CustomerTransaction">
<Property Name="RemainingAmount" Type="Edm.Decimal"/>
</EntityType>
<EntityContainer Name="Public">
<EntitySet Name="CustomerTransactions" EntityType="D365.CustomerTransaction"/>
<EntitySet Name="CustomerOpenTransactions" EntityType="D365.CustomerOpenTransaction"/>
</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>"""


class MetadataClient:
    def __init__(self, xml=METADATA, probe_error=None, request_error=None):
        self.xml = xml
        self.probe_error = probe_error
        self.request_error = request_error
        self.requests = []
        self.probes = []

    async def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
        if self.request_error is not None:
            raise self.request_error
        return httpx.Response(200, text=self.xml)

    async def get(self, entity, **kwargs):
        self.probes.append((entity, kwargs))
        if self.probe_error is not None:
            raise self.probe_error
        return [{"CustomerAccount": "PRIVATE-CUSTOMER-RECORD", "token": "PRIVATE-PROBE-TOKEN"}]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'finance.db'}",
        d365_base_url="https://finance.example.com",
        d365_tenant_id="private-tenant-id",
        d365_client_id="private-client-id",
        d365_client_secret="PRIVATE-CLIENT-SECRET",
    )


def cache_bytes(resolver, body):
    resolver.cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    resolver.cache_path.write_bytes(body)
    resolver.cache_path.chmod(0o600)


@pytest.mark.asyncio
async def test_cold_download_uses_metadata_specific_limits(settings):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    stages = []

    registry = await resolver.load(on_stage=stages.append)

    assert client.requests == [
        (
            ("GET", "$metadata"),
            {"timeout_seconds": 180, "max_response_bytes": 64 * 1024**2, "retry_reads": False},
        )
    ]
    assert stages == ["loading_metadata", "discovering_entities"]
    assert registry.resolved == {
        "customer_transactions": "CustomerTransactions",
        "open_transactions": "CustomerOpenTransactions",
    }
    assert resolver.cache_source == "network"


@pytest.mark.asyncio
async def test_cache_reuse_skips_download_but_probes_current_permissions(settings):
    first = D365MetadataResolver(MetadataClient(), settings)
    await first.load()
    client = MetadataClient()
    second = D365MetadataResolver(client, settings)
    stages = []

    await second.load(on_stage=stages.append)

    assert client.requests == []
    assert client.probes == [
        ("CustomerTransactions", {"top": 1, "retry_reads": False}),
        ("CustomerOpenTransactions", {"top": 1, "retry_reads": False}),
    ]
    assert stages == ["loading_cached_metadata", "discovering_entities"]
    assert second.cache_source == "cache"


@pytest.mark.asyncio
async def test_cache_is_xml_only_private_and_excludes_credentials_and_probe_records(settings):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    await resolver.load()

    assert resolver.cache_path.read_text() == METADATA
    if hasattr(os, "getuid"):
        assert stat.S_IMODE(resolver.cache_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(resolver.cache_path.parent.stat().st_mode) == 0o700
    assert resolver.cache_path.parent == settings.database_path().parent / "metadata-cache"
    assert resolver.cache_path.name.startswith("v1-")
    serialized = resolver.cache_path.name + resolver.cache_path.read_text()
    for private in (
        settings.d365_tenant_id,
        settings.d365_client_id,
        settings.d365_client_secret,
        "PRIVATE-CUSTOMER-RECORD",
        "PRIVATE-PROBE-TOKEN",
    ):
        assert private not in serialized
    assert list(resolver.cache_path.parent.glob("*.tmp")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("d365_base_url", "https://other-finance.example.com"),
        ("d365_tenant_id", "other-tenant"),
        ("d365_client_id", "other-client"),
    ],
)
async def test_endpoint_tenant_and_application_have_distinct_cache_scopes(settings, field, value):
    first = D365MetadataResolver(MetadataClient(), settings)
    await first.load()
    client = MetadataClient()
    other = D365MetadataResolver(client, settings.model_copy(update={field: value}))

    await other.load()

    assert first.cache_path != other.cache_path
    assert len(client.requests) == 1
    assert first.cache_path.exists()
    assert other.cache_path.exists()


@pytest.mark.asyncio
async def test_client_secret_rotation_reuses_structural_cache(settings):
    first = D365MetadataResolver(MetadataClient(), settings)
    await first.load()
    client = MetadataClient()
    rotated = D365MetadataResolver(
        client, settings.model_copy(update={"d365_client_secret": "ROTATED-SECRET"})
    )

    await rotated.load()

    assert rotated.cache_path == first.cache_path
    assert client.requests == []


@pytest.mark.asyncio
async def test_expired_cache_downloads_fresh_metadata(settings):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    await resolver.load()
    expired = time.time() - settings.d365_metadata_cache_hours * 3600 - 1
    os.utime(resolver.cache_path, (expired, expired))
    client.xml = METADATA.replace('Name="Voucher" Type=', 'Name="ChangedVoucher" Type=')

    await resolver.load()

    assert len(client.requests) == 2
    assert resolver.cache_path.read_text() == client.xml
    assert resolver.cache_source == "network"


@pytest.mark.asyncio
async def test_force_refresh_ignores_unexpired_cache(settings):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    await resolver.load()

    await resolver.load(force_refresh=True)

    assert len(client.requests) == 2
    assert resolver.cache_source == "network"


@pytest.mark.asyncio
async def test_zero_cache_ttl_disables_reads_and_writes(settings):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings.model_copy(update={"d365_metadata_cache_hours": 0}))

    await resolver.load()
    await resolver.load()

    assert len(client.requests) == 2
    assert not resolver.cache_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"<malformed",
        b"<!DOCTYPE foo [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><foo/>",
        b"<!ENTITY unsafe 'value'><foo/>",
        b"\xff\xfeinvalid-utf8",
        b"<root/>",
        METADATA.replace('Name="CustomerTransactions" EntityType=', "EntityType=").encode(),
        METADATA.replace('Name="AmountCur" Type=', "Type=").encode(),
        METADATA.replace(
            'Name="CustomerTransaction">',
            'Name="CustomerTransaction" BaseType="D365.CustomerOpenTransaction">',
        ).encode(),
    ],
)
async def test_invalid_cache_is_replaced_by_fresh_valid_xml(settings, body):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    cache_bytes(resolver, body)

    registry = await resolver.load()

    assert len(client.requests) == 1
    assert registry.loaded
    assert resolver.cache_source == "network"
    assert resolver.cache_path.read_text() == METADATA


@pytest.mark.asyncio
async def test_oversized_cache_is_not_reused(settings):
    settings = settings.model_copy(update={"d365_metadata_max_mb": 1})
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    cache_bytes(resolver, b" " * (1024**2 + 1))

    await resolver.load()

    assert len(client.requests) == 1
    assert client.requests[0][1]["max_response_bytes"] == 1024**2
    assert resolver.cache_path.read_text() == METADATA


@pytest.mark.asyncio
async def test_future_cache_timestamp_requires_fresh_download(settings):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    await resolver.load()
    future = time.time() + 3600
    os.utime(resolver.cache_path, (future, future))
    client = MetadataClient()

    await D365MetadataResolver(client, settings).load()

    assert len(client.requests) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permission bits are unavailable")
async def test_cache_with_nonprivate_permissions_is_refreshed(settings):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    cache_bytes(resolver, METADATA.encode())
    resolver.cache_path.chmod(0o644)

    await resolver.load()

    assert len(client.requests) == 1
    assert stat.S_IMODE(resolver.cache_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_symlink_cache_is_not_followed_or_target_overwritten(settings, tmp_path):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    resolver.cache_path.parent.mkdir(mode=0o700)
    target = tmp_path / "unrelated-user-file.xml"
    target.write_text(METADATA)
    target.chmod(0o600)
    try:
        resolver.cache_path.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Creating symbolic links is unavailable on this platform")

    await resolver.load()

    assert len(client.requests) == 1
    assert not resolver.cache_path.is_symlink()
    assert target.read_text() == METADATA


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes are unavailable")
async def test_nonregular_cache_is_skipped_without_blocking(settings):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    resolver.cache_path.parent.mkdir(mode=0o700)
    os.mkfifo(resolver.cache_path, mode=0o600)

    await resolver.load()

    assert len(client.requests) == 1
    assert resolver.cache_path.is_file()


@pytest.mark.asyncio
async def test_atomic_write_failure_does_not_break_fresh_connection_or_leave_temporary_files(
    settings, monkeypatch
):
    def cannot_replace(*args, **kwargs):
        raise PermissionError("Cache storage is read-only")

    monkeypatch.setattr("app.integrations.d365.metadata.os.replace", cannot_replace)
    resolver = D365MetadataResolver(MetadataClient(), settings)

    registry = await resolver.load()

    assert registry.resolved["customer_transactions"] == "CustomerTransactions"
    assert registry.resolved["open_transactions"] == "CustomerOpenTransactions"
    assert not resolver.cache_path.exists()
    assert list(resolver.cache_path.parent.iterdir()) == []


@pytest.mark.asyncio
async def test_cache_read_permission_failure_downloads_fresh_metadata(settings, monkeypatch):
    client = MetadataClient()
    resolver = D365MetadataResolver(client, settings)
    cache_bytes(resolver, METADATA.encode())
    real_open = os.open

    def blocked_read(path, flags, *args, **kwargs):
        if Path(path) == resolver.cache_path:
            raise PermissionError("Cache read denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("app.integrations.d365.metadata.os.open", blocked_read)

    registry = await resolver.load()

    assert len(client.requests) == 1
    assert registry.loaded


@pytest.mark.asyncio
async def test_cached_structure_does_not_preserve_revoked_capabilities(settings):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    registry = await resolver.load()
    original = registry
    resolver.client = MetadataClient(
        probe_error=AppError("D365_PERMISSION_ERROR", "Entity access denied.", status_code=403)
    )

    registry = await resolver.load()

    assert registry is original
    assert resolver.client.requests == []
    assert len(resolver.client.probes) == 2
    assert registry.resolved == {"customer_transactions": None, "open_transactions": None}


@pytest.mark.asyncio
async def test_failed_refresh_never_falls_back_to_expired_metadata_or_old_capabilities(settings):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    original = await resolver.load()
    expired = time.time() - settings.d365_metadata_cache_hours * 3600 - 1
    os.utime(resolver.cache_path, (expired, expired))
    resolver.client = MetadataClient(
        request_error=AppError("D365_CONNECTION_ERROR", "Fresh download failed.", status_code=503)
    )

    with pytest.raises(AppError, match="Fresh download failed"):
        await resolver.load()

    assert resolver.registry is original
    assert resolver.registry.entities == {}
    assert resolver.registry.resolved == {"customer_transactions": None, "open_transactions": None}
    assert not resolver.registry.loaded
    assert resolver.cache_source is None
    assert resolver.client.probes == []


@pytest.mark.asyncio
async def test_first_probe_failure_cannot_preserve_previous_open_transaction_capability(settings):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    original = await resolver.load()
    resolver.client = MetadataClient(
        probe_error=AppError("D365_AUTHENTICATION_ERROR", "Authentication rejected.", status_code=401)
    )

    with pytest.raises(AppError):
        await resolver.load()

    assert resolver.registry is original
    assert original.resolved == {"customer_transactions": None, "open_transactions": None}


@pytest.mark.asyncio
async def test_invalid_fresh_xml_is_never_persisted(settings):
    resolver = D365MetadataResolver(MetadataClient(xml="<malformed"), settings)

    with pytest.raises(AppError) as error:
        await resolver.load()

    assert error.value.code == "D365_METADATA_INVALID"
    assert not resolver.cache_path.exists()


def test_parser_accepts_valid_metadata_larger_than_previous_ten_megabyte_limit(settings):
    resolver = D365MetadataResolver(None, settings)
    xml = "<!--" + "x" * 10_000_001 + "-->" + METADATA

    assert "CustomerTransactions" in resolver.parse(xml)


def test_parser_limit_counts_utf8_bytes(settings):
    resolver = D365MetadataResolver(None, settings.model_copy(update={"d365_metadata_max_mb": 1}))
    xml = "<!--" + "é" * 600_000 + "-->" + METADATA
    assert len(xml) < 1024**2

    with pytest.raises(AppError) as error:
        resolver.parse(xml)

    assert error.value.code == "D365_METADATA_INVALID"


def test_parser_rejects_excessive_inheritance_without_recursing(settings):
    types = "".join(
        f'<EntityType Name="Type{index}" BaseType="D365.Type{index + 1}"/>' for index in range(130)
    )
    xml = (
        '<Schema Namespace="D365">'
        + types
        + '<EntityContainer><EntitySet Name="Customers" EntityType="D365.Type0"/></EntityContainer></Schema>'
    )

    with pytest.raises(AppError) as error:
        D365MetadataResolver(None, settings).parse(xml)

    assert error.value.code == "D365_METADATA_INVALID"


def test_parser_inheritance_depth_limit_is_independent_of_entity_set_order(settings):
    types = "".join(
        f'<EntityType Name="Type{index}" BaseType="D365.Type{index + 1}"/>' for index in range(130)
    )
    entity_sets = "".join(
        f'<EntitySet Name="Customers{index}" EntityType="D365.Type{index}"/>'
        for index in reversed(range(130))
    )
    xml = (
        '<Schema Namespace="D365">'
        + types
        + "<EntityContainer>"
        + entity_sets
        + "</EntityContainer></Schema>"
    )

    with pytest.raises(AppError) as error:
        D365MetadataResolver(None, settings).parse(xml)

    assert error.value.code == "D365_METADATA_INVALID"


def test_non_sqlite_database_uses_backend_data_cache(settings):
    resolver = D365MetadataResolver(
        None, settings.model_copy(update={"database_url": "postgresql://finance@localhost/finance"})
    )

    assert resolver.cache_path.parent == Path("data/metadata-cache")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unavailable", [("O_NOFOLLOW",), ("getuid",), ("fchmod",), ("O_NOFOLLOW", "getuid", "fchmod")]
)
async def test_cache_operates_without_posix_only_os_features(settings, monkeypatch, unavailable):
    with monkeypatch.context() as context:
        for attribute in unavailable:
            context.delattr(os, attribute, raising=False)
        client = MetadataClient()
        resolver = D365MetadataResolver(client, settings)

        await resolver.load()
        await resolver.load()

        assert len(client.requests) == 1
        assert resolver.cache_source == "cache"
        assert resolver.cache_path.read_text() == METADATA


@pytest.mark.asyncio
async def test_cancelled_parse_worker_cannot_publish_registry_or_cache(settings, monkeypatch):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    parse = resolver._parse_entities

    def blocked_parse(xml):
        started.set()
        try:
            assert release.wait(2)
            return parse(xml)
        finally:
            finished.set()

    monkeypatch.setattr(resolver, "_parse_entities", blocked_parse)
    loading = asyncio.create_task(resolver.load())
    assert await asyncio.to_thread(started.wait, 2)
    loading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loading
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)

    assert resolver.registry.entities == {}
    assert not resolver.registry.loaded
    assert not resolver.cache_path.exists()


@pytest.mark.asyncio
async def test_cancelled_cache_writer_cannot_publish_cache_and_cleans_staging_file(settings, monkeypatch):
    resolver = D365MetadataResolver(MetadataClient(), settings)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    fsync = os.fsync
    stage = resolver._stage_cached_xml

    def blocked_fsync(descriptor):
        started.set()
        assert release.wait(2)
        return fsync(descriptor)

    def watched_stage(*args):
        try:
            return stage(*args)
        finally:
            finished.set()

    monkeypatch.setattr("app.integrations.d365.metadata.os.fsync", blocked_fsync)
    monkeypatch.setattr(resolver, "_stage_cached_xml", watched_stage)
    loading = asyncio.create_task(resolver.load())
    assert await asyncio.to_thread(started.wait, 2)
    loading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loading
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)

    assert resolver.registry.entities == {}
    assert not resolver.registry.loaded
    assert not resolver.cache_path.exists()
    assert list(resolver.cache_path.parent.iterdir()) == []


@pytest.mark.asyncio
async def test_concurrent_cache_writes_publish_complete_xml(settings):
    first = D365MetadataResolver(None, settings)
    second = D365MetadataResolver(None, settings)
    alternate = METADATA.replace('Name="Voucher" Type=', 'Name="AlternateVoucher" Type=')

    await asyncio.gather(first._write_cached_xml(METADATA), second._write_cached_xml(alternate))

    assert first.cache_path.read_text() in (METADATA, alternate)
    if hasattr(os, "getuid"):
        assert stat.S_IMODE(first.cache_path.stat().st_mode) == 0o600
    assert list(first.cache_path.parent.glob("*.tmp")) == []
