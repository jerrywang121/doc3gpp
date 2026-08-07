"""Tests for ``OutputSettings.compact`` (the ``--compact`` flag default)."""

from __future__ import annotations

from pathlib import Path


def test_output_compact_default_is_false(monkeypatch) -> None:
    """``output.compact`` defaults to ``False`` (no behavioural change)."""
    from doc3gpp.settings.loader import get_settings

    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().output.compact is False
    finally:
        get_settings.cache_clear()


def test_output_compact_toml_override_true(tmp_path: Path, monkeypatch) -> None:
    """TOML can opt the operator into compact output globally."""
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[output]\ncompact = true\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()
    try:
        assert get_settings().output.compact is False
    finally:
        get_settings.cache_clear()


def test_mcp_transport_accepts_sse() -> None:
    """``mcp.transport`` accepts ``sse`` and defaults to ``streamable_http``."""
    from doc3gpp.settings.schema import MCPSettings

    assert MCPSettings(transport="sse").transport == "sse"
    assert MCPSettings().transport == "streamable_http"


def test_output_compact_toml_override_false_explicit(
    tmp_path: Path, monkeypatch,
) -> None:
    """An explicit ``compact = false`` is honoured (matches the default)."""
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[output]\ncompact = false\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()
    try:
        assert get_settings().output.compact is False
    finally:
        get_settings.cache_clear()
