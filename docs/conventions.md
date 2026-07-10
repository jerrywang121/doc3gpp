# Conventions

Implementation-specific rules that are too detailed for `AGENTS.md`
but matter when changing the codebase. If something here disagrees with
the implementation, the implementation wins — and this file is what
gets updated alongside the fix.

## Tests

- New features ship with both a **unit test** (mock external calls) and
  an **integration test** against sqlite under `tests/integration/`.
- `pyproject.toml [tool.pytest.ini_options]` sets
  `pythonpath = ["src"]`, so tests resolve `doc3gpp.*` without an
  editable install.
- Default `pytest` excludes both `online` and `mysql` markers. New tests
  stay in the default pool unless they need network or a MySQL server.
- MySQL tests are double-gated (`pytestmark` marker +
  `@pytest.mark.skipif` on `DOC3GPP_TEST_MYSQL_URL`); online tests hit
  live 3gpp.org + FTP and are flaky — run with `-rs` to surface skip
  reasons.

## Lint / static analysis

- Ruff only: `line-length = 100`, no custom rule selection (defaults).
- `target-version = "py310"` in `pyproject.toml`; new code targets that
  runtime.
- No `mypy` / `pyright` configured.
- Patterns the no-excuse audit (worth a manual scan when refactoring):
  `as any` / `# type: ignore` / `@ts-ignore` style escape hatches are
  banned — fix the type, don't silence the checker.
- `cache.py` and `export.py` sit at `storage/` root, not in a
  subpackage. Mildly unconventional but stable.

## Commits

- Conventional Commits with an English, present-tense subject:
  `feat(area):`, `fix(area):`, `refactor(area):`, `docs:` /
  `docs(area):`, `chore(area):`, `test(area):`. Scope is the area in
  lowercase (`cli`, `tdoc`, `scrape`, `extract`, `cr`, …).
- Branch names mirror the same `area/topic` shape (e.g.
  `docs/audit-cli-and-implementation-references`,
  `feat/config-file-settings`).
- The project does **not** auto-commit. Plan first, implement, run lint
  + the sqlite test profile, then hand off to the user.
- User-driven commits happen in **one** commit each; subsequent edits
  get a new commit only when the user asks again.
- Scripts in `scripts/` use `set -euo pipefail`.

## Documentation sync

- When behaviour or the CLI surface changes, update `README.md`,
  `AGENTS.md`, and `docs/*.md` in the **same** change set.
- The repo's docs surface is:
  [`docs/architecture.md`](architecture.md) — layers, data flow, schema,
  CLI surface, composition, testing layout, design rules.
  [`docs/cli.md`](cli.md) — full CLI reference.
  [`docs/3gpp-knowledge.md`](3gpp-knowledge.md) — 3GPP URLs, naming
  conventions, parser field semantics.
  [`docs/code-map.md`](code-map.md) — symbol → file navigation.
  [`docs/conventions.md`](conventions.md) — this file.
  [`docs/known-constraints.md`](known-constraints.md) — open issues.

## Settings caching — flush in tests

Both loaders are `@lru_cache(maxsize=1)`:

- `doc3gpp.settings.loader.get_settings`
- `doc3gpp.storage.db.session.get_engine`

If a test or fixture mutates `DOC3GPP_*` env vars via `monkeypatch`,
it **must** `cache_clear()` both. The `sqlite_env` fixture in
`tests/conftest.py` is the canonical pattern.

Because the engine is cached, the CLI's `db reset` flow explicitly
calls `get_engine.cache_clear()` after deleting the SQLite file so the
next `create_schema()` opens a connection to the empty file.

## TOML config-file layer

`Settings` precedence (highest wins): **CLI flag > environment
variable > TOML file > built-in default**. The
`Settings.settings_customise_sources` hook reorders pydantic-settings
sources so env vars beat the TOML file (otherwise the file would
shadow the env).

Discovery order (first hit wins):

1. `$DOC3GPP_CONFIG` (file or directory).
2. `./doc3gpp.toml` (project-local — checked into git for team defaults).
3. `~/.config/doc3gpp/config.toml` (XDG; honors `$XDG_CONFIG_HOME`).

A missing file is silent (defaults are used); a malformed file raises
`ValueError` pointing at the path. `Settings(extra="ignore")` ignores
unrelated keys so the file can be co-tenanted with third-party
tooling metadata.

Inspect what's in effect with:

```bash
doc3gpp config path   # which file is being read
doc3gpp config show   # the fully-resolved settings, as JSON
```

The full TOML schema lives in [`doc3gpp.toml.example`](../doc3gpp.toml.example).

Recognised env vars (see `Settings` in `src/doc3gpp/settings/schema.py`
for the full list):

```
DOC3GPP_DATABASE_URL
DOC3GPP_DB_ECHO
DOC3GPP_DB_POOL_SIZE
DOC3GPP_DB_AUTO_MIGRATE
DOC3GPP_LOG_LEVEL
DOC3GPP_HTTP_VERIFY
DOC3GPP_HTTP_MAX_RETRIES
DOC3GPP_HTTP_RETRY_BACKOFF
```

Nested settings are overridable via the `__` delimiter:

```
DOC3GPP_MEETING_SYNC__CLOSED_YEARS=5
DOC3GPP_OUTPUT__FORMAT=json
DOC3GPP_CACHE__DIR=~/.cache/doc3gpp/tdocs
DOC3GPP_CACHE__PURGE_CONFIRM=false
DOC3GPP_TDOC_PARSE__MAX_BATCH=200
```

MySQL tests additionally use `DOC3GPP_TEST_MYSQL_URL`.

## Filter grammar (tdoc list / tdoc parse)

Both `tdoc list` and `tdoc parse` share a single grammar defined in
`src/doc3gpp/cli_filters.py`. For text columns the flag value can be:

| Value | Effect |
| --- | --- |
| `null` | match rows whose column is `NULL` |
| `not-null` | match rows whose column is not `NULL` |
| `!<pattern>` | match rows whose column does **not** `LIKE` `<pattern>` — the `!` is consumed and the rest is bound as the LIKE pattern (e.g. `!%Sidelink%` excludes titles containing `Sidelink`) |
| any other text | bound as a SQL `LIKE` pattern (`%` / `_` wildcards) |

For `--uploaded-date` the same `null` / `not-null` tokens are accepted
plus a parameterised SQL comparison of the form
`"<op> 'YYYY-MM-DD'"` where `<op>` is one of `=`, `!=`, `<`, `<=`, `>`,
`>=`. The operator and date literal are always bound as SQLAlchemy
parameters — never string-interpolated into the SQL, so the surface is
injection-safe.

`validate_date_filter` rejects anything else at the CLI boundary
before the database is touched. The repository's `_apply_text_filter`
and `_apply_date_filter` helpers emit the SQLAlchemy bindings.

The text-column flag set is uniform across both commands:
`--status`, `--cr-cat`, `--spec`, `--wi`, `--title`, `--source`,
`--type`, `--revision-of`, `--revised-to`, `--ftp-url`, `--release`,
`--version`, `--cr-num`, `--cr-pack`, `--uploaded-date`.
`--meeting` follows the same grammar.

## tdoc parse workflow

`tdoc parse` is filter-driven end to end. Every flag is a filter;
`--tdoc` is a `LIKE` pattern on `tdoc_id` (singular, not repeatable)
and combines freely with `--meeting-id`, `--meeting`, and every
text/date filter. The earlier `--tdoc-id` integer PK selector and the
mutual exclusivity with `--meeting-id` are gone — pick the filter
combination that targets the intended subset.

Before extraction the CLI partitions the matches into already-parsed
vs to-parse groups, prints both with a base column set (`tdoc_id`,
`title`, `type`, `cr_cat`, `status`) plus one extra column per active
filter (see `docs/cli.md` for the table), and prompts unless
`--yes` / `-y` is passed.

The candidate set is capped by `Settings.tdoc_parse.max_batch`
(default `100`, configurable via `[tdoc_parse] max_batch` in TOML or
`DOC3GPP_TDOC_PARSE__MAX_BATCH` in env). When the filter result
exceeds the cap, a warning is printed with a `Remaining` counter;
re-run the same command **without** `--force` to continue where the
previous invocation stopped (already-parsed rows are skipped, so the
second run picks up the next batch).

## meeting list --tdoc flow

`meeting list --tdoc <id>` finds the meeting whose `start_doc` /
`end_doc` range brackets the TDoc. The flag accepts a 9-character
CR-shape id matching `cli_filters.TDOC_ID_RE` (e.g. `R5-260013`,
`R5s260009`, `R5w260013`); validation happens at the CLI boundary via
`parse_tdoc_id` and the parsed `(prefix, number)` tuple is forwarded
to the repository.

Matching rules:

- `start_doc` prefix (case-insensitive) must equal the TDoc's prefix.
- The 6-digit `start_doc` number must be `≤` the TDoc number.
- If `end_doc` is non-null, the same prefix + number `≥` rule applies.
- Meetings without a `start_doc` never match.

Prefix matching is implemented with
`func.upper(func.substr(...)) == prefix.upper()` so it works on SQLite
/ MySQL / Postgres without a dialect-specific `ILIKE`.

## Anti-patterns (this project)

- **Protocol ↔ implementation signature drift.** Previously:
  `MeetingRepository.list` declared only `limit`, but
  `SQLAlchemyMeetingRepository.list` took
  `limit, tsg, name_like, location_like, year`. Resolved 2026-07-02 (M2).
  When changing filter signatures on any repo, keep the Protocol and
  the impl in sync.
- **`create_schema()` called from CLI flows.** `meeting sync`,
  `wi sync`, and `tsg seed` still call `create_schema()`
  for fresh-database ergonomics; `tdoc sync` and `tdoc parse` no longer
  do. Idempotent but blurs the `db init` boundary.
- **Cross-service orchestration in the CLI.** Mostly addressed:
  `tdoc sync` delegates to `TDocSyncCoordinator`. Other commands still
  construct their own services via `services.factory.build_*` helpers.
- **Acknowledged `# noqa: F401`.** Four in `storage/db/migrate.py` —
  side-effect imports required for SQLAlchemy `Base.metadata`
  registration. Do not remove without restructuring the registration.
- **`ScraperClient._is_retryable_exception`** deliberately treats only
  transient `httpx` subclasses as retryable. Programming errors
  (e.g. `httpx.InvalidURL`) raise immediately — do not broaden the
  catch or you'll silently swallow real bugs.
- **Doc drift.** When CLI surface or behaviour changes, update
  `README.md`, `AGENTS.md`, and `docs/*.md` in the same change set.
  The audit pass on `docs/audit-cli-and-implementation-references`
  recorded this drift explicitly; keep the docs in sync going forward.

## Unique styles

- **`repository/` (abstract) and `storage/repositories/` (concrete)
  are separate packages.** Abstractions live in
  `src/doc3gpp/repository/`, implementations in
  `src/doc3gpp/storage/repositories/`. The split means a reader
  follows two paths to trace a repo from contract to SQL — keep the
  drift minimal (see the anti-pattern above).
- **`config.py` is a re-export shim** for backwards compatibility. New
  imports should go to `doc3gpp.settings` directly.
- **Defaults pinned in `OutputFieldsSettings.meeting` / `.tdoc` /
  `.tsg` / `.wi`.** The hardcoded defaults that previously lived in
  `cli.py` were centralised so users can override per-column defaults
  via the TOML config file without touching code.
- **`TDocService` is the only service that has a "with meeting" join
  variant.** Filter and projection helpers live alongside the repo
  (`SQLAlchemyTDocRepository.list_with_meeting`) so the join cost
  only happens when the CLI asks for `meeting_name`.
