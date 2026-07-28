"""Unit tests for :mod:`doc3gpp.services.factory` wiring.

Today the factory is mostly a thin assembler that constructs
:class:`TDocCrService` from the configured settings + repository
implementations. The size-limit knob (``tdoc_parse.max_tdoc_size_kb``)
plumbs into the service via :func:`build_tdoc_cr_service`'s
``max_tdoc_size_bytes`` kwarg — these tests cover that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _pin_max_tdoc_size_kb(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    max_tdoc_size_kb: int,
) -> None:
    """Pin ``tdoc_parse.max_tdoc_size_kb`` via a TOML config file.

    Mirrors :func:`tests.unit.test_tdoc_parse_cli._pin_max_tdoc_size_kb_via_toml`;
    duplicated here to keep the factory tests self-contained.
    """
    from doc3gpp.config import get_settings

    config_path = tmp_path / "doc3gpp-factory.toml"
    config_path.write_text(
        f"[tdoc_parse]\nmax_tdoc_size_kb = {max_tdoc_size_kb}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()


def test_factory_threads_max_tdoc_size_bytes_from_settings(
    monkeypatch, tmp_path,
) -> None:
    """Factory resolves ``max_tdoc_size_bytes`` from
    ``settings.tdoc_parse.max_tdoc_size_kb * 1024``."""
    from doc3gpp.config import get_settings
    from doc3gpp.services.factory import build_tdoc_cr_service

    _pin_max_tdoc_size_kb(monkeypatch, tmp_path, max_tdoc_size_kb=500)
    try:
        service = build_tdoc_cr_service()
        assert service._max_tdoc_size_bytes == 500 * 1024
        assert get_settings().tdoc_parse.max_tdoc_size_kb == 500
    finally:
        get_settings.cache_clear()


def test_factory_explicit_override_wins(monkeypatch, tmp_path) -> None:
    """Passing ``max_tdoc_size_bytes`` explicitly overrides the setting."""
    from doc3gpp.config import get_settings
    from doc3gpp.services.factory import build_tdoc_cr_service

    _pin_max_tdoc_size_kb(monkeypatch, tmp_path, max_tdoc_size_kb=500)
    try:
        service = build_tdoc_cr_service(max_tdoc_size_bytes=10 * 1024 * 1024)
        assert service._max_tdoc_size_bytes == 10 * 1024 * 1024
    finally:
        get_settings.cache_clear()


def test_factory_default_zero_when_setting_unset(monkeypatch) -> None:
    """When the setting is absent and no override is given, the
    service-level default of ``1000 * 1024`` bytes applies (the
    schema default; the factory simply multiplies by 1024).

    Resets the cache so the test reads a fresh Settings instance.
    """
    from doc3gpp.config import get_settings
    from doc3gpp.services.factory import build_tdoc_cr_service

    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    get_settings.cache_clear()
    try:
        service = build_tdoc_cr_service()
        # 1000 KB default → 1000 * 1024 bytes.
        assert service._max_tdoc_size_bytes == 1000 * 1024
    finally:
        get_settings.cache_clear()


def test_factory_zero_override_disables(monkeypatch) -> None:
    """``max_tdoc_size_bytes=0`` propagates verbatim — disables the guard."""
    from doc3gpp.config import get_settings
    from doc3gpp.services.factory import build_tdoc_cr_service

    monkeypatch.delenv("DOC3GPP_CONFIG", raising=False)
    get_settings.cache_clear()
    try:
        service = build_tdoc_cr_service(max_tdoc_size_bytes=0)
        assert service._max_tdoc_size_bytes == 0
    finally:
        get_settings.cache_clear()