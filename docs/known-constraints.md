# Known Constraints

Open / known limitations that future work should respect. This file is
the single source of truth — `docs/architecture.md` §Out of scope (today)
and the per-feature notes below should stay in lock-step with this list.

When a constraint is lifted, **edit this file** as part of the same
change set so the docs stay honest.

## Schema

- **No Alembic migrations.** `Base.metadata.create_all` via
  `storage/db/migrate.py` (invoked by `db init`) is the only schema
  bootstrap. `db_auto_migrate` is a TOML-only field (see
  `doc3gpp.settings.schema.ALLOWED_ENV_VARS`) that does **not** run
  migrations. After pulling an ORM shape change, **existing SQLite
  installs must run `doc3gpp db reset --yes`** (or a backend-native
  migration for MySQL / PostgreSQL) — otherwise the live schema stays
  out of sync and SQL repos raise `OperationalError`.
- **`meeting sync`, `wi sync`, and `tsg seed` call `create_schema()`**
  for fresh-database ergonomics. Idempotent but blurs the `db init`
  boundary; `tdoc sync` and `tdoc parse` already dropped the call.
  See [`docs/conventions.md`](conventions.md) for the canonical list.

## Settings caching

- **`get_settings` is `@lru_cache(maxsize=1)`** and `ScraperClient`'s
  `__init__` reads settings once, so env changes mid-process do not
  propagate after the first call. Restart the process to pick up new
  `DOC3GPP_*` values (only the closed
  `doc3gpp.settings.schema.ALLOWED_ENV_VARS` subset is honoured by
  `Settings` — everything else is silently ignored). Tests must
  `cache_clear()` both loaders when mutating env via `monkeypatch` —
  see [`docs/conventions.md`](conventions.md) for the canonical fixture.

## Network / scraping

- **`https://www.3gpp.org/ftp/` is hardcoded** in the FTP scraper.
  If 3GPP moves the assets to a CDN, `meeting sync` will silently
  return empty rows rather than raise.
- **`ScraperClient.get_text` retries via a narrow exception surface**
  (specific `httpx` retryable subclasses + retryable HTTP status
  codes). Programming errors propagate immediately — see the
  anti-pattern in [`docs/conventions.md`](conventions.md).

## Calendar parser

- **Coupled to the current 3GPP DynaReport table layout.** Upstream
  HTML changes will silently break `meeting sync` (no error, just
  fewer rows). Re-pin tests against a recorded page snapshot when the
  table shape changes.

## TDoc list sync

- **TDoc lists are downloaded from the 3GPP portal.**
  `GenerateDocumentList.aspx?meetingId={meeting_id}` returns the XLSX
  directly; the URL template is configurable via
  `sync.tdoc_list_url_template`. Auxiliary TDoc files are still scanned
  from the meeting's FTP folders.
- **Skip rules are limited to the closed window and the local sync
  interval.** The upstream XLSX does not return a reliable
  `Last-Modified` header, so the coordinator no longer performs an
  mtime comparison. Use `--force` to bypass the remaining rules.
- **TDoc date parsing** accepts both ISO (`YYYY-MM-DD`) and the
  `DD/MM/YYYY HH:MM:SS` shape used upstream; both are stored as
  `Date`. Anything outside these shapes falls through to `NULL`.

## TDoc CR extraction

- **URL templates locked in for `R5s` (TTCN email CR) and `R5w`
  (TTCN workshop CR) branches** against offline fixtures. The `R5-`
  and `C6-` templates return `None` until exercised against the live
  site — treat `None` as "not yet supported", not as an error. See the
  inline docstring at
  `src/doc3gpp/scraping/tdoc_zip_source.py:_build_tdoc_zip_url`.
- **`python-docx` is an opt-in extra.** Without `doc3gpp[extract]`
  installed, the CLI prints a friendly install hint and exits 1.
- **`TDoc` types other than CR (LS, DRAFT, BB, …) are not handled.**
  The extractor's type guard raises `TDocTypeUnsupportedError` for
  non-CR ids; future expansion is a separate change.
- **`tdoc_cr_ttcn_details` is created lazily on first write, not on
  `doc3gpp db init`.** `TDocCrTtcnDetailOrm` is registered with
  `Base.metadata` so a fresh install picks it up via the standard
  `create_schema()` call, but existing installs that pre-date the
  sidecar gain the table only when `TDocCrService` writes the first
  TTCN row. Each repo implements `_ensure_table_exists()`: a
  `SELECT 1 FROM tdoc_cr_ttcn_details LIMIT 0` is attempted, and an
  `OperationalError("no such table")` triggers an idempotent
  `Base.metadata.create_all()` followed by a retry. The same lazy
  bootstrap covers `tdoc_cr_details` on legacy DBs.
- **`direct_parse_bytes` returns a loosely-typed tuple.**
  The helper exposes the third element of its return tuple as
  `object` (`tuple[str, str, object]`) to keep `parsers/`
  independent of the `models/` import cycle; the call sites that
  assign to `DirectParseResult.details` rely on a runtime
  `isinstance` check rather than a typed assignment. Pyright will
  flag the assignment as a soft error (`reportArgumentType` /
  `reportAssignmentType`) until the return type is tightened. The
  trade-off is intentional today — the wider fix is a follow-up.

## Test surface

- **`online` tests access live `3gpp.org` + FTP** and are flaky.
  Always run with `-rs` to surface skip reasons; do not gate CI on
  them. They live behind the `online` pytest marker so the default
  profile (`pytest -m "not mysql and not online"`) skips them.
- **MySQL tests are double-gated** (`pytestmark` marker +
  `@pytest.mark.skipif` on `DOC3GPP_TEST_MYSQL_URL`). They do not
  run in the default profile.
- **No CI pipeline exists.** The project relies on local
  `scripts/test_sqlite.sh` runs. There is no `.github/workflows/`,
  no Makefile, no Dockerfile.

## Configuration writer

- **`doc3gpp config set` rewrites the entire TOML file via `tomli_w`**
  (`src/doc3gpp/settings/config_writer.py:write_toml`); comments, blank
  lines, and key ordering are not preserved. Keep the file under VCS
  if formatting matters — diffs will look like full rewrites on the
  first edit.
