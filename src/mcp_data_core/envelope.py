"""Response envelope models per CONNECTOR_STANDARDS.md §5.9.

Every MCP tool in `patent_client_agents` returns either a
:class:`ResponseEnvelope` (single record) or a :class:`ListEnvelope`
(zero or more records). Both carry :class:`Provenance` metadata so
agents can quote source + retrieval time without a separate lookup.

Cursors are opaque base64(JSON); connectors decide the payload, agents
treat them as bytes and pass them back unchanged.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


_connector_version: str = "unknown"


def configure(connector_version: str) -> None:
    """Register the consuming connector's package version.

    Mirrors :func:`mcp_data_core.logging.configure`. Called once at
    import time by the consumer package (e.g. ``patent_client_agents``)
    so :func:`make_provenance` can stamp envelope responses without each
    call site repeating the version.
    """
    global _connector_version
    _connector_version = connector_version


class Provenance(BaseModel):
    """Per-response provenance metadata.

    Surfaces on every envelope. See CONNECTOR_STANDARDS.md §1 for the
    four-category model and per-category provenance contracts:

    - ``registered_ip``       : ``retrieved_at`` + ``source_url``
    - ``adjudicative_records``: ``retrieved_at`` + ``as_of_status``
                                (status of the proceeding at fetch time)
    - ``substantive_law``     : ``retrieved_at`` + ``corpus_synced_at``
                                + ``corpus_version``
    - ``fees``                : ``retrieved_at`` + ``effective_date``
                                (effective date of the fee schedule)

    All category-specific fields are model-optional; the per-category
    CI compliance test enforces which categories must set which fields.
    """

    model_config = ConfigDict(frozen=True)

    retrieved_at: datetime = Field(
        description=(
            "UTC timestamp of the original upstream fetch. When "
            "cache_hit is True, this is the cached fetch time."
        ),
    )
    source_url: str = Field(description="Canonical upstream URL.")
    source_name: str = Field(
        description="Human-readable upstream name (mirrors coverage/sources.yaml).",
    )
    cache_hit: bool = Field(
        default=False,
        description="True if served from the local HTTP cache.",
    )
    connector_version: str = Field(
        description="patent_client_agents package version that produced the response.",
    )

    corpus_synced_at: datetime | None = Field(
        default=None,
        description="When the bundled corpus was last refreshed (substantive_law only).",
    )
    corpus_version: str | None = Field(
        default=None,
        description="Vendor-style corpus version string (substantive_law only).",
    )

    effective_date: date | None = Field(
        default=None,
        description=(
            "Effective date of the fee schedule (fees category only). The "
            "office's most recent revision date. CI requires this on every "
            "fees-category response so agents can quote fees with provenance."
        ),
    )
    as_of_status: str | None = Field(
        default=None,
        description=(
            "Proceeding status at retrieval time (adjudicative_records only). "
            "Free-text upstream label (e.g., 'Instituted', 'Final Written "
            "Decision', 'Terminated-Settled'). Snapshot semantics: dockets "
            "change; this records what we saw when retrieved_at fired."
        ),
    )


class ResponseEnvelope(BaseModel, Generic[T]):
    """Single-record response envelope.

    ``summary`` is short Markdown (≤5 lines) an agent can quote without
    re-summarizing the JSON. ``details`` carries the typed record.
    """

    summary: str
    details: T
    provenance: Provenance


class ListEnvelope(BaseModel, Generic[T]):
    """Multi-record response envelope.

    Returned by ``search_*`` tools and by ``get_*`` tools that accept a
    list of identifiers (§5.4). A list-accepting ``get_*`` returns this
    envelope even for a single identifier so the response shape stays
    stable.

    Pagination: ``next_cursor`` is opaque base64(JSON); the connector
    chooses the payload shape (see :func:`encode_cursor`). When
    ``more_available`` is False the caller has reached the last page.
    """

    summary: str
    items: list[T]
    next_cursor: str | None = None
    more_available: bool = False
    provenance: Provenance


def make_provenance(
    source_url: str,
    source_name: str,
    *,
    cache_hit: bool = False,
    retrieved_at: datetime | None = None,
    corpus_synced_at: datetime | None = None,
    corpus_version: str | None = None,
    effective_date: date | None = None,
    as_of_status: str | None = None,
    connector_version: str | None = None,
) -> Provenance:
    """Build a :class:`Provenance`, defaulting ``retrieved_at`` to now (UTC).

    ``connector_version`` falls back to whatever the consumer registered
    via :func:`configure`. Passing it explicitly is supported but rarely
    needed.

    Category-specific kwargs:
        ``corpus_synced_at`` / ``corpus_version`` — substantive_law
        ``effective_date``                       — fees
        ``as_of_status``                         — adjudicative_records
    """
    return Provenance(
        retrieved_at=retrieved_at if retrieved_at is not None else datetime.now(UTC),
        source_url=source_url,
        source_name=source_name,
        cache_hit=cache_hit,
        connector_version=connector_version or _connector_version,
        corpus_synced_at=corpus_synced_at,
        corpus_version=corpus_version,
        effective_date=effective_date,
        as_of_status=as_of_status,
    )


def encode_cursor(payload: dict) -> str:
    """Encode a pagination payload as a URL-safe base64(JSON) string.

    Sorted keys keep the encoding deterministic — useful for cache keys
    and snapshot tests.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(token: str) -> dict:
    """Decode a cursor produced by :func:`encode_cursor`.

    Re-adds the padding stripped during encoding. Raises ``ValueError``
    if the token is not valid base64 or not valid JSON.
    """
    padding = b"=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii") + padding)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"cursor is not valid base64: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"cursor is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"cursor payload must be a JSON object, got {type(payload).__name__}")
    return payload


__all__ = [
    "Provenance",
    "ResponseEnvelope",
    "ListEnvelope",
    "configure",
    "make_provenance",
    "encode_cursor",
    "decode_cursor",
]
