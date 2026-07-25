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
- `.[mysql]` / `.[postgres]` — DB drivers.
- `.[extract]` — `python-docx` for the TDoc extraction pipeline.

`pip install doc3gpp` installs the SDK only; `pip install "doc3gpp[cli]"`
or `pipx install "doc3gpp[cli]"` adds the `doc3gpp` CLI command.

## Structure (high level)

```
doc3gpp/
├── src/doc3gpp/          # package root
│   ├── cli.py            # Typer commands (7 groups, 20 commands)
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
│   ├── integration/      # sqlite by default; online + mysql opt-in
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
| Add a domain model | `src/doc3gpp/models/` | `@dataclass(slots=True)`; never expose ORM attrs. |
| Add a storage backend | `src/doc3gpp/storage/backends/` | Engine kwargs per dialect. |
| Change filters for a list | `src/doc3gpp/repository/protocols.py` + `src/doc3gpp/storage/repositories/` | Update **both** the Protocol and the impl. |
| Run all tests | `./scripts/test_sqlite.sh` | Unit + integration, sqlite-only. |
| Run online tests | `python -m pytest -m online -rs` | Hits live 3gpp.org + FTP. |

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
  FTP URL. **No meeting row → no TDoc sync.** Skips when the meeting
  is outside `Settings.sync.tdoc_list_closed_window` or was synced
  within `Settings.sync.tdoc_list_sync_interval`; `--force` bypasses
  both checks.
- `doc3gpp tdoc sync` (no selector) →
  `TDocSyncCoordinator.sync_all_tracked_meetings` → every distinct
  ``meeting_id`` in the ``tdocs`` table is synced individually with the
  same two skip rules applied per meeting.
- `doc3gpp wi sync --tsg <s>` → `WiService.sync` → `fetch_wis` →
  `parse_3gpp_wis` → `SQLAlchemyWiRepository.upsert_many` (composite
  PK `(wi_id, tsg_short)`; `tsgs` table is auto-seeded so the FK
  validates).
- `doc3gpp tdoc parse <filters>` is end-to-end filter-driven — every
  flag is a filter, capped by `Settings.tdoc_parse.max_batch`. In normal
  mode the SQL query excludes rows already present in `tdoc_cr_details`,
  so the batch cap applies only to pending TDocs; the preview and
  confirmation list only pending rows. `--force` explicitly includes and
  re-parses already-parsed matches. The parser returns
  `TDocCRParseResult(cover, ttcn)` — when the parser recognised a
  TTCN CR, the sidecar's `changed_functions` aggregate
  (sorted + deduped `<module_basename>.<function_name>` entries) is
  auto-derived from `required_changes` at parse time by
  `parsers/cr/ttcn_functions.py::extract_changed_functions` and round-trips
  through the `tdoc_cr_ttcn_details.changed_functions` newline-delimited
  text column (searchable via `LIKE`). `TDocCrService` fans the result
  out across THREE independent upserts: the slim cover-page row in
  `tdoc_cr_details`, the optional `tdoc_cr_ttcn_details` sidecar (only
  when the parser recognised a TTCN CR), and the `tdoc_extracts`
  metadata row. Full grammar and prompt-completion semantics in
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
  [`src/doc3gpp/cli.py:1353-1366`](src/doc3gpp/cli.py) and the
  "Auto-sync from URL candidates" section in `docs/cli.md`. The
  ordering is TSG sync → meeting sync → parse, so the meeting_id
  resolution can usually find the parent row by the time the parse
  fires. Same skip rules as DB-mode apply; non-3GPP URLs never
  trigger auto-sync; failures stay warnings.
- `doc3gpp tdoc show --tdoc <id>` resolves the parent `tdoc` row, then
  looks up the slim cover-page details and the extract metadata by the
  row's immutable `tdoc.ftp_url` (one row per URL — the URL is the row
  identity for both `tdoc_cr_details` and `tdoc_extracts`). The TTCN
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
  rows across four tables (`tdocs`, `tdoc_cr_details`,
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

# Full sqlite test suite (unit + integration, excludes online + mysql)
./scripts/test_sqlite.sh

# Online tests (opt-in, hits live 3gpp.org and FTP)
python -m pytest -m online -rs

# MySQL tests (needs DOC3GPP_TEST_MYSQL_URL)
python -m pytest -m mysql

# Bootstrap dev environment
./scripts/dev_run.sh
```

`pyproject.toml [tool.pytest.ini_options]` sets `pythonpath = ["src"]`,
so tests resolve `doc3gpp.*` without an editable install.

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

Update `README.md`, `AGENTS.md`, and the relevant `docs/*.md` in the
same change set when CLI surface or behaviour changes — see
[`docs/conventions.md`](docs/conventions.md) §"Documentation sync" for
the convention.

<!-- headroom:rtk-instructions -->
# RTK (Rust Token Killer) - Token-Optimized Commands

When running shell commands, **always prefix with `rtk`**. This reduces context
usage by 60-90% with zero behavior change. If rtk has no filter for a command,
it passes through unchanged — so it is always safe to use.

## Key Commands
```bash
# Git (59-80% savings)
rtk git status          rtk git diff            rtk git log

# Files & Search (60-75% savings)
rtk ls <path>           rtk read <file>         rtk grep <pattern>
rtk find <pattern>      rtk diff <file>

# Test (90-99% savings) — shows failures only
rtk pytest tests/       rtk cargo test          rtk test <cmd>

# Build & Lint (80-90% savings) — shows errors only
rtk tsc                 rtk lint                rtk cargo build
rtk prettier --check    rtk mypy                rtk ruff check

# Analysis (70-90% savings)
rtk err <cmd>           rtk log <file>          rtk json <file>
rtk summary <cmd>       rtk deps                rtk env

# GitHub (26-87% savings)
rtk gh pr view <n>      rtk gh run list         rtk gh issue list

# Infrastructure (85% savings)
rtk docker ps           rtk kubectl get         rtk docker logs <c>

# Package managers (70-90% savings)
rtk pip list            rtk pnpm install        rtk npm run <script>
```

## Rules
- In command chains, prefix each segment: `rtk git add . && rtk git commit -m "msg"`
- For debugging, use raw command without rtk prefix
- `rtk proxy <cmd>` runs command without filtering but tracks usage
<!-- /headroom:rtk-instructions -->
