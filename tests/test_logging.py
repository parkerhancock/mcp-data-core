"""Tests for shared logging configuration."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest

from mcp_data_core import logging as core_logging


def test_configure_uses_env_log_dir(monkeypatch, tmp_path: Path) -> None:
    app_name = "test_logging_env"
    monkeypatch.setenv("LAW_TOOLS_CORE_LOG_DIR", str(tmp_path))

    log_file = core_logging.configure(app_name)

    assert log_file == tmp_path / f"{app_name}.log"
    logging.getLogger(app_name).error("test message")
    assert log_file.exists()


def test_log_file_hint_returns_configured_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LAW_TOOLS_CORE_SUPPRESS_LOG_HINT", raising=False)
    log_file = tmp_path / "some_app.log"
    monkeypatch.setattr(core_logging, "_configured_log_files", {"some_app": log_file})

    assert core_logging.log_file_hint() == f"details: {log_file}"


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
def test_log_file_hint_suppressed_by_env(monkeypatch, tmp_path: Path, truthy: str) -> None:
    """LAW_TOOLS_CORE_SUPPRESS_LOG_HINT hides server-local paths (remote deploys)."""
    monkeypatch.setenv("LAW_TOOLS_CORE_SUPPRESS_LOG_HINT", truthy)
    log_file = tmp_path / "some_app.log"
    monkeypatch.setattr(core_logging, "_configured_log_files", {"some_app": log_file})

    assert core_logging.log_file_hint() == ""


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
def test_log_file_hint_kept_for_falsy_values(monkeypatch, tmp_path: Path, falsy: str) -> None:
    monkeypatch.setenv("LAW_TOOLS_CORE_SUPPRESS_LOG_HINT", falsy)
    log_file = tmp_path / "some_app.log"
    monkeypatch.setattr(core_logging, "_configured_log_files", {"some_app": log_file})

    assert core_logging.log_file_hint() == f"details: {log_file}"


def test_api_error_str_omits_log_path_when_suppressed(monkeypatch, tmp_path: Path) -> None:
    """Exception __str__ consults the flag, so remote clients never see the path."""
    from mcp_data_core.exceptions import AuthenticationError

    log_file = tmp_path / "some_app.log"
    monkeypatch.setattr(core_logging, "_configured_log_files", {"some_app": log_file})
    exc = AuthenticationError("PACER authentication failed", status_code=401)

    monkeypatch.delenv("LAW_TOOLS_CORE_SUPPRESS_LOG_HINT", raising=False)
    assert str(exc) == f"PACER authentication failed (HTTP 401, details: {log_file})"

    monkeypatch.setenv("LAW_TOOLS_CORE_SUPPRESS_LOG_HINT", "true")
    assert str(exc) == "PACER authentication failed (HTTP 401)"


def test_configure_falls_back_to_tempdir_when_file_handler_fails(monkeypatch) -> None:
    app_name = "test_logging_fallback"
    original_file_handler = core_logging.logging.FileHandler
    calls: list[Path] = []

    def flaky_file_handler(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(Path(path))
        if len(calls) == 1:
            raise OSError("not writable")
        return original_file_handler(path, *args, **kwargs)

    monkeypatch.setattr(core_logging.logging, "FileHandler", flaky_file_handler)

    log_file = core_logging.configure(app_name)

    assert calls[0].name == f"{app_name}.log"
    assert log_file == Path(tempfile.gettempdir()) / app_name / f"{app_name}.log"
