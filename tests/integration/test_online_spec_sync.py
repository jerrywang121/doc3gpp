"""Online integration test for ``SpecService.sync``.

Exercises the live 3GPP DynaReport list page for ``R5`` (``Spec`` /
``SpecVersion`` rows) plus the per-spec detail pages, then verifies the
``SpecRepository`` round-trip (``get`` / ``list_versions``). Run
explicitly with::

    python -m pytest -m online -rs

The test skips itself unless the ``@pytest.mark.online`` marker is
selected (default pytest skips online tests via
``pyproject.toml [tool.pytest.ini_options]``) so the SQLite suite stays
self-contained.

The database is pinned under a temporary path so the live test never
writes into a user's real ``~/.local/share/...`` SQLite file, and the
``tsgs`` table is auto-seeded via ``create_schema`` so the
``tsgs.spec_last_sync`` FK on ``specs.tsg`` validates.
"""

from __future__ import annotations

import httpx
import pytest

from doc3gpp.services.factory import build_spec_service

pytestmark = pytest.mark.online


def test_spec_sync_r5_online(tmp_path, monkeypatch) -> None:
    """End-to-end live ``SpecService.sync('R5')`` against 3gpp.org.

    Hits the live DynaReport list page for ``R5``, fans out across
    per-spec detail pages in a thread pool, and persists the result via
    the production ``build_spec_service`` factory. Then verifies the
    cached repository round-trip:
    ``get('36.579-5')`` returns the stored header and
    ``list_versions('36.579-5')`` returns at least one row whose
    ``ftp_url`` is populated.

    ``36.579-5`` is the LTE/NR conformance test spec — long-lived, TSG
    5-owned, with multiple historical versions on the live system — so
    a healthy upstream always has at least one version row.
    """
    monkeypatch.setenv(
        "DOC3GPP_DATABASE_URL",
        f"sqlite+pysqlite:///{tmp_path / 'doc3gpp.db'}",
    )

    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.session import get_engine

    get_settings.cache_clear()
    get_engine.cache_clear()

    service = build_spec_service()

    try:
        outcome = service.sync("R5", force=True)
    except httpx.HTTPError as exc:
        pytest.skip(f"online spec endpoints not reachable in this environment: {exc}")

    assert outcome.status == "synced", outcome.reason
    assert outcome.synced_count >= 1

    spec = service.get("36.579-5")
    assert spec is not None, (
        "Expected R5 sync to populate 36.579-5 — got None. "
        "Either the canonical id is stale (URL template may have rotated) "
        "or the parser dropped the row."
    )

    versions = service.list_versions("36.579-5")
    assert len(versions) >= 1, (
        f"Expected at least one version for 36.579-5, got {len(versions)}"
    )
    assert all(v.ftp_url for v in versions), (
        "Every SpecVersion row must carry a non-empty ftp_url — the parser "
        "is responsible for filling this from the DynaReport detail page."
    )
