"""Tests for BaseAsyncClient URL building."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from mcp_data_core.base_client import BaseAsyncClient


def test_absolute_urls_bypass_base_url(tmp_path: Path) -> None:
    class Client(BaseAsyncClient):
        DEFAULT_BASE_URL = "https://api.test/v1"

    client = Client(cache_path=tmp_path, use_cache=False)
    assert client._build_url("/items") == "https://api.test/v1/items"
    assert client._build_url("https://files.test/a.pdf") == "https://files.test/a.pdf"
    assert client._build_url("http://files.test/a.pdf") == "http://files.test/a.pdf"


class _BearerAuth(httpx2.Auth):
    """Minimal httpx2 auth flow that stamps a bearer token."""

    def auth_flow(self, request: httpx2.Request):  # type: ignore[override]
        request.headers["Authorization"] = "Bearer t0k"
        yield request


async def test_injected_transport_keeps_headers_auth_and_base_url(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True})

    class Client(BaseAsyncClient):
        DEFAULT_BASE_URL = "https://api.test/v1"

    async with Client(
        cache_path=tmp_path,
        use_cache=False,
        headers={"X-Api-Key": "k"},
        auth=_BearerAuth(),
        transport=httpx2.MockTransport(handler),
    ) as client:
        assert await client._request_json("GET", "/items") == {"ok": True}

    assert str(seen[0].url) == "https://api.test/v1/items"
    assert seen[0].headers["X-Api-Key"] == "k"
    assert seen[0].headers["Authorization"] == "Bearer t0k"


async def test_injected_transport_sits_below_the_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json={"n": calls}, headers={"Cache-Control": "max-age=60"})

    class Client(BaseAsyncClient):
        DEFAULT_BASE_URL = "https://api.test"
        CACHE_NAME = "transport_test"

    async with Client(cache_path=tmp_path, transport=httpx2.MockTransport(handler)) as client:
        first = await client._request_json("GET", "/a")
        second = await client._request_json("GET", "/a")

    assert first == second == {"n": 1}
    assert calls == 1


async def test_injected_transport_gets_retries_and_typed_errors(tmp_path: Path) -> None:
    from mcp_data_core.exceptions import NotFoundError

    statuses = iter([503, 200])

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/missing":
            return httpx2.Response(404, text="nope")
        return httpx2.Response(next(statuses), json={"ok": True})

    class Client(BaseAsyncClient):
        DEFAULT_BASE_URL = "https://api.test"

    async with Client(
        cache_path=tmp_path, use_cache=False, transport=httpx2.MockTransport(handler)
    ) as client:
        assert await client._request_json("GET", "/flaky") == {"ok": True}
        with pytest.raises(NotFoundError):
            await client._request("GET", "/missing")
