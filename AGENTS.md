# doc3gpp Agent Guide

**Generated:** 2026-07-10
**Branch:** main

A Python CLI/library that scrapes 3GPP meeting calendars, TDoc lists, auxiliary TDoc files, CR cover pages, and WIs into SQL.

This guide stays lean on purpose: it covers the **shape** of the
codebase — layout, where to look for a change, architecture rules, and
common commands. Everything that drifts easily (symbol tables, settings
caching details, filter grammar, anti-patterns, known constraints) lives
in [`docs/`](docs/) and is linked from the "Doc pointers" section below.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
doc3gpp db init
doc3gpp db check
```

Build system: **hatchling**. Stack: Python 3.10+, SQLAlchemy 2.0,
Pydantic v2 + pydantic-settings, httpx, BeautifulSoup4 + lxml, openpyxl,
alembic (installed but not wired). Optional extras:

- `.[cli]` — Typer CLI (also in `[dev]`).
- `.[extract]` — `python-docx` for the TDoc extraction pipeline.
- `.[all]` — every runtime extra (CLI, extract, search, semantic).

`pip install doc3gpp` installs the SDK only; `pip install "doc3gpp[cli]"`
or `pipx install "doc3gpp[cli]"` adds the `doc3gpp` CLI command.

## Structure (high level)

```
doc3gpp/
├── src/doc3gpp/          # package root
│   ├── cli.py            # Typer commands (9 sub-apps, 26 commands) + cli_server.py (server sub-app, 6 commands)
│   ├── models/           # domain dataclasses — never leak ORM attrs out
│   ├── repository/       # abstract repo contracts (Protocols)
│   ├── services/         # orchestration; CLI-injected via factory
│   ├── scraping/         # HTTP / FTP transport — no parsing
│   ├── parsers/          # HTML / Excel → domain objects — no network
│   ├── settings/         # pydantic-settings + TOML discovery
│   ├── storage/          # persistence umbrella
│   │   ├── compression.py    # shared gzip JSON helpers (cover + TTCN sidecars)
│   │   ├── db/               # ORM models, engine, create_schema bootstrap
│   │   ├── backends/         # per-dialect engine kwargs
│   │   └── repositories/     # SQL impls of the repository Protocols
│   │       └── tdoc_cr_ttcn_sql.py   # SQL impl of TDocCrTTCNDetailRepository
│   └── cli_filters.py    # shared filter / TDoc-id grammar
├── tests/
│   ├── unit/             # mock external calls
│   ├── integration/      # sqlite by default; online opt-in
│   └── fixtures/         # sample HTML + XLSX + zip docs
├── docs/                 # architecture, CLI ref, conventions, code map, constraints
└── scripts/              # test_sqlite.sh, dev_run.sh
```

For the full symbol-to-file table, see
[`docs/code-map.md`](docs/code-map.md).

## Where to look

| Task | Location | Notes |
| --- | --- | --- |
| Add a CLI command | `src/doc3gpp/cli.py` | Follow pattern: service → repo → CLI. |
| Add a config writer / CLI set command | `src/doc3gpp/settings/config_writer.py` + `src/doc3gpp/cli.py` (`config_app`) | TOML read-modify-write helpers; Typer `config set` command. |
| Add a data source | `src/doc3gpp/scraping/` + `src/doc3gpp/parsers/` | Network in `scraping/`, parsing in `parsers/`. |
| Add a body-change extraction | `src/doc3gpp/parsers/cr/body_changes.py` + `src/doc3gpp/storage/repositories/tdoc_cr_change_details_sql.py` | Pure function in parsers, sidecar repo in storage. |
| Add a domain model | `src/doc3gpp/models/` | `@dataclass(slots=True)`; never expose ORM attrs. |
| Add a storage backend | `src/doc3gpp/storage/backends/` | Engine kwargs per dialect. |
| Add a spec list / detail source | `src/doc3gpp/scraping/spec_source.py` + `src/doc3gpp/parsers/spec_parser.py` + `src/doc3gpp/services/spec_service.py` + `src/doc3gpp/storage/repositories/spec_sql.py` | List page → `parse_spec_list` → per-spec detail → `parse_spec_detail`. `SpecService.sync` fans out across detail pages in a thread pool, runs ETSI PDF + CR-list follow-ups inside each worker, and honours the per-spec `specs.last_synced_at` skip rule (one row per spec, no TSG-level gate) — each per-worker `_sync_one_spec` short-circuits specs whose `last_synced_at` is within `Settings.sync.spec_sync_interval` and stamps the spec's own `last_synced_at` on a successful re-sync. `SpecService.sync_spec` syncs a single spec (no list page); on a local `specs`-table miss it bootstraps the header from `https://www.3gpp.org/DynaReport/{no_dot}.htm` via `fetch_dynareport_detail` + `parse_dynareport_header`, normalises the responsible group to a seeded `tsgs.short_name`, and hands the in-memory `Spec` to the same `_sync_one_spec` pipeline as the stored-row path. `list_distinct_tsgs` drives the no-selector fallback. |
| Change filters for a list | `src/doc3gpp/repository/protocols.py` + `src/doc3gpp/storage/repositories/` | Update **both** the Protocol and the impl. |
| Run all tests | `./scripts/test_sqlite.sh` | Unit + integration, sqlite-only. Uses `-n auto` when xdist is installed. |
| Run online tests | `python -m pytest -m online -rs` | Hits live 3gpp.org + FTP. |
| Add a search command / hook | `src/doc3gpp/cli.py` (`search_app`) + `src/doc3gpp/services/search_service.py` + `src/doc3gpp/storage/repositories/search_sql.py` | FTS5 over sqlite + index-time normalize_query; rebuild resume via `tdoc_search_meta` |
| Add a search rerank flag / knob | `src/doc3gpp/services/semantic_reranker.py` + `src/doc3gpp/services/search_service.py` (`PassthroughReranker`) + `src/doc3gpp/settings/schema.py` (`SearchSettings.search_fanout_factor`) + `src/doc3gpp/cli.py` (`search_command`) | The `EmbeddingReranker` Protocol lives in `src/doc3gpp/repository/protocols.py`. Vector lookup helper: `VectorIndexRepository.get_min_distance_for_tdocs`. |
| Tune the FTS5 search subsystem | `src/doc3gpp/settings/schema.py` (`SearchSettings`) | FTS5 search knobs (`enabled`, `auto_index_on_parse`, `rebuild_batch_size`, `snippet_tokens`, `bm25_weights`, `search_fanout_factor`); TOML `[search]` block. Per-column previews are driven by `bm25_weights` (weight>0 → snippet bound; match in snippet → surfaced; weight=0 → both skipped). |
| Add a semantic search knob | `src/doc3gpp/settings/schema.py` (`SemanticSearchSettings`) | TOML `[semantic_search]` block. |
| Add a `search sem` flag | `src/doc3gpp/cli.py` (`sem_command`) | Mirror `search_command` pattern. |
| Add an embedding model | `src/doc3gpp/services/embedding/embedder.py` | Lazy model load; `Embedder` Protocol in `repository/protocols.py`. |
| Add a vector DDL change | `src/doc3gpp/storage/db/migrate.py` (`_create_vector_schema`) + `src/doc3gpp/storage/repositories/vector_sql.py` | Gated on sqlite + sqlite-vec. |
| Add a web route / HTML page | `src/doc3gpp/web/routes/` + `src/doc3gpp/web/render.py` + templates in `src/doc3gpp/web/templates/` + `src/doc3gpp/web/filters.py` (`is_htmx_request`) | Routes are thin adapters over services via `web/deps.py` `Depends` helpers; keep HTML/JSON/CLI output byte-consistent. List routes that pair with an HTMX filter form (e.g. meetings / tdocs / wis / search) must render a `partials/<resource>_results.html` fragment when the request sets `HX-Request: true` and the full page otherwise — the `outerHTML` swap target `#results` only fits a fragment, not a full HTML document. The tdoc detail page's Parse card enqueues `POST /jobs/parse/tdocs` (single-tdoc filter) and polls `partials/job_status.html` via `static/js/tdoc_parse.js`; the search pages share a 5-column grid form with a `sem` rerank input on `/search` and full filter parity on `/search/sem`. The web app builds ONE shared `SentenceTransformerEmbedder` in `build_state` and injects it into `build_tdoc_cr_service` / `build_search_service` / `build_semantic_search_service` so the model loads once per process.
- The tdoc detail page (`tdoc_show.html`) renders two extra cards when the parent TDoc has been parsed: 'Required changes' (one entry per TTCN `required_changes` dict) for TTCN CRs, and 'Extracted changes' (one entry per body-derived change block) for non-TTCN CRs. Both cards are gated on the sidecar's presence and are mutually exclusive. |
| Add a sync hub panel / sync hub page | `src/doc3gpp/web/routes/sync.py` + `src/doc3gpp/web/templates/sync.html` + `src/doc3gpp/web/static/js/sync_hub.js` | Each new enqueue panel follows the existing nine-form pattern (one `<form id="*-form">` per MCP tool) and reuses `bindJobPolling` + `JobRepository.create`. New routes that need HTTP exposure land in `web/routes/jobs.py` next to their existing siblings (e.g. `POST /jobs/parse/tdoc-url` closed the MCP-vs-HTTP gap for the MCP `parse_tdoc_url` tool). |
| Add an MCP tool | `src/doc3gpp/web/mcp_server.py` | Register via `@server.tool`; the tool result must byte-match the equivalent HTTP `?format=json` route (`_to_json` uses compact separators + `ensure_ascii=False`). The MCP mount supports both `streamable_http` (default) and `sse` transports, selected via `[mcp] transport` in `doc3gpp.toml`; browser origins are allowed via `[mcp] allowed_origins` (defaults to `http://127.0.0.1` and `http://localhost`). |
| Add a background job kind | `src/doc3gpp/models/jobs.py` (`JobKind`, e.g. `JobKind.PARSE_TDOC_URL`) + `src/doc3gpp/web/workers/handlers.py` (`KIND_TO_HANDLER`, e.g. `_parse_tdoc_url`) + `src/doc3gpp/web/routes/jobs.py` | Enqueue from route/MCP via `JobWorkerHandle.enqueue`; SSE progress via `append_log`/progress callbacks. |
| Add a spec sync job / MCP tool | `src/doc3gpp/models/jobs.py` (`JobKind.SYNC_SPECS`) + `src/doc3gpp/web/workers/handlers.py` (`_sync_specs`) + `src/doc3gpp/web/routes/jobs.py` (`POST /jobs/sync/specs`) + `src/doc3gpp/web/mcp_server.py` (`sync_specs`) | Enqueue from route/MCP via `JobWorkerHandle.enqueue`; the handler validates the `tsg` against the `tsgs` reference table (rejecting unknown/legacy TSGs before any network work) then calls `services.spec.sync(tsg, force=force, on_progress=...)`. The `_sync_meetings` handler applies the same `tsgs`-table validation before `services.meeting.sync`. |
| Change job repo / worker | `src/doc3gpp/storage/repositories/jobs_sql.py` + `src/doc3gpp/web/workers/job_worker.py` + `src/doc3gpp/web/state.py` (`JobWorkerHandle`) | `get_job_repo` / `get_job_worker` deps in `web/deps.py`; tests override them. |
| Add a `server` CLI command | `src/doc3gpp/cli_server.py` | Each subcommand starts with `_require_server_enabled(settings)`; OS install delegates to `web/install.py`. |
| Change server/OS-settings or install helpers | `src/doc3gpp/web/install.py` + `src/doc3gpp/settings/schema.py` (`ServerSettings`/`MCPSettings`) | `[server]`/`[mcp]` TOML blocks; server settings are TOML-only (not in the env allowlist). |

For deeper conventions (filter grammar, settings caching, anti-patterns,
commit policy), see [`docs/conventions.md`](docs/conventions.md).

## Architecture boundaries

Strict layered separation in `src/doc3gpp/`. Each layer depends only on
the layer below it; services reach down into storage through the
`repository/` Protocols rather than touching the concrete ORM. See
[`docs/architecture.md`](docs/architecture.md) for the layered diagram,
runtime data flow, and ORM schema.

| Layer | Rule |
| --- | --- |
| `models/` | pass between layers; **never leak ORM attributes** |
| `repository/` (abstract) | Protocol contracts only |
| `services/` | orchestration; injected with a repo impl via `services/factory.build_*` |
| `scraping/` | HTTP / FTP transport only — **no HTML parsing** |
| `parsers/` | HTML / Excel → domain only — **no network** |
| `storage/db/` | ORM models, engine factory, `create_schema` bootstrap |
| `storage/compression.py` | shared gzip JSON helpers for binary detail columns |
| `storage/repositories/` | SQL impls of the `repository/` Protocols |
| `settings/` | env + TOML config (pydantic-settings; precedence: CLI > env > file > defaults) |
| `cli.py` | thin Typer commands; never instantiate SQL repos directly |
| `web/` | thin FastAPI adapters over services via `web/deps.py`; HTML/JSON/MCP output byte-consistent; MCP via `web/mcp_server.py`; background jobs via `web/workers/`. `cancel_job` is idempotent on terminal jobs (returns the envelope instead of erroring). The MCP `parse_tdoc_url` tool enqueues a `PARSE_TDOC_URL` job; see `src/doc3gpp/web/workers/handlers.py::_parse_tdoc_url` and `src/doc3gpp/web/mcp_server.py:parse_tdoc_url`. |

Workflows in one line (full prose in `docs/architecture.md`):

- `doc3gpp meeting sync --tsg <s>` → `MeetingService.sync` → DynaReport
  HTML → `parse_3gpp_calendar` → stamp `Meeting.tsg` →
  `SQLAlchemyMeetingRepository.upsert_many`. Skips when the TSG was synced
  within `Settings.sync.meeting_sync_interval` unless `--force` is used.
- `doc3gpp tdoc sync --meeting-id <id>` / `--meeting <name>` →
  `TDocSyncCoordinator.sync_for_meeting_id` /
  `sync_for_meeting_name` → `TDocService.sync_tdoc_list` +
  `TDocFileService.sync_from_meeting_ftp`. The TDoc list comes from
  `Settings.sync.tdoc_list_url_template` (default
  `https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}`);
  the auxiliary TDoc file scan still uses the stored meeting row's
  FTP URL. **No meeting row → no TDoc sync.** When the meeting has no
  stored `ftp_url` the TDoc list sync still runs and the auxiliary
  file scan is skipped (the outcome reason notes the skip); the
  sync never errors on a missing FTP URL. Skips when the meeting
  has been synced before **and** is outside
  `Settings.sync.tdoc_list_closed_window` or was synced within
  `Settings.sync.tdoc_list_sync_interval`; the closed-window check is
  skipped on a never-synced meeting so a first-time sync can populate
  the tdocs table for an old meeting. `--force` bypasses both checks.
- `doc3gpp tdoc sync` (no selector) →
  `TDocSyncCoordinator.sync_all_tracked_meetings` → every distinct
  ``meeting_id`` in the ``tdocs`` table is synced individually with the
  same two skip rules applied per meeting.
- `doc3gpp wi sync --tsg <s>` → `WiService.sync` → `fetch_wis` →
  `parse_3gpp_wis` → `SQLAlchemyWiRepository.upsert_many` (composite
  PK `(wi_id, tsg_short)`; `tsgs` table is auto-seeded so the FK
  validates).
- `doc3gpp spec sync --tsg <s>` → `SpecService.sync` → DynaReport
  list page + parallel per-spec detail page fans-out
  (`ThreadPoolExecutor`, capped at `min(32, cpu+4)` workers) →
  `parse_spec_list` + `parse_spec_detail` (+ optional ETSI PDF
  text + CR list follow-ups gated on recency / emptiness; pass
  `--per-version-details` to always re-fetch both for every version
  on every run) → `SQLAlchemySpecRepository.upsert` +
  `upsert_versions`. The per-spec `specs.last_synced_at` skip rule is
  honoured (each per-worker `_sync_one_spec` short-circuits specs
  whose `last_synced_at` is within
  `Settings.sync.spec_sync_interval`) unless `--force` is passed.
  Per-spec ETSI / CR failures log a warning and the sweep continues.
  `doc3gpp spec sync --spec-id <id>` → `SpecService.sync_spec` →
  look up in the local `specs` table; if missing, fetch
  `https://www.3gpp.org/DynaReport/{no_dot}.htm`, parse
  `#titleVal` / `#typeVal` / `#PrimaryResponsibleGroupLbl`, normalise
  the group to a seeded `tsgs.short_name`, build an in-memory
  `Spec`, and hand off to the same `_sync_one_spec` pipeline as the
  stored-row path. A 404 or unrecognised group label surfaces as
  `typer.BadParameter("spec {spec_id!r} is unknown on the 3GPP DynaReport upstream ({reason}); nothing to sync")`;
  an unknown seeded-TSG short name surfaces as
  `typer.BadParameter("spec {spec_id!r} has unknown TSG short name {short_name!r} (normalised from {long_name!r}); run 'doc3gpp tsg seed' or 'doc3gpp tsg list' to inspect the reference table")`. With
  neither `--tsg` nor `--spec-id`, every distinct TSG in the `specs`
  table is synced via `SpecService.list_distinct_tsgs`.
- `doc3gpp spec list [filters]` / `doc3gpp spec show <spec-id>`
  read cached rows via `SpecRepository.list` /
  `SQLAlchemySpecRepository.get` + `list_versions`; the show command
  raises `SpecNotFoundError` (rendered as `typer.BadParameter`) when
  no header row exists for the id. `spec show` versions can be
  paginated/limited via `--limit` (default 10) / `--offset` (default 0),
  filtered by `--version <pattern>` (rich filter grammar on the version
  string), and slimmed with `--no-wis-crs` (drops the `wis` header field
  and per-version `crs` field). `spec list` default output fields are
  `spec_id, type, title, status, radio_tech, initial_release, tsg,
  rapporteurs` (no `wis`).
- `tdoc/meeting/wi * --compact` (any `--format` command) strips
  decorators from JSON / Markdown output. JSON becomes single line
  (`separators=(",", ":")`, no trailing newline); Markdown drops
  bold, italic, headings, bullets, GFM tables, code fences, and
  emits `key: value` lines with blank-line section separators. No-op
  for `table` and `raw`. CLI flag wins over `[output] compact` in
  `doc3gpp.toml`. See `_resolve_compact` (`src/doc3gpp/cli.py:269`)
  and `Settings.output.compact` (`src/doc3gpp/settings/schema.py:221`).
- `doc3gpp tdoc parse <filters>` is end-to-end filter-driven — every
  flag is a filter, capped by `Settings.tdoc_parse.max_batch`. The
  per-file byte cap is `Settings.tdoc_parse.max_tdoc_size_kb` (default
  `1000` KB; `0` = unlimited); oversized sources are routed to the skip
  bucket via `TDocTooLargeError`. In normal
  mode the SQL query excludes rows already present in `tdoc_cr_cover_page`,
  so the batch cap applies only to pending TDocs; the preview and
  confirmation list only pending rows. `--force` explicitly includes and
  re-parses already-parsed matches. The parser returns
  `TDocCRParseResult(cover, ttcn)` — when the parser recognised a
  TTCN CR, the sidecar's `changed_functions` aggregate
  (sorted + deduped `<module_basename>.<function_name>` entries) is
  auto-derived from `required_changes` at parse time by
  `parsers/cr/ttcn_functions.py::extract_changed_functions` and round-trips
  through the `tdoc_cr_ttcn_details.changed_functions` newline-delimited
  text column (searchable via `LIKE`). Partial-extraction markers apply:
  when only the module basename is recoverable the entry is recorded
  as `'<module>.'` (trailing-dot sentinel); when only the function name
  is recoverable it is recorded as `'.<function>'` (leading-dot
  sentinel); when neither is recoverable the correction is dropped. `TDocCrService` fans the result
  out across THREE independent upserts: the slim cover-page row in
  `tdoc_cr_cover_page`, the optional `tdoc_cr_ttcn_details` sidecar (only
  when the parser recognised a TTCN CR), and the `tdoc_extracts`
  metadata row. `TDocCrService` also writes a `tdoc_cr_change_details`
  row (non-TTCN CRs only) when the parser detects revision marks.
  Full grammar and prompt-completion semantics in
  [`docs/conventions.md`](docs/conventions.md) and
  [`docs/cli.md`](docs/cli.md).
- `doc3gpp tdoc parse --from-path PATH` / `--from-url URL` is a
  direct-mode alternative that bypasses the database filters. Local
  files parse in-memory only; 3GPP-URL downloads follow the
  FK-aware behaviour matrix in `docs/cli.md` (cache + DB writes
  land when the filename's tdoc_id is present in `tdocs`; otherwise
  the result is still emitted with a warning). The zip cache is
  keyed on the **original (sanitized) filename** (D10 fix) so
  multiple revisions of the same tdoc_id never collide. When
  `Settings.sync.auto_sync` is enabled, `tdoc parse --from-url`
  on a 3GPP FTP URL extracts tdoc_id candidates from the URL
  (basename for file URLs; BFS up to `--max-depth` / `--recursive`
  for folder URLs) and runs `trigger_auto_sync(...)` **before**
  dispatching to the per-file parse helpers — see
  [`src/doc3gpp/cli.py:1527-1539`](src/doc3gpp/cli.py) and the
  "Auto-sync from URL candidates" section in `docs/cli.md`. The
  ordering is TSG sync → meeting sync → parse, so the meeting_id
  resolution can usually find the parent row by the time the parse
  fires. Same skip rules as DB-mode apply; non-3GPP URLs never
  trigger auto-sync; failures stay warnings.
- `doc3gpp tdoc show --tdoc <id>` resolves the parent `tdoc` row, then
  looks up the slim cover-page details and the extract metadata by the
  row's immutable `tdoc.ftp_url` (one row per URL — the URL is the row
  identity for both `tdoc_cr_cover_page` and `tdoc_extracts`). The TTCN
  sidecar is joined in via `cr_ttcn_repo.get_by_url(tdoc.ftp_url)` only
  when `is_ttcn_tdoc(tdoc.tdoc_id)` is `True`. Auxiliary files are
  read once via `file_repo.get_for_tdoc_id(tdoc.tdoc_id)` and match
  by `tdoc_id` (not URL) so all revisions / reviews / support files
  surface in a single pass. The CLI's renderers emit `cover`, the
  optional `ttcn` block, `extracted_at` (sourced from the
  `tdoc_extracts` row via PK JOIN), and `files` (the auxiliary files
  block / placeholder) as separate sections — the legacy `details` /
  `parser_version` fields no longer appear in the output. The `tdoc_extracts` row carries a single `cache_file` column
  (basename, derived from `tdoc.ftp_url` via `derive_cache_file()`);
  the CLI reconstructs paths as `{cache.dir}/zips/<cache_file>` and
  `{cache.dir}/markdown/<cache_file>` via `_build_cache().root` +
  `derive_cache_file(ftp_url)`. **Both subtrees write real ZIPs** so
  the `.zip` extension maps to a format `unzip` / 7z / WinZip can open
  straight from disk — the zip subtree holds the 3GPP-served zip bytes;
  the markdown subtree holds a `zipfile.ZipFile` wrapper produced by
  `_wrap_markdown_zip` (single entry named `<docx stem>.md`,
  `ZIP_DEFLATED`).
- `doc3gpp tdoc show --ftp-url <url>` resolves the URL into matching
  rows across four tables (`tdocs`, `tdoc_cr_cover_page`,
  `tdoc_cr_ttcn_details`, `tdoc_files`) directly — `tdocs` and
  `tdoc_files` use the new `get_by_ftp_url` lookups
  (`SQLAlchemyTDocRepository.get_by_ftp_url` /
  `SQLAlchemyTDocFileRepository.get_by_ftp_url`); the cover / TTCN
  tables use the existing URL-PK lookups. The `--ftp-url` path is
  **mutually exclusive** with `--tdoc` (an XOR validator raises
  `BadParameter` when neither or both are supplied) and **does NOT
  trigger** `trigger_auto_sync` — there's no parent TDoc to anchor a
  meeting sync on, and a URL-keyed read should be a deterministic
  snapshot of whatever is already in the DB. URL is normalised via
  `normalize_ftp_path` so both full URLs
  (`https://www.3gpp.org/ftp/...`) and bare relative paths resolve
  the same row. `tdocs.ftp_url` is maintained as a 1:1 invariant by
  the upload pipeline (no DB-level `UNIQUE` constraint); the lookup
  returns a single row via `ORDER BY tdoc_id ASC LIMIT 1` as a
  deterministic fallback if the invariant is ever violated.
  `--format raw` on the URL path reads the cache file directly via
  `derive_cache_file(url)` (no `TDocCrService.extract` detour)
  because the URL is the row identity — a cache miss raises
  `BadParameter` pointing at `doc3gpp tdoc parse --from-url <url>`
  or `doc3gpp tdoc parse --tdoc <id>`. The CLI bundles the result
  into a new `TDocShowRecordByUrl(ftp_url, tdoc, cover, ttcn,
  extracted_at, files)` DTO and renders it under a
  `# FTP URL` / `[FTP URL]` anchor; the renderer contract follows
  the same omit-when-null convention as the `--tdoc` path's
  `TDocShowRecord`.
- `doc3gpp search query "QUERY" [filters]` → `SearchService.search(query,
  filters)` → `repo.search` (FTS5 MATCH + filters + bm25) →
  `EmbeddingReranker.rerank` (`PassthroughReranker` for v1) →
  `list[SearchHit]` → CLI formatter. The ranking stage uses
  `bm25(tdoc_search, ...)` with the configurable column-weight
  vector in `Settings.search.bm25_weights` (see the `tdoc_search`
  schema below for the column order); the same `bm25_weights`
  vector drives snippet selection — one `snippet(...)` per
  `weight > 0` column, and the result surfaces in the hit's
  `previews` map only when the snippet contains a match. Fires
  the stale-index hint on the side (one-shot, gated on `--quiet`).
- `doc3gpp search index --rebuild` → `SearchService.rebuild(...)`
  generator → `repo.rebuild_batch(...)` per batch → per-row
  `repo.upsert(tdoc_id)` → updates `tdoc_search_meta` cursor.
  Resumable via `--resume`; cheap incremental via `--stale-only`.
- `doc3gpp search sem QUERY [filters]` →
  `SemanticSearchService.search` → original `QUERY` embedding (vector
  path, always on) → opt-in FTS5 path (the explicit `--fts5-query`
  string is preprocessed by `SearchQueryBuilder` — no stopword strip)
  → when `--fts5-query` is supplied, FTS5 fan-out (`2N`) and vector
  KNN fan-out (`2N`) flow through `rrf_merge` and the result is
  truncated to `--limit` (default 20); when `--fts5-query` is absent,
  FTS5 + RRF are skipped and pure vector KNN results return, dressed
  as `SemanticSearchHit` with `rank_fts5=None`. `--fts5-weight`
  (0.0..1.0, default 0.5) blends the two ranks via
  `rrf = 1/(k + rank_fts5) * fts5_weight + 1/(k + rank_vec) *
  (1 - fts5_weight)` (`k=60`); the flag is ignored when `--fts5-query`
  is omitted. `search query` (FTS5-only) is unchanged.
- `doc3gpp search index --rebuild-embeddings [--stale-only] [--batch N]
  [--resume] [--quiet]` → `SemanticSearchService.rebuild_embeddings`
  → drops + recreates `vec_tdoc_embeddings`; iterates every `tdocs`
  row, calls `index_for_tdoc` per id (build embed text → chunk →
  embed → upsert); updates `vec_meta` for resume + staleness.
  `--rebuild-all` runs both FTS5 and vector rebuilds in sequence.
- `doc3gpp config path` / `doc3gpp config show` dump the resolved
  TOML + env settings for diffing against `doc3gpp.toml.example`.
- `doc3gpp config init --target <auto|project|user> [--force]` writes
  the packaged default TOML template (full defaults) at the bootstrap
  target — `auto` (default) picks `./doc3gpp.toml` when run from a
  project root, otherwise `~/.config/doc3gpp/config.toml`. Refuses
  while `DOC3GPP_CONFIG` is set; `--force` overwrites an existing file.
- `doc3gpp config set <key> <value>` writes one setting into the active
  TOML config file (refuses when none is in use; run `config init` to
  bootstrap one); the previous `--init` / `--target` / `--force` flags
  are removed — see the plan at
  `.omo/plans/config-set-command.md` for the full command contract.
- When `Settings.sync.auto_sync` is enabled, `meeting list`, `tdoc list`,
  `tdoc show`, and database-mode `tdoc parse` internally trigger the
  same meeting-calendar and TDoc-list sync paths used by explicit
  `meeting sync` / `tdoc sync`. The same skip rules apply and are never
  bypassed; failures are logged as warnings and do not abort the read
  command. Direct-mode `tdoc parse --from-path` / `--from-url` never
  triggers auto-sync. The `tdoc show --ftp-url` selector also never
  triggers auto-sync — the URL is the row identity and there's no
  parent TDoc / meeting to anchor a sync on; users wanting a fresh
  extract at the URL must run `tdoc parse --from-url <url>` or
  `tdoc parse --tdoc <id>` explicitly.

## Common commands

```bash
# Lint (ruff is the only configured tool)
ruff check .

# Full sqlite test suite (unit + integration, excludes online)
./scripts/test_sqlite.sh
# (auto-uses pytest-xdist -n auto when the [dev] extra is installed;
#  falls back to sequential otherwise. ~3x speedup on 4+ core machines.)

# Online tests (opt-in, hits live 3gpp.org and FTP)
python -m pytest -m online -rs

# Plain `pytest` is offline by default (addopts in pyproject.toml).

# Bootstrap dev environment
./scripts/dev_run.sh
```

`pyproject.toml [tool.pytest.ini_options]` sets `pythonpath = ["src"]`,
so tests resolve `doc3gpp.*` without an editable install, and
`addopts = ["-m", "not online"]` so plain `pytest` is the offline
suite.

## Doc pointers

| Topic | Doc |
| --- | --- |
| Layered diagram, runtime data flow, ORM schema, CLI inventory, testing layout, design rules | [`docs/architecture.md`](docs/architecture.md) |
| Per-command CLI reference (every flag, default, example) | [`docs/cli.md`](docs/cli.md) |
| 3GPP URL conventions, naming conventions, parser field semantics | [`docs/3gpp-knowledge.md`](docs/3gpp-knowledge.md) |
| Where each public symbol lives (symbol → file reference) | [`docs/code-map.md`](docs/code-map.md) |
| Filter grammar, settings caching, commit policy, anti-patterns, unique styles | [`docs/conventions.md`](docs/conventions.md) |
| Open limitations (schema bootstrap, hardcoded FTP URL, R5-/C6- URL templates, test surface, …) | [`docs/known-constraints.md`](docs/known-constraints.md) |
| Per-knob TOML schema reference | [`doc3gpp.toml.example`](doc3gpp.toml.example) |
| Search index design + spec contract | [`docs/superpowers/specs/2026-07-29-fts5-search-design.md`](docs/superpowers/specs/2026-07-29-fts5-search-design.md) |
| FTS5 implementation plan | [`docs/superpowers/plans/2026-07-29-fts5-search.md`](docs/superpowers/plans/2026-07-29-fts5-search.md) |
| Spec sync / list / show design + plan | [`docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md`](docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md) + [`docs/superpowers/plans/2026-08-10-spec-sync-list-show.md`](docs/superpowers/plans/2026-08-10-spec-sync-list-show.md) |
| Web server + MCP + jobs end-user guide | [`docs/web-server.md`](docs/web-server.md) |

Update `README.md`, `AGENTS.md`, and the relevant `docs/*.md` in the
same change set when CLI surface or behaviour changes — see
[`docs/conventions.md`](docs/conventions.md) §"Documentation sync" for
the convention.
