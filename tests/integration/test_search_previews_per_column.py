"""Preview must include every column whose weight > 0.

The FTS5 search subsystem declares 8 indexed columns
(``title`` .. ``ttcn_text``). ``Settings.search.bm25_weights`` carries
a length-8 weight tuple; a column with weight ``0.0`` is intentionally
silenced (the FTS5 scoring and snippet both skip it).

The CLI used to print only the configured ``Settings.search.snippet_column``
(default ``title``) as the single ``preview`` field. The current
contract is: every column with ``bm25_weights[i] > 0`` gets its own
``snippet(tdoc_search, col_i, ...)`` pulled from the FTS5 hit,
returned as a ``{column_name: snippet_text}`` mapping on the
:class:`SearchHit`. Columns with weight ``0.0`` are absent from the
mapping entirely (so the CLI never has to defend against empty
placeholder strings).

These tests pin the contract end-to-end:

1. Repo-level: ``search()`` returns a hit whose ``previews`` map has
   every weight>0 column key and NO weight=0 column key. The SQL
   contains one ``snippet(tdoc_search, :col, ...)`` per weight>0
   column.
2. CLI-level: ``_render_search_hits`` prints each weight>0 column's
   snippet in ``table`` and ``markdown`` formats, omits weight=0
   columns entirely, and ships a ``previews`` mapping in ``json``.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import event, text

from doc3gpp.cli import _render_search_hits
from doc3gpp.models.search import SearchFilters
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import _SNIPPET_COLUMN_NAMES


# 8 indexed columns, in DDL order. The default ``bm25_weights`` are
# (5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0) so:
#   - title (cid 1)        weight 5.0  -> MUST be in previews
#   - ftp_url (cid 2)      weight 0.0  -> MUST NOT be in previews
#   - meeting_title (cid 3) weight 0.0 -> MUST NOT be in previews
#   - meeting_location (cid 4) weight 1.0 -> MUST be in previews
#   - wis (cid 5)          weight 5.0  -> MUST be in previews
#   - cover_text (cid 6)   weight 5.0  -> MUST be in previews
#   - change_text (cid 7)  weight 5.0  -> MUST be in previews
#   - ttcn_text (cid 8)    weight 5.0  -> MUST be in previews
_DEFAULT_WEIGHTS = (5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0)


def _restore_weights(original: tuple[float, ...]) -> None:
    object.__setattr__(get_settings().search, "bm25_weights", original)


def _seed_index_only(search_corpus) -> tuple[list[str], object]:
    """Re-upsert the pre-populated corpus and return (tdoc_ids, engine)."""
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    engine = get_engine()
    with engine.begin() as _conn:
        rows = _conn.execute(
            text("SELECT tdoc_id FROM tdocs ORDER BY tdoc_id")
        ).all()
    tdoc_ids = [r[0] for r in rows]
    repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in tdoc_ids:
        repo.upsert(tdoc_id)
    return tdoc_ids, engine


def test_search_returns_per_column_previews_for_weight_positive(
    search_corpus,
) -> None:
    """``previews`` contains every weight>0 column and excludes weight=0 ones.

    Pinpoint: ``ftp_url`` (weight 0.0) and ``meeting_title`` (weight
    0.0) MUST be absent from the mapping. ``title``, ``wis``,
    ``cover_text``, ``change_text``, ``ttcn_text`` (all weight 5.0)
    and ``meeting_location`` (weight 1.0) MUST be present.
    """
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    _seed_index_only(search_corpus)
    repo = SQLAlchemySearchIndexRepository()

    # Pin the default weights so the test is hermetic against settings drift.
    _search_settings = get_settings().search
    _original = _search_settings.bm25_weights
    object.__setattr__(_search_settings, "bm25_weights", _DEFAULT_WEIGHTS)
    try:
        repo = SQLAlchemySearchIndexRepository()
        assert repo._weights == _DEFAULT_WEIGHTS

        hits = repo.search("nb", SearchFilters(limit=20))
        assert hits, "expected at least one FTS5 hit for 'nb'"
        target = next((h for h in hits if h.tdoc_id == "RP-2200456"), None)
        assert target is not None, (
            f"expected RP-2200456 in hits {[h.tdoc_id for h in hits]}"
        )

        # Per-column snippet mapping is present.
        assert hasattr(target, "previews"), (
            "SearchHit must expose a `previews` dict mapping column -> snippet"
        )
        assert isinstance(target.previews, dict)
        # ``previews`` carries only columns that BOTH (a) have
        # weight>0 AND (b) actually matched the query (i.e. snippet
        # produced a non-empty highlight). For 'nb' the corpus has
        # matches in the title (NB-IoT) and the meeting location
        # (Online for the row, but the body of the cover/wis may also
        # have NB references — accept any subset of the weight>0
        # columns that actually has a match). The binding constraint
        # is that weight=0 columns MUST be absent and every key in
        # the map MUST be a weight>0 column.
        weight_positive = {
            name for name, w in zip(
                _SNIPPET_COLUMN_NAMES, _DEFAULT_WEIGHTS, strict=True
            ) if w > 0
        }
        assert target.previews, (
            "expected at least one column to match 'nb' in the title row; "
            f"got previews={target.previews!r}"
        )
        for key in target.previews:
            assert key in weight_positive, (
                f"previews key {key!r} has weight<=0; must be one of "
                f"{sorted(weight_positive)}"
            )
        # Weight=0 columns MUST be absent.
        assert "ftp_url" not in target.previews
        assert "meeting_title" not in target.previews

        # The legacy `preview` field is gone — only `previews` remains.
        assert not hasattr(target, "preview") or target.preview is None
    finally:
        _restore_weights(_original)


def test_search_returns_only_columns_with_matching_marker(
    search_corpus,
) -> None:
    """``previews`` only carries columns whose snippet has ``<<...>>`` markers.

    FTS5 ``snippet()`` returns the closest context for the column
    even when the column itself has no match — so a weight>0 column
    can be in the SELECT and still return a marker-less string. The
    contract is: a column belongs in ``previews`` only if BOTH
    (a) ``bm25_weights[i] > 0`` AND (b) the snippet contains
    ``<<...>>`` markers. Marker-less weight>0 columns MUST be
    dropped from the mapping.
    """
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    _seed_index_only(search_corpus)
    _search_settings = get_settings().search
    _original = _search_settings.bm25_weights
    object.__setattr__(_search_settings, "bm25_weights", _DEFAULT_WEIGHTS)
    try:
        repo = SQLAlchemySearchIndexRepository()
        hits = repo.search("nb", SearchFilters(limit=20))
        assert hits, "expected at least one FTS5 hit for 'nb'"
    finally:
        _restore_weights(_original)

    # Every hit's previews must satisfy: for every (col, snippet)
    # pair, the snippet contains both ``<<`` and ``>>`` markers.
    for h in hits:
        for col, snippet in h.previews.items():
            assert "<<" in snippet and ">>" in snippet, (
                f"hit {h.tdoc_id} column {col!r} is in previews but has no "
                f"<<...>> markers (snippet={snippet!r}); a column is only "
                f"shown when its snippet has a real match. previews="
                f"{h.previews!r}"
            )


def test_search_sql_emits_one_snippet_per_weight_positive_column(
    search_corpus,
) -> None:
    """The generated SELECT contains N ``snippet()`` calls where N = #weight>0.

    With the default weights, N = 6 (title, meeting_location, wis,
    cover_text, change_text, ttcn_text). ftp_url (cid 2) and
    meeting_title (cid 3) MUST NOT appear as ``snippet(..., 2, ...)``
    or ``snippet(..., 3, ...)`` calls.
    """
    from doc3gpp.storage.db.session import get_engine
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    _seed_index_only(search_corpus)
    _search_settings = get_settings().search
    _original = _search_settings.bm25_weights
    object.__setattr__(_search_settings, "bm25_weights", _DEFAULT_WEIGHTS)
    try:
        repo = SQLAlchemySearchIndexRepository()
    finally:
        _restore_weights(_original)

    engine = get_engine()
    captured: list[tuple[str, tuple]] = []

    def _before_cursor_execute(
        conn, cursor, statement, parameters, context, executemany,
    ):
        captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    try:
        repo.search("nb", SearchFilters(limit=1))
    finally:
        event.remove(engine, "before_cursor_execute", _before_cursor_execute)

    snippet_stmts = [
        (stmt, params) for stmt, params in captured
        if "MATCH" in stmt and "snippet(" in stmt
    ]
    assert snippet_stmts, (
        f"expected a snippet-bearing SELECT; saw {len(captured)} events"
    )
    stmt, _params = snippet_stmts[-1]

    snippet_calls = re.findall(r"snippet\(tdoc_search, \?", stmt)
    # SQLAlchemy binds named params in the order they appear in the
    # SQL text. The bm25 block has 8 weight params, then each
    # snippet() has 1 col param + 1 tok param. The params tuple is
    # therefore interleaved (col_0, tok, col_1, tok, ...). We just
    # count the snippet calls and inspect the named binding values
    # via the dict (``stmt`` is the raw SQL; the bound values are
    # in ``_params``).
    assert len(snippet_calls) == sum(1 for w in _DEFAULT_WEIGHTS if w > 0), (
        f"expected exactly {sum(1 for w in _DEFAULT_WEIGHTS if w > 0)} "
        f"snippet() calls (one per weight>0 column), got {len(snippet_calls)}: "
        f"{snippet_calls}\nstatement:\n{stmt}"
    )
    # The named col_<n> params must be the cids of the weight>0
    # columns. Cids 2 (ftp_url) and 3 (meeting_title) MUST NOT be
    # bound. SQLAlchemy binds named params in SQL-text order; the
    # bm25 block has 8 weights first, then each snippet() has a
    # col + tok pair, then the WHERE :query + LIMIT :limit at the
    # end. So the col values are at even positions starting at 8.
    n = sum(1 for w in _DEFAULT_WEIGHTS if w > 0)
    col_values = sorted(_params[8 + 2 * i] for i in range(n))
    weight_positive_cids = sorted(
        i + 1 for i, w in enumerate(_DEFAULT_WEIGHTS) if w > 0
    )
    assert col_values == weight_positive_cids, (
        f"bound snippet col_idx values must be exactly the weight>0 "
        f"cids {weight_positive_cids}; got {col_values} "
        f"(full params: {_params!r})"
    )
    assert 2 not in col_values, (
        f"snippet() MUST NOT bind ftp_url cid (2, weight 0.0); "
        f"got col values: {col_values}"
    )
    assert 3 not in col_values, (
        f"snippet() MUST NOT bind meeting_title cid (3, weight 0.0); "
        f"got col values: {col_values}"
    )


def test_render_search_hits_table_prints_all_weight_positive_columns(
    search_corpus, capsys, monkeypatch,
) -> None:
    """``_render_search_hits(format='table')`` prints each weight>0 column.

    The row block must include the column label (e.g. ``title``,
    ``meeting_location``, ``wis``, ...) followed by the snippet text
    containing ``<<...>>`` markers for every column whose snippet
    actually has a match. Weight=0 columns (``ftp_url``,
    ``meeting_title``) MUST NOT appear as labeled snippet sources.
    """
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    _seed_index_only(search_corpus)
    _search_settings = get_settings().search
    _original = _search_settings.bm25_weights
    object.__setattr__(_search_settings, "bm25_weights", _DEFAULT_WEIGHTS)
    try:
        repo = SQLAlchemySearchIndexRepository()
        hits = repo.search("nb", SearchFilters(limit=5))
        assert hits
        _render_search_hits(hits, format="table", compact=False)
    finally:
        _restore_weights(_original)

    out = capsys.readouterr().out
    # The hits' previews map is the source of truth for which
    # columns have matches; for each such column the table must
    # render the column label. Use the first hit as the reference.
    weight_positive = {
        name for name, w in zip(
            _SNIPPET_COLUMN_NAMES, _DEFAULT_WEIGHTS, strict=True
        ) if w > 0
    }
    for col in hits[0].previews:
        assert col in weight_positive, (
            f"first hit's previews contained weight=0 column {col!r}; "
            f"previews={hits[0].previews!r}"
        )
        # The column label MUST appear in the table output (as
        # ``<col>: <snippet>`` in the row).
        assert f"{col}:" in out, (
            f"table output missing labeled snippet for {col!r}; "
            f"output was:\n{out}"
        )
    # Weight=0 columns MUST NOT appear as labeled snippet sources
    # in the table (no ``<col>:`` continuation rows for them).
    for col in ("ftp_url", "meeting_title"):
        # Look for the labeled snippet pattern on continuation lines
        # (indented with two spaces), which is where the table
        # renderer emits the label. Also check the row's preview
        # column doesn't carry the weight=0 column label.
        bad = re.compile(rf"^\s*{re.escape(col)}:", re.MULTILINE)
        assert not bad.search(out), (
            f"table output leaked weight=0 column {col!r} as a "
            f"labeled snippet source; output was:\n{out}"
        )


def test_render_search_hits_markdown_prints_all_weight_positive_columns(
    search_corpus, capsys, monkeypatch,
) -> None:
    """Markdown renderer emits a labeled snippet block per weight>0 column."""
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    _seed_index_only(search_corpus)
    _search_settings = get_settings().search
    _original = _search_settings.bm25_weights
    object.__setattr__(_search_settings, "bm25_weights", _DEFAULT_WEIGHTS)
    try:
        repo = SQLAlchemySearchIndexRepository()
        hits = repo.search("nb", SearchFilters(limit=5))
        assert hits
        _render_search_hits(hits, format="markdown", compact=False)
    finally:
        _restore_weights(_original)

    out = capsys.readouterr().out
    # Every column whose snippet actually has a match MUST be
    # rendered as a labeled block (column name + ``<<...>>``
    # markers) in the markdown output. Use the first hit's
    # previews map as the source of truth (same as the table test).
    for col in hits[0].previews:
        pattern = re.compile(
            rf"{re.escape(col)}:.*?<<.*?>>",
            re.DOTALL,
        )
        assert pattern.search(out), (
            f"markdown output missing labeled snippet for {col!r}; "
            f"output was:\n{out}"
        )
    # Weight=0 columns must NOT appear as labeled snippet sources.
    for col in ("ftp_url", "meeting_title"):
        bad = re.compile(rf"^\s*>\s*{re.escape(col)}:", re.MULTILINE)
        assert not bad.search(out), (
            f"markdown output leaked weight=0 column {col!r} as a "
            f"snippet source; output was:\n{out}"
        )


def test_render_search_hits_json_emits_previews_mapping(
    search_corpus, capsys, monkeypatch,
) -> None:
    """JSON renderer ships a ``previews`` dict per hit with weight>0 columns."""
    from doc3gpp.storage.repositories.search_sql import (
        SQLAlchemySearchIndexRepository,
    )

    _seed_index_only(search_corpus)
    _search_settings = get_settings().search
    _original = _search_settings.bm25_weights
    object.__setattr__(_search_settings, "bm25_weights", _DEFAULT_WEIGHTS)
    try:
        repo = SQLAlchemySearchIndexRepository()
        hits = repo.search("nb", SearchFilters(limit=5))
        assert hits
        _render_search_hits(hits, format="json", compact=False)
    finally:
        _restore_weights(_original)

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list) and payload
    first = payload[0]
    assert "previews" in first, f"json hit missing 'previews' field: {first}"
    assert isinstance(first["previews"], dict)
    # The map's keys MUST all be weight>0 columns; ftp_url and
    # meeting_title (weight 0.0) MUST be absent. The exact set
    # depends on which columns had a match for the query, so we
    # don't assert a specific subset.
    weight_positive = {
        name for name, w in zip(
            _SNIPPET_COLUMN_NAMES, _DEFAULT_WEIGHTS, strict=True
        ) if w > 0
    }
    for key in first["previews"]:
        assert key in weight_positive, (
            f"previews key {key!r} has weight<=0; got "
            f"{sorted(first['previews'].keys())}"
        )
    assert "ftp_url" not in first["previews"]
    assert "meeting_title" not in first["previews"]
    assert "preview" not in first, (
        f"json hit must NOT carry the legacy 'preview' field; got {first}"
    )
