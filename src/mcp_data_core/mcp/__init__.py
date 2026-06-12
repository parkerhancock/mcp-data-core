"""Shared MCP scaffolding for `patent-client-agents` and `law-tools`.

Consumers compose a FastMCP server via ``build_server()`` and register
domain-specific download fetchers via
``mcp_data_core.mcp.downloads.register_source``.

Env vars (all optional; deployment-specific):
    LAW_TOOLS_CORE_API_KEY      bearer token + HMAC signing secret
    LAW_TOOLS_CORE_PUBLIC_URL   base URL for signed download links
    LAW_TOOLS_CORE_LOG_DIR      directory for tool-call JSONL logs
    LAW_TOOLS_CORE_DOWNLOAD_CACHE      on-disk cache dir for downloads
    LAW_TOOLS_CORE_DOWNLOAD_TTL_SECONDS HMAC rotation bucket (default 86400)
    LAW_TOOLS_CORE_RESOURCES_ENABLED   emit MCP resource_link blocks in
                                       download results (default ON; set to
                                       0/false/no/off for text-only — the
                                       signed HTTP URL is always present
                                       either way)

All variables accept a legacy ``LAW_TOOLS_*`` alias for backward
compatibility with pre-split law-tools deployments.
"""

from .annotations import DOWNLOAD, READ_ONLY
from .auth import make_auth, make_domain_gate_middleware
from .conditional import (
    conditional_resource,
    conditional_tool,
    register_source_if_configured,
)
from .downloads import (
    RESOURCE_SCHEME,
    BlobStore,
    BulkItem,
    build_download_url,
    build_download_url_or_fetch,
    build_resource_uri,
    download_bulk_response,
    download_bulk_tool_result,
    download_response,
    download_tool_result,
    fetch_with_cache,
    get_blob_store,
    handle_download,
    read_resource,
    reap_stale_bulk_zips,
    register_source,
    set_blob_store,
    sign_path,
    verify_path,
)

# ``BearerTokenAuth`` is re-exported (redundant ``as`` alias) for backward
# compatibility but intentionally left out of ``__all__``: it is deprecated in
# favor of FastMCP-native auth via ``make_auth`` / ``build_server(auth=...)``.
# The alias keeps it importable (``from mcp_data_core.mcp import
# BearerTokenAuth``) so existing consumers don't break, just un-advertised so
# new code doesn't couple to it.
from .middleware import BearerTokenAuth as BearerTokenAuth
from .middleware import FriendlyErrors, ToolCallLogger
from .server_factory import build_server

__all__ = [
    "DOWNLOAD",
    "READ_ONLY",
    "RESOURCE_SCHEME",
    "BlobStore",
    "BulkItem",
    "FriendlyErrors",
    "ToolCallLogger",
    "build_download_url",
    "build_download_url_or_fetch",
    "build_resource_uri",
    "build_server",
    "conditional_resource",
    "conditional_tool",
    "download_bulk_response",
    "download_bulk_tool_result",
    "download_response",
    "download_tool_result",
    "fetch_with_cache",
    "get_blob_store",
    "handle_download",
    "make_auth",
    "make_domain_gate_middleware",
    "read_resource",
    "reap_stale_bulk_zips",
    "register_source",
    "register_source_if_configured",
    "set_blob_store",
    "sign_path",
    "verify_path",
]
