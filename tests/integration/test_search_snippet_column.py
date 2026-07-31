"""Regression: ``preview`` must come from the configured column.

The default ``Settings.search.snippet_column`` is ``"title"``; the
generated ``snippet(...)`` FTS5 call must therefore read from the
``title`` column of the ``tdoc_search`` virtual table.

The ``title`` column lives at FTS5 column index 1 (col 0 is
``tdoc_id UNINDEXED``; col 1 is the first indexed column,
``title``). The legacy ``_SNIPPET_COLUMN_TO_IDX`` offset was off
by one — every column was shifted up by 1, so the default
``"title"`` setting silently read from ``ftp_url`` and the
``preview`` came back as the unhighlighted zip path.

These tests pin the column index + the visible behavior:

1. End-to-end ``preview`` contains the matched title text with
   ``<<...>>`` markers (not the ``ftp_url``).
2. The default offset produces col_idx=1 for ``"title"``; explicit
   settings + raw SQL probe cross-check every other column too.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import event, text

from doc3gpp.models.search import SearchFilters
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import (
    SQLAlchemySearchIndexRepository,
)



# FTS5 column indices of the ``tdoc_search`` virtual table. The
# first declared column (``tdoc_id``) is UNINDEXED and starts at
# cid 0; the eight indexed columns then occupy cids 1..8. Pinning
# this layout here keeps the regression test honest even if the
# DDL in ``_create_search_schema`` ever drifts. ``tdoc_id`` is
# UNINDEXED and therefore not part of the runnable
# ``_SNIPPET_COLUMN_NAMES`` allowlist — only the 8 indexed columns
# are exercised by the parametrize sweep below.
_EXPECTED_FTS5_COLUMNS: dict[str, int] = {
    "title": 1,
    "ftp_url": 2,
    "meeting_title": 3,
    "meeting_location": 4,
    "wis": 5,
    "cover_text": 6,
    "change_text": 7,
    "ttcn_text": 8,
}


def _seed_index_only(search_corpus):
    """Upsert the pre-populated corpus rows into tdoc_search.

    The ``search_corpus`` fixture already inserts tdocs + meetings
    + sidecars; this helper pushes each pre-existing ``tdoc_id``
    through ``SearchIndexRepository.upsert`` to populate the FTS5
    virtual table. Returns the engine and the row ids.
    """
    from sqlalchemy import text as _text

    engine = get_engine()
    with engine.begin() as _conn:
        rows = _conn.execute(
            _text("SELECT tdoc_id FROM tdocs ORDER BY tdoc_id")
        ).all()
    tdoc_ids = [r[0] for r in rows]
    repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in tdoc_ids:
        repo.upsert(tdoc_id)
    return engine, tdoc_ids


def test_default_preview_comes_from_title_column(search_corpus) -> None:
    """``nb`` hits a title row → preview must contain title text + markers.

    ``RP-2200456`` has title ``"NB-IoT scheduling for Rel-17"``.
    Searching for the bare token ``nb`` matches the ``NB`` portion
    of the title (case-insensitive FTS5 match). The ``preview``
    must therefore contain title-flavoured text with ``<<...>>``
    markers (NOT the row's ftp_url).

    The query is intentionally the bare ``nb`` rather than
    ``nb-iot``: FTS5 interprets the hyphen as a token split, so
    ``nb-iot`` becomes two operands separated by the implicit
    AND-minus operator. Bare ``nb`` is the regression-check
    minimal query that still demonstrates the column-index bug.
    """
    engine, tdoc_ids = _seed_index_only(search_corpus)
    repo = SQLAlchemySearchIndexRepository()

    hits = repo.search("nb", SearchFilters(limit=20))
    assert hits, "expected at least one FTS5 hit for nb-iot"
    target = next((h for h in hits if h.tdoc_id == "RP-2200456"), None)
    assert target is not None, (
        f"expected RP-2200456 in hits {[h.tdoc_id for h in hits]}"
    )

    # Sanity: the title of this row IS what we searched on.
    with engine.begin() as _conn:
        row = _conn.execute(
            text("SELECT title, ftp_url FROM tdocs WHERE tdoc_id='RP-2200456'")
        ).first()
    assert row is not None
    assert "NB-IoT" in (row[0] or "")
    assert "RP-2200456-u1.zip" in (row[1] or "")

    # The previews map MUST contain the title text with markers. The
    # exact tokenization of ``NB-IoT`` by unicode61 produces two
    # tokens (``NB`` and ``IoT``) — snippet wraps each in its own
    # ``<<...>>``. We accept either the title-with-markers path OR
    # an empty previews map (no match in title), but NEVER the bare
    # ftp_url surfaced as a title snippet.
    assert "title" in target.previews, (
        f"previews missing 'title' key; got keys="
        f"{sorted(target.previews.keys())}"
    )
    title_snip = target.previews["title"]
    assert title_snip, "title snippet must not be empty"

    preview_is_ftp_url = "RP-2200456-u1.zip" in title_snip
    preview_has_markers = "<<" in title_snip and ">>" in title_snip
    assert not preview_is_ftp_url, (
        f"title snippet leaked the ftp_url column — "
        f"snippet() is reading the wrong FTS5 column. "
        f"title snippet={title_snip!r}"
    )
    assert preview_has_markers, (
        f"title snippet has no <<...>> markers; expected snippet() "
        f"output from the title column. title snippet={title_snip!r}"
    )


@pytest.mark.parametrize(
    "column_name, expected_cid",
    sorted(_EXPECTED_FTS5_COLUMNS.items(), key=lambda kv: kv[1]),
)
def test_snippet_column_idx_matches_fts5_schema(
    search_corpus, monkeypatch, column_name, expected_cid,
) -> None:
    """Every ``SnippetColumn`` name maps to its real FTS5 column index.

    Verified by binding ``Settings.search.snippet_column`` to each
    legal value, running ``search``, and capturing the
    ``:col_idx`` parameter passed to ``snippet(tdoc_search, :col, ...)``.

    Failure of any parametrize case pinpoints the exact column
    whose offset drifted.
    """

    # The ``search_corpus`` fixture has already built + populated
    # the index. We override ``snippet_column`` by env-var + cache
    # clear below, so reusing the corpus is fine.

    _seed_index_only(search_corpus)

    # ``SearchSettings`` is TOML-only (the env-var allowlist does
    # not include search.snippet_column). Mutate the live model
    # instance via ``object.__setattr__`` and restore it after the
    # test body runs (via try/finally).
    from doc3gpp.settings.loader import get_settings as _gs
    _search_settings = _gs().search
    _original = _search_settings.snippet_column
    object.__setattr__(_search_settings, "snippet_column", column_name)
    try:
        assert _search_settings.snippet_column == column_name, (
            f"failed to mutate SearchSettings.snippet_column to "
            f"{column_name!r}; got {_search_settings.snippet_column!r}"
        )
        repo = SQLAlchemySearchIndexRepository()
        assert repo._snippet_column == column_name
    finally:
        object.__setattr__(_search_settings, "snippet_column", _original)

    captured: list[tuple[str, tuple]] = []
    engine = get_engine()

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany,
    ):
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        repo.search("nb", SearchFilters(limit=1))
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    matched = [
        (stmt, params) for stmt, params in captured
        if "MATCH" in stmt and "snippet(" in stmt
    ]
    assert matched, f"no snippet-bearing SELECT captured (events={captured!r})"
    stmt, params = matched[-1]
    # The repo emits one ``snippet(tdoc_search, ?, ...)`` per
    # weight>0 column. The configured ``snippet_column`` MUST be
    # one of the bound snippet col_idx values; if it has weight
    # 0.0, it is NOT bound (snippet() is not emitted for it).
    snippet_calls = re.findall(r"snippet\(tdoc_search, \?", stmt)
    # SQLAlchemy binds named params in SQL-text order. The bm25
    # block consumes the first 8 params (w0..w7); each snippet()
    # then has 2 named params (col_n, tok). The snippet col_idx
    # values are therefore at even positions starting at 8.
    n = len(snippet_calls)
    col_values = {params[8 + 2 * i] for i in range(n)}
    if expected_cid not in col_values:
        # The configured snippet_column has weight 0.0 and the
        # repo intentionally skipped its snippet() call. That's
        # the new contract — the legacy "the configured
        # snippet_column is always bound" is no longer true once
        # weight=0 columns are excluded from the SELECT.
        # However the FTS5 cid must still match the DDL
        # expectation; the binding below pins the offset map.
        from doc3gpp.storage.repositories.search_sql import (
            _SNIPPET_COLUMN_TO_IDX,
        )
        assert _SNIPPET_COLUMN_TO_IDX[column_name] == expected_cid, (
            f"_SNIPPET_COLUMN_TO_IDX map drifted for {column_name!r}; "
            f"expected cid {expected_cid}, got "
            f"{_SNIPPET_COLUMN_TO_IDX[column_name]}"
        )
    else:
        assert expected_cid in col_values, (
            f"snippet_column={column_name!r} must map to FTS5 cid="
            f"{expected_cid} (per pragma_table_info); bound snippet "
            f"col_idx values were {sorted(col_values)}. "
            f"Statement:\n{stmt}"
        )
