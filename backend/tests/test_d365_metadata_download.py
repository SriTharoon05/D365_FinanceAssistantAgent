import asyncio
import gzip

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.integrations.d365.client import D365ODataClient


class Auth:
    async def get_token(self, force_refresh=False):
        return "server-memory-test-token"


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), stall=False):
        self.chunks = chunks
        self.stall = stall
        self.closed = False
        self.consumed = 0

    async def __aiter__(self):
        if self.stall:
            await asyncio.Event().wait()
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_metadata_read_timeout_override_leaves_normal_transport_timeouts_unchanged():
    calls = []
    streams = []

    def transport(request):
        calls.append(request)
        stream = ChunkStream([b"<metadata/>"])
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    settings = Settings(_env_file=None, d365_base_url="https://erp.example.test")
    transport_timeout = httpx.Timeout(30, connect=7, write=9, pool=11)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport), timeout=transport_timeout) as http:
        client = D365ODataClient(settings, Auth(), http)
        result = await client.request(
            "GET", "$metadata", timeout_seconds=180, max_response_bytes=64 * 1024 * 1024, retry_reads=False
        )
        assert result.text == "<metadata/>"
        await client.request("GET", "CustomersV3")
    assert calls[0].extensions["timeout"] == {"connect": 7, "read": 180, "write": 9, "pool": 11}
    assert calls[1].extensions["timeout"] == {"connect": 7, "read": 30, "write": 9, "pool": 11}
    assert calls[0].headers["Accept"] == "application/xml"
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
async def test_declared_oversized_metadata_is_rejected_before_reading_body():
    stream = ChunkStream([b"never read"])
    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Length": "1000000"}, stream=stream)
        )
    ) as http:
        client = D365ODataClient(settings, Auth(), http)
        with pytest.raises(AppError) as error:
            await client.request("GET", "$metadata", timeout_seconds=180, max_response_bytes=100)
    assert error.value.code == "D365_METADATA_TOO_LARGE"
    assert stream.consumed == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_chunked_metadata_size_limit_stops_reading_and_closes_stream():
    stream = ChunkStream([b"x" * 65536] * 5)
    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as http:
        client = D365ODataClient(settings, Auth(), http)
        with pytest.raises(AppError) as error:
            await client.request("GET", "$metadata", timeout_seconds=180, max_response_bytes=65536)
    assert error.value.code == "D365_METADATA_TOO_LARGE"
    assert stream.consumed == 2
    assert stream.closed


@pytest.mark.asyncio
async def test_metadata_size_limit_applies_after_gzip_decompression():
    decoded = b"<metadata>" + b"a" * 100000 + b"</metadata>"
    encoded = gzip.compress(decoded)
    stream = ChunkStream([encoded])
    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(encoded))}, stream=stream
            )
        )
    ) as http:
        client = D365ODataClient(settings, Auth(), http)
        with pytest.raises(AppError) as error:
            await client.request("GET", "$metadata", timeout_seconds=180, max_response_bytes=65536)
    assert error.value.code == "D365_METADATA_TOO_LARGE"
    assert stream.closed


@pytest.mark.asyncio
async def test_compressed_metadata_is_decoded_once_and_remains_readable():
    decoded = b"<metadata>known structure</metadata>"
    encoded = gzip.compress(decoded)
    stream = ChunkStream([encoded])
    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(encoded))}, stream=stream
            )
        )
    ) as http:
        result = await D365ODataClient(settings, Auth(), http).request(
            "GET", "$metadata", timeout_seconds=180, max_response_bytes=65536
        )
    assert result.content == decoded
    assert "Content-Encoding" not in result.headers
    assert stream.closed


@pytest.mark.asyncio
async def test_metadata_wall_clock_deadline_cancels_stalled_stream():
    stream = ChunkStream(stall=True)
    failures = []
    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as http:
        client = D365ODataClient(settings, Auth(), http, on_failure=failures.append)
        with pytest.raises(AppError) as error:
            await client.request(
                "GET", "$metadata", timeout_seconds=0.01, max_response_bytes=65536, retry_reads=False
            )
    assert error.value.code == "D365_METADATA_TIMEOUT"
    assert "D365_METADATA_TIMEOUT_SECONDS" in error.value.message
    assert failures == ["Dynamics 365 request timed out."]
    assert stream.closed


@pytest.mark.asyncio
async def test_metadata_error_response_is_not_buffered_or_retried():
    stream = ChunkStream(stall=True)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(503, stream=stream)

    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = D365ODataClient(settings, Auth(), http)
        with pytest.raises(AppError) as error:
            await client.request(
                "GET", "$metadata", timeout_seconds=180, max_response_bytes=65536, retry_reads=False
            )
    assert error.value.code == "D365_CONNECTION_ERROR"
    assert len(calls) == 1
    assert stream.consumed == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_larger_than_old_ten_mb_metadata_download_is_supported():
    total = 11 * 1024 * 1024
    stream = ChunkStream([b"x" * 65536] * (total // 65536))
    settings = Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    ) as http:
        client = D365ODataClient(settings, Auth(), http)
        result = await client.request(
            "GET", "$metadata", timeout_seconds=180, max_response_bytes=64 * 1024 * 1024, retry_reads=False
        )
    assert len(result.content) == total
    assert stream.closed
