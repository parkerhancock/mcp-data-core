# mcp-data-core

**Batteries-included async HTTP scaffolding and MCP server plumbing for Python data-fetching libraries.**

If you're writing a Python library that pulls structured data from an API — patents, court filings, FDA records, financial filings, anything — you end up rebuilding the same eight things: an `httpx` client with retry, an HTTP cache, a tenacity policy, an OAuth helper, response envelopes, a typed exception hierarchy, per-app logging, and (if you ship an MCP server) tool registration, auth, and signed downloads. `mcp-data-core` is those eight things, packaged.

## Quick Start

```bash
uv add mcp-data-core           # core scaffolding
uv add "mcp-data-core[mcp]"    # + FastMCP server helpers
```

```python
from mcp_data_core import BaseAsyncClient

class MyApiClient(BaseAsyncClient):
    DEFAULT_BASE_URL = "https://api.example.com"
    CACHE_NAME = "my_api"

    async def get_thing(self, id: str) -> dict:
        return await self._request_json("GET", f"/things/{id}")

async with MyApiClient() as client:
    result = await client.get_thing("42")
    stats = await client.cache_stats()
    print(f"Cache hit rate: {stats.hit_rate:.1f}%")
```

That's the full surface. Retry, caching, error mapping, and connection pooling are already wired up.

## Features

| Feature | What you get |
|---|---|
| **`BaseAsyncClient`** | `httpx.AsyncClient` subclass with retry, caching, error mapping, and cache-management methods. Override `DEFAULT_BASE_URL` + `CACHE_NAME`; the rest is inherited. |
| **HTTP caching** | `hishel`-backed cache with a custom SQLite/WAL storage layer. Respects HTTP cache headers by default, with TTL override. Inspection (`cache_stats`), eviction (`cache_clear_expired`), and pattern-based invalidation (`cache_invalidate`) built in. |
| **Retry policy** | `tenacity`-based exponential-jitter retry (4 attempts default). Retryable status set covers 408, 429, 500-504. Honors `Retry-After` headers. |
| **OAuth2 client credentials** | `OAuth2ClientCredentialsAuth` — drop-in `httpx.Auth` that handles token refresh, retries on 401, and works behind the cache layer. |
| **Response envelopes** | `ResponseEnvelope`, `ListEnvelope`, `Provenance`. Cursor-based pagination helpers (`encode_cursor` / `decode_cursor`). Every response carries source provenance so downstream consumers can cite. |
| **Typed exceptions** | `McpDataCoreError` base + `ApiError`, `RateLimitError`, `NotFoundError`, `AuthenticationError`, `ServerError`, `ConfigurationError`, `ValidationError`, `ParseError`. Log-first error formatting: `str(err)` appends the log path so agents can inspect without keeping stacktraces in context. |
| **Per-app file logging** | `logging.configure("my_app")` attaches a file handler under the `my_app` logger tree, writing to `~/.cache/my_app/my_app.log`. Idempotent; each consumer library logs to its own file. |
| **Bundled corpora** | `corpus_db` (SQLite/FTS5 reader) and `corpus_compression` (zstd) for libraries that ship statutes, manuals, or other reference text alongside their API client. |
| **MCP server scaffolding** *(opt-in)* | FastMCP server factory, bearer-token auth, domain gating middleware, conditional tool registration, signed HMAC download URLs with on-disk cache, and OAuth 2.1 + PKCE + DCR helpers. |

## Real-world usage

A trimmed-down version of how [patent-client-agents](https://github.com/parkerhancock/patent-client-agents) wires up a USPTO connector:

```python
import os
from mcp_data_core import (
    BaseAsyncClient,
    ConfigurationError,
    ListEnvelope,
    make_provenance,
)

BASE_URL = "https://api.uspto.gov"


class UsptoOdpClient(BaseAsyncClient):
    DEFAULT_BASE_URL = BASE_URL
    CACHE_NAME = "uspto_odp"

    def __init__(self, *, api_key: str | None = None, **kwargs) -> None:
        api_key = api_key or os.environ.get("USPTO_ODP_API_KEY")
        if not api_key:
            raise ConfigurationError("USPTO_ODP_API_KEY required")
        super().__init__(headers={"X-API-KEY": api_key}, **kwargs)

    async def search_applications(
        self, query: str, *, limit: int = 25
    ) -> ListEnvelope[dict]:
        payload = await self._request_json(
            "POST",
            "/api/v1/patent/applications/search",
            json={"q": query, "pagination": {"limit": limit}},
        )
        return ListEnvelope(
            summary=f"{payload['count']} applications matching {query!r}",
            items=payload["patentFileWrapperDataBag"],
            provenance=make_provenance(
                source_url=f"{BASE_URL}/api/v1/patent/applications/search",
                source_name="USPTO Open Data Portal",
            ),
        )
```

No retry loop. No cache invalidation. No exception remapping. No connection lifecycle. The library author writes the API-shaped methods; `mcp-data-core` handles everything else.

## What's inside

```
mcp_data_core/
├── base_client.py        # BaseAsyncClient
├── cache.py              # CacheManager, build_cached_http_client, SQLite/WAL storage
├── resilience.py         # default_retryer, with_retry, RETRYABLE_STATUS_CODES
├── oauth2.py             # OAuth2ClientCredentialsAuth
├── envelope.py           # ResponseEnvelope, ListEnvelope, Provenance, cursor helpers
├── exceptions.py         # McpDataCoreError + 8 subclasses
├── logging.py            # configure() — per-app file logging
├── filenames.py          # Download filename conventions
├── corpus_db.py          # SQLite/FTS5 corpus reader
├── corpus_compression.py # zstd helpers
└── mcp/                  # Optional — installed via [mcp] extra
    ├── server_factory.py # FastMCP app factory
    ├── auth.py           # OAuth 2.1 + bearer-token helpers
    ├── middleware.py     # Domain gate, friendly errors, logging
    ├── conditional.py    # Conditional tool registration
    ├── downloads.py      # Signed HMAC download URLs + on-disk cache
    └── annotations.py    # Tool annotations (READ_ONLY, DESTRUCTIVE)
```

## Provenance

Extracted from [patent-client-agents](https://github.com/parkerhancock/patent-client-agents) 0.20.0 (May 2026) where it had matured as the shared infrastructure across multiple law and patent connectors. Split out as a standalone package so non-IP toolkits — regulatory (FDA), financial, scientific — can use the same scaffolding without pulling the IP-specific connector surface.

### Used by

- [patent-client-agents](https://github.com/parkerhancock/patent-client-agents) — IP-registry connectors (USPTO, EPO, JPO, EUIPO, IP Australia, …)

## Compatibility

- Python 3.11, 3.12, 3.13
- macOS, Linux. Windows untested.
- `httpx` 0.27+, `pydantic` 2.7+, `tenacity` 8.4+

## Development

```bash
git clone https://github.com/parkerhancock/mcp-data-core
cd mcp-data-core
uv sync --all-extras --dev
uv run pytest                                 # 166 tests
uv run ruff check src tests
uv run ruff format src tests
```

Tests are pure-Python — no network, no fixtures, no live APIs. They exercise the cache, retry policy, OAuth refresh, MCP middleware, signed download URLs, and corpus reader against an in-memory transport.

## License

Apache-2.0
