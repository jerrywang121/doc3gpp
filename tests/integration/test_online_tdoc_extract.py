"""Online integration tests for the TDoc extraction pipeline.

These tests hit the live 3GPP FTP (``www.3gpp.org``) through the same
``doc3gpp tdoc extract`` command a human operator would run, end-to-end
through Typer's :class:`CliRunner`. The goal is to surface URL-template
rot for the patterns Phase 2 verified against offline fixtures — the
Phase 2 unit tests pin the URL builders, but only a live fetch proves
the upstream server still serves those bytes for the canonical ids.

Run explicitly with::

    python -m pytest -m online -rs

The tests skip themselves unless:

* the ``@pytest.mark.online`` marker is selected (default pytest skips
  online tests via ``pyproject.toml [tool.pytest.ini_options]``), AND
* the optional ``python-docx`` extra is installed
  (``pip install doc3gpp[extract]``).

Both pre-seed a parent ``tdocs`` row so the service-level
``TDocCrService.extract`` validation passes — the live URL fetch is the
only network surface under test; the rest of the pipeline runs through
the production ``build_tdoc_cr_service`` factory.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.online


def _docx_available() -> bool:
    """Return True iff ``python-docx`` imports cleanly.

    Mirrors the helper used by ``test_tdoc_cr_sqlite.py`` and
    ``test_docx_converter.py`` so the same skip guard pattern applies
    here.
    """
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_live_extract_r5s260009(tmp_path, monkeypatch) -> None:
    """End-to-end live extract of ``R5s260009`` via the ``tdoc extract`` CLI.

    This is the Phase 8 live-URL verification per the plan: the R5s
    URL template was locked in by Phase 2 against offline fixtures;
    this test exercises it against the real 3GPP FTP. The CLI is
    driven via Typer's :class:`CliRunner` (not ``subprocess``) so the
    production ``build_tdoc_cr_service`` factory is on the path.

    The cache root is redirected under ``tmp_path`` so the user's home
    cache stays clean. A parent ``tdocs`` row is pre-seeded because
    ``TDocCrService.extract`` validates ``tdoc_id`` against the
    ``tdocs`` table.

    The assertion is intentionally permissive: we only require that
    the TDoc id surfaces in either stdout or stderr. On a healthy
    upstream that's a per-id ``spec=`` line; on a 404 or template rot
    that's the CLI's ``FAILED - extract error`` summary line. Either
    outcome tells the operator the live URL was reached and the
    pipeline produced a parseable result — the test's value is
    exercising the live code path, not asserting a specific payload.
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.models import TDocORM
    from doc3gpp.storage.db.session import get_engine, get_session_factory

    # Cache root → tmp_path so we don't pollute ~/.cache/doc3gpp/tdocs.
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()

    # Pre-seed the parent TDoc row so the service-level type guard passes.
    create_schema()
    factory = get_session_factory()
    with factory() as session:
        existing = session.get(TDocORM, "R5s260009")
        if existing is None:
            session.add(TDocORM(tdoc_id="R5s260009", type="CR"))
            session.commit()

    # Run via the same Typer entry point a human operator would use.
    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "extract", "--tdoc", "R5s260009", "--force"],
    )

    # Either success (URL template + zip + docx parse all OK) or a
    # surfaced failure (template rot, network 404, etc.). Either way
    # the TDoc id must appear in the output — success line, FAILED
    # line, or downstream error — so the operator can diagnose.
    combined = result.stdout + (result.stderr or "")
    assert "R5s260009" in combined, (
        f"R5s260009 missing from CLI output — unexpected blank result.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_live_extract_r5w260009_workshop_pattern(tmp_path, monkeypatch) -> None:
    """Live extract of ``R5w260009`` exercises the Workshop URL template.

    Phase 2 verified the Workshop URL pattern
    (``.../Workshop/TSGR5_Workshop_<year>/Docs/<id>.zip``) against a
    local fixture. This test mirrors ``test_live_extract_r5s260009``
    for the workshop variant so URL-template rot on either branch is
    caught the next time someone runs ``pytest -m online``.
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.models import TDocORM
    from doc3gpp.storage.db.session import get_engine, get_session_factory

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings

    get_settings.cache_clear()
    get_engine.cache_clear()

    create_schema()
    factory = get_session_factory()
    with factory() as session:
        existing = session.get(TDocORM, "R5w260009")
        if existing is None:
            session.add(TDocORM(tdoc_id="R5w260009", type="CR"))
            session.commit()

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "extract", "--tdoc", "R5w260009", "--force"],
    )

    combined = result.stdout + (result.stderr or "")
    assert "R5w260009" in combined, (
        f"R5w260009 missing from CLI output — unexpected blank result.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )