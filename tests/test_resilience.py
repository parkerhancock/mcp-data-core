"""Tests for retry behavior on transient upstream failures.

Regression coverage for the bug where typed ``ServerError`` (5xx) was never
retried: ``is_retryable_error`` only consulted ``RETRYABLE_STATUS_CODES`` for
``httpx.HTTPStatusError``, which ``BaseAsyncClient`` never raises — it raises
the typed errors instead. So USPTO ``data-documents`` 504s blew straight
through with zero retries.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_data_core.base_client import BaseAsyncClient, _redact_url_query
from mcp_data_core.exceptions import (
    ApiError,
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    RetryableAuthenticationError,
    ServerError,
)
from mcp_data_core.resilience import is_retryable_error


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tenacity's between-attempt backoff instant so tests stay fast."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _instant)


class _Client(BaseAsyncClient):
    DEFAULT_BASE_URL = "https://example.test"
    CACHE_NAME = "test_resilience"


def _client(handler, *, max_retries: int = 4) -> _Client:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return _Client(client=http, use_cache=False, max_retries=max_retries)


class TestIsRetryableError:
    def test_server_error_5xx_is_retryable(self) -> None:
        for status in (500, 502, 503, 504):
            assert is_retryable_error(ServerError("boom", status)) is True

    def test_rate_limit_error_is_retryable(self) -> None:
        assert is_retryable_error(RateLimitError("slow down", 429)) is True
        # Retryable even if no status_code was attached.
        assert is_retryable_error(RateLimitError("slow down")) is True

    def test_not_found_is_not_retryable(self) -> None:
        assert is_retryable_error(NotFoundError("missing", 404)) is False

    def test_auth_error_is_not_retryable(self) -> None:
        assert is_retryable_error(AuthenticationError("nope", 403)) is False

    def test_retryable_auth_error_is_retryable(self) -> None:
        assert is_retryable_error(RetryableAuthenticationError("try again", 400)) is True

    def test_transport_error_is_retryable(self) -> None:
        assert is_retryable_error(httpx.ConnectError("refused")) is True

    def test_unknown_exception_is_not_retryable(self) -> None:
        assert is_retryable_error(ValueError("nope")) is False


class TestRequestRetries:
    @pytest.mark.asyncio
    async def test_transient_504_is_retried_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(504, text="<h1>504 Gateway Time-out</h1>")
            return httpx.Response(200, json={"ok": True})

        async with _client(handler, max_retries=4) as c:
            response = await c._request("GET", "/doc", context="download document X")

        assert response.status_code == 200
        assert calls["n"] == 3  # two 504s, then a 200

    @pytest.mark.asyncio
    async def test_persistent_504_raises_clear_upstream_error(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(504, text="<h1>504 Gateway Time-out</h1>")

        async with _client(handler, max_retries=4) as c:
            with pytest.raises(ServerError) as excinfo:
                await c._request("GET", "/doc", context="download document X for 18320232")

        assert calls["n"] == 4  # exhausted every attempt
        err = excinfo.value
        assert err.status_code == 504
        message = str(err)
        # The original context survives, plus the transient-outage guidance.
        assert "download document X for 18320232" in message
        assert "transient upstream outage" in message
        assert "Retry in a few minutes" in message
        # The original ServerError is chained as the cause.
        assert isinstance(err.__cause__, ServerError)

    @pytest.mark.asyncio
    async def test_404_is_not_retried(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404, text="not found")

        async with _client(handler, max_retries=4) as c:
            with pytest.raises(NotFoundError):
                await c._request("GET", "/doc")

        assert calls["n"] == 1  # no retries on a 404


class TestHttpErrorLogging:
    @pytest.mark.asyncio
    async def test_sensitive_query_values_are_redacted(self, caplog) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad request")

        path = (
            "/doc?api_key=alpha&ServiceKey=bravo&TK=charlie&Key=delta&ToKeN=echo"
            "&access_token=foxtrot&Refresh_Token=golf&normal=a%2Fb"
        )
        async with _client(handler, max_retries=1) as client:
            with caplog.at_level("ERROR", logger="mcp_data_core.base_client"):
                with pytest.raises(ApiError):
                    await client._request("GET", path)

        log_text = caplog.text
        for secret in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"):
            assert secret not in log_text
        assert "api_key=[REDACTED]" in log_text
        assert "ServiceKey=[REDACTED]" in log_text
        assert "TK=[REDACTED]" in log_text
        assert "Key=[REDACTED]" in log_text
        assert "ToKeN=[REDACTED]" in log_text
        assert "access_token=[REDACTED]" in log_text
        assert "Refresh_Token=[REDACTED]" in log_text
        assert "normal=a%2Fb" in log_text

    def test_normal_query_url_is_unchanged(self) -> None:
        url = (
            "https://example.test/doc?not_api_key=alpha&monkey=bravo"
            "&accessToken=charlie&redirect=https%3A%2F%2Fother.test%2Fa%3Fkey%3Dvalue"
        )

        assert _redact_url_query(url) == url
