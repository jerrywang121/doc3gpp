"""Tests for the ``ServerSettings`` / ``MCPSettings`` nested settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import MCPSettings, ServerSettings, Settings


def test_server_defaults() -> None:
    """``ServerSettings`` ships with safe loopback defaults."""
    settings = Settings()
    assert settings.server.host == "127.0.0.1"
    assert settings.server.port == 8765
    assert settings.server.enabled is False


def test_mcp_defaults() -> None:
    """``MCPSettings`` enables streamable_http by default and gates on ``server.enabled``."""
    settings = Settings()
    assert settings.mcp.enabled is True
    assert settings.mcp.transport == "streamable_http"


def test_server_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A TOML fixture flows through ``get_settings()`` into the nested ``server`` block."""
    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[server]\n"
        "enabled = true\n"
        'port = 9000\n'
        'cache_subdir = "server-cache"\n'
        'log_retention = "30d"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()
    try:
        server = get_settings().server
        assert server.enabled is True
        assert server.port == 9000
        assert server.cache_subdir == "server-cache"
        assert server.log_retention == "30d"
    finally:
        get_settings.cache_clear()


def test_log_retention_invalid() -> None:
    """Non-parseable ``log_retention`` strings raise :class:`ValidationError`."""
    with pytest.raises(ValidationError):
        ServerSettings(log_retention="banana")


def test_max_concurrent_jobs_bounds() -> None:
    """``max_concurrent_jobs`` accepts the closed range 1..16 inclusive."""
    with pytest.raises(ValidationError):
        ServerSettings(max_concurrent_jobs=0)
    with pytest.raises(ValidationError):
        ServerSettings(max_concurrent_jobs=17)


def test_mcp_sse_queue_size_bounds() -> None:
    """``sse_queue_size`` rejects values below 10."""
    with pytest.raises(ValidationError):
        MCPSettings(sse_queue_size=9)
    assert MCPSettings(sse_queue_size=10).sse_queue_size == 10
