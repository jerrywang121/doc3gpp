# Cover-page `Summary of change` extraction

**Status:** Draft
**Date:** 2026-08-14
**Branch:** main
**Target:** parser → ORM → repository → CLI renderers (by-id +
by-url, table / json / markdown) → web template → web JSON
envelope → MCP `get_tdoc` → FTS5 `cover_text` → semantic
embedding

## Problem

3GPP CR cover pages carry an optional `| Summary of change: | … |` row
between `Reason for change:` and `Consequences if not approved:`. Real
example (3GPP TSG-SA1 #115, S1-263274, 22.011 CR 0382 rev -, current
version 19.6.0, Rel-21, B):

```
|     Reason for change: | When a network service fails, particularly a failure in the core network ... |
|     Summary of change: | A new clause is added specifying that the system can make service outage awareness information available to the UE, subject to operator policy, and specifying what information can be conveyed. |
|     Consequences if not approved: | The UE remains unable to distinguish a network service outage from other problems. |
```

The CR cover-page parser
(`src/doc3gpp/parsers/cr/cover_page.py::CRCoverPageParser`) does **not**
match this row. Every other narrative cell on the cover page is
extracted: `reason_for_change`, `consequences_if_not_approved`,
`clauses_affected`, `other_comments`, `revision_history`. The
`tdoc_cr_cover_page` table therefore has no column for `summary_of
change`, so `doc3gpp tdoc show`, the FTS5 `cover_text` projection, and
the semantic embedding path all miss this short, free-text description
of what the CR actually does.

The existing parser does parse a `Summary of change` label **inside the
TTCN body-change corrections table** (per-function summary in
`src/doc3gpp/parsers/cr/ttcn_sections.py`). That is a different cell on
a different page of the same document and is unaffected by this
change.

## Goal

Add `summary_of_change` as a first-class cover-page field: parse the
row, persist it on `tdoc_cr_cover_page`, and surface it in every read
path that already exposes the other narrative cover-page fields, with
the same omit-when-`None` contract.

Out of scope:

* Re-parsing pre-existing cover-page rows to backfill the column
  (matches the precedent for every other column added to this table —
  e.g. `_migrate_spec_rapporteurs` does not backfill). Rows stay
  `NULL` until the operator re-runs `doc3gpp tdoc parse --tdoc <id>`
  (or any filter that covers them).
* Changing the cover-page row order in the rendered output.
* Changing the slim embed-text projection to mirror the full FTS5
  projection.

## Design

### 1. Parser

**`src/doc3gpp/parsers/cr/cover_page.py`** — add one regex + one
`patterns` entry, matching the existing pattern of every other
optional narrative cell.

```python
_COVER_SUMMARY_RE = re.compile(
    r"\|\s*Summary of change:(?:\s*\|)+\s*(.*?)\s*\|",
    re.IGNORECASE,
)
```

Add to `CRCoverPageParser.parse`'s `patterns` list, marked `optional=True`
(analogous to `_COVER_CLAUSES_RE` / `_COVER_OTHER_RE` /
`_COVER_REVHIST_RE`):

```python
(True, ["summary_of_change"], [1], _COVER_SUMMARY_RE),
```

Add `summary_of_change` to the `max_text_length` truncation list
inside the same `parse` method so a runaway cell gets truncated like
its peers (`reason_for_change`, `consequences_if_not_approved`,
`other_comments`, `revision_history`).

### 2. Helpers

**`src/doc3gpp/parsers/cr/helpers.py`** — append `"summary_of_change"`
to `_COVER_FIELDS` so the existing `for key in _COVER_FIELDS` loop in
`CRParserBase.parse` picks the new key up without further edits.

### 3. Domain model

**`src/doc3gpp/models/tdoc_cr.py`** — add `summary_of_change: str |
None = None` to `TDocCRDetails`, with a matching docstring entry
between `reason_for_change` and `consequences_if_not_approved`. Include
it in `to_persisted()` in the same position.

### 4. ORM

**`src/doc3gpp/storage/db/models.py`** — add `summary_of_change:
Mapped[str | None] = mapped_column(Text, nullable=True)` on
`TDocCrDetailOrm`, mirroring `reason_for_change` / `clauses_affected`.
No index — the column is a free-text narrative field, never queried
directly.

### 5. Migration

**`src/doc3gpp/storage/db/migrate.py`** — add one helper:

```python
def _migrate_tdoc_cr_cover_page_summary_of_change() -> None:
    """Add ``tdoc_cr_cover_page.summary_of_change`` to databases created
    before that column existed. Idempotent: probe
    ``PRAGMA table_info`` first (same pattern as
    :func:`_migrate_spec_rapporteurs`)."""
```

Wire it into `create_schema()` next to the existing `_migrate_*` calls.
No backfill pass — pre-existing rows stay `NULL` until re-parsed.

### 6. Repository

**`src/doc3gpp/storage/repositories/tdoc_cr_sql.py`** — add the new
field to both `_details_to_orm` and `_orm_to_details`. No change to
`upsert` / `get_by_url` / `get` — they already operate on
`TDocCRDetails`, so the column flows through automatically.

### 7. CLI table renderer

**`src/doc3gpp/cli.py::_render_tdoc_show_table_body`** — insert one
line in the `[Extracted Details]` block between `reason_for_change`
and `consequences_if_not_approved`, using `_truncate_for_display`
(same helper used by the two neighbouring rows):

```python
stream.write(
    "summary_of_change: "
    f"{_truncate_for_display(details.summary_of_change)}\n"
)
```

### 8. CLI direct-mode field list

**`src/doc3gpp/cli.py::_DIRECT_PARSE_FIELDS`** — insert
`"summary_of_change"` next to `reason_for_change` /
`consequences_if_not_approved` so direct-mode table output carries the
field.

### 9. JSON / Markdown renderers

No change. Both `_render_tdoc_show_json` (`_build_show_payload` →
`dataclasses.asdict`) and `_render_tdoc_show_markdown_full` /
`_render_tdoc_show_markdown_compact` iterate `dataclass_fields(record.cover)`
and emit every field. The new field flows through automatically.

### 9a. CLI by-URL show path

**`doc3gpp tdoc show --ftp-url <url>`** (`_tdoc_show_by_ftp_url` →
`TDocShowRecordByUrl.from_ftp_url`) — no code change. The by-url
record carries `cover: TDocCRDetails | None`, the table / JSON /
markdown renderers are shared with the by-id path, and both already
iterate dataclass fields. `summary_of_change` surfaces the same way
it does on the by-id path. Worth a unit test asserting the field
appears in `_render_tdoc_show_by_ftp_url_table` /
`_render_tdoc_show_by_ftp_url_json` /
`_render_tdoc_show_by_ftp_url_markdown` when the cover row exists.

### 10. Web template

**`src/doc3gpp/web/templates/tdoc_show.html`** — insert inside the
`{% if record.cover %}` block between the existing `Reason for
change` / `Consequences if not approved` rows:

```html
<dt>Summary of change</dt><dd>{{ record.cover.summary_of_change or '-' }}</dd>
```

Matches the source CR's natural row order and the placeholder
convention already used by every other Cover-card `<dd>`.

The web app has no by-URL show route (only the by-id
`GET /tdocs/{tdoc_id}`), so no second template edit is needed.

### 10a. Web JSON envelope

**`GET /tdocs/{tdoc_id}?format=json`** (`src/doc3gpp/web/routes/tdocs.py::show_tdoc`)
— no code change. The route delegates to `to_jsonable(record)`, which
iterates dataclass fields. `summary_of_change` appears as
`cover.summary_of_change` in the JSON payload.

### 10b. MCP `get_tdoc` tool

**`src/doc3gpp/web/mcp_server.py::get_tdoc`** — no code change. The
tool composes a `TDocShowRecord` via `from_tdoc_id` and returns
`render.to_jsonable(record)` as JSON, byte-identical to the CLI /
web JSON paths. The `description` string and tool schema carry no
field-level typing (the schema describes a single `tdoc_id`
parameter), so no MCP schema change is required. Worth a test in
`tests/integration/test_mcp_end_to_end.py` asserting
`cover.summary_of_change` appears in the response when a parsed row
exists.

The MCP `parse_tdoc_url` / `parse_tdoc` tools enqueue a
`PARSE_TDOC_URL` / `PARSE_TDOC` job that runs the same `TDocCrService`
pipeline; the new column flows through the same upsert path with no
MCP-server-level change.

### 11. FTS5 `cover_text`

### 11. FTS5 `cover_text`

**`src/doc3gpp/storage/repositories/search_sql.py::_cover_text`** —
add `summary_of_change` to the `SELECT` projection so a rebuilt index
makes the field searchable. No other change; the column will land in
the joined space-separated string via the existing
`" ".join(str(v) for v in row if v is not None)` call.

### 12. Semantic embedding

**`src/doc3gpp/storage/repositories/vector_sql.py::_build_embed_text`**
— extend the existing `SELECT title FROM tdoc_cr_cover_page WHERE
tdoc_id = :id` to also pull `summary_of_change`, append the value to
`parts` (same pattern as `cover[0]`), so a fresh embedding reflects
the new field. Embeddings for already-indexed rows update on the next
`doc3gpp search index --rebuild-embeddings --stale-only` (or any
incremental rebuild trigger).

## Data flow

```
docx bytes
  → python-docx → markdown
  → CRCoverPageParser.parse  →  details["summary_of_change"]
  → CRParserBase.parse        →  TDocCRDetails(summary_of_change=...)
  → TDocCrService.upsert cover → TDocCrRepository._details_to_orm
  → tdoc_cr_cover_page.summary_of_change (TEXT, NULLABLE)
  → _cover_text() rebuild  →  cover_text in tdoc_search FTS5
  → _build_embed_text() rebuild → chunk embeddings in vec_tdoc_embeddings
  → tdoc show (table/json/markdown, both --tdoc and --ftp-url),
    web /tdocs/{id} (HTML + JSON), MCP get_tdoc
```

## Error handling / edge cases

| Case | Behaviour |
| --- | --- |
| `Summary of change` row absent in source | Optional match, no warning, field stays `None`, column stays `NULL`. Mirrors `clauses_affected` / `other_comments` / `revision_history`. |
| Row present but blank | `_blank_cells_to_none` coerces to `None`; column `NULL`. |
| Pre-existing rows | Migration adds column as `NULL`. Operator re-runs `tdoc parse` to populate; matches the precedent for every other column added to this table. |
| Over-long cell | Truncated to `max_text_length` alongside `reason_for_change` / `consequences_if_not_approved` / `other_comments` / `revision_history`. |
| Different label shape (e.g. `Summary of Change:` casing, leading/trailing whitespace) | Handled by `re.IGNORECASE` on the regex and `match.group(1).strip()` plus the trailing-`|` cleanup in `_search_pattern_in_lines`. |
| Old TTCN `Summary of change` row inside the corrections table | Unaffected — that regex lives in `src/doc3gpp/parsers/cr/ttcn_sections.py`, not in `cover_page.py`. |

## Testing

### Unit

* **`tests/unit/test_cr_parser.py`** — three new tests:
  * With a `_HEADER_WITH_SUMMARY` fixture mirroring `_HEADER_LINES`
    plus a `| Summary of change: | A new clause … |` row, assert
    `parsed.cover.summary_of_change == "A new clause …"`.
  * Without the row, assert `parsed.cover.summary_of_change is None`
    and no warning fires.
  * With a blank `| Summary of change: | |` row, assert the field
    is `None` (post-`_blank_cells_to_none`).
* **`tests/unit/test_tdoc_cr_model.py`** — extend the
  `to_persisted()` shape test to assert `summary_of_change` lands in
  the returned dict.
* **`tests/unit/test_compact_helpers.py`** or a new
  `tests/unit/test_tdoc_show_renderers.py` — feed a record with
  `summary_of_change` set; assert the field surfaces in
  `_render_tdoc_show_table_body`, `_render_tdoc_show_json`, and
  `_render_tdoc_show_markdown_full`. Assert absence renders the
  field as `null` in JSON (matching the existing
  `reason_for_change` / `clauses_affected` contract — the
  cover block is a flat dataclass dump, not an omit-when-null
  shape), and skips the line in compact markdown.
* **`tests/unit/test_web_routes.py`** — assert the field renders as
  `<dt>Summary of change</dt><dd>{{ record.cover.summary_of_change
  or '-' }}</dd>` inside the Cover card (next to the existing TTCN
  `Summary of change` test on line 2594, which targets a different
  template branch). Also assert `GET /tdocs/{id}?format=json`
  surfaces `cover.summary_of_change`.
* **By-url CLI** (`tests/unit/test_compact_helpers.py` or new
  `tests/unit/test_tdoc_show_renderers.py`) — feed a
  `TDocShowRecordByUrl` with `cover.summary_of_change` set; assert
  the field surfaces in
  `_render_tdoc_show_by_ftp_url_table`,
  `_render_tdoc_show_by_ftp_url_json`, and
  `_render_tdoc_show_by_ftp_url_markdown`.
* **MCP** (`tests/integration/test_mcp_end_to_end.py`) — call the
  `get_tdoc` tool against a TDoc whose cover row carries
  `summary_of_change`; assert the JSON envelope contains
  `cover.summary_of_change` with the expected value.

### Integration

* **Migration** — sqlite integration test that calls
  `create_schema()` twice on a database that already carries
  `tdoc_cr_cover_page` without the column; assert the column exists
  after the first call and is unchanged after the second (idempotent).
* **Round-trip** — insert a `TDocCRDetails` with
  `summary_of_change="…"` and a second one with
  `summary_of_change=None`; assert both round-trip via `get_by_url`.
* **FTS5 + embed** — after re-indexing via
  `search.rebuild(...)` / `semantic_search.rebuild_embeddings(...)`,
  assert the rebuilt `tdoc_search.cover_text` contains the
  `summary_of_change` substring and that an embedding rebuilt for
  that `tdoc_id` differs from the pre-change baseline.

## Documentation

* `docs/3gpp-knowledge.md` §"TTCN CR sidecar fields" — no change
  (that table covers the TTCN sidecar, not the cover-page row;
  `summary_of_change` is a cover-page field).
* `docs/cli.md` — no change (no new flag; the column surfaces
  automatically in `tdoc show` output).
* `README.md` — no change.
* `AGENTS.md` — no change (no new code path; existing parser/repo
  pipeline absorbs the column).

## Risks / non-goals

* **Backfill cost** — large pre-existing `tdoc_cr_cover_page` corpora
  will keep their `summary_of_change` as `NULL` until the operator
  re-runs `tdoc parse` for each TDoc. This is intentional and matches
  the precedent for every other column added to this table. Operators
  wanting to backfill en masse can run
  `doc3gpp tdoc parse --from-url …` per row or iterate via a single
  filter (`doc3gpp tdoc parse --meeting-id <id>` already covers an
  entire meeting).
* **Embedding invalidation** — the slim embed-text projection changes
  shape for any `tdoc_id` whose row is re-indexed. This invalidates
  existing embeddings; the next `rebuild_embeddings` pass picks it up.
  Same blast radius as any embed-text change.
* **FTS5 schema** — no DDL change; FTS5 stores
  `tdoc_cr_cover_page.summary_of_change` as part of the existing
  `cover_text` projection via a `SELECT` rather than a schema
  rewrite. Pre-existing FTS5 rows stay unchanged until rebuilt.

## Open questions

None — user approved full parity, schema-migration-only backfill, and
appending `summary_of_change` to the existing slim embed-text SELECT.
