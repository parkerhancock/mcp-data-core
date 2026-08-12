"""FastMCP middleware: friendly errors, bearer auth, and tool-call logging.

Install order matters. ``FriendlyErrors`` must sit outer of
``ToolCallLogger`` so the JSONL log records the original exception type
(e.g. ``httpx.ReadError``) rather than the remapped ``ToolError``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import time
import traceback
import warnings
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

from mcp_data_core.exceptions import (
    AuthenticationError,
    ConfigurationError,
    McpDataCoreError,
    NotFoundError,
    RateLimitError,
    RetryableAuthenticationError,
    ServerError,
)

from . import _env

# ---------------------------------------------------------------------------
# FriendlyErrors
# ---------------------------------------------------------------------------

_friendly_logger = logging.getLogger(__name__ + ".friendly")

RETRYABLE = "[retryable]"
NOT_RETRYABLE = "[not-retryable]"


def _friendly_message(tool_name: str, exc: BaseException) -> str | None:
    """Map an exception to a clean client-facing message, or None to pass through."""
    if isinstance(exc, (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError)):
        return (
            f"{RETRYABLE} Upstream service dropped the connection while "
            f"handling {tool_name}. This is usually transient — please retry."
        )
    if isinstance(exc, httpx.TimeoutException):
        return (
            f"{RETRYABLE} Upstream service timed out while handling {tool_name}. "
            f"Retry, or narrow the request if possible."
        )
    if isinstance(exc, RateLimitError):
        return f"{RETRYABLE} Rate limited by upstream: {exc}"
    if isinstance(exc, ServerError):
        return f"{RETRYABLE} Upstream server error: {exc}"
    if isinstance(exc, NotFoundError):
        return f"{NOT_RETRYABLE} Not found: {exc}"
    if isinstance(exc, RetryableAuthenticationError):
        return f"{RETRYABLE} The request could not be completed. Please retry shortly."
    if isinstance(exc, AuthenticationError):
        return (
            f"{NOT_RETRYABLE} The request could not be completed. "
            "Retrying is unlikely to help. Please contact support."
        )
    if isinstance(exc, ConfigurationError):
        return f"{NOT_RETRYABLE} Server misconfiguration: {exc}"
    if isinstance(exc, McpDataCoreError):
        return f"{NOT_RETRYABLE} {exc}"
    return None


class FriendlyErrors(Middleware):
    """Remap transport/typed errors to clean ToolError messages."""

    async def on_call_tool(self, context, call_next):  # noqa: ANN001
        tool_name = getattr(context.message, "name", "unknown_tool")
        try:
            return await call_next(context)
        except ToolError as exc:
            # FastMCP wraps exceptions raised inside tool bodies in
            # ToolError("Error calling tool 'X': <str(exc)>") *below* this
            # middleware (fastmcp.server.server.FastMCP.call_tool runs
            # middleware around an inner call_tool(run_middleware=False)
            # that does the wrapping), chaining the original via
            # ``raise ... from exc``. Walk the explicit __cause__ chain so
            # typed exceptions still get the friendly mapping. ToolErrors
            # raised deliberately by tools carry no __cause__ and pass
            # through untouched.
            cause = exc.__cause__
            depth = 0
            while cause is not None and depth < 10:
                message = _friendly_message(tool_name, cause)
                if message is not None:
                    _friendly_logger.warning(
                        "Remapping %s from %s: %s",
                        tool_name,
                        type(cause).__name__,
                        cause,
                        exc_info=cause,
                    )
                    raise ToolError(message) from cause
                cause = cause.__cause__
                depth += 1
            raise
        except Exception as exc:
            message = _friendly_message(tool_name, exc)
            if message is None:
                raise
            _friendly_logger.warning(
                "Remapping %s from %s: %s",
                tool_name,
                type(exc).__name__,
                exc,
                exc_info=exc,
            )
            raise ToolError(message) from exc


# ---------------------------------------------------------------------------
# BearerTokenAuth
# ---------------------------------------------------------------------------


class BearerTokenAuth(Middleware):
    """Reject requests without a valid bearer token.

    .. deprecated::
        ``BearerTokenAuth`` predates FastMCP's auth-provider model and
        duplicates what FastMCP's ``StaticTokenVerifier`` does natively.
        Use :func:`mcp_data_core.mcp.auth.make_auth` (which returns a
        ``StaticTokenVerifier`` for the static-token case) and pass it to
        ``build_server(auth=...)`` instead. This middleware is retained for
        backward compatibility only and is slated for removal once known
        consumers are confirmed migrated.

    Reads the expected token from ``LAW_TOOLS_CORE_API_KEY`` (or the
    legacy ``LAW_TOOLS_API_KEY`` alias). If the variable is not set,
    all requests are allowed (local/stdio mode).
    """

    def __init__(self) -> None:
        warnings.warn(
            "BearerTokenAuth is deprecated and will be removed in a future "
            "release. Use mcp_data_core.mcp.auth.make_auth(...) with "
            "build_server(auth=...) (FastMCP StaticTokenVerifier / MultiAuth) "
            "instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._token = _env.get("API_KEY", "")

    async def on_call_tool(self, context: MiddlewareContext, call_next):  # noqa: ANN001
        if self._token and not self._check_auth(context):
            raise ToolError("Unauthorized: invalid or missing bearer token")
        return await call_next(context)

    def _check_auth(self, context: MiddlewareContext) -> bool:
        # FastMCP exposes transport headers via context.request
        # when running over HTTP. For stdio, headers are absent.
        request = getattr(context, "request", None)
        if request is None:
            return True  # stdio — no auth needed
        headers = getattr(request, "headers", {})
        auth_header = headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:] == self._token
        return False


# ---------------------------------------------------------------------------
# ToolCallLogger
# ---------------------------------------------------------------------------

_tool_logger = logging.getLogger("mcp_data_core.mcp.tools")
_tool_logger.setLevel(logging.INFO)
_tool_logger.propagate = False

# Idempotence flag. Guarding on ``_tool_logger.handlers`` is not safe:
# other code (e.g. pytest's logging plugin) may attach its own handlers
# to this logger, which would make configuration silently no-op.
_tool_logger_configured = False


def pseudonymous_actor_id(email: str | None, secret: str | None = None) -> str | None:
    """Return a service-scoped actor ID without retaining the email address."""
    normalized_email = (email or "").strip().lower()
    hash_key = secret if secret is not None else _env.get("ACTOR_HASH_KEY")
    if not normalized_email or normalized_email == "unauthenticated" or not hash_key:
        return None
    return hmac.new(
        hash_key.encode(),
        normalized_email.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def _current_actor_id() -> str | None:
    """Read the authenticated email claim and immediately pseudonymize it."""
    if not _env.get("ACTOR_HASH_KEY"):
        return None
    try:
        token = get_access_token()
    except Exception:
        return None
    claims = getattr(token, "claims", None) or {}
    return pseudonymous_actor_id(claims.get("email"))


def _configure_tool_logger() -> None:
    """Attach handlers based on env config. Safe to call multiple times.

    Handlers attached:

    * ``LAW_TOOLS_CORE_LOG_DIR=/path`` → rotating file at
      ``<dir>/tool_calls.jsonl`` (50 MB × 5 backups). Right for VM-style
      deploys where the filesystem persists.
    * ``LAW_TOOLS_CORE_LOG_TO_STDOUT=true`` → stream handler to
      ``sys.stdout``. Right for Cloud Run / container deploys where the
      filesystem is ephemeral and stdout is captured by Cloud Logging.

    Either, both, or neither. If neither is set, structured tool-call
    logging is silently disabled (callers get tool results normally).
    """
    global _tool_logger_configured
    if _tool_logger_configured:
        return
    _tool_logger_configured = True
    formatter = logging.Formatter("%(message)s")
    log_dir = _env.get("LOG_DIR")
    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path / "tool_calls.jsonl",
            maxBytes=50 * 1024 * 1024,  # 50 MB
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        _tool_logger.addHandler(file_handler)
    if _env.get("LOG_TO_STDOUT").lower() in ("1", "true", "yes", "on"):
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        _tool_logger.addHandler(stdout_handler)


class ToolCallLogger(Middleware):
    """Log every tool call as a JSON line: tool name, duration, success/error."""

    def __init__(self) -> None:
        _configure_tool_logger()

    async def on_call_tool(self, context, call_next):  # noqa: ANN001
        params = context.message
        tool_name = params.name
        t0 = time.monotonic()
        error_msg = None
        error_tb = None
        try:
            result = await call_next(context)
            return result
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            error_tb = traceback.format_exc()
            raise
        finally:
            duration_ms = round((time.monotonic() - t0) * 1000, 1)
            record = {
                "ts": context.timestamp.isoformat(),
                "tool": tool_name,
                "duration_ms": duration_ms,
                "ok": error_msg is None,
            }
            actor_id = _current_actor_id()
            if actor_id:
                record["actor_id"] = actor_id
            if error_msg:
                record["error"] = error_msg
                record["traceback"] = error_tb
            _tool_logger.info(json.dumps(record, default=str))


# ---------------------------------------------------------------------------
# DefaultToolTitles
# ---------------------------------------------------------------------------

# Acronyms kept upper-cased in a humanized title instead of being title-cased
# ("fda" -> "FDA", not "Fda"). Domain terms for the data sources these
# servers wrap.
_TITLE_ACRONYMS = frozenset(
    {
        "fda",
        "cms",
        "cdc",
        "nih",
        "ncbi",
        "nci",
        "nppes",
        "umls",
        "uspstf",
        "ndc",
        "pma",
        "udi",
        "spl",
        "loinc",
        "icd",
        "icd10cm",
        "rxnorm",
        "rxcui",
        "cpc",
        "evs",
        "vehss",
        "mhs",
        "pfs",
        "gudid",
        "pmc",
        "hpo",
        "ucum",
        "epo",
        "ptab",
        "uspto",
        "sec",
        "nsde",
        "ucr",
        "api",
        "url",
        "id",
        "ndcs",
        "gdc",
    }
)


def _humanize_tool_name(name: str) -> str:
    """Derive a human-readable title from a snake_case tool name.

    ``get_drug_label`` -> ``Get Drug Label``; known acronyms stay upper.
    """
    words = [w for w in name.replace("-", "_").split("_") if w]
    return " ".join(w.upper() if w.lower() in _TITLE_ACRONYMS else w.capitalize() for w in words)


class DefaultToolTitles(Middleware):
    """Fill a human-readable ``title`` on any tool that lacks one.

    MCP clients and directory reviews (notably Anthropic's Connectors
    Directory) expect every tool to carry a ``title`` alongside its
    ``readOnlyHint``. Tools declared with just ``@mcp.tool(annotations=
    READ_ONLY)`` have ``title=None``; this derives one from the tool name at
    list time so every connector built on this factory ships titles without
    per-tool boilerplate. Tools that set their own ``title`` are left as-is.
    """

    async def on_list_tools(self, context, call_next):  # noqa: ANN001
        tools = await call_next(context)
        result = []
        for tool in tools:
            if getattr(tool, "title", None):
                result.append(tool)
            else:
                result.append(tool.model_copy(update={"title": _humanize_tool_name(tool.name)}))
        return result
