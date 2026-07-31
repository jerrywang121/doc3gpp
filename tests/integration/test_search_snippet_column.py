"""Regression: per-column previews must surface from each FTS5 column.

The read path binds one ``snippet(tdoc_search, ...)`` per column whose
``bm25_weights[i] > 0`` and only surfaces the snippet in the result
``previews`` map when the snippet actually contains a match
(``<<...>>`` markers). A weight-0 column produces no snippet() call
and no entry in ``previews``.

The 8 FTS5 columns of the ``tdoc_search`` virtual table occupy cids
1..8 (cid 0 is the UNINDEXED ``tdoc_id``). The offset is the only
piece of FTS5-specific knowledge in the repo; the helper
``_snippet_column_cid(name)`` computes it from
``_SNIPPET_COLUMN_NAMES.index(name) + 1``.

These tests pin the offset map and one concrete preview behaviour:

1. End-to-end ``previews`` map for a title hit on a TDoc whose
   ``bm25_weights[title] > 0`` contains the title text with
   ``<<...>>`` markers (the matching-column snippet) and never the
   bare ``ftp_url``.
2. Every indexed column name maps to its real FTS5 column index in
   the helper.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from doc3gpp.models.search import SearchFilters
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import (
    _snippet_column_cid,
)


# FTS5 column indices of the ``tdoc_search`` virtual table. The
# first declared column (``tdoc_id``) is UNINDEXED and starts at
# cid 0; the eight indexed columns then occupy cids 1..8. Pinning
# this layout here keeps the regression test honest even if the
# DDL in ``_create_search_schema`` ever drifts.
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
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )
    repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in tdoc_ids:
        repo.upsert(tdoc_id)
    return engine, tdoc_ids


def test_title_preview_contains_title_text_with_markers(search_corpus) -> None:
    """``nb`` hits a title row → ``previews['title']`` is title text + markers.

    ``RP-2200456`` has title ``"NB-IoT scheduling for Rel-17"``.
    Searching for the bare token ``nb`` matches the ``NB`` portion
    of the title (case-insensitive FTS5 match) and the default
    ``bm25_weights`` set ``title`` to ``5.0`` so the title column
    is bound in the SELECT and the matching-column snippet
    surfaces. The ``previews`` map must therefore contain
    title-flavoured text with ``<<...>>`` markers, NOT the row's
    ``ftp_url``.

    The query is intentionally the bare ``nb`` rather than
    ``nb-iot``: FTS5 interprets the hyphen as a token split, so
    ``nb-iot`` becomes two operands separated by the implicit
    AND-minus operator. Bare ``nb`` is the regression-check
    minimal query that still demonstrates a per-column preview.
    """
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    engine, _tdoc_ids = _seed_index_only(search_corpus)
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
def test_snippet_column_cid_matches_fts5_schema(
    column_name: str, expected_cid: int,
) -> None:
    """Every indexed column name maps to its real FTS5 column index.

    The repo's ``_snippet_column_cid(name)`` is the only piece of
    FTS5-specific offset knowledge. The bm25 / snippet() SQL is
    fully parameterised on the returned ``cid``; if this offset
    drifts, every per-column snippet silently reads from the wrong
    column. Failure of any parametrize case pinpoints the column
    whose offset drifted.
    """
    assert _snippet_column_cid(column_name) == expected_cid, (
        f"_snippet_column_cid({column_name!r}) drifted; "
        f"expected cid {expected_cid}, got "
        f"{_snippet_column_cid(column_name)}"
    )
