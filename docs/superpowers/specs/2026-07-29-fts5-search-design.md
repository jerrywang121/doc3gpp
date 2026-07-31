# FTS5 Full-Text Search — Design Spec

**Status:** Draft (pending user review)
**Date:** 2026-07-29
**Branch:** main
**Author:** brainstorming session

## Goal

Add a fast, in-process full-text search over every TDoc, CR cover page,
CR change details, TTCN details, meeting metadata, and related Work
Items to doc3gpp, using SQLite's built-in FTS5 virtual table. Searches
return ranked hits with snippets; the index is rebuilt or kept fresh
automatically as the underlying data changes.

The dominant use case is **identifier-first search**: a user knows the
3GPP TDoc id (e.g. `R5-1234567`, `RP-2200456`, `38.300`) or a piece
of jargon (`NB-IoT`, `eNB`) and wants to find the matching document
quickly. A secondary use case is free-text search across the body of
parsed CRs.

## Non-goals

- **Semantic / embedding search.** Deferred to a separate spec. This
  spec ships a `PassthroughReranker` so the embeddings spec can plug in
  a real reranker without any other change.
- **MCP server transport.** Deferred. When that spec lands, the
  existing `SearchService` is the integration point.
- **Web UI / search box.** Deferred.
- **3GPP-domain keyword extraction / NER.** Deferred. The
  tokenizer-level recognition of TDoc ids and spec numbers is enough
  for v1.
- **Multi-backend full-text (MySQL `FULLTEXT`, PostgreSQL `pg_trgm`,
  external ElasticSearch).** FTS5 is sqlite-only by design; on
  other dialects `search` reports unavailable and degrades gracefully.
- **Substring / wildcard search by default.** FTS5's default is
  whole-token `MATCH`; users searching for `12345` will not find
  `R5-12345`. Prefix queries (`R5-*`) and exact-id queries
  (`R5-12345`) are documented and recommended.
- **Live auto-rebuild on every tdoc row change.** The auto-index
  hook fires after `tdoc parse` (which is the dominant write path);
  non-parse updates (e.g. background resync) leave the index stale
  until the next parse or an explicit `search index --rebuild`.

## Architecture

doc3gpp's existing layering rules (from AGENTS.md) are preserved:

```
CLI subcommand ──► SearchService ──► SearchIndexRepository (Protocol)
                          │                       │
                          │                       └─► SQLAlchemySearchIndexRepository
                          │                              uses SQLAlchemy engine + raw FTS5 SQL
                          │
                          └─► EmbeddingReranker (Protocol) ──► PassthroughReranker (default)
                                                              ──► [later] EmbeddingReranker impl
```

### New modules

| File | Purpose |
|---|---|
| `models/search.py` | `SearchHit`, `SearchQuery`, `SearchFilters`, `SearchIndexStatus`, `RebuildProgress`, error hierarchy (`SearchError`, `SearchUnavailableError`, `SearchQueryError`, `SearchIndexCorruptError`) |
| `repository/protocols.py` (extend) | `SearchIndexRepository` Protocol, `EmbeddingReranker` Protocol |
| `storage/db/fts5_query.py` | The Python-side pre-processor `normalize_query(text) -> str` + the `TDOC_ID_BASE_RE`, `SPEC_ID_RE` regexes |
| `storage/repositories/search_sql.py` | SQL impl of `SearchIndexRepository`; FTS5 DDL/DML; gzip decompression at index time; runs `normalize_query` on every text column before INSERT |
| `services/search_service.py` | Orchestration: `upsert_for_tdoc`, `remove_for_tdoc`, `search`, `rebuild`, `status` |
| `services/factory.py` (extend) | `build_search_service(settings, repo=None, reranker=None)` |
| `services/tdoc_cr_service.py` (modify) | Two new call sites for `_index_after_parse(tdoc_id)` |
| `cli.py` (extend) | `search_app` Typer sub-app with `query` and `index` commands; CLI filter parsing; `--rerank`, `--snippet-tokens`, `--quiet`, `--explain` flags |
| `cli_filters.py` (extend) | `parse_date_filter`, `parse_release_filter`, `parse_spec_filter`; new `SearchQueryBuilder` for FTS5 query normalization |
| `settings/schema.py` (extend) | New `Settings.search` section: `enabled`, `auto_index_on_parse`, `rebuild_batch_size`, `snippet_tokens` |

### Single integration point

All writes to the index flow through `SearchService.upsert_for_tdoc`.
The hook fires from **two** call sites in `TDocCrService` because both
produce the same final DB state (cover + ttcn + change + extracts
rows):

1. **DB-mode `extract()` happy path** — `tdoc parse --tdoc <id>` /
   `--meeting-id <id>` / `--meeting <name>` / filter-driven batch.
2. **Direct-mode `_extract_from_3gpp_url()` happy path** —
   `tdoc parse --from-url <3gpp-url>` and the per-file branch of
   `--from-url <3gpp-folder>` (recursive). The auto-sync helper
   (`trigger_auto_sync`) populates the `tdocs` table first; the FK
   check passes; the same four upserts run.

In both cases, the helper fires *after* all four upserts return
successfully, never on the early-return (cache hit) or FK-miss
branches.

`--from-path` (local file) and non-3GPP `--from-url` paths bypass DB
writes entirely per AGENTS.md and never reach the hook.

### Data flow — read path

```
doc3gpp search query "scheduling NR" [--tsg RAN1] ...
        │
        ▼
SearchCommand.__call__(cli_filters)
        │   ─ normalize query (SearchQueryBuilder)
        │   ─ validate filters (parse_*_filter)
        │
        ▼
SearchService.search(query, filters)
        │   ── FTS5 MATCH + filters via repo
        │   ── EmbeddingReranker.rerank(query, hits)   ← PassthroughReranker for v1
        │
        ▼
list[SearchHit]
        │
        ▼
CLI formatter (table|json|markdown, --compact)
```

### Graceful degradation

Following world-intel-mcp's pattern for optional subsystems:

1. **Search is gated behind a new `[search]` optional extra** in
   `pyproject.toml` (no new runtime deps; the extra exists to mark
   the FTS5 module + tokenizer for explicit opt-in and to give the
   installer a clean `pip install doc3gpp[search]` story).
2. **Runtime FTS5 probe** — `SQLAlchemySearchIndexRepository.__init__`
   runs `SELECT sqlite_version()` and `PRAGMA compile_options` to
   detect the FTS5 build; raises `SearchUnavailableError` with the
   detected version + dialect on non-sqlite or FTS5-less builds.
3. **`build_search_service` catches `SearchUnavailableError`** and
   returns `None`. `TDocCrService.__init__` accepts
   `search_service: SearchService | None = None`; the hook short-
   circuits on `None`. **Net effect: doc3gpp without `[search]`
   behaves exactly like doc3gpp today.**
4. **CLI catches each error type** and prints a friendly one-liner:

   | Error | Message | Exit |
   |---|---|---|
   | `SearchUnavailableError` | `search unavailable: <reason>` + `pip install doc3gpp[search]` | 1 |
   | `SearchQueryError` | `bad query: <reason>` | 2 |
   | `SearchIndexCorruptError` | `search index corrupt; run \`doc3gpp search index --rebuild\`` | 3 |
   | `Settings.search.enabled = false` | `search disabled in settings` | 0 |

## FTS5 schema

Two new objects in the sqlite database, created by
`storage/db/create_schema.py`:

```sql
-- Main FTS5 virtual table.
CREATE VIRTUAL TABLE IF NOT EXISTS tdoc_search USING fts5(
    tdoc_id UNINDEXED,                       -- PK; joins back to tdocs
    title,                                   -- tdocs.title (pre-normalized)
    ftp_url,                                 -- tdocs.ftp_url
    meeting_title,                           -- meetings.title (joined)
    meeting_location,                        -- meetings.location (joined)
    wis,                                     -- related_wis acronyms + names joined as text
    cover_text,                              -- tdoc_cr_cover_page flat text cols + decoded blob (pre-normalized)
    change_text,                             -- tdoc_cr_change_details.clauses + decoded changes blob (pre-normalized)
    ttcn_text                                -- tdoc_cr_ttcn_details flat cols + decoded required_changes + changed_functions (pre-normalized)
);

-- Sidecar metadata table for rebuild resume + staleness detection.
CREATE TABLE IF NOT EXISTS tdoc_search_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Rows:
--   'last_rebuild_at'                   <iso timestamp of last successful rebuild>
--   'last_indexed_uploaded_date'        <max tdocs.uploaded_date at last successful upsert>
--   'last_rebuild_last_tdoc_id'         <cursor for resume>
--   'last_indexed_at'                   <iso timestamp of last successful upsert (any path)>
```

### Why `tdoc_id` (not `ftp_url`) is the FTS5 row identity

The user thinks in TDoc terms ("find CRs about NR scheduling"), not
URL terms. Multiple revisions of the same tdoc_id are conceptually one
*thing*. The index always reflects the *latest* revision's text (the
underlying join picks the `tdocs` row and follows its FK to the
matching detail rows); users who want a specific revision query
`R5-1234567 r2` and the corpus test pins that behaviour.

This sits alongside (not against) the existing CR identity contract
where `ftp_url` is the PK for `tdoc_cr_cover_page`,
`tdoc_cr_ttcn_details`, `tdoc_cr_change_details`, and `tdoc_extracts`.
The FTS5 row is a derived projection; its keying follows the
searcher's mental model, not the storage identity.

### What gets concatenated into each text column

At index time, `SQLAlchemySearchIndexRepository._build_index_text(tdoc_id)`
runs a single SELECT joining all source tables and decompresses the
gzip blobs in Python (sqlite has no `gzip()` SQL builtin; see
`storage/compression.py` for the shared helpers):

```
tdocs.title            ─► title
tdocs.ftp_url          ─► ftp_url
meetings.title         ─► meeting_title   (LEFT JOIN)
meetings.location      ─► meeting_location (LEFT JOIN)
GROUP_CONCAT(wis.acronym || ' ' || wis.name, ' ')
    via tdocs.related_wis (comma-separated, split by comma)
                       ─► wis
tdoc_cr_cover_page.flat cols joined with ' '
    + gzip_decompress(blob) stringified  ─► cover_text
tdoc_cr_change_details.clauses (gzip → JSON → join)
    + gzip_decompress(changes) stringified  ─► change_text
tdoc_cr_ttcn_details.flat cols joined with ' '
    + gzip_decompress(required_changes) stringified
    + changed_functions (already text)    ─► ttcn_text
```

A row with none of the sidecars present still gets indexed from the
metadata-only columns (title + meeting + WIs) — useful for LS / DRAFT
TDocs that have no cover page.

### DML

- `upsert(tdoc_id)` —
  `INSERT OR REPLACE INTO tdoc_search (tdoc_id, ...) VALUES (...)`.
  Naturally idempotent under retries.
- `delete(tdoc_id)` —
  `DELETE FROM tdoc_search WHERE tdoc_id = ?`.
- `search(query, filters, limit)` —
  ```sql
  SELECT tdoc_id,
         bm25(tdoc_search) AS score,
         snippet(tdoc_search, <first-text-col>, '<<', '>>', '…', :snippet_tokens) AS preview
    FROM tdoc_search
   WHERE tdoc_search MATCH :query
     [AND filters...]
   ORDER BY score
   LIMIT :limit
  ```
  Filters apply as additional `AND` clauses on the joined
  `tdocs` + `meetings` tables. The first text column used for
  `snippet()` is configurable; default is `title` for short
  previews.
- `rebuild(batch_size, resume, stale_only)` — iterates over
  tdoc_ids, calls `upsert(tdoc_id)` per row, updates
  `tdoc_search_meta` for resume.
- `status()` — reads `tdoc_search_meta` + runs
  `SELECT COUNT(*) FROM tdoc_search` and
  `SELECT MAX(uploaded_date) FROM tdocs` to compute `is_stale`.

## Tokenizer design

The FTS5 virtual table uses the **built-in `unicode61` tokenizer**
(stock sqlite, no Python callable required). Python's bundled
sqlite (3.45.1, current as of 2026) lacks `ENABLE_FTS5_TOKENIZER`,
so a custom Python tokenizer registered via `fts5_tokenizer()`
is not portable to the runtime we ship against. The `tokenchars`
directive that would let `unicode61` preserve `.` and `-` is
also rejected by the runtime (parse error in `tokenize` directive).

The trade-off:

- **What we keep:** TDoc ids split into `r5`, `1234567`, `r2` for
  `R5-1234567r2` (which is exactly what the spec wanted — searching
  for the base id finds every revision); hyphenated jargon splits
  automatically (`NB-IoT` → `nb`, `iot`); spec numbers like
  `38.300` split into `38`, `300` (one **less** ideal than the
  original "stay one token" goal but matches reality).
- **What we lose:** `38.300` is no longer a single FTS5 token.
  Searches for the full spec id need prefix matching (`38.300*`
  — the dot still groups `38` then `300` on either side). The
  CLI search command will handle this transparently.

A small Python-side pre-processor
(`doc3gpp.storage.db.fts5_query.normalize_query`) exists to
do two things FTS5 cannot do on its own:

1. **Apply the `TDOC_ID_BASE_RE` recognition rule at index time**
   so `R5-1234567r2` writes as `R5-1234567 R5-1234567r2` — the
   full id stays one token (`unicode61` would otherwise split on
   the hyphen) AND the base id is also a separate token (so
   searches for the base find every revision). This is the only
   piece of the recognition table that the `unicode61` tokenizer
   does not naturally give us.
2. **Apply the `SPEC_ID_RE` recognition rule at index time** so
   `38.300` writes as `38_300` (underscore instead of dot) so the
   full spec id stays one token in the index. Searches using the
   spec must use the same normalization (`38_300` rather than
   `38.300`) — the CLI's `SearchQueryBuilder` does this
   automatically.

The normalization runs **once per FTS5 column at index time**;
queries run against the index do NOT run the pre-processor —
they only go through `SearchQueryBuilder` (T9) which handles
spec-id normalization on the user-input side.

### Recognition table (what `unicode61` does naturally)

| Input | Tokens emitted by `unicode61` |
|---|---|
| `R5-1234567r2` | `r5`, `1234567`, `r2` |
| `R5-1234567` | `r5`, `1234567` |
| `NB-IoT` | `nb`, `iot` |
| `5G NR scheduling` | `5g`, `nr`, `scheduling` |
| `eNB/gNB` | `enb`, `gnb` |
| `38.300` | `38`, `300` |
| `R5-12345678` | `r5`, `12345678` |

### What `normalize_query` adds on top

| Input | After `normalize_query` (written to FTS5) |
|---|---|
| `R5-1234567r2` | `R5-1234567 R5-1234567r2` — both base and full id are now searchable |
| `38.300` | `38_300` — full spec id becomes a single token |
| `38.300-1` | `38_300-1` — full versioned spec id becomes a single token |
| `NB-IoT` | unchanged — `unicode61` already splits it |

### Test contract

`tests/unit/test_fts5_query.py` ships two parametrized tests
pinning the `normalize_query` behaviour:

```python
@pytest.mark.parametrize("raw,normalized", [
    ("R5-1234567r2",  "R5-1234567 R5-1234567r2"),
    ("R5-1234567",    "R5-1234567 R5-1234567"),
    ("RP-2200456r10", "RP-2200456 RP-2200456r10"),
    ("38.300",        "38_300"),
    ("38.300-1",      "38_300-1"),
    ("NB-IoT",        "NB-IoT"),     # passthrough
    ("5G NR",         "5G NR"),      # passthrough
    ("",              ""),           # empty
])
def test_normalize_query(raw, normalized):
    assert normalize_query(raw) == normalized
```

The corpus is the contract. If a recognition rule changes, the
test changes with it — never one without the other.

### Note on the existing `CR_ID_RE`

The current code in `parsers/tdoc_parser.py:15` and
`scraping/tdoc_zip_source.py:35` uses
`[RSC][1-9][-sw]\d{6,7}(?:r\d{1})?`, which excludes `P` for plenary
TDocs (`RP-...`, `SP-...`, `CP-...`). The
`TDOC_ID_BASE_RE` in `storage/db/fts5_query.py` uses
`[RSC][1-9P]` which fixes that gap for tokenization purposes
only. Fixing the existing `CR_ID_RE` in those two files is
**out of scope** for this spec; the user explicitly scoped
that to a separate change.

## Service layer & auto-index hook

### `SearchService` shape

```python
class SearchService:
    def __init__(self, repo: SearchIndexRepository, reranker: EmbeddingReranker): ...

    # Write paths
    def upsert_for_tdoc(self, tdoc_id: int) -> None: ...
    def remove_for_tdoc(self, tdoc_id: int) -> None: ...

    # Read path
    def search(self, query: str, filters: SearchFilters) -> list[SearchHit]: ...

    # Maintenance
    def rebuild(self, batch_size: int = 500, resume: bool = False,
                stale_only: bool = False, quiet: bool = False) -> Iterator[RebuildProgress]: ...
    def status(self) -> SearchIndexStatus: ...
```

- `upsert_for_tdoc(tdoc_id)` delegates to `repo.upsert(tdoc_id)`,
  which builds the concatenated text from the joined tables.
- `search(query, filters)` runs the FTS5 query, then passes hits
  through `reranker.rerank(query, hits)` before returning.
- `rebuild()` is a **generator** that yields `RebuildProgress` per
  batch — the CLI's `--quiet` flag controls whether the consumer
  prints each batch.
- `status()` returns the dataclass:
  ```python
  @dataclass(slots=True)
  class SearchIndexStatus:
      enabled: bool
      row_count: int
      last_rebuild_at: datetime | None
      last_indexed_uploaded_date: datetime | None
      latest_tdocs_uploaded_date: datetime | None
      is_stale: bool
  ```

### Factory wiring

```python
def build_search_service(
    settings: Settings,
    repo: SearchIndexRepository | None = None,
    reranker: EmbeddingReranker | None = None,
) -> SearchService | None:
    """Build a SearchService or return None if FTS5 is unavailable."""
    try:
        if repo is None:
            repo = SQLAlchemySearchIndexRepository(settings)
        if reranker is None:
            reranker = PassthroughReranker()
        return SearchService(repo=repo, reranker=reranker)
    except SearchUnavailableError:
        return None
```

`build_tdoc_cr_service` calls `build_search_service(settings)` and
passes the result (possibly `None`) as
`search_service=...` to `TDocCrService(...)`.

### `TDocCrService` integration

`TDocCrService.__init__` gains one parameter:

```python
def __init__(
    self,
    settings: Settings,
    cover_repo: ...,
    cr_ttcn_repo: ...,
    change_details_repo: ...,
    extract_repo: ...,
    tdoc_repo: ...,
    scraper: ...,
    cache: ...,
    max_tdoc_size_bytes: int = 0,
    search_service: SearchService | None = None,  # NEW
) -> None: ...
```

The new private helper:

```python
def _index_after_parse(self, tdoc_id: int) -> None:
    """Best-effort FTS5 upsert. Logs warnings, never raises.

    Called by both the DB-mode ``extract`` happy path and the
    direct-mode ``_extract_from_3gpp_url`` happy path — both
    produce the same final DB state, so both should keep the index
    in sync. Skipped when ``Settings.search.auto_index_on_parse``
    is False or when the search extra is not installed.
    """
    if not self._settings.search.auto_index_on_parse:
        return
    if self._search_service is None:
        return
    try:
        self._search_service.upsert_for_tdoc(tdoc_id)
    except SearchUnavailableError:
        logger.debug("search extra not installed; skipping index upsert")
    except Exception as exc:
        logger.warning(
            "failed to update search index for tdoc_id=%s: %s",
            tdoc_id, exc,
        )
```

**Two call sites** (both at the very end of the happy path, after
all four upserts return successfully):

1. End of `extract()` after `cover_repo.upsert(...)`,
   `cr_ttcn_repo.upsert(...)`, `change_details_repo.upsert(...)`,
   and `extract_repo.upsert_extract_meta(...)` — DB-mode happy path.
2. End of `_extract_from_3gpp_url()` after the same four upserts —
   direct-mode 3GPP-URL happy path.

The cache-hit early return (line 1048-1068 of the current code)
returns before any upsert, so the hook does not fire there — which
is correct because no new state was written.

### Cascade on `tdocs` row deletion

Today, the only path that deletes a `tdocs` row is the meeting resync
that drops a TDoc from the upstream list. The FK cascade on
`tdoc_cr_cover_page.tdoc_id` etc. cleans up the detail rows but not
`tdoc_search`. Add `search_service.remove_for_tdoc(tdoc_id)` at the
same point where `tdocs.delete(tdoc_id)` happens. Implementation
phase locates the exact site.

## CLI surface

### New Typer sub-app `search_app`

Registered alongside the existing `meeting_app`, `tdoc_app`,
`wi_app`, `config_app`, `db_app` in `src/doc3gpp/cli.py`. Two
commands.

### `doc3gpp search query QUERY [filters]`

```
doc3gpp search query "scheduling NR" [flags]
```

Flags:

| Flag | Type | Default | Effect |
|---|---|---|---|
| `QUERY` (positional, required) | string | — | FTS5 MATCH expression |
| `--tsg` | str | None | `meetings.tsg` filter |
| `--meeting` | str | None | `meetings.name` filter |
| `--meeting-id` | int | None | `meetings.meeting_id` filter |
| `--tdoc-id` | str | None | exact `tdocs.tdoc_id` filter |
| `--release` | str | None | `tdocs.release` filter |
| `--spec` | str | None | spec-number filter (e.g. `38.300`) |
| `--since` | date (YYYY-MM-DD) | None | `tdocs.uploaded_date >= since` |
| `--until` | date (YYYY-MM-DD) | None | `tdocs.uploaded_date <= until` |
| `--limit` | int | 20 | max results |
| `--format` | choice | `table` | `table` \| `json` \| `markdown` |
| `--compact` | flag | False | strip markdown/json decorators |
| `--rerank` | flag | False | invoke `EmbeddingReranker.rerank` (no-op with `PassthroughReranker`) |
| `--snippet-tokens` | int | from settings (8) | snippet length |
| `--explain` | flag | False | print resolved FTS5 MATCH expression + SQL plan |
| `--quiet` | flag | False | suppress stale-index hint |

Output formats:

- **`table`** (default): columns `tdoc_id`, `meeting`, `tsg`,
  `uploaded`, `score`, `preview`. Preview column is the FTS5
  `snippet()` output with `<<…>>` markers around matches.
- **`json`**: full `SearchHit` records. `--compact` →
  single-line JSON (`separators=(",", ":")`).
- **`markdown`**: human-friendly list of hits with bolded tdoc_id,
  meeting context, and preview blockquote. `--compact` strips
  bold/headings/bullets per existing convention.

### `doc3gpp search index [flags]`

```
doc3gpp search index [--rebuild] [--batch N] [--resume] [--stale-only] [--quiet]
```

With no flags, prints `SearchIndexStatus`:

```
Search index: enabled (sqlite + fts5)
Rows indexed:  4,231
Last rebuild:  2026-07-28 14:32:11 UTC
Last indexed:  tdocs.uploaded_date ≤ 2026-07-28 14:18:00 UTC
Latest tdocs:  tdocs.uploaded_date  2026-07-29 09:01:14 UTC
Status:        STALE — newer tdocs exist; run `doc3gpp search index --rebuild`
```

Flags:

| Flag | Effect |
|---|---|
| `--rebuild` | Drop and rebuild the FTS5 table by walking every `tdocs` row. |
| `--batch N` | Override `Settings.search.rebuild_batch_size` for this run. |
| `--resume` | Continue from the last `tdoc_id` in `tdoc_search_meta` instead of starting at zero. Implies `--rebuild`. |
| `--stale-only` | Only re-index rows whose `tdocs.uploaded_date > last_indexed_uploaded_date`. Skip already-indexed rows. Makes incremental rebuilds cheap. |
| `--quiet` | Suppress per-batch progress logs; print only the final summary. |

### Search query syntax

A `SearchQueryBuilder` helper in `cli_filters.py` (or a new
`cli_search_filters.py`) normalizes the input:

- **Plain text** → wrapped as a quoted FTS5 expression (implicit
  AND between terms).
- **FTS5 operator passthrough** — queries containing `AND`, `OR`,
  `NOT`, `*`, `NEAR`, or `"…"` are passed through unchanged.
- **Special-character escaping** — `(`, `)`, `:`, `"`, `*`, `\` get
  backslash-escaped before being passed to FTS5 to prevent injection.
- **Empty query** → `SearchQueryError("query required")`.
- **Stopwords-only query** (e.g. `"the a"`) →
  `SearchQueryError("query has only stopwords")`.

### Filter validation

Filters go through the same `cli_filters.py` chain as the rest of
the CLI:

- `parse_meeting_filter(meeting)` → `(tsg=None, meeting_name=...)`
- `parse_tdoc_filter(tdoc)` → `tdoc_id=...`
- `parse_date_filter(since)` / `parse_date_filter(until)` —
  validate `YYYY-MM-DD`.
- `parse_release_filter(release)` / `parse_spec_filter(spec)` —
  new, simple string validators.

Invalid input raises `typer.BadParameter` with a clear message
pointing at the help.

### Stale-index hint

After every CLI command that touches tdocs (`search`, `parse`,
`show`, `list`), the CLI checks
`SearchService.status().is_stale` once and, if true, prints to
stderr:

```
search index is stale; run `doc3gpp search index --rebuild` to refresh
```

Suppressed when `--quiet` is passed, when
`Settings.search.enabled = false`, or when the hint has already
been printed in this CLI invocation (tracked via a module-level
flag — no spam in long batch runs).

## Error handling & resilience

### Error hierarchy

```
SearchError
├── SearchUnavailableError           # FTS5 missing, extra not installed, wrong dialect
├── SearchQueryError                 # malformed query
└── SearchIndexCorruptError          # FTS5 virtual table broken (rare)
```

All live in `models/search.py`. `SearchError` is the base class;
CLI catches each subclass with its own exit code (1 / 2 / 3) so
scripts can distinguish infrastructure problems from query
problems from index corruption.

### Three-layer defense

1. **`SearchService` build is best-effort.** `build_search_service`
   catches `SearchUnavailableError` and returns `None`. Net effect:
   doc3gpp without `[search]` behaves exactly like today.
2. **The hook is best-effort.** `_index_after_parse` catches every
   exception and logs a warning. A failing index write never aborts
   a successful parse.
3. **The repo layer raises typed exceptions.** `SQLAlchemySearchIndexRepository`
   catches raw sqlite `OperationalError`s and re-raises as
   `SearchQueryError` (FTS5 syntax errors) or
   `SearchIndexCorruptError` (schema / tokenizer mismatches). No
   raw sqlite exceptions leak to the CLI.

### Rebuild resilience

`SearchService.rebuild()` is a generator:

```python
def rebuild(self, batch_size, resume, stale_only):
    total = self._repo.count_tdocs_to_index(stale_only=stale_only)
    last_id = self._repo.get_resume_cursor() if resume else None
    processed = 0
    for batch in self._repo.iter_tdocs_to_index(
        batch_size, after_id=last_id, stale_only=stale_only,
    ):
        for tdoc_id in batch:
            try:
                self.upsert_for_tdoc(tdoc_id)
            except Exception as exc:
                logger.warning("index upsert failed for tdoc_id=%s: %s", tdoc_id, exc)
                continue
            processed += 1
        self._repo.set_resume_cursor(batch[-1])
        yield RebuildProgress(processed=processed, total=total, current_tdoc_id=batch[-1])
```

Properties:

- **Resumable**: resume cursor updated per-batch in
  `tdoc_search_meta`. A crashed rebuild picks up where it left off
  with `--resume`.
- **Per-row fault tolerance**: one failing row (corrupt gzip blob,
  FK violation) doesn't kill the rebuild.
- **Generator yielding progress**: the CLI's `--quiet` flag controls
  whether the consumer prints each batch.

### Index-text normalization lifecycle

`normalize_query(text)` in `storage/db/fts5_query.py` runs **at
index time only** — every time `SQLAlchemySearchIndexRepository.upsert`
writes an FTS5 column. It is NOT a sqlite-level tokenizer; the
underlying FTS5 virtual table uses stock `unicode61` (no
`tokenize=` directive in the DDL).

Two properties to lock in:

1. **Pure function, no I/O** — `normalize_query` is called once
   per text column per row, with the joined text already in
   memory. No database roundtrip, no network, no logging side
   effects.
2. **Single source of truth for index-time recognition rules** —
   the `TDOC_ID_BASE_RE` and `SPEC_ID_RE` regexes live in
   `storage/db/fts5_query.py` and are used by the repo only.
   `SearchQueryBuilder` (T9) applies the same regexes on the
   user-input side so user queries and indexed text see
   identical normalization.

If `normalize_query` raises for any reason, the repo constructor
catches the exception and raises `SearchUnavailableError` with
the sqlite version info.

### Cross-cutting concerns

| Concern | Strategy |
|---|---|
| `tdocs` row deletion (meeting resync) | Add `search_service.remove_for_tdoc(tdoc_id)` call alongside the existing `tdocs.delete(tdoc_id)` |
| `tdoc_cr_cover_page` row updated (re-parse same tdoc_id) | FTS row auto-re-upserted by the hook |
| `tdoc_extracts` row deleted (rare) | Remove FTS row alongside |
| MySQL/Postgres deployment | `SearchUnavailableError` with dialect name; FTS5 virtual table never created in `create_schema` |
| `[search]` extra toggling mid-session | Auto-index hook checks `_search_service is None` at call time; toggling requires restart, which is fine |
| Concurrent rebuilds | Simple `Lock` prevents two rebuilds running at once |
| Tokenizer pattern changes | Status output can show `tokenizer_version`; CLI hints when a rebuild is needed after upgrade |
| Empty query / stopwords | `SearchQueryError` with a clear message |
| Settings precedence | CLI flag > `[search]` TOML > default (matches AGENTS.md convention) |

## Settings additions

New `[search]` section in `doc3gpp.toml` / `Settings`:

```python
class SearchSettings(BaseModel):
    enabled: bool = True
    auto_index_on_parse: bool = True
    rebuild_batch_size: int = 500
    snippet_tokens: int = 8
```

Defaults match the conservative end; users can disable the
auto-hook if they prefer to manage the index manually.

## Testing strategy

### Test layout

```
tests/
├── unit/
│   ├── test_fts5_query.py                # corpus-driven normalize_query contract
│   ├── test_search_service.py            # service orchestration with mock repo
│   ├── test_search_query_builder.py      # query normalization + escaping
│   └── test_cli_search.py                # CLI flag parsing + error mapping
├── integration/
│   ├── test_search_index_lifecycle.py    # full upsert/search/rebuild on sqlite
│   ├── test_search_filters.py            # every filter flag in cli_filters grammar
│   ├── test_search_extras_disabled.py    # graceful degradation paths
│   └── test_search_after_parse.py        # auto-index hook integration with TDocCrService
└── fixtures/
    └── search_corpus.py                  # fixture corpus of TDocs with cover/ttcn/change blobs
```

### Unit tests

| File | Covers |
|---|---|
| `test_fts5_query.py` | Every row in the `normalize_query` corpus above. Pins the index-time recognition rules. |
| `test_search_service.py` | Service orchestration with mock repo: `upsert_for_tdoc` delegates, `search` runs query then rerank, `rebuild` iterates and yields progress, `status` computes `is_stale` correctly. |
| `test_search_query_builder.py` | Plain text → quoted FTS5 expression; FTS5 operator passthrough; special-character escaping; empty / stopwords-only raise `SearchQueryError`. |
| `test_cli_search.py` | Typer `CliRunner`: flag parsing, filter validation (`typer.BadParameter` for bad dates), error-to-message mapping, `--compact` / `--format` matrix. |

### Integration tests (sqlite-only by default)

| File | Covers |
|---|---|
| `test_search_index_lifecycle.py` | Insert fixture tdocs + cover + ttcn + change + extracts rows; `SearchService.upsert_for_tdoc` → FTS5 row exists → search returns expected hits. `rebuild()` drops and rebuilds; same hits. `remove_for_tdoc(tdoc_id)` removes the row. |
| `test_search_filters.py` | For each filter flag, insert 20+ TDocs with varied metadata, run CLI, assert only matching rows appear. Combined filters. |
| `test_search_extras_disabled.py` | Three scenarios: extra not installed (mocked), non-sqlite backend (mocked), `Settings.search.enabled = false` (TOML-only). |
| `test_search_after_parse.py` | Run `TDocCrService.extract` on a fixture TDoc, then `SearchService.search` returns the expected hit. Run `extract_from_url` with 3GPP URL + FK present, verify hook fires on that path. Run with FK-miss, verify no FTS row and no hook error escaped. |

### Fixture corpus

`tests/fixtures/search_corpus.py` exports a small but diverse set:

- 5 CR TDocs spanning RAN/SA plenary (`RP-...`, `SP-...`, `S2-...`)
  and regular meetings (`R5-...`)
- 2 with TTCN sidecars
- 2 with change-details rows
- 1 with no cover/ttcn/change (metadata only)
- 1 with mixed-case TDoc id
- 1 with spec number reference (`38.300`)
- 1 with hyphenated jargon (`NB-IoT scheduling`)
- 2 with revised TDocs (same tdoc_id, different ftp_url)

Rows use small gzip-compressed JSON blobs so the
`_build_index_text` SQL JOIN runs end-to-end.

### MySQL/Postgres behavior tests

`test_search_extras_disabled.py` covers non-sqlite paths with
mocked engines (no real connection needed). Real-backend tests are
gated behind `-m mysql` and use the existing
`DOC3GPP_TEST_MYSQL_URL` env var.

### Edge cases pinned in tests

1. Search for TDoc base id → all revisions returned.
2. Search for `R5-1234567 r2` (two tokens) → only that revision.
3. Search for `R5-*` (prefix) → all `R5-*` IDs match.
4. Search for `38.300` → all TDocs referencing that spec.
5. `--spec 38.300 AND query "scheduling"` → both must hold.
6. `--limit 0` → returns nothing (picked at impl, pinned in test).
7. `--limit -1` → rejected at CLI as `BadParameter`.
8. Empty DB → empty list, exit 0, "no matches" message.
9. Concurrent rebuilds → second fails fast.
10. Index upsert during a parse that fails halfway → no FTS row
    created (transactional — handled by the same DB commit boundary).
11. Stale-index hint fires once per CLI invocation, not per parsed TDoc.
12. `search index --rebuild --resume` after a simulated crash →
    picks up at the cursor.
13. Tokenizer corpus (above) — the contract.

### Coverage targets

Per the existing suite convention, ≥ 90% line coverage for new
modules. The tokenizer corpus test alone locks down the highest-
risk component.

## File / symbol summary

| File | Symbols |
|---|---|
| `models/search.py` | `SearchHit`, `SearchQuery`, `SearchFilters`, `SearchIndexStatus`, `RebuildProgress`, `SearchError`, `SearchUnavailableError`, `SearchQueryError`, `SearchIndexCorruptError` |
| `storage/db/fts5_query.py` (new) | `normalize_query`, `TDOC_ID_BASE_RE`, `SPEC_ID_RE` |
| `storage/repositories/search_sql.py` | `SQLAlchemySearchIndexRepository`, `register_tdocid_tokenizer` (event listener), `_build_index_text`, `_decompress_blob` |
| `repository/protocols.py` (extend) | `SearchIndexRepository` Protocol, `EmbeddingReranker` Protocol |
| `services/search_service.py` | `SearchService` (`upsert_for_tdoc`, `remove_for_tdoc`, `search`, `rebuild`, `status`); `PassthroughReranker` |
| `services/factory.py` (extend) | `build_search_service` |
| `services/tdoc_cr_service.py` (modify) | `TDocCrService.__init__` gains `search_service`; `_index_after_parse` private helper; two new call sites |
| `cli.py` (extend) | `search_app` Typer sub-app, `search` and `search index` commands, `_emit_search_status` helper |
| `cli_filters.py` (extend) | `parse_date_filter`, `parse_release_filter`, `parse_spec_filter`, `SearchQueryBuilder` |
| `settings/schema.py` (extend) | `SearchSettings` |
| `pyproject.toml` (modify) | New `[search]` optional extra; new `doc3gpp[search]` install target |

## Open implementation notes

The following are deliberately left for the implementation phase
because they require hands-on testing rather than design decisions:

1. ~~**Exact `fts5_tokenizer()` registration syntax.**~~ **Resolved
   by architecture change** — we use stock `unicode61` (no
   `tokenize=` directive) + Python-side `normalize_query` at
   index time. See §"Tokenizer design" for the new contract.
2. **Snippet column choice.** The first text column used by
   `snippet()` is `title` by default but configurable per
   `Settings.search.snippet_tokens`. Implementation phase tests
   which column gives the most useful previews.
3. **`GROUP_CONCAT` for the `wis` column.** SQLite's default
   separator is `,`; we want ` `. Implementation phase wires the
   exact separator string.
4. **Migration story.** `create_schema.py` is the bootstrap; if a
   user has an existing sqlite DB without `tdoc_search`, the next
   `doc3gpp db init` (or first search command) creates it. No
   alembic wiring needed (matches existing convention).

## TL;DR

- FTS5 virtual table `tdoc_search` keyed on `tdoc_id`; sidecar
  `tdoc_search_meta` for rebuild resume + staleness tracking.
- Built-in `unicode61` tokenizer (stock sqlite). Python-side
  `normalize_query` (in `storage/db/fts5_query.py`) handles
  the two recognition rules FTS5 cannot do on its own —
  TDoc ID base+rev split (`R5-1234567r2` → indexed as both
  `R5-1234567` and `R5-1234567r2`) and spec id preservation
  (`38.300` → indexed as `38_300` so it stays one token).
  Everything else splits naturally: `NB-IoT` → `nb`, `iot`;
  `R5-1234567` → `r5`, `1234567`.
- `SearchService` orchestrates indexing + search; `PassthroughReranker`
  is the v1 default with a forward hook for embeddings.
- Auto-index hook fires from two call sites in `TDocCrService`
  (DB-mode `extract` happy path + direct-mode 3GPP-URL happy path);
  best-effort, never raises, gated behind `Settings.search.auto_index_on_parse`.
- CLI: `doc3gpp search query QUERY [filters]` + `doc3gpp search index
  [--rebuild] [--resume] [--stale-only]`. Reuses existing
  `--format` / `--compact` semantics.
- Three-layer graceful degradation: build is best-effort, hook is
  best-effort, repo raises typed errors. Search becomes unavailable
  with a one-liner on MySQL/Postgres, missing FTS5, or extra not
  installed — never crashes the rest of doc3gpp.
- `normalize_query` corpus test (`test_fts5_query.py`) is the
  contract; every index-time recognition decision flows through it.