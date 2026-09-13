"""Online integration test for ``TestCaseService.sync``.

Exercises the live RAN5 TTCN status ``History/`` folder: lists the
status files, selects the latest, downloads it, parses the workbook,
and persists the rows via the production ``build_testcase_service``
factory. Run explicitly with::

    python -m pytest -m online -rs

The test skips itself unless the ``@pytest.mark.online`` marker is
selected (default pytest skips online tests via
``pyproject.toml [tool.pytest.ini_options]``) so the SQLite suite stays
self-contained.

The database is pinned under a temporary path so the live test never
writes into a user's real ``~/.local/share/...`` SQLite file. No exact
counts are asserted (the upstream file changes weekly); the test only
asserts that at least one testcase row and at least one source-ledger
row were stored.
"""

from __future__ import annotations

import httpx
import pytest

from doc3gpp.services.factory import build_testcase_service

pytestmark = pytest.mark.online


def test_online_testcase_sync(sqlite_env) -> None:
    """End-to-end live ``TestCaseService.sync()`` against 3gpp.org.

    Hits the live ``History/`` listing, downloads the latest
    ``TTCN CR Agreement Status`` zip, parses the workbook, and upserts
    the testcase headers + status rows. Then verifies the repository
    round-trip: ``list(limit=1)`` returns at least one row and
    ``get_source(latest)`` returns the ledger row recorded during sync.
    """
    from doc3gpp.scraping.testcase_source import list_history_files, select_latest
    from doc3gpp.settings.loader import get_settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.testcase_sql import (
        SQLAlchemyTestCaseRepository,
    )

    get_settings.cache_clear()
    get_engine.cache_clear()
    create_schema()

    try:
        latest = select_latest(list_history_files())
        outcome = build_testcase_service().sync()
    except httpx.HTTPError as exc:
        pytest.skip(f"online testcase endpoints not reachable: {exc}")

    assert outcome.status in ("synced", "skipped"), outcome.reason

    repo = SQLAlchemyTestCaseRepository()
    assert len(repo.list(limit=1)) >= 1, (
        "Expected the live sync to store at least one testcase row."
    )
    assert repo.get_source(latest) is not None, (
        f"Expected a testcase_sources ledger row for {latest!r}."
    )
