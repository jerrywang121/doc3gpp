# TDoc CR Body Changes — Design Spec

**Status:** Draft (pending user review)
**Date:** 2026-07-29
**Branch:** main
**Author:** brainstorming session

## Goal

Extract the list of clauses and the list of change blocks from the
*body* of a non-TTCN TDoc CR (the lines after the cover-page table)
and persist them in a new sidecar table `tdoc_cr_change_details`,
keyed on the same `ftp_url` already used by `tdoc_cr_cover_page`,
`tdoc_cr_ttcn_details`, and `tdoc_extracts`.

The body of a TDoc CR contains `<w:ins>` / `<w:del>` revision marks
emitted by the CR author in MS Word. The docx converter already
flattens these into the converted markdown as
`<ins>[Inserted: <content>]</ins>` and
`<del>[Deleted: <content>]</del>` spans (one span per line). The
new body extractor scans the converted markdown line by line, finds
runs of lines that contain revision marks, captures each run plus a
configurable number of plain context lines on each side, and
records:

- **`clauses`** — the most recent heading number (e.g. `5.2.3`) and
  the most recent table number (e.g. `5.2.3-1`) observed immediately
  before and inside the captured change block. Newline-delimited text.
- **`changes`** — a gzip-JSON array of captured change blocks, each
  block being a list of original markdown lines (the gap-window
  bridge lines + `context_padding` plain lines on each side).

The extraction is fully automatic and runs as part of the existing
`tdoc parse` pipeline, in the same fan-out step that already writes
the `tdoc_cr_cover_page` and `tdoc_cr_ttcn_details` sidecars.

## Non-goals

- TTCN CRs — they write the `tdoc_cr_ttcn_details` sidecar instead.
  The body extractor is registered only on the base `CRParser`, not on
  `TTCNCRParser`.
- Re-parsing revision marks that live inside the cover-page table.
  These are exceedingly rare in real CRs and treating them as part of
  the cover-page row avoids duplicating clause numbers; if a future
  use-case needs them, the body-start line index can be re-tuned.
- Cross-block deduplication. Two adjacent change blocks separated by
  more than `gap_window` plain lines are kept as two entries even when
  their captured context overlaps.
- Per-clause query surface. The new table is a 1:1 sidecar with the
  same `ftp_url` PK; consumers that want per-clause search filter the
  JSON-decoded `changes` array client-side. A future change can add
  a child table if query patterns demand it.
- Any change to the existing `clauses_affected` column on
  `tdoc_cr_cover_page`. That field is the source-of-truth clause list
  authored by the CR author; the new table is body-derived and lives
  alongside, not in place of, the cover row.

## Architecture

The body extractor is a new function
`extract_body_changes` in `src/doc3gpp/parsers/cr/body_changes.py`
that consumes the full converted markdown line list and returns a
`TDocCRChangeDetails` domain object. We deliberately run on the
*whole* line list (no `body_start` offset) — the cover-page table
rows in real CRs do not contain `<w:ins>` / `<w:del>` revision marks,
and the heading/table-number trackers reset on every new heading
anyway, so any spurious matches inside the cover page are harmless.
This keeps the cover-page parser signature unchanged.

```
                   ┌──────────────────────────┐
                   │ docx_converter (existing)│
                   │  .docx  →  markdown      │
                   └──────────────┬───────────┘
                                  │ lines
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │  CRParserBase.parse(lines)                      │
        │                                                 │
        │  ┌────────────────────────┐                     │
        │  │ CRCoverPageParser      │──> TDocCRDetails    │
        │  │  + records body_start  │    + body_start idx │
        │  └───────────┬────────────┘                     │
        │              │                                  │
        │              ▼                                  │
        │  ┌────────────────────────┐                     │
        │  │ BodyChangeExtractor    │──> TDocCRChangeDetails
        │  │  (only on base CRParser;                      │
        │  │   TTCNCRParser skips)   │                     │
        │  └───────────┬────────────┘                     │
        │              │                                  │
        │              ▼                                  │
        │  ┌────────────────────────┐                     │
        │  │ SectionParser list     │──> TDocCRTTCNDetails│
        │  │  (TTCN only)           │    (or None)        │
        │  └────────────────────────┘                     │
        └─────────────────┬───────────────────────────────┘
                          │ TDocCRParseResult(cover, ttcn, changes)
                          ▼
                ┌─────────────────────────┐
                │  TDocCrService.extract  │  (fan-out: 3 or 4 rows)
                │  • tdoc_cr_cover_page   │  always
                │  • tdoc_cr_ttcn_details │  TTCN only
                │  • tdoc_cr_change_details │ non-TTCN only
                │  • tdoc_extracts        │  always
                └─────────────────────────┘
```

The body extractor is invoked from `CRParserBase.parse` only when
`Settings.tdoc_parse.body_change_enabled` is `True`. When `False`,
`TDocCRParseResult.changes` is always `None` and the
`tdoc_cr_change_details` row is never written — same convention as
the `tdoc_cr_ttcn_details` row being omitted for non-TTCN CRs.

## Data model

### New table: `tdoc_cr_change_details`

ORM: `src/doc3gpp/storage/db/models.py`
Migration: bootstrap via `create_schema` (no Alembic wiring).

| Column      | Type           | Constraints                                   |
|-------------|----------------|-----------------------------------------------|
| `ftp_url`   | `String(1024)` | PRIMARY KEY, FK → `tdoc_cr_cover_page.ftp_url` ON DELETE CASCADE |
| `tdoc_id`   | `String(64)`   | NOT NULL, FK → `tdocs.tdoc_id` ON DELETE CASCADE |
| `clauses`   | `Text`         | NOT NULL DEFAULT `''`. Newline-delimited, sorted, unique. |
| `changes`   | `LargeBinary`  | NOT NULL. gzip-compressed JSON of `list[dict]`. |

Notes:

- The `tdoc_id` FK is a denormalised convenience for queries that
  don't want to JOIN through `tdoc_cr_cover_page`. It is set in the
  same upsert.
- `extracted_at` is **not** stored on this table. The
  `tdoc_extracts` row at the same `ftp_url` is the single source of
  truth for extraction timestamps. The read-side renderers already
  JOIN `tdoc_extracts` to surface `extracted_at`.
- The `clauses` text column uses the same newline-delimited
  convention as `tdoc_cr_ttcn_details.changed_functions` (sorted,
  unique, `LIKE`-searchable). Each entry is a single clause number
  such as `5.2.3` or `5.2.3-1`.
- The `changes` column reuses the
  `storage.compression.dump_json_gz` / `load_json_gz` helpers (same
  pattern as `tdoc_cr_ttcn_details.required_changes`).

### New domain dataclass: `TDocCRChangeDetails`

File: `src/doc3gpp/models/tdoc_cr_change_details.py`

```python
@dataclass(slots=True, frozen=True)
class TDocCRChangeDetails:
    ftp_url: str
    tdoc_id: str
    clauses: tuple[str, ...] = ()
    changes: tuple[tuple[str, ...], ...] = ()
```

- `clauses` is a sorted, unique tuple of clause numbers — one entry
  per heading or table number observed anywhere in the body that
  belongs to a captured change block. Stored as a newline-delimited
  text column on the table; reconstructed by `splitlines()` on read.
- `changes` is a tuple of one block per change block. Each block is
  itself a tuple of the captured markdown lines (gap-window bridge
  lines + `context_padding` plain context lines on each side). The
  outer structure round-trips as gzip-JSON.

### New Protocol: `TDocCRChangeDetailsRepository`

File: `src/doc3gpp/repository/protocols.py`

```python
class TDocCRChangeDetailsRepository(Protocol):
    def upsert(self, details: TDocCRChangeDetails) -> None: ...
    def get_by_url(self, ftp_url: str) -> TDocCRChangeDetails | None: ...
    def get_for_tdoc_id(self, tdoc_id: str) -> list[TDocCRChangeDetails]: ...
```

Concrete impl: `SQLAlchemyTDocCRChangeDetailsRepository` in
`src/doc3gpp/storage/repositories/tdoc_cr_change_details_sql.py`,
mirroring the structure of `tdoc_cr_ttcn_details_sql.py`.

## Scanning algorithm

`src/doc3gpp/parsers/cr/body_changes.py::extract_body_changes`.
Pseudocode:

```
def extract_body_changes(
    lines: list[str],
    *,
    gap_window: int = 2,
    context_padding: int = 2,
) -> TDocCRChangeDetails:
    """Returns a TDocCRChangeDetails with empty clauses/changes when
    the body has no revision marks."""
    all_clauses: set[str] = set()
    blocks: list[list[str]] = []
    last_heading: str | None = None
    last_table: str | None = None

    # State machine over the line list.
    # i is the index of the current marker line. We bridge plain
    # lines between two marker lines so long as the gap is <=
    # gap_window. A heading or table row always terminates the
    # current block.
    pending_lines: list[str] = []   # bridge lines for the current run
    run_start: int | None = None    # first marker-line index of run
    run_end: int | None = None      # last index of the active run
    run_gap_remaining: int = 0      # how many plain lines we tolerate
    block_clauses: list[str] = []   # clauses seen during current run
    block_pre_clauses: list[str] = []  # last heading/table before run

    def flush() -> None:
        nonlocal pending_lines, run_start, run_end, run_gap_remaining
        nonlocal block_clauses, block_pre_clauses
        if run_start is None:
            return
        # Capture the run slice from the *original* lines, padded by
        # context_padding on each side. pending_lines is only used for
        # gap-bridging decisions; the captured block is the literal
        # lines[run_start : run_end + 1] extended.
        start_ctx = max(0, run_start - context_padding)
        end_ctx = min(len(lines), run_end + 1 + context_padding)
        captured_lines = lines[start_ctx:end_ctx]
        blocks.append(captured_lines)
        for c in block_pre_clauses + block_clauses:
            all_clauses.add(c)
        # reset
        run_start = None
        run_end = None
        run_gap_remaining = 0
        pending_lines = []
        block_clauses = []
        block_pre_clauses = [c for c in (last_heading, last_table) if c]

    for i, line in enumerate(lines):
        # 1. Heading / table-number tracking.
        heading = match_heading(line)        # e.g. "## 5.2.3 ..." -> "5.2.3"
        if heading is not None:
            last_heading = heading
            if run_start is not None:
                # Heading terminates the current run.
                block_pre_clauses = [last_heading]  # for next block
                flush()
            continue
        table_no = match_table_number(line)  # e.g. "Table 5.2.3-1: ..."
        if table_no is not None:
            last_table = table_no
            # Table-number lines don't terminate a run (a table is
            # where the change usually lives); we just record.
            if run_start is not None:
                block_clauses.append(table_no)
            continue

        # 2. Marker-line test.
        if is_marker_line(line):  # contains <ins> or <del>
            if run_start is None:
                run_start = i
                block_pre_clauses = [c for c in (last_heading, last_table) if c]
            run_end = i
            if last_heading is not None:
                block_clauses.append(last_heading)
            if last_table is not None:
                block_clauses.append(last_table)
            run_gap_remaining = gap_window + 1   # reset
            pending_lines = []
        else:
            # Plain line. If we're inside a run, consume one slot of
            # the gap window; otherwise pass through.
            if run_start is not None and run_gap_remaining > 0:
                pending_lines.append(line)
                run_gap_remaining -= 1
            elif run_start is not None:
                # Gap exceeded; close the run.
                flush()

    flush()  # tail

    return TDocCRChangeDetails(
        ftp_url="",    # filled by caller
        tdoc_id="",    # filled by caller
        clauses=tuple(sorted(all_clauses)),
        changes=tuple(tuple(b) for b in blocks),
    )
```

Key invariants:

- A `## 5.2.3 ...` heading always terminates the current run and
  resets `last_heading`.
- A `Table 5.2.3-1:` line does **not** terminate the run (the change
  is usually inside the table); it just updates `last_table` and, if
  a run is active, appends `last_table` to the current block's
  clauses.
- `block_pre_clauses` is the `(last_heading, last_table)` tuple as of
  the last heading/table update *before* `run_start`. It is recorded
  once, when the run begins.
- `context_padding` is applied to the *captured* slice around the
  marker-line range (including the bridge lines), not to the
  pending_lines array.

### Regex shapes

- Heading number: `r"^#{1,6}\s+(\d+(?:\.\d+){0,4})(?:-(\d+))?\b"`
  captures dotted path with optional trailing `-N` (e.g. `5.2.3` or
  `5.2.3-1`). The full heading text is preserved separately so the
  captured change block can quote it.
- Table number: `r"^Table\s+(\d+(?:\.\d+){0,4}(?:-\d+)?)\b[:.]"`
- Marker line: `r"<(ins|del)\[|\[(Inserted|Deleted):"` — matches both
  the `<ins>` / `<del>` form and the bracketed fallback form that
  the docx converter already emits.

## Service layer

`src/doc3gpp/services/tdoc_cr_service.py` `extract(...)` (currently
fans out to cover / ttcn / extracts) gains a fourth sidecar:

```python
# After existing tdoc_cr_cover_page + tdoc_cr_ttcn_details upserts:
if result.changes is not None and tdoc_row is not None:
    details = dataclasses.replace(
        result.changes,
        ftp_url=tdoc_row.ftp_url,
        tdoc_id=tdoc_row.tdoc_id,
    )
    self._change_details_repo.upsert(details)
```

`TDocCrService` is constructed with a new
`change_details_repo: TDocCRChangeDetailsRepository` parameter. The
`factory.build_tdoc_cr_service` helper is updated to inject the
SQL impl by default. Tests construct the service with an in-memory
fake alongside the existing `cover_repo` and `cr_ttcn_repo` fakes.

The `extract(...)` method only writes the new row when:

1. `result.changes is not None` (the body extractor ran and produced
   a value — base CR parser only, with `body_change_enabled=True`).
2. `tdoc_row is not None` (FK anchor exists; the URL must be in the
   `tdocs` table for the FK to validate).
3. Either `clauses` or `changes` is non-empty (i.e. the body
   contained at least one revision mark). Empty results still write
   a row with empty tuples, for symmetry with
   `tdoc_cr_ttcn_details.required_changes` behaviour.

## Settings

File: `src/doc3gpp/settings/schema.py`, `TDocParseSettings` class
(currently at line 212).

Three new fields:

```python
body_change_enabled: bool = Field(
    default=True,
    description=(
        "Run the body-change extractor on non-TTCN CRs and persist "
        "the result to tdoc_cr_change_details. Disable to skip the "
        "extraction step entirely."
    ),
)
body_change_gap_window: int = Field(
    default=2,
    ge=0,
    le=20,
    description=(
        "Max number of plain (non-marker) lines that may sit between "
        "two <ins>/<del> lines and still count as the same change "
        "block. 0 = only consecutive marker lines count."
    ),
)
body_change_context_padding: int = Field(
    default=2,
    ge=0,
    le=50,
    description=(
        "Plain context lines captured before and after each change "
        "block. 0 = no context, only the marker lines + bridge."
    ),
)
```

Wired into the service via
`Settings.tdoc_parse.body_change_*` reads inside
`factory.build_tdoc_cr_service`. No new env vars; the existing
`DOC3GPP_*` env-var precedence applies.

`doc3gpp.toml.example` gets a `[tdoc_parse]` block documenting each
new field with its default.

## CLI surface

No new command. `tdoc show --tdoc <id>` and `tdoc show --ftp-url <url>`
gain a `## Change Details` / `## FTP URL Change Details` block in
their output, in the same omit-when-null convention as the existing
TTCN sidecar.

### DTO change

`TDocShowRecord` and `TDocShowRecordByUrl` in
`src/doc3gpp/cli.py` (lines 2186 and 2222) gain a new field:

```python
changes: TDocCRChangeDetails | None = None
```

The `tdoc show` resolver queries
`change_details_repo.get_for_tdoc_id(tdoc.tdoc_id)` /
`get_by_url(ftp_url)` alongside the existing TTCN lookup, and only
populates `record.changes` when the returned dataclass is non-None.

### Renderers

- **JSON** (`_render_tdoc_show_json` and the by-url twin): when
  `record.changes is not None`, emit a `changes` key. The dataclass
  round-trips via `dataclasses.asdict`. In `compact=True` mode, the
  inner `changes` list (list of blocks, each list of lines) is
  serialised as a single line; no truncation.
- **Markdown** (`_render_tdoc_show_markdown`): when
  `record.changes is not None`, emit a `## Change Details` block
  with `clauses` as a JSON fenced array and `changes` as a fenced
  block per change block (preserves the captured markdown verbatim
  including the `<ins>` / `<del>` markers). Compact mode drops
  fences and bullets, one `line:` per captured line.
- **Table** (`_render_tdoc_show_table` and by-url twin): when
  `record.changes is not None`, emit a `[Change Details]` block with
  a `clauses: <count> clause(s)` line and a
  `changes: <count> change block(s)` line, followed by an indented
  `- block <i>:` per block (no per-line expansion — that would
  blow up the line-oriented table).

`compact=True` honoured across all three renderers using the
existing `_serialise_show_value` helpers.

## Testing

### Unit tests (`tests/unit/`)

- `test_body_changes_extractor.py`
  - Synthetic markdown with one heading, one table, two consecutive
    `<ins>`/`<del>` lines → one block, clauses `{5.2.3, 5.2.3-1}`.
  - Two `<ins>` lines separated by 3 plain lines with default
    `gap_window=2` → two blocks.
  - Two `<ins>` lines separated by 2 plain lines with default
    `gap_window=2` → one block.
  - Heading between two marker lines → heading terminates the
    first block; second block uses the new heading.
  - `context_padding=0` → captured block is exactly the marker
    lines + bridge, no plain context.
  - Empty body → `TDocCRChangeDetails(ftp_url="", clauses=(),
    changes=())`.
- `test_body_changes_orm.py`
  - `upsert` overwrites the previous row (no duplicate PKs).
  - `get_by_url` round-trips a `TDocCRChangeDetails` with non-empty
    `clauses` and `changes` losslessly.
  - `get_for_tdoc_id` returns multiple rows when called on a
    `tdoc_id` that has been re-extracted with a different `ftp_url`.
  - Cascade delete via `tdocs.tdoc_id` removes the row.

### Integration tests (`tests/integration/`)

- `test_tdoc_cr_change_details_sqlite.py`
  - End-to-end: load a real CR zip from `tests/fixtures/tdoc_cr_doc/`,
    call `TDocCrService.extract` via the Typer CLI, assert the
    `tdoc_cr_change_details` row exists with non-empty `clauses`
    and `changes`.
  - Re-run with `--force` and assert the row is replaced.
  - Run with `body_change_enabled=False` and assert no row is
    written.
  - `tdoc show --tdoc <id>` JSON output contains the new `changes`
    key with the expected block count.
- Snapshot tests for the JSON / Markdown / Table renderers when
  `record.changes` is `None` (no change in output), non-None with
  empty tuples (placeholder), and non-None with populated tuples
  (full block).

## Documentation sync

`docs/architecture.md` — add `tdoc_cr_change_details` to the ORM
schema section. Mention the body extractor in the
"`doc3gpp tdoc parse` workflow" line.

`docs/code-map.md` — add the new files
(`parsers/cr/body_changes.py`,
`storage/repositories/tdoc_cr_change_details_sql.py`,
`models/tdoc_cr_change_details.py`).

`docs/cli.md` — update the `tdoc show` reference to mention the
`## Change Details` block, the new `[Change Details]` table block,
and the new JSON `changes` key.

`doc3gpp.toml.example` — document the three new `[tdoc_parse]`
fields with their defaults.

`AGENTS.md` — extend the "Where to look" table with a row for the
body extractor, and update the `tdoc parse` workflow bullet to
include the body extractor step.
