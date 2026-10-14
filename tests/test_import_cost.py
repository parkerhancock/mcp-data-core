"""Importing the MCP helpers must not load the Google OAuth provider.

GoogleProvider is only needed when Google credentials are configured, and
importing it costs a large share of server start time.
"""

from __future__ import annotations

import subprocess
import sys


def test_mcp_import_does_not_load_google_provider() -> None:
    code = (
        "import sys, mcp_data_core.mcp\n"
        "print('fastmcp.server.auth.providers.google' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert out.stdout.strip().splitlines()[-1] == "False"
