"""End-to-end filter-flag tests against a populated sqlite DB."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.services.factory import build_search_service
from doc3gpp.storage.db.migrate import create_schema


def _seed_minimal_db(title: str = "RAN1#1") -> None:
    """Insert one tdocs row + matching sidecar tables for the search index."""
    from doc3gpp.storage.db.session import get_engine

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    1, 'RAN1#1', :title, 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_1',
                    '2026-01-05T00:00:00'
                )
                """
            ),
            {"title": title},
        )
        conn.execute(
            text(
                """
                INSERT INTO tdocs (
                    tdoc_id, meeting_id, title, ftp_url, type, source,
                    uploaded_date, release, spec
                ) VALUES (
                    'R5-1000000', 1, 'NB-IoT scheduling study',
                    'https://www.3gpp.org/ftp/R5-1000000.zip',
                    'CR', 'TSG', '2026-01-02T00:00:00', 'Rel-17', '38.300'
                )
                """
            )
        )


def test_search_with_filters(sqlite_env) -> None:
    create_schema()
    _seed_minimal_db()
    svc = build_search_service()
    assert svc is not None
    svc.upsert_for_tdoc("R5-1000000")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search", "query", "NB-IoT",
            "--tsg", "RAN1",
            "--release", "Rel-17",
            "--spec", "38.300",
            "--since", "2026-01-01",
            "--until", "2026-01-31",
            "--limit", "5",
            "--format", "json",
        ],
    )
    # The CLI may print a stale-index hint to stderr; exit 0 is the
    # contract here.
    assert result.exit_code == 0
    assert "R5-1000000" in result.output


def test_search_meeting_filter_is_like(semantic_search_corpus) -> None:
    """--meeting is a LIKE pattern over name OR title (user types the
    displayed title, e.g. ``%CT6%``).

    Regression: the FTS5 repo compared ``m.name = :meeting`` exactly, so
    any wildcard pattern returned zero rows.
    """
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    repo = SQLAlchemySearchIndexRepository()
    for tid in ["SEM-NB-001", "SEM-NB-002"]:
        repo.upsert(tid)

    # `%WG meeting%` matches the display column m.title of meetings
    # SEM#9101 (SEM-NB-002, SEM-CHG-001/002) and SEM#9102
    # (SEM-TTCN-001); SEM-NB-001 lives in meeting SEM#9100 (title
    # "SEM#9100 plenary") and must be filtered out.
    hits = repo.search('"NB-IoT"', SearchFilters(meeting="%WG meeting%", limit=10))
    assert {h.tdoc_id for h in hits} == {
        "SEM-NB-002", "SEM-TTCN-001", "SEM-CHG-001", "SEM-CHG-002",
    }

    # A plain-string value matches the m.name column exactly (LIKE with
    # no wildcards degenerates to equality).
    hits = repo.search('"NB-IoT"', SearchFilters(meeting="SEM#9100", limit=10))
    assert {h.tdoc_id for h in hits} == {"SEM-NB-001"}


def test_search_release_filter_is_like(semantic_search_corpus) -> None:
    """--release is a LIKE pattern over tdocs.release.

    Regression: ``t.release = :release`` exact match could never match
    versioned / NULL release rows.
    """
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    repo = SQLAlchemySearchIndexRepository()

    # Every "NB-IoT"-matching row carries release Rel-17.
    hits = repo.search('"NB-IoT"', SearchFilters(release="Rel-1%", limit=10))
    assert {h.tdoc_id for h in hits} == {
        "SEM-CHG-001", "SEM-CHG-002", "SEM-NB-001", "SEM-NB-002",
        "SEM-TTCN-001",
    }

    hits = repo.search('"NB-IoT"', SearchFilters(release="Rel-99", limit=10))
    assert hits == []


def test_search_spec_filter_is_like(semantic_search_corpus) -> None:
    """--spec is a LIKE pattern over tdocs.spec.

    Regression: ``t.spec = :spec`` exact match could never partial-match
    versioned spec strings (``38.300-1``).
    """
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    repo = SQLAlchemySearchIndexRepository()

    # "NB-IoT" base set: SEM-CHG-001/002 (38.300/38.211), SEM-NB-001/002
    # (38.300/38.321), SEM-TTCN-001 (36.523).
    hits = repo.search('"NB-IoT"', SearchFilters(spec="38.3%", limit=10))
    assert {h.tdoc_id for h in hits} == {
        "SEM-CHG-001", "SEM-NB-001", "SEM-NB-002",
    }

    hits = repo.search('"NB-IoT"', SearchFilters(spec="36.5%", limit=10))
    assert {h.tdoc_id for h in hits} == {"SEM-TTCN-001"}


def test_search_spec_negated_grammar(semantic_search_corpus) -> None:
    """--spec honours the rich ``!pattern`` grammar."""
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    repo = SQLAlchemySearchIndexRepository()

    # NOT spec 38.3%: every "NB-IoT" hit whose spec is not 38.300/38.321.
    hits = repo.search('"NB-IoT"', SearchFilters(spec="!38.3%", limit=10))
    assert {h.tdoc_id for h in hits} == {
        "SEM-CHG-002", "SEM-TTCN-001",
    }


def test_search_meeting_negated_grammar(semantic_search_corpus) -> None:
    """--meeting honours the rich ``!pattern`` grammar over name OR title."""
    from doc3gpp.models.search import SearchFilters
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    repo = SQLAlchemySearchIndexRepository()

    # NOT "SEM#9100": excludes the SEM-NB-001 plenary row; everything
    # else in the NB-IoT base set lives in SEM#9101 / SEM#9102.
    hits = repo.search('"NB-IoT"', SearchFilters(meeting="!SEM#9100", limit=10))
    assert {h.tdoc_id for h in hits} == {
        "SEM-CHG-001", "SEM-CHG-002", "SEM-NB-002", "SEM-TTCN-001",
    }


def test_search_with_lowercase_tsg(sqlite_env) -> None:
    """meetings.tsg is stored upper-case; a lowercase --tsg must still hit.

    Regression: the FTS5 repo compared ``m.tsg = :tsg`` case-sensitively,
    so ``--tsg ran1`` returned zero rows.
    """
    create_schema()
    _seed_minimal_db()
    svc = build_search_service()
    assert svc is not None
    svc.upsert_for_tdoc("R5-1000000")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "search", "query", "NB-IoT",
            "--tsg", "ran1",
            "--limit", "5",
            "--format", "json",
        ],
    )
    assert result.exit_code == 0
    assert "R5-1000000" in result.output


def test_search_malformed_match_raises_query_error(semantic_search_corpus) -> None:
    """A malformed FTS5 ``MATCH`` expression surfaces as a clean
    :class:`SearchQueryError` rather than a raw ``OperationalError``
    traceback.

    Regression: an unbalanced double quote (``"foo``) crashes FTS5 with
    ``OperationalError: unterminated string``; the repo now classifies
    query-shaped failures as ``SearchQueryError`` so the CLI prints a
    one-liner instead of a stack trace.
    """
    from doc3gpp.models.search import SearchFilters, SearchQueryError
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    repo = SQLAlchemySearchIndexRepository()
    with pytest.raises(SearchQueryError):
        repo.search('"foo', SearchFilters(limit=10))


def test_search_malformed_match_cli_exits_cleanly(semantic_search_corpus) -> None:
    """The CLI turns a malformed MATCH into a friendly stderr message
    with exit code 2, not a traceback."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["search", "query", '"foo', "--format", "json"],
    )
    assert result.exit_code == 2
    assert "bad query" in result.output
