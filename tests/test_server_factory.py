"""Unit tests for ``mcp_data_core.mcp.server_factory.build_server``.

Focused on the ``serverInfo`` plumbing that surfaces in MCP clients
(spec 2025-11-25): ``icons`` and ``websiteUrl``. Hosted UIs like
Claude.ai's connector card read these from the ``initialize`` response;
without them the card shows a generic placeholder.
"""

from __future__ import annotations

import warnings

import mcp.types
import pytest

from mcp_data_core.mcp.server_factory import build_server


def test_build_server_without_icons_leaves_serverinfo_empty() -> None:
    mcp = build_server(name="t", instructions="x")
    assert mcp._mcp_server.icons is None
    assert mcp._mcp_server.website_url is None


def test_build_server_forwards_icons_and_website_url() -> None:
    icons = [
        mcp.types.Icon(
            src="https://example.com/icon.svg",
            mimeType="image/svg+xml",
            sizes=["any"],
        ),
        mcp.types.Icon(
            src="https://example.com/icon.png",
            mimeType="image/png",
            sizes=["512x512"],
        ),
    ]
    mcp_server = build_server(
        name="t",
        instructions="x",
        icons=icons,
        website_url="https://example.com/",
    )
    assert mcp_server._mcp_server.icons == icons
    assert mcp_server._mcp_server.website_url == "https://example.com/"


# ---------------------------------------------------------------------------
# Legacy-auth deprecation (BearerTokenAuth + /oauth/token)
# ---------------------------------------------------------------------------


def test_legacy_auth_path_warns_when_api_key_set(monkeypatch) -> None:
    """auth=None + LAW_TOOLS_CORE_API_KEY wires the deprecated path and warns."""
    monkeypatch.setenv("LAW_TOOLS_CORE_API_KEY", "secret")
    with pytest.warns(DeprecationWarning, match="make_auth"):
        build_server(name="t", instructions="x")


def test_legacy_auth_path_quiet_without_api_key(monkeypatch) -> None:
    """auth=None with no API key (stdio / local dev) is not deprecated."""
    monkeypatch.delenv("LAW_TOOLS_CORE_API_KEY", raising=False)
    monkeypatch.delenv("LAW_TOOLS_API_KEY", raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        build_server(name="t", instructions="x")


def test_modern_auth_path_does_not_warn(monkeypatch) -> None:
    """Passing auth=... (the FastMCP-native path) never wires the legacy path."""
    monkeypatch.setenv("LAW_TOOLS_CORE_API_KEY", "secret")
    from mcp_data_core.mcp.auth import make_auth

    auth = make_auth()  # StaticTokenVerifier from the API key
    assert auth is not None
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        build_server(name="t", instructions="x", auth=auth)
