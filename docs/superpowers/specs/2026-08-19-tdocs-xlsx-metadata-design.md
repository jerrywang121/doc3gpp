# TDoc list XLSX: capture `To` / `Cc` / `Original LS` / `For` / `Abstract` / `Secretary Remarks`

**Status:** Draft
**Date:** 2026-08-19
**Branch:** main
**Target:** parser → ORM → dataclass → repository → repository
Protocol → service layer → CLI `tdoc list` (`--fields` + filter flags)
→ web `/tdocs` form & template → web `tdoc_show.html` detail panel →
MCP `list_tdocs` (filter flags + `_TDOC_FIELDS`) → MCP `get_tdoc`
(JSON envelope) → doc3gpp.toml.example

## Problem

The 3GPP meeting TDoc list XLSX
(`TDoc_List_Meeting_<name>.xlsx` /
`https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId=…`)
carries six columns the parser currently drops:

| Header in XLSX | Currently captured | Stored as |
| --- | --- | --- |
| `For` | no | new `tdoc_for` |
| `Abstract` | no | new `abstract` |
| `Secretary Remarks` | no | new `secretary_remarks` |
| `To` | no | new `ls_to` |
| `Cc` | no | new `ls_cc` |
| `Original LS` | no | new `original_ls` |

`For` carries a categorical code (`Information` / `Approval` / `Discussion`
/ `Action`); `Abstract` is the row's free-form TL;DR; `Secretary Remarks`
is the working-group secretary's free-form annotation. The `To` / `Cc` /
`Original LS` triple is LS-routing metadata — only meaningfully populated
for LS-shape TDoc rows but present on every row in the header.

Existing XLSX parser
(`src/doc3gpp/parsers/tdoc_parser.py::read_tdoc_sheet`) reads the
`To` / `Cc` / `Original LS` / `For` / `Abstract` / `Secretary Remarks`
cells but never plumbs the values downstream. The scraper
(`src/doc3gpp/scraping/portal_source.py::fetch_tdocs_from_portal`) maps
the parser dict keys onto the `TDoc` dataclass; the ORM
(`src/doc3gpp/storage/repositories/tdoc_sql.py`) and Protocol
(`src/doc3gpp/repository/protocols.py`) lose track of the values
silently.

End result: an LS like
`R5-252176_LS_to_RAN2_on_NR_LTE_coexistence.zip` carries a populated
`To` cell pointing at RAN2 and a populated `Original LS` cell pointing at
an upstream LS, but the database stores `None` for both — and the CLI
`tdoc show --format json`, the web `tdoc_show.html`, and the MCP
`get_tdoc` tool have no way to surface them.

## Goal

Add six first-class metadata fields to the `TDoc` value object and the
`tdocs` table so the XLSX `To` / `Cc` / `Original LS` / `For` / `Abstract`
/ `Secretary Remarks` cells round-trip end-to-end through the parser,
storage, repository, service, CLI list+show, web list+show, and MCP
list+get. Each is exposed as:

* an SQL column on `tdocs`,
* a dataclass field on `TDoc`,
* a `--fields` / `?fields=` / `MCP list_tdocs fields=` selector,
* a CLI / web / MCP filter flag (`--ls-to`, `--ls-cc`,
  `--original-ls`, `--for`, `--abstract`, `--secretary-remarks`),
* an entry in `to_jsonable` (web JSON envelope) / `TDocShowRecord.tdoc`
  (CLI JSON envelope) — automatic via dataclass-field walking,
* a single "XLSX metadata" panel on `tdoc_show.html` when any of the six
  is non-`None`,
* a representation in the MCP `list_tdocs` filter kwargs (default
  `_TDOC_FIELDS` is unchanged — opt-in keeps the default table quiet).

Out of scope:

* Re-parsing every pre-existing `tdocs` row to backfill the new
  columns. **Backfill happens implicitly on the next `tdoc sync`**:
  `TDocService.sync_tdoc_list` re-runs the parser on the upstream
  XLSX; the upsert is by-PK and the parser is deterministic, so the
  write is idempotent (same value the cell had before lands in the
  same column). The `tdoc sync` skip rule
  (`Settings.sync.tdoc_list_sync_interval`,
  `Settings.sync.tdoc_list_closed_window`) still gates how often this
  happens — matching the precedent set by every previous column
  addition (`_migrate_spec_rapporteurs`,
  `_migrate_tdoc_cr_cover_page_summary_of_change`,
  `_migrate_spec_versions_drop_comment` all add the column and let
  next-time-re-sync fill it in).
* Promotion of any of the six into the default `settings.output.fields.tdoc`
  list. The user opted for "Don't promote" — columns are opt-in via
  `--fields all`, `?fields=all`, or TOML `[output.fields] tdoc`.
* Gating the LS fields on `tdoc.type == "LS"`. Real XLSX rows carry
  the `To` / `Cc` / `Original LS` cells on every row type; non-LS rows
  naturally end up with `None` for the LS triple. A parser-emit-gate
  would risk dropping a real LS that happens to have an
  unrecognised `type` label.
* Changing the cover-page / TTCN sidecar tables. The new fields live
  on `TDoc` / `tdocs`, not on `tdoc_cr_cover_page` / `tdoc_cr_ttcn_details`.
* Touching the FTS5 `cover_text` projection, the embedding slim-text
  projection, or the `tdoc_search` virtual table. Any future PR can
  add `tdoc_for` / `abstract` to those projections if needed.

## Design

### 1. ORM + dataclass

**`src/doc3gpp/models/tdoc.py`.** Six new optional fields on the `TDoc`
dataclass, declared after `cr_pack`:

```python
tdoc_for: str | None = None          # XLSX "For" column (categorical)
abstract: str | None = None          # XLSX "Abstract" column (free-form TL;DR)
secretary_remarks: str | None = None # XLSX "Secretary Remarks" column
ls_to: str | None = None             # XLSX "To" column (LS routing target)
ls_cc: str | None = None             # XLSX "Cc" column (LS CC routing)
original_ls: str | None = None       # XLSX "Original LS" column (LS origin)
```

Each carries a short docstring mirroring the style of the existing
fields — explains what XLSX column it comes from and when it is
populated. `TDocWithMeeting` is unchanged (it composes a `TDoc`).

**`src/doc3gpp/storage/db/models.py`.** Six new column declarations on
`TDocORM`, declared after `cr_pack` so existing rows are untouched
(`nullable=True`, no default). Column types:

| Attribute | Type | Rationale |
| --- | --- | --- |
| `tdoc_for` | `String(64)` | Short categorical label (`Information` / `Approval` / `Discussion` / `Action`); mirrors `cr_cat` length cap |
| `abstract` | `Text` | Free-form; mirrors `title` |
| `secretary_remarks` | `Text` | Free-form; mirrors `title` |
| `ls_to` | `String(256)` | Comma-separated TSG short names or free-form group label; mirrors `source` length cap |
| `ls_cc` | `String(256)` | Same shape as `ls_to` |
| `original_ls` | `Text` | LS origin pointer (free-form); mirrors `title` |

`TDocFileORM` / `tdoc_cr_cover_page` / `tdoc_cr_ttcn_details` are
**unchanged**. Only `TDocORM` gains columns.

### 2. Migration (`src/doc3gpp/storage/db/migrate.py`)

Add `_migrate_tdocs_xlsx_metadata()` registered after
`_migrate_tdoc_cr_cover_page_summary_of_change` in `create_schema()`:

```python
def _migrate_tdocs_xlsx_metadata() -> None:
    """Add six XLSX-metadata columns to ``tdocs``.

    Idempotent: probes ``PRAGMA table_info(tdocs)`` and only issues
    ``ALTER TABLE`` statements for the columns that are absent. Same
    shape as :func:`_migrate_spec_rapporteurs` /
    :func:`_migrate_tdoc_cr_cover_page_summary_of_change`.
    """
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text("SELECT 1 FROM sqlite_master "
                 "WHERE type='table' AND name='tdocs' LIMIT 1")
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        column_names = {row[1] for row in rows}
        additions = [
            ("tdoc_for",          "VARCHAR(64)"),
            ("abstract",          "TEXT"),
            ("secretary_remarks", "TEXT"),
            ("ls_to",             "VARCHAR(256)"),
            ("ls_cc",             "VARCHAR(256)"),
            ("original_ls",       "TEXT"),
        ]
        for name, ddl_type in additions:
            if name in column_names:
                continue
            conn.execute(text(f"ALTER TABLE tdocs ADD COLUMN {name} {ddl_type}"))
```

Order matches the dataclass declaration so the migration is auditable
end-to-end. The `table_exists` short-circuit prevents the migration
from racing against `Base.metadata.create_all` (same defensive
pattern used by the other migrations in the file) — the freshly
created table will already have the columns via the ORM declaration
and the ALTER becomes a no-op.

### 3. Parser (`src/doc3gpp/parsers/tdoc_parser.py`)

Extend the `mapping` dict in `read_tdoc_sheet` with six new entries,
after the existing `cr_pack` entry:

```python
"tdoc_for":          pick_col(header_map, ["For"]),
"abstract":          pick_col(header_map, ["Abstract"]),
"secretary_remarks": pick_col(header_map, ["Secretary Remarks"]),
"ls_to":             pick_col(header_map, ["To"]),
"ls_cc":             pick_col(header_map, ["Cc"]),
"original_ls":       pick_col(header_map, ["Original LS"]),
```

Header detection (`_HEADER_ROW_MARKERS`) is **unchanged** — the new
column headers (`For` / `To` / `Cc` / `Original LS`) are not part of
the marker set because they appear in unrelated spreadsheets; relying
on them would invite false-positive header detection.

`pick_col` already returns `None` when the column is absent, so a
fixture without the new headers degrades to `None` per row (same
behaviour as `release` / `spec` / `cr_pack` already exhibit). Empty
cells continue to flow through `to_text` → `None`.

### 4. Scraper (`src/doc3gpp/scraping/portal_source.py`)

`fetch_tdocs_from_portal`'s `TDoc(...)` ctor grows by six kwargs:

```python
tdoc_for=row.get("tdoc_for"),
abstract=row.get("abstract"),
secretary_remarks=row.get("secretary_remarks"),
ls_to=row.get("ls_to"),
ls_cc=row.get("ls_cc"),
original_ls=row.get("original_ls"),
```

Field order matches the dataclass / ORM / migration ordering.

### 5. SQL repository (`src/doc3gpp/storage/repositories/tdoc_sql.py`)

`_copy_fields` gains six new assignments after `cr_pack`:

```python
target.tdoc_for = tdoc.tdoc_for
target.abstract = tdoc.abstract
target.secretary_remarks = tdoc.secretary_remarks
target.ls_to = tdoc.ls_to
target.ls_cc = tdoc.ls_cc
target.original_ls = tdoc.original_ls
```

`_orm_to_domain` (file-private mapper at the bottom of the module)
gains six new kwargs on its `TDoc(...)` ctor call, mirroring the
scraper forwards. (For pre-existing rows whose new columns are `None`,
the constructor's dataclass defaults already cover the case — `None`
persists through unchanged.)

`list(...)` and `list_with_meeting(...)` signatures gain six new
optional kwargs (`ls_to`, `ls_cc`, `original_ls`, `tdoc_for`,
`abstract`, `secretary_remarks`), each applied to the matching
`_apply_text_filter(stmt, TDocORM.<col>, kwarg)` call. Re-uses the
existing rich-filter plumbing (`null` / `not-null` / `!pattern` /
plain LIKE) — no new grammar.

### 6. Protocol (`src/doc3gpp/repository/protocols.py`)

`TDocRepository.list` and `TDocRepository.list_with_meeting` docstrings
gain six new lines in the filter-parameter bullet list, alongside
`release`, `version`, `cr_num`, `cr_pack`. No other Protocol method
changes — `upsert` / `upsert_many` already preserve every field the
`TDoc` carries, `get_by_id` / `get_by_ftp_url` already return the full
domain object.

### 7. Service layer (`src/doc3gpp/services/tdoc_service.py`)

`TDocService.list_recent_with_meeting` signature gains the same six
optional kwargs and forwards them to `repository.list_with_meeting`
verbatim. No new orchestration method — `sync_tdoc_list` is unchanged.

### 8. CLI (`src/doc3gpp/cli.py`)

**`tdoc list`.** Six new `--typer.Option` filter flags, declared after
`--cr-pack` and following the exact same pattern (rich-filter grammar,
docstring one-liner, `help=` text):

| Flag | Source column |
| --- | --- |
| `--ls-to` | `tdocs.ls_to` |
| `--ls-cc` | `tdocs.ls_cc` |
| `--original-ls` | `tdocs.original_ls` |
| `--for` (alias `--tdoc-for`) | `tdocs.tdoc_for` |
| `--abstract` | `tdocs.abstract` |
| `--secretary-remarks` | `tdocs.secretary_remarks` |

The six kwargs flow into `service.list_recent_with_meeting(...)` next
to `cr_pack=`, and the `logger.info(...)` summary block lists them all.

`allowed_fields` is rebuilt via `dataclass_fields(TDoc) + ["meeting_name"]`,
so the new attrs are valid `--fields` selectors automatically. The
default `out_fields` (`settings.output.fields.tdoc`) stays put — columns
are opt-in.

**`tdoc show`.** No new flag. The JSON envelope's `tdoc` block already
includes every dataclass field by virtue of `_build_show_payload` (CLI)
and `render.to_jsonable` (web) walking `dataclasses.fields(TDoc)`.

**`tdoc parse`.** Out of scope (operates on cover/TTC sidecars, not on
the `tdocs` table).

### 9. Web (`src/doc3gpp/web/`)

**`routes/tdocs.py::list_tdocs`** gets six new `Query(default=None,
alias="ls-to"|...)` parameters in the same shape as the existing
`--cr-cat` / `--cr-pack` ones. Each is parsed via `parse_text_query`
and forwarded into `service.list_recent_with_meeting(...)`.

The `filters` context dict grows six new keys (each defaults to `""`)
so the form rehydration matches the existing convention.

**`render.py`.** Extend `TDOC_COLUMN_LABELS` with six new entries:

```python
"ls_to":             "LS To",
"ls_cc":             "LS Cc",
"original_ls":       "Original LS",
"tdoc_for":          "For",
"abstract":          "Abstract",
"secretary_remarks": "Secretary Remarks",
```

`TDOC_HTML_DEFAULT_FIELDS` is **unchanged** — the new columns appear in
the HTML table only when the user explicitly passes `?fields=...`
(opt-in). `tdoc_rows(...)` already routes unknown names through
`getattr(item.tdoc, f, None)`, so no per-field code change is needed.

**`templates/tdoc_list.html`** gets six new `<input>` cells in the
existing 5-column grid form, with `name="ls-to"` /
`name="ls-cc"` / … naming that matches the route's `Query(alias=...)`
keys. Optional: a "XLSX metadata" filter group visually separated
from "CR metadata".

**`templates/tdoc_show.html`** gets a **single new `.card` panel**
("XLSX metadata") rendered when **any** of the six new fields on
`record.tdoc` is non-`None`. Inside the panel:

* `tdoc_for` / `ls_to` / `ls_cc` rendered as a `<dl>` row each (short
  single-line labels).
* `original_ls` / `abstract` / `secretary_remarks` rendered with a
  `<dl>` row whose value cell uses `<pre class="xlsx-meta-pre">…</pre>`
  (or a similar long-text style) so long secretary remarks wrap
  cleanly without breaking the grid.

The panel goes immediately after the existing TDoc-metadata block
(file header → cover → TTCN → changes → auxiliary files → **XLSX
metadata**). The single-panel layout matches the user's "Single
panel" answer.

### 10. MCP (`src/doc3gpp/web/mcp_server.py`)

* **`list_tdocs` tool.** Six new optional `Annotated[str | None,
  Field(...)]` parameters, one per filter. Each forwarded to
  `services.tdoc.list_recent_with_meeting(...)` exactly like the
  existing `cr_pack` / `release` / `cr_num` kwargs. Tool description
  extended with the six new filter names in the same sentence that
  already lists `cr_pack` etc.
* **`_TDOC_FIELDS` default** is **unchanged** — opt-in to keep the
  default MCP list output quiet (mirrors the CLI / web opt-in).
* **`get_tdoc` tool.** No code change. `render.to_jsonable(record)`
  walks every dataclass field, so the six new attrs appear in the
  JSON envelope automatically.

### 11. TOML example (`src/doc3gpp/data/doc3gpp.toml.example`)

Extend the commented `output.fields.tdoc` block with three opt-in
examples:

```toml
# tdoc = [
#   "tdoc_id", "meeting_name", "title",
#   "source", "type", "status",
#   "cr_cat", "spec", "version", "related_wis",
#   # "ftp_url" — relative path to the TDoc zip on https://www.3gpp.org/ftp/
#   # "abstract" — TL;DR pulled from the XLSX "Abstract" column
#   # "secretary_remarks" — free-form secretary annotations
#   # "original_ls" — LS origin pointer from the XLSX "Original LS" column
# ]
```

### 12. Docs / AGENTS sync

`docs/cli.md` (`tdoc list` filter-table) gains six new rows for the
new `--ls-to` / `--ls-cc` / `--original-ls` / `--for` / `--abstract` /
`--secretary-remarks` flags. `docs/architecture.md` (TDoc row schema
diagram) gets a one-line addition listing the six new columns under
`tdocs`. `docs/web-server.md` (TDoc detail page) gets a paragraph
about the new XLSX metadata panel. `AGENTS.md` "Where to look" table
stays put (no new public surface). `docs/code-map.md` stays put (no
new public symbol — every helper is reused). `docs/conventions.md`
stays put.

## Failure modes / edge cases

* **Header absent.** `pick_col` returns `None` → parser writes
  `None` per row → upsert writes `NULL` for that column → no read
  path surfaces it (omit-when-`None` convention).
* **Cell present but empty.** `to_text(None|""|"   ")` returns
  `None` → same downstream behaviour.
* **Cell present with text.** `to_text` returns trimmed string →
  upsert writes the trimmed value. Identical contract to every other
  cell capture in `read_tdoc_sheet`.
* **Existing rows (`backfill`).** Stays `NULL` until the next
  `doc3gpp tdoc sync --meeting-id <id>` runs and re-reads the XLSX.
  Operators who want immediate backfill run `doc3gpp meeting sync
  --tsg <s>` first (to refresh the upstream URL slot) then
  `doc3gpp tdoc sync --meeting-id <id>` (no `--force` because the
  skip rule allows re-runs after the interval, and the closed-window
  check is skipped on a never-synced meeting). Online fixtures
  exercised in the integration tests cover the implicit backfill
  path.
* **Corrupt XLSX.** `_extract_tdoc_hyperlinks` already degrades to an
  empty dict for any `zipfile.BadZipFile` / `KeyError` /
  `UnicodeDecodeError`. The new columns ride on openpyxl's normal
  iteration path, which raises the same exceptions as before — no
  regression.
* **MySQL / Postgres.** `mapped_column(String(64))` /
  `mapped_column(Text)` are dialect-portable; `create_schema`'s
  per-dialect branch (see `src/doc3gpp/storage/backends/`) handles
  any TEXT-size differences (mysql needs an explicit length for
  `Text` indexed columns, but these new columns carry no index). The
  `_migrate_tdocs_xlsx_metadata` migration gates on SQLite via
  `PRAGMA table_info` — for other dialects, `Base.metadata.create_all`
  on a fresh database handles the column addition (no migration
  needed); for an existing MySQL/Postgres database, a manual
  `ALTER TABLE` runs once at deploy time and the PR's migration
  helper becomes a no-op.

## Testing strategy

* **Unit (offline, fast).**
  * `tests/unit/test_tdoc_parser.py`: add three tests that build
    a 4-column × 1-row fixture with the new headers, parse it, and
    assert the new keys land in the dict. Mirror the existing
    `_extract_tdoc_hyperlinks` test style.
  * `tests/unit/test_tdoc_repository_crud.py`: assert
    `_copy_fields` writes the new fields and `_orm_to_domain`
    reads them back.
  * `tests/unit/test_tdoc_repository_filters.py`: add six new
    filter-parameter cases (one per column) showing
    `null`/`not-null`/`!pattern`/LIKE grammar works end-to-end
    against an in-memory SQLite engine.
  * `tests/unit/test_tdoc_cli_fields.py`: extend the
    `--fields`-selection test with the six new names so the CLI
    field-routing stays green.
* **Integration (offline, sqlite).**
  * New test `tests/integration/test_tdocs_xlsx_metadata_sqlite.py`:
    create a synthetic XLSX with the new columns populated,
    call `fetch_tdocs_from_portal`, run `repository.upsert_many`,
    assert the stored rows carry the six new values, exercise the
    CLI's six new filter flags through `CliRunner`, exercise the
    web route via `TestClient` with `?format=json` and the
    `partials/tdoc_results.html` form, and exercise the MCP tool
    path via the `mcp_client` if available (else a direct call to
    `services.tdoc.list_recent_with_meeting` with the new kwargs).
  * Extend `tests/integration/test_tdoc_sqlite.py` if needed to
    cover the field-walking in `TDocShowRecord.tdoc` JSON envelope.
* **Online (opt-in via `-m online`).** Re-sync an existing
  RAN5#111 fixture meeting and assert the new fields round-trip
  (see fixture `tests/fixtures/tdoc_xlsx/TDoc_List_Meeting_RAN5#111.xlsx`).
* **Migration.** A focused unit test loads a sqlite engine with
  the pre-migration schema, runs `create_schema()`, and asserts
  the six new columns land with the expected types. A second
  run asserts idempotency (no `ALTER` errors, no duplicate
  columns).

## Surface summary

| Surface | What changes |
| --- | --- |
| `TDoc` dataclass | +6 fields |
| `TDocORM` | +6 columns |
| `migrate.py` | +1 idempotent migration |
| `parsers/tdoc_parser.py` | +6 `mapping` entries |
| `scraping/portal_source.py` | +6 `TDoc(...)` kwargs |
| `repositories/tdoc_sql.py` | +6 `_copy_fields` assignments, +6 `_orm_to_domain` ctor args, +6 kwargs in `list`/`list_with_meeting` |
| `repository/protocols.py` | +6 kwargs in `list`/`list_with_meeting` docstrings |
| `services/tdoc_service.py` | +6 kwargs forward through `list_recent_with_meeting` |
| `cli.py::tdoc_list` | +6 filter flags, +6 logger fields |
| `web/routes/tdocs.py::list_tdocs` | +6 `Query` parameters, +6 `filters` context keys |
| `web/render.py` | +6 `TDOC_COLUMN_LABELS` entries |
| `web/templates/tdoc_list.html` | +6 form input cells (opt-in via 5-column grid) |
| `web/templates/tdoc_show.html` | +1 "XLSX metadata" panel |
| `web/mcp_server.py::list_tdocs` | +6 kwargs forwarded, description extended |
| `data/doc3gpp.toml.example` | +3 commented opt-ins |
| `docs/cli.md`, `docs/architecture.md`, `docs/web-server.md` | one-liner additions |
