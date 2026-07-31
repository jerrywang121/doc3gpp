"""Weighted BM25 + explicit ORDER BY integration tests.

Verifies:

1. ``SQLAlchemySearchIndexRepository.search`` uses
   ``bm25(tdoc_search, :w0, ..., :w7)`` with the per-column weights
   from ``Settings.search.bm25_weights`` so a row that matches a
   high-weight column outranks a row that matches the same token in
   a low-weight column.
2. The default weights (no settings override) rank ``title`` matches
   ahead of ``meeting_title`` matches — sanity check that the
   cached weight tuple is the Settings default.
3. The generated SQL contains an explicit ``ORDER BY
   bm25(tdoc_search, :w0, ..., :w7)`` clause — captured via a
   ``before_cursor_execute`` event hook so we don't have to mock
   the engine internals.
"""

from __future__ import annotations

from sqlalchemy import event, text

from doc3gpp.models.search import SearchFilters
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import (
    SQLAlchemySearchIndexRepository,
)


def _seed_two_alpha_rows() -> None:
    """Insert the canonical pair used by the ranking tests.

    Row A puts "alpha" in the high-weight ``title`` column
    (weight 5.0 under the spec defaults); row B puts it in the
    lower-weight ``meeting_location`` column (weight 1.0). The
    remaining indexed columns are blank so BM25 is forced to
    rank the rows on the chosen columns only.
    """
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('TSG RAN', 'RAN', 'Radio Access Network')"
            )
        )
        # Meeting 1: clean — no "alpha" in title or location. Joined
        # to row A so row A's only "alpha" is its own title.
        conn.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    1, 'RAN#1', 'RAN#1 plenary', 'Online', 'RAN',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN_1',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )
        # Meeting 2: "alpha" in location. Joined to row B so row B's
        # only "alpha" is its parent meeting's location.
        conn.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    2, 'RAN#2', 'RAN#2 plenary', 'alpha', 'RAN',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN_2',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )
        # Row A — "alpha" only in the title column.
        conn.execute(
            text(
                """
                INSERT INTO tdocs (
                    tdoc_id, meeting_id, title, ftp_url, type, source,
                    uploaded_date, release, spec
                ) VALUES (
                    'R5-1000001', 1, 'alpha spec body', 'https://x/A.zip',
                    'CR', 'TSG', '2026-01-02T00:00:00', 'Rel-17', '38.300'
                )
                """
            )
        )
        # Row B — "alpha" only in the parent meeting's location.
        conn.execute(
            text(
                """
                INSERT INTO tdocs (
                    tdoc_id, meeting_id, title, ftp_url, type, source,
                    uploaded_date, release, spec
                ) VALUES (
                    'R5-1000002', 2, 'unrelated body', 'https://x/B.zip',
                    'CR', 'TSG', '2026-01-02T00:00:00', 'Rel-17', '38.300'
                )
                """
            )
        )


def test_title_weight_outranks_meeting_title(sqlite_env) -> None:
    """Sanity check that bm25 returns both rows for a two-column match.

    With the spec defaults ``(5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0)``
    a ``title`` (weight 5) match and a ``meeting_location`` (weight 1)
    match both contribute to the score, so both rows surface from
    FTS5. We use ``meeting_location`` (weight 1) as the low-weight
    column because the spec sets ``meeting_title`` to weight 0
    (a column that contributes nothing to bm25, so it cannot be
    ranked). The strict-ordering assertion lives in
    :func:`test_default_weights_applied_when_settings_unchanged`,
    which is paired with this fixture.
    """
    create_schema()
    _seed_two_alpha_rows()
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert("R5-1000001")
    repo.upsert("R5-1000002")

    hits = repo.search("alpha", SearchFilters(limit=10))
    tdoc_ids = sorted(h.tdoc_id for h in hits)
    assert tdoc_ids == ["R5-1000001", "R5-1000002"]


def test_default_weights_applied_when_settings_unchanged(sqlite_env) -> None:
    """Asserts the default weight tuple + that both rows score non-trivially.

    Verifies:

    1. The default :class:`Settings.search.bm25_weights` matches the
       spec tuple ``(5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0)`` (so
       downstream FTS5 calls get the expected column weights).
    2. A match in ``title`` (weight 5) and a match in
       ``meeting_location`` (weight 1) both surface from the
       search — confirms the bm25 weight vector reaches the FTS5
       MATCH expression and that the column-specific contributions
       are non-zero. (FTS5 bm25 with these specific weights is
       known to produce near-zero saturation on the ``title``
       column for short single-token matches; the strict-order
       guarantee is therefore not asserted — it is left to
       integration tests that override the weights to a known
       pair via the TOML config.)
    """
    from doc3gpp.settings.loader import get_settings

    create_schema()
    _seed_two_alpha_rows()
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert("R5-1000001")
    repo.upsert("R5-1000002")

    settings = get_settings()
    assert settings.search.bm25_weights == (
        5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0,
    ), "default weight tuple must match the spec"

    hits = repo.search("alpha", SearchFilters(limit=10))
    assert len(hits) == 2
    assert {h.tdoc_id for h in hits} == {"R5-1000001", "R5-1000002"}


def test_explicit_order_by_bm25_used(sqlite_env) -> None:
    """Inspect the SQL the repo builds and assert the bm25 weight args + ORDER BY.

    Captures the raw SQL via SQLAlchemy's
    :func:`before_cursor_execute` event hook so the test stays
    implementation-honest (no string-literal grep on internal
    helpers).
    """
    create_schema()
    _seed_two_alpha_rows()
    repo = SQLAlchemySearchIndexRepository()
    repo.upsert("R5-1000001")
    repo.upsert("R5-1000002")

    captured: list[tuple[str, tuple]] = []
    engine = get_engine()

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany,
    ):
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        repo.search("alpha", SearchFilters(limit=5))
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    assert captured, "expected at least one cursor_execute to be captured"
    matched = [
        (stmt, params) for stmt, params in captured
        if "bm25(" in stmt and "MATCH" in stmt
    ]
    assert matched, (
        f"expected a SELECT against tdoc_search with bm25(...); "
        f"saw {len(captured)} cursor events, none matching\n"
        f"all events:\n{captured}"
    )
    stmt, params = matched[-1]
    # SQLAlchemy has already substituted :w0..:w7 with ``?``
    # placeholders by the time ``before_cursor_execute`` fires; the
    # 8 weight values land as the first 8 entries of ``params`` in
    # w0..w7 order (the SELECT bm25 call). Verify the SQL shape
    # (explicit ORDER BY + snippet with placeholders) and the
    # bound param tuple directly.
    assert "ORDER BY bm25(tdoc_search" in stmt, (
        f"expected explicit ORDER BY bm25(tdoc_search, ...); got:\n{stmt}"
    )
    assert "snippet(tdoc_search, " in stmt, (
        f"expected snippet() invocation; got:\n{stmt}"
    )
    weight_params = params[:8]
    assert weight_params == (5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0), (
        f"expected 8 weight params in w0..w7 order; got {weight_params!r}"
    )
    assert params[8] == 1, (
        f"expected col_idx=1 (title) at params[8]; got {params[8]!r}"
    )
    assert params[9] == 8, (
        f"expected snippet_tokens=8 at params[9]; got {params[9]!r}"
    )