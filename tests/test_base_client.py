"""Tests for BaseAsyncClient URL building."""

from __future__ import annotations

from pathlib import Path

from mcp_data_core.base_client import BaseAsyncClient


def test_absolute_urls_bypass_base_url(tmp_path: Path) -> None:
    class Client(BaseAsyncClient):
        DEFAULT_BASE_URL = "https://api.test/v1"

    client = Client(cache_path=tmp_path, use_cache=False)
    assert client._build_url("/items") == "https://api.test/v1/items"
    assert client._build_url("https://files.test/a.pdf") == "https://files.test/a.pdf"
    assert client._build_url("http://files.test/a.pdf") == "http://files.test/a.pdf"
