# Spec Document Parse, Chunk, and Search — Design

**Date:** 2026-09-23
**Branch:** `feature/spec-doc-parse`
**Status:** approved, pending implementation plan

## Goal

For a spec already in the main DB (`specs` / `spec_versions`), download the
version zip for a specific release/version (default: numerically-latest
version), convert all `.docx` files inside to markdown preserving tables
and heading/TOC structure, chunk to DB rows with section/table metadata in
a **new separate sqlite file**, and expose TOC query + FTS5 search
(+ optional semantic) over that corpus via CLI, web, and MCP.

## Non-goals

- No change to `spec sync/list/show` semantics, `Spec`/`SpecVersion`
  models, or the `specs`/`spec_versions` tables in the main DB.
- No parsing of legacy `.doc` binary files (warn-skip, same as TDoc).
- No support-file conversion (PDFs, XLSs, images inside the spec zip
  are ignored).
- No re-download/re-parse of an already-parsed `(spec_id, version)` —
  spec zips are **immutable**. Parsed rows are skipped unconditionally
  (no interval, no recency window). `--force` is the only override and
  it re-downloads the zip and replaces TOC + chunks.
- No alembic involvement (bootstrap stays `create_all`-based).
- No cross-database joins or FKs between the main DB and the specdata DB.

## Decisions (user rulings)

1. **Version selection:** numeric version-tuple sort, newest first — the
   same ordering `SQLAlchemySpecRepository.list_versions` uses
   (`18.10.1 > 18.2.1`). `upload_date` and `release` do not participate.
2. **Storage:** separate file mirroring the testcase pattern: sibling
   `<main-stem>_specdata.db`, own declarative base + engine + session
   factory, `create_schema(scope="specdata")`, `db reset --scope specdata`.
3. **TOC granularity:** one TOC record per `(spec_id, version)`, not per
   spec. Each downloaded version gets its own TOC snapshot + file-order map.
4. **CLI surface:** new `spec doc` sub-app
   (`fetch | parse | toc show | search query | search sem`).
5. **Stack:** full stack now — CLI + web routes + MCP tools, byte-consistent
   JSON like other resources (not CLI-only first).
6. **Chunking:** reuse `semantic_search.chunk_size` (default 512 whitespace
   tokens) + `chunk_overlap` (default 24) as the primary budget; add
   `[spec_doc] max_chunk_chars` (default 1500, configurable) as a hard
   ceiling. Tables split row-wise only (rows are atomic); oversized tables
   split across chunks with `table_no`/`table_title` repeated.
7. **`parse` is combined:** `fetch` downloads the zip only; `parse` does
   fetch-if-missing + convert + chunk + index. There is no strictly-separate
   mode where parse fails on a missing zip.

## Architecture

### New modules (all under existing layers)

| Piece | Location | Notes |
| --- | --- | --- |
| Version resolution + download | `src/doc3gpp/scraping/spec_doc_source.py` | `resolve_spec_doc_version(spec_id, release?, version?)` reads main-DB `spec_versions`; `fetch_spec_doc_zip(ftp_url, client?)` returns bytes via shared `ScraperClient.get_bytes`. Network only, no parsing. |
| Multi-docx extract + order | `src/doc3gpp/parsers/spec_doc.py` | `list_spec_docx(zip_bytes)` (all `.docx`, skip `__MACOSX/`); `order_spec_files(files)` by first-heading section number, TOC file breaks ties; `extract_spec_toc(blocks)` builds the heading tree. |
| Structured converter | extend `src/doc3gpp/parsers/docx_converter.py` | Add block-emitting path alongside the existing string path: `convert_document_to_blocks(doc_bytes, filename) -> list[Block]` where `Block = Heading(level, section_no, title) \| Paragraph(text) \| Table(gfm, table_no, table_title)`. GFM table rendering and heading-style logic unchanged. `Table N: title` captions detected from adjacent paragraphs. |
| Chunker | `src/doc3gpp/parsers/spec_doc_chunker.py` | Pure function `chunk_blocks(blocks, chunk_size, chunk_overlap, max_chunk_chars) -> list[SpecDocChunk]`. Break order paragraph → sentence → table-row. |
| Domain models | `src/doc3gpp/models/spec_doc.py` | `@dataclass(slots=True)`: `SpecDocSource`, `SpecDocToc`, `SpecDocChunk` (fields in §Storage). Never leak ORM attrs. |
| Repository protocol | `src/doc3gpp/repository/protocols.py` | `SpecDocRepository` Protocol: `get_source / record_download / record_parsed / get_toc / upsert_toc / replace_chunks / list_chunks` (chunk reads with metadata filters). Search lives on sibling `SpecDocSearchRepository` (FTS5) + vector repo mirroring the `search_sql` / `vector_sql` split. Update Protocol + impls together. |
| SQL impls | `src/doc3gpp/storage/repositories/spec_doc_sql.py` + `spec_doc_search_sql.py` | Chunk/TOC/source CRUD backed by the specdata session factory; FTS5 repo mirrors `search_sql.py` (explicit upserts, meta cursor); vector repo mirrors `vector_sql.py`. |
| Service | `src/doc3gpp/services/spec_doc_service.py` | `SpecDocService`: `fetch / parse / get_toc / search` + `parse_many` with skip/failure buckets. Injected with main-DB `SpecRepository` (version lookup) + specdata repo + cache + scraper. |
| Settings | `src/doc3gpp/settings/schema.py` | New `SpecDocSettings` (`[spec_doc]` block); `Settings.specdata_database_url`. |

### Data flow

```
spec doc fetch --spec <id> [--release R] [--version V]
  → SpecDocService.fetch
    → main-DB SpecRepository.list_versions(spec_id) → numeric-newest
      (or exact (release, version) match; miss → typer.BadParameter)
    → ledger probe: spec_doc_sources(parsed_at set, no --force) → skipped
    → ScraperClient.get_bytes(spec_versions.ftp_url)
    → cache {cache.dir}/specs/zips/<spec_id>/<version>.zip
    → record_downloaded

spec doc parse --spec <id> [--release R] [--version V] [--force]
  → fetch-if-missing (same skip rule; --force re-downloads + replaces)
  → list_spec_docx → per-file convert_document_to_blocks
  → order_spec_files → extract_spec_toc → upsert_toc
  → chunk_blocks → replace_chunks → record_parsed
  → best-effort auto-index (FTS5) + auto-embed (vector), same
    _index_after_parse / _embed_after_parse pattern as TDocCrService
```

### Multi-file ordering rule

Most specs ship one `.docx`; large specs split by sections across several.
Order is derived from content, not zip order:

1. Convert each `.docx` to blocks; read each file's first heading with a
   numeric `section_no` prefix (e.g. `4.2.1 Scope`).
2. Sort files by that section-number tuple ascending.
3. Tie-break: the file containing TOC-like headings (`Contents`,
   `Table of contents`) sorts first.
4. Files with no numeric first heading sort after numbered files, in
   lexicographic filename order (stable, logged at DEBUG).
5. The `(spec_id, version)` TOC stores the ordered `source_file` list
   alongside the heading tree so ordering is reproducible without re-parse.

### Chunk rule

- Primary budget: `semantic_search.chunk_size` whitespace tokens
  (default 512) with `chunk_overlap` trailing-token overlap (default 24).
  Reuses the existing knobs — no new token settings.
- Hard ceiling: `[spec_doc] max_chunk_chars` (default 1500). A chunk is
  closed at whichever budget hits first.
- Break priority inside a chunk: paragraph boundary → sentence boundary
  (`. ! ?` + whitespace) → table-row boundary. Never split mid-row.
- Tables: a table that fits stays in one chunk (chars may overflow the
  token budget — allowed, like TDoc's whole-table preference). An
  oversized table splits row-wise; every emitted chunk repeats
  `table_no` / `table_title`.
- Each chunk row: `spec_id, version, release, file_order, source_file,
  chunk_index, section_no, section_title, table_no, table_title, text`.
  `chunk_id` PK is `{spec_id}@{version}#{chunk_index}`.

## Storage (separate file)

- New `SpecDataBase(DeclarativeBase)` in
  `src/doc3gpp/storage/db/specdata_base.py`.
- `resolve_specdata_database_url(settings?)` in
  `src/doc3gpp/storage/db/session.py`: explicit `specdata_database_url`
  wins; else sibling of `database_url` with `_specdata` stem suffix
  (`doc3gpp.db` → `doc3gpp_specdata.db`); `:memory:` main → private
  `:memory:`; non-sqlite main raises `ValueError` telling the operator to
  set `specdata_database_url` explicitly (same shape as the testcase helper).
- `get_specdata_engine()` / `get_specdata_session_factory()` (same
  `lru_cache` + sqlite backend-kwargs shape).
- `create_schema(scope)` gains `"specdata"`; `"all"` (default) does
  main + testcase + specdata. Call sites updated to `create_schema("all")`.
- Tables (all in specdata DB):

  | Table | PK | Columns |
  | --- | --- | --- |
  | `spec_doc_sources` | `(spec_id, version)` | `release, ftp_url, downloaded_at, parsed_at, chunk_count, docx_count` |
  | `spec_doc_tocs` | `(spec_id, version)` | `release, toc_json_gzip (BLOB), file_order_json (TEXT), docx_count, created_at` |
  | `spec_doc_chunks` | `chunk_id` | `spec_id (index), version (index), release, file_order, source_file, chunk_index, section_no, section_title, table_no, table_title, text` |
  | `spec_doc_search` (FTS5) | — | `chunk_id UNINDEXED, text, section_title, table_title, spec_id, version, release` + `spec_doc_search_meta(key, value)` cursor sidecar |
  | `vec_spec_doc_embeddings` (vec0) | `chunk_id` | `spec_id, chunk_index, embedding FLOAT[dim] cosine` + `vec_spec_doc_meta(key, value)` |

- `TOC` JSON shape: ordered list of
  `{level, section_no, title, source_file, file_order}` plus the ordered
  `files: [{source_file, file_order, first_section}]` map. Compressed with
  the shared `storage/compression.py` gzip helpers.
- On-disk markdown (not in DB): `{cache.dir}/specs/markdown/<spec_id>/<version>/<file_order>-<stem>.md`
  (renders kept for debugging + `--format raw`-style inspection; DB holds
  TOC + chunks only).
- No FK from specdata tables into main-DB `specs`/`spec_versions`
  (cross-file FKs are unenforceable); `spec_id`/`version` are plain
  indexed columns.

## Search

- FTS5 `spec_doc_search` lives in the specdata DB (same DDL + trigger-less
  explicit-upsert pattern as `tdoc_search`): `SearchService`-shaped
  `SpecDocSearchService.search(query, filters)` → `MATCH` + structured
  WHERE on `spec_id / version / release / section` (rich-filter grammar via
  `apply_text_filter` / exact match for `version`/`release`) + `bm25()`
  with `[spec_doc] bm25_weights` (6 columns: text, section_title,
  table_title, spec_id, version, release).
- Snippets: one `snippet()` per weight>0 column, surfaced in `previews`
  only on match (same rule as TDoc search).
- Optional semantic: `vec_spec_doc_embeddings` in the same file, shared
  `OpenAICompatibleEmbedder` from `WebState.build_state`; hybrid RRF path
  mirrors `SemanticSearchService` (`--fts5-query` + `--fts5-weight`,
  `k=60`, fanout via `fanout_multiplier`). Disabled when
  `embedding_base_url` unset.
- Stale-index hint + `--stale-only` / `--resume` / `--rebuild-all` flags
  mirror `search index`.
- `toc show` never touches FTS5 — direct `spec_doc_tocs` PK lookup.

## CLI (`spec doc` sub-app)

```
doc3gpp spec doc fetch --spec <id> [--release R] [--version V] [--force]
  # download only; immutable skip unless --force (re-download + replace rows)

doc3gpp spec doc parse --spec <id> [--release R] [--version V] [--force]
  # fetch-if-missing + convert + chunk + index; batch form accepts
  # multiple --spec ids with success/skip/failure summary buckets

doc3gpp spec doc toc show --spec <id> [--version V] [--release R]
  # whole TOC for the resolved (spec_id, version); miss → BadParameter
  # pointing at `spec doc parse`. Renders tree (table/markdown) or JSON.

doc3gpp spec doc search query "TEXT" [--spec ...] [--release ...]
    [--version ...] [--section ...] [--limit/--offset]
  # FTS5 cross-spec search with metadata filters

doc3gpp spec doc search sem "TEXT" [--fts5-query ...] [--spec ...]
    [--release ...] [--fts5-weight ...]
  # hybrid vector + FTS5 RRF; pure vector when --fts5-query omitted
```

- All five commands honour `--format table|json|markdown|raw` and
  `--compact` via the shared `_resolve_compact` path.
- Default output fields (TOML `[output] fields.spec_doc`, overridable
  with `--fields`): chunks →
  `spec_id, version, release, section_no, section_title, table_no,
  table_title, chunk_index, text`; TOC →
  `section_no, title, level, source_file`.
- `db init | reset | check` gain `--scope specdata` (same shape as the
  testcase scope work); `reset` requires sqlite URLs for all selected
  scopes.

## Web / MCP / jobs

- `WebState` gains `specdata_engine: Engine` + `spec_doc` service;
  `build_state` wires both; lifespan disposes all three engines.
- Routes: `GET /specs/{spec_id}/docs/toc?version=&release=&format=` (full
  page vs `partials/spec_doc_toc_results.html` on `HX-Request`, same
  outerHTML-`#results` contract); `GET /spec-docs/search?...` (5-column
  filter form mirroring `/search`, HTMX fragment + JSON envelope
  byte-identical to CLI JSON).
- MCP tools: `get_spec_toc(spec_id, version?, release?)`,
  `search_spec_docs(query, spec_id?, release?, version?, section?, limit?)`
  — JSON byte-matches the `?format=json` routes (`_to_json` compact
  separators + `ensure_ascii=False`).
- Job kind `PARSE_SPEC_DOCS = "parse_spec_docs"`: route/MCP enqueue via
  `JobWorkerHandle.enqueue`; handler calls `SpecDocService.parse_many`
  with progress callbacks; SSE progress via `append_log`.
- Schema surface: `doc3gpp spec doc schema` + `GET /spec-docs/schema` +
  `get_spec_doc_schema` MCP tool from the static `models/schema_info.py`
  registry (3 tables + FTS5 columns documented, no DB reads).

## Settings (`[spec_doc]` block + top-level URL)

```toml
specdata_database_url = null   # null → sibling <main-stem>_specdata.db

[spec_doc]
max_zip_size_kb = 0            # 0 = unlimited (spec zips routinely > 1MB; no TDoc-style 1000KB default)
max_chunk_chars = 1500         # hard ceiling alongside semantic_search.chunk_size tokens
chunk_overlap = null           # null → reuse semantic_search.chunk_overlap
auto_index_on_parse = true
auto_embed_on_parse = true
bm25_weights = [5.0, 5.0, 5.0, 1.0, 1.0, 1.0]  # text, section_title, table_title, spec_id, version, release
```

- `Settings.specdata_database_url: str | None = None`; env
  `DOC3GPP_SPECDATA_DATABASE_URL` added to `ALLOWED_ENV_VARS`.
- `chunk_size`/`chunk_overlap` are **not** duplicated: the chunker reads
  `settings.semantic_search.chunk_size` / `.chunk_overlap` unless
  `[spec_doc] chunk_overlap` is set (explicit override wins).
- `max_zip_size_kb = 0` default: spec zips are legitimately large
  (multi-MB); the TDoc 1000KB default would false-skip them.

## Error handling

- Unknown `--spec` (no `spec_versions` rows in main DB, even after
  `spec sync --spec-id` bootstrap attempt): `typer.BadParameter`
  naming the id and pointing at `doc3gpp spec sync --spec-id <id>`.
- Unknown `--version`/`--release` for a known spec: `typer.BadParameter`
  listing the closest available versions (up to 5, numeric-newest first).
- Oversized zip (`max_zip_size_kb > 0` and exceeded): skip bucket
  (operational decision, mirrors `TDocTooLargeError`), not failure.
- Zip with no `.docx` entries: failure bucket naming the zip.
- `.doc` legacy entries: warn-skip per file; if *only* `.doc` files exist
  the zip fails with a message naming the files.
- Parse of one spec in a batch never aborts the rest (log + bucket).

## Testing

- Unit (offline): version resolution (exact / release-only / default-newest /
  miss); file ordering (numbered / TOC-tiebreak / unnumbered-stable);
  caption detection (`Table N: title` variants); chunker row-atomicity
  (fitting table whole, oversized splits with repeated metadata, char
  ceiling wins over token budget, sentence fallback); filter grammar.
- Integration (sqlite, in `scripts/test_sqlite.sh`): ledger identity-skip
  (parse twice → second skipped, no re-download); `--force` replaces rows;
  TOC round-trip (gzip JSON + file order); chunk metadata persistence;
  FTS5 search with each filter; `db reset --scope specdata` independence
  (main + testcase rows survive); `create_schema("specdata")` creates only
  specdata tables on the specdata engine.
- Online (opt-in `-m online`): one small spec end-to-end
  (fetch → parse → toc → search) against live 3gpp.org FTP.
- Full gate: `./scripts/test_sqlite.sh` + `ruff check .`.

## Docs to update in the implementation

- `AGENTS.md` (spec-doc workflow one-liners + storage row).
- `docs/cli.md` (all five commands, every flag/default/example).
- `docs/architecture.md` (layered diagram: new modules; storage: third
  base/engine; runtime data flow: spec-doc pipeline).
- `docs/code-map.md` (new symbols).
- `docs/conventions.md` if new filter grammar or skip-rule shape appears.
- `docs/web-server.md` (routes + MCP tools + job kind).
- `docs/3gpp-knowledge.md` (spec-zip layout: multi-docx section splits,
  TOC conventions) if anything general is learned.
- `doc3gpp.toml.example` (`specdata_database_url` + `[spec_doc]` block).
- `README.md` if CLI surface list changes.
