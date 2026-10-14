"""Tests for per-host request pacing and Retry-After handling."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from tenacity import RetryCallState

from mcp_data_core.base_client import BaseAsyncClient, _parse_retry_after
from mcp_data_core.cache import build_cached_http_client
from mcp_data_core.exceptions import RateLimitError, ServerError
from mcp_data_core.pacing import HostPacer, PacedTransport
from mcp_data_core.resilience import default_retryer, wait_retry_after


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.sent_at: list[tuple[str, float]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.sent_at.append((request.url.host, time.monotonic()))
        return httpx.Response(200, json={"ok": True}, headers={"Cache-Control": "max-age=60"})


async def test_pacer_spaces_concurrent_requests_to_one_host() -> None:
    pacer = HostPacer()
    inner = _RecordingTransport()
    async with httpx.AsyncClient(transport=PacedTransport(inner, 0.05, pacer)) as client:
        await asyncio.gather(*(client.get(f"https://a.test/{i}") for i in range(4)))

    times = [at for _, at in inner.sent_at]
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert all(gap >= 0.045 for gap in gaps), gaps


async def test_pacer_does_not_delay_other_hosts() -> None:
    pacer = HostPacer()
    inner = _RecordingTransport()
    async with httpx.AsyncClient(transport=PacedTransport(inner, 1.0, pacer)) as client:
        started = time.monotonic()
        await asyncio.gather(client.get("https://a.test/"), client.get("https://b.test/"))

    assert time.monotonic() - started < 0.5


async def test_zero_interval_never_waits() -> None:
    pacer = HostPacer()
    started = time.monotonic()
    for _ in range(50):
        await pacer.wait("a.test", 0)
    assert time.monotonic() - started < 0.05


def test_pacer_works_across_event_loops() -> None:
    pacer = HostPacer()
    asyncio.run(pacer.wait("a.test", 0.05))
    started = time.monotonic()
    asyncio.run(pacer.wait("a.test", 0.05))
    # The second loop still honors the slot the first loop reserved.
    assert time.monotonic() - started >= 0.03


async def test_cache_hits_skip_pacing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inner = _RecordingTransport()
    real = httpx.AsyncHTTPTransport
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_kwargs: inner)
    try:
        client, manager = build_cached_http_client(
            use_cache=True,
            cache_name="pacing",
            cache_dir=tmp_path,
            min_request_interval=0.5,
        )
    finally:
        monkeypatch.setattr(httpx, "AsyncHTTPTransport", real)

    async with client:
        await client.get("https://a.test/same")
        started = time.monotonic()
        await client.get("https://a.test/same")
        elapsed = time.monotonic() - started
    assert manager is not None
    await manager.close()

    assert len(inner.sent_at) == 1
    assert elapsed < 0.25


def test_base_client_reads_class_interval(tmp_path: Path) -> None:
    class Paced(BaseAsyncClient):
        DEFAULT_BASE_URL = "https://a.test"
        CACHE_NAME = "paced"
        MIN_REQUEST_INTERVAL = 2.0

    paced = Paced(cache_path=tmp_path, use_cache=False)
    transport = paced._client._transport
    assert isinstance(transport, PacedTransport)
    assert transport._interval == 2.0

    unpaced = Paced(cache_path=tmp_path, use_cache=False, min_request_interval=0)
    assert not isinstance(unpaced._client._transport, PacedTransport)


class TestRetryAfter:
    def test_parses_delay_seconds(self) -> None:
        assert _parse_retry_after("7") == 7.0

    def test_parses_http_date(self) -> None:
        when = datetime.now(UTC) + timedelta(seconds=30)
        parsed = _parse_retry_after(format_datetime(when, usegmt=True))
        assert parsed is not None and 25 <= parsed <= 31

    def test_past_date_is_zero(self) -> None:
        when = datetime.now(UTC) - timedelta(minutes=5)
        assert _parse_retry_after(format_datetime(when, usegmt=True)) == 0.0

    def test_garbage_is_none(self) -> None:
        assert _parse_retry_after("soon") is None
        assert _parse_retry_after(None) is None

    @staticmethod
    def _state_after(exc: BaseException) -> RetryCallState:
        state = RetryCallState(retry_object=default_retryer(), fn=None, args=(), kwargs={})
        state.set_exception((type(exc), exc, None))
        return state

    def test_wait_uses_retry_after(self) -> None:
        wait = wait_retry_after(lambda _state: 99.0, max_retry_after=60.0)
        assert wait(self._state_after(RateLimitError("slow", 429, retry_after=3.0))) == 3.0

    def test_wait_caps_retry_after(self) -> None:
        wait = wait_retry_after(lambda _state: 99.0, max_retry_after=60.0)
        assert wait(self._state_after(RateLimitError("slow", 429, retry_after=600.0))) == 60.0

    def test_wait_falls_back_without_retry_after(self) -> None:
        wait = wait_retry_after(lambda _state: 1.5, max_retry_after=60.0)
        assert wait(self._state_after(RateLimitError("slow", 429))) == 1.5
        assert wait(self._state_after(ServerError("down", 503))) == 1.5
