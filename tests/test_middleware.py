"""Tests for the tool-call logger configuration in mcp_data_core.mcp.middleware."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError


@pytest.fixture
def fresh_middleware(monkeypatch, tmp_path):
    """Reload the middleware module with a clean logger between tests.

    The module attaches handlers to a process-level logger
    (``mcp_data_core.mcp.tools``); reloading + clearing handlers before
    each test gives an isolated configuration call.
    """
    # Clear any LOG_DIR / LOG_TO_STDOUT inherited from the host environment
    for var in (
        "LAW_TOOLS_CORE_LOG_DIR",
        "LAW_TOOLS_LOG_DIR",
        "LAW_TOOLS_CORE_LOG_TO_STDOUT",
        "LAW_TOOLS_LOG_TO_STDOUT",
        "LAW_TOOLS_CORE_ACTOR_HASH_KEY",
        "LAW_TOOLS_ACTOR_HASH_KEY",
    ):
        monkeypatch.delenv(var, raising=False)

    from mcp_data_core.mcp import middleware

    importlib.reload(middleware)
    middleware._tool_logger.handlers.clear()
    yield middleware
    middleware._tool_logger.handlers.clear()


def _attached_handlers(middleware_mod) -> list[logging.Handler]:
    """Handlers attached by _configure_tool_logger.

    pytest's logging plugin attaches its own LogCaptureHandlers to every
    logger during the test-call phase; filter those out so assertions see
    only what the module under test attached.
    """
    return [
        h
        for h in middleware_mod._tool_logger.handlers
        if not type(h).__module__.startswith("_pytest")
    ]


def test_no_handlers_when_no_env(fresh_middleware) -> None:
    """With neither env var set, no handler is attached and logging is a no-op."""
    fresh_middleware._configure_tool_logger()
    assert _attached_handlers(fresh_middleware) == []


def test_file_handler_when_log_dir_set(monkeypatch, tmp_path, fresh_middleware) -> None:
    """LAW_TOOLS_CORE_LOG_DIR attaches a rotating file handler."""
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_DIR", str(tmp_path))
    fresh_middleware._configure_tool_logger()
    handlers = _attached_handlers(fresh_middleware)
    assert len(handlers) == 1
    assert isinstance(handlers[0], RotatingFileHandler)
    assert str(handlers[0].baseFilename).endswith("tool_calls.jsonl")


def test_stdout_handler_when_log_to_stdout_set(monkeypatch, fresh_middleware) -> None:
    """LAW_TOOLS_CORE_LOG_TO_STDOUT=true attaches a StreamHandler to sys.stdout."""
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_TO_STDOUT", "true")
    fresh_middleware._configure_tool_logger()
    handlers = _attached_handlers(fresh_middleware)
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.StreamHandler)
    assert handlers[0].stream is sys.stdout


def test_both_handlers_when_both_set(monkeypatch, tmp_path, fresh_middleware) -> None:
    """Both env vars set → both handlers attached."""
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_TO_STDOUT", "1")
    fresh_middleware._configure_tool_logger()
    assert len(_attached_handlers(fresh_middleware)) == 2


def test_stdout_handler_emits_json(monkeypatch, capsys, fresh_middleware) -> None:
    """Records logged through the configured logger reach stdout as one JSON line."""
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_TO_STDOUT", "yes")
    fresh_middleware._configure_tool_logger()
    fresh_middleware._tool_logger.info(json.dumps({"tool": "test_tool", "ok": True}))
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip()) == {"tool": "test_tool", "ok": True}


def test_idempotent_configure(monkeypatch, fresh_middleware) -> None:
    """Calling _configure_tool_logger twice attaches handlers only once."""
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_TO_STDOUT", "true")
    fresh_middleware._configure_tool_logger()
    fresh_middleware._configure_tool_logger()
    assert len(_attached_handlers(fresh_middleware)) == 1


def test_pseudonymous_actor_id_is_normalized_and_service_scoped(fresh_middleware) -> None:
    """The same normalized email is stable only within a service's key."""
    expected = hmac.new(
        b"service-a",
        b"person@example.com",
        hashlib.sha256,
    ).hexdigest()[:32]

    assert (
        fresh_middleware.pseudonymous_actor_id(
            " Person@Example.COM ",
            "service-a",
        )
        == expected
    )
    assert (
        fresh_middleware.pseudonymous_actor_id(
            "person@example.com",
            "service-b",
        )
        != expected
    )


@pytest.mark.parametrize("email", [None, "", "unauthenticated"])
def test_pseudonymous_actor_id_omits_non_users(email, fresh_middleware) -> None:
    assert fresh_middleware.pseudonymous_actor_id(email, "service-a") is None


async def test_tool_call_logger_adds_only_pseudonymous_actor(
    monkeypatch,
    capsys,
    fresh_middleware,
) -> None:
    """Authenticated calls include an actor ID without exposing the email."""
    email = "person@example.com"
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_TO_STDOUT", "true")
    monkeypatch.setenv("LAW_TOOLS_CORE_ACTOR_HASH_KEY", "service-a")
    monkeypatch.setattr(
        fresh_middleware,
        "get_access_token",
        lambda: SimpleNamespace(claims={"email": email}),
    )
    context = SimpleNamespace(
        message=SimpleNamespace(name="test_tool"),
        timestamp=SimpleNamespace(isoformat=lambda: "2026-07-27T12:00:00+00:00"),
    )

    async def call_next(_context):
        return {"ok": True}

    assert await fresh_middleware.ToolCallLogger().on_call_tool(context, call_next) == {
        "ok": True
    }
    record = json.loads(capsys.readouterr().out.strip())
    assert record["actor_id"] == fresh_middleware.pseudonymous_actor_id(
        email,
        "service-a",
    )
    assert email not in json.dumps(record)


@pytest.mark.parametrize("falsy", ["false", "FALSE", "0", "no", "off", ""])
def test_log_to_stdout_falsy_values_dont_attach(monkeypatch, falsy, fresh_middleware) -> None:
    """Falsy values for LOG_TO_STDOUT do not attach a stdout handler."""
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_TO_STDOUT", falsy)
    fresh_middleware._configure_tool_logger()
    assert _attached_handlers(fresh_middleware) == []


# ---------------------------------------------------------------------------
# FriendlyErrors
# ---------------------------------------------------------------------------


def _fake_context(name: str = "some_tool") -> Any:
    """Minimal stand-in for MiddlewareContext with a tool-call message."""
    return SimpleNamespace(message=SimpleNamespace(name=name))


def _raiser(exc: BaseException):
    async def call_next(context):  # noqa: ANN001, ANN202
        raise exc

    return call_next


async def test_friendly_errors_maps_typed_exception() -> None:
    """A typed exception raised below the middleware gets the friendly mapping."""
    from mcp_data_core.exceptions import RateLimitError
    from mcp_data_core.mcp.middleware import FriendlyErrors

    exc = RateLimitError("slow down", status_code=429)
    with pytest.raises(ToolError, match=r"\[retryable\] Rate limited by upstream"):
        await FriendlyErrors().on_call_tool(_fake_context(), _raiser(exc))


async def test_friendly_errors_maps_toolerror_wrapping_typed_cause() -> None:
    """FastMCP wraps tool-body exceptions in ToolError below the middleware.

    fastmcp.server.server.FastMCP.call_tool runs middleware around an inner
    ``call_tool(run_middleware=False)`` that converts a tool-raised exception
    to ``ToolError("Error calling tool 'X': <str(exc)>") from exc``. The
    middleware must recover the typed cause and remap it.
    """
    from mcp_data_core.exceptions import AuthenticationError
    from mcp_data_core.mcp.middleware import FriendlyErrors

    cause = AuthenticationError("PACER authentication failed", status_code=401)
    try:
        raise ToolError(f"Error calling tool 'some_tool': {cause}") from cause
    except ToolError as wrapped_exc:
        wrapped = wrapped_exc

    with pytest.raises(
        ToolError,
        match=r"\[not-retryable\] Upstream authentication failed: "
        r"PACER authentication failed",
    ) as excinfo:
        await FriendlyErrors().on_call_tool(_fake_context(), _raiser(wrapped))
    assert excinfo.value.__cause__ is cause


async def test_friendly_errors_walks_nested_cause_chain() -> None:
    """A typed exception buried more than one link deep is still found."""
    from mcp_data_core.exceptions import ServerError
    from mcp_data_core.mcp.middleware import FriendlyErrors

    root = ServerError("upstream 500", status_code=500)
    try:
        raise RuntimeError("intermediate") from root
    except RuntimeError as mid_exc:
        try:
            raise ToolError("Error calling tool 'some_tool': intermediate") from mid_exc
        except ToolError as wrapped_exc:
            wrapped = wrapped_exc

    with pytest.raises(ToolError, match=r"\[retryable\] Upstream server error"):
        await FriendlyErrors().on_call_tool(_fake_context(), _raiser(wrapped))


async def test_friendly_errors_passes_through_deliberate_toolerror() -> None:
    """A ToolError raised directly by a tool (no __cause__) is untouched."""
    from mcp_data_core.mcp.middleware import FriendlyErrors

    deliberate = ToolError("deliberate client-facing message")
    with pytest.raises(ToolError, match="^deliberate client-facing message$"):
        await FriendlyErrors().on_call_tool(_fake_context(), _raiser(deliberate))


async def test_friendly_errors_passes_through_toolerror_with_unmapped_cause() -> None:
    """A ToolError chained from an exception with no mapping is untouched."""
    from mcp_data_core.mcp.middleware import FriendlyErrors

    try:
        raise ToolError("Error calling tool 'some_tool': boom") from KeyError("boom")
    except ToolError as wrapped_exc:
        wrapped = wrapped_exc

    with pytest.raises(ToolError, match="boom") as excinfo:
        await FriendlyErrors().on_call_tool(_fake_context(), _raiser(wrapped))
    assert excinfo.value is wrapped


async def test_friendly_errors_end_to_end_over_fastmcp() -> None:
    """Full stack: tool raises AuthenticationError, client sees the friendly message."""
    from fastmcp import Client, FastMCP

    from mcp_data_core.exceptions import AuthenticationError
    from mcp_data_core.mcp.middleware import FriendlyErrors

    server = FastMCP("test")
    server.add_middleware(FriendlyErrors())

    @server.tool
    async def pacer_tool() -> dict:
        raise AuthenticationError(
            "PACER authentication failed: Invalid username or password",
            status_code=401,
        )

    @server.tool
    async def deliberate_tool() -> dict:
        raise ToolError("deliberate client-facing message")

    async with Client(server) as client:
        with pytest.raises(
            ToolError,
            match=r"^\[not-retryable\] Upstream authentication failed: "
            r"PACER authentication failed: Invalid username or password",
        ):
            await client.call_tool("pacer_tool", {})
        with pytest.raises(ToolError, match="^deliberate client-facing message$"):
            await client.call_tool("deliberate_tool", {})


def test_bearer_token_auth_is_deprecated() -> None:
    """Constructing the legacy BearerTokenAuth middleware emits a DeprecationWarning."""
    from mcp_data_core.mcp.middleware import BearerTokenAuth

    with pytest.warns(DeprecationWarning, match="make_auth"):
        BearerTokenAuth()


def test_bearer_token_auth_not_in_public_api() -> None:
    """BearerTokenAuth is importable but no longer advertised in the package __all__."""
    import mcp_data_core.mcp as mcp_pkg

    assert "BearerTokenAuth" not in mcp_pkg.__all__
    # Still importable for backward compatibility.
    assert mcp_pkg.BearerTokenAuth is not None
