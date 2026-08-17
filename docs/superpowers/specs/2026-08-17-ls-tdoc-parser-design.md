# LS TDoc parser — design

**Status:** approved
**Date:** 2026-08-17
**Branch target:** `feat/ls-tdoc-parser`

## Background

`doc3gpp` today parses two TDoc shapes — generic CR and TTCN CR — via
`parsers/cr/`. 3GPP meetings also produce **Liaison Statements** (LS
documents), written to the same `tdocs` table with `tdoc_type='LS'` but
no parser ever fires for them. `tdoc parse` falls through to `CRParser`,
which raises `CRHeaderMissingError` because LS files do not carry the
`| CHANGE REQUEST |` header.

The goal of this change is to add a dedicated LS parser that extracts the
structured header fields from the [3GPP LS
template](https://www.3gpp.org/ftp/Information/All_Templates/LS_Template.zip)
into a new `tdoc_cr_ls_details` sidecar table, surfaces them through
`tdoc show`, the web UI, MCP, and the FTS5 + vector search pipelines.

## Goals (in scope)

1. Detect LS TDoc markdown via header shape (no reliance on
   `| CHANGE REQUEST |`).
2. Extract the eleven structured header fields defined below into a new
   sidecar table.
3. Wire `tdoc parse` so the new parser is auto-selected for rows with
   `tdoc_type='LS'` (no new flag).
4. Surface the extracted fields via `tdoc show` (table + JSON + Markdown),
   the web TDoc detail page (new `LS Cover` card), and the MCP
   `get_tdoc` tool.
5. Project LS title / response_to_title / to_groups / cc_groups into the
   existing FTS5 `cover_text` column and the semantic embed-text
   projection so LS rows are searchable.
6. Unit + integration tests, including a synthesized LS fixture
   generated from the official template.
7. **Variant framework**: the design accommodates LS documents from
   non-3GPP sources (e.g. IEEE, ETSI, OARF, individual companies) that
   follow different templates. Variant selection is driven by
   `tdocs.source` (the company / organisation / group / standards-body
   that submitted the TDoc). v1 ships the 3GPP variant only; the
   framework is the only requirement.

## Non-goals (out of scope for v1)

- Freeform body extraction (`§1 Overall description`, `§2 Actions`,
  `§3 Next meetings`) — these are prose paragraphs, not structured
  fields; storing them would bloat the row and complicate search.
- Contact person / email / phone — PII risk; not useful for filtering.
- Re-rendering the LS as a new DOCX — we already cache the source bytes.
- A general "doc-type plugin" framework — we only add LS for now.
- TSG / WG / meeting / TDoc number — already on the parent `tdocs` row
  and the joined `meetings` row.

## Field model

Eleven fields extracted from the LS template header (`LS-Template-130.docx`,
P0–P17):

| Column | Type | Source line | Notes |
|---|---|---|---|
| `title` | TEXT | P3 right of `Title:` | nullable |
| `response_to_doc` | TEXT | P4 regex `LS\s+(\S+)\s+on` | nullable |
| `response_to_title` | TEXT | P4 regex `on\s+(.+?)\s+from` | nullable |
| `response_to_group` | TEXT | P4 regex `from\s+(.+)$` | nullable |
| `release` | TEXT | P5 right of `Release:` | nullable |
| `work_item_name` | TEXT | P6 regex `^(.+?)\s*\(` | nullable |
| `work_item_code` | TEXT | P6 regex `\(([^)]+)\)` | nullable |
| `source` | TEXT | P8 right of `Source:` | nullable; the source company / organisation / group / standards-body name(s) who submit the LS |
| `to_groups` | TEXT | P9 right of `To:` | newline-delimited, LIKE-searchable |
| `cc_groups` | TEXT | P10 right of `Cc:` | newline-delimited |
| `attachments_json` | BLOB | P17 right of `Attachments:` | gzip JSON list of `{doc_number, description}` |

`tdoc_id`, `ftp_url`, `parser_version`, `extracted_at` are bookkeeping
columns mirroring `tdoc_cr_cover_page`.

## Multi-variant design

3GPP is the dominant source but not the only one. LS documents produced
by other standards bodies or individual companies follow different
templates (e.g. IEEE 802 / IETF RFC-style liaisons, ETSI TB LS, OARF
memos). Each format defines a different header layout and a different
field set, so a single extractor cannot serve them all.

### Dispatch (`tdocs.source` → parser variant)

The existing `tdocs.source` column already carries the submitting
organisation (e.g. `"Ericsson"`, `"RAN WG2"`, `"IEEE 802.11"`).
`build_default_registry().resolve(...)` is extended to accept `source`
as a dispatch argument alongside `tdoc_type` and `spec`:

```python
class TDocParser(Protocol):
    parser_version: str
    def supports(
        self,
        tdoc_id: str,
        *,
        tdoc_type: str | None = None,
        spec: str | None = None,
        source: str | None = None,
    ) -> bool: ...
```

Specific 3GPP-LS parsers declare their `source` predicate (e.g.
`source in {"3GPP TSG", "3GPP WG", "RAN WG2", "RAN WG3", ...}` or simply
`source is not None` to catch the common case). Generic fall-through
parsers (one per source family) declare their `source` range. The
registry iterates registered parsers in registration order — most
specific first — and returns the first match, mirroring the existing
`TTCNCRParser → CRParser` ordering rule.

### Subclass-per-variant (`LSParserBase`)

The LS family follows the same shape as the existing CR family:

```
parsers/ls/
├── header.py                 # shared is_ls_header_present() + LSHeaderMissingError
├── cover_page.py             # shared LSCoverPageParser (3GPP fields)
├── ls_parsers.py             # LSParserBase (orchestrator)
└── variants/
    ├── __init__.py
    ├── three_gpp.py          # ThreeGPPLSParser — v1
    ├── ieee.py               # placeholder, raises NotImplementedError
    └── etsi.py               # placeholder, raises NotImplementedError
```

`LSParserBase` is the orchestrator (mirrors `CRParserBase`):

- Holds an `LSCoverPageParser` (the variant-specific extractor).
- `supports(tdoc_type="LS", source=...)` — delegates to the variant
  predicate.
- `parse_ls(markdown, *, tdoc_id, ...) -> TDocLSParserResult` — runs
  shared header detection + variant extraction.

`ThreeGPPLSParser(LSParserBase)` is v1. `IEEELSParser` and
`ETSILSParser` ship as stubs that register and raise
`NotImplementedError` so future work has a clear seam. The registry's
registration order is:

```
ThreeGPPLSParser → TTCNCRParser → CRParser
```

CR / TTCN rows fall through to `CRParser`; LS rows from a 3GPP source
match `ThreeGPPLSParser`. Non-3GPP LS rows currently match nothing
(`LookupError`) — acceptable for v1; future variants slot in before
`CRParser`.

### Shared schema with nullable variant columns

One `tdoc_cr_ls_details` table holds every variant. Variant-specific
columns (e.g. an IEEE `meeting_id` field) are nullable text columns. The
table grows column-wise, not table-wise — same as the existing CR
sidecar pattern (single table, many columns).

A `variant` column (`TEXT NOT NULL DEFAULT '3gpp'`) tags the row so
the show record and search index can branch on it without re-running
the parser. The `parser_version` column already disambiguates parsers
within a variant; `variant` disambiguates across formats.

The current 3GPP fields are all nullable, so a non-3GPP row with
`variant='ieee'` simply has every 3GPP column `NULL` plus whatever
IEEE-specific columns the future variant defines.

### Service integration

`TDocCrService.extract_many` / `extract_from_url` /
`extract_from_bytes` resolve via the registry and dispatch on the
return value:

- `isinstance(parser, CRParserBase)` (covers `CRParser`,
  `TTCNCRParser`) → existing CR/TTCN sidecar writes (unchanged).
- `isinstance(parser, LSParserBase)` → call `parser.parse_ls(...)`,
  write `TDocLSDetails` via `ls_repository.upsert(...)`.

`LSParserRepository` Protocol gains one method:
`get_by_variant(ftp_url: str, variant: str) -> TDocLSDetails | None`
so the show record can branch on `tdoc.variant` (defaults to
`"3gpp"`).

## Architecture

```
src/doc3gpp/
├── models/
│   └── tdoc_ls.py                     # NEW — TDocLSDetails, TDocLSParserResult
├── parsers/
│   ├── tdoc_parsers.py                # extend Protocol (parse_ls, source kwarg)
│   └── ls/                            # NEW
│       ├── __init__.py
│       ├── header.py                  # is_ls_header_present, LSHeaderMissingError
│       ├── cover_page.py              # shared LSCoverPageParser
│       ├── ls_parsers.py              # LSParserBase (orchestrator)
│       └── variants/                  # NEW — subclass-per-format
│           ├── __init__.py
│           ├── three_gpp.py           # ThreeGPPLSParser (v1)
│           ├── ieee.py                # IEEELSParser stub
│           └── etsi.py                # ETSILSParser stub
├── storage/
│   ├── db/
│   │   └── orm.py                     # add TDocCrLSDetails ORM
│   └── repositories/
│       └── tdoc_cr_ls_sql.py          # NEW
├── repository/
│   └── protocols.py                   # add LSParserRepository Protocol
├── services/
│   ├── factory.py                     # build_ls_repository() + wiring
│   └── tdoc_cr_service.py             # 4th sidecar write (LSBase branch)
├── web/
│   └── routes/tdocs.py                # LS Cover card + ls envelope
└── cli.py                             # tdoc show LS block
```

## Parser behavior

### Header detection (`parsers/ls/header.py`)

```python
def is_ls_header_present(markdown: str) -> tuple[bool, str]:
    """True iff the markdown contains:

    1. A first non-blank line matching
       ``3GPP TSG <X> WG <Y> Meeting <Z>\\tTDoc <N>`` OR a line containing
       both ``Meeting`` and ``TDoc`` separated by a tab, AND
    2. A ``Title:`` line whose right-hand value starts with ``LS on``
       (case-insensitive), AND
    3. At least one of ``Source:``, ``To:``, ``Cc:`` lines.
    """
```

Failure raises `LSHeaderMissingError(message, snippet=header_blob[:100])`.

### Extraction (`parsers/ls/cover_page.py`)

`LSCoverPageParser.parse(lines)` returns `(ok: bool, payload: dict, advanced: int)`,
mirroring `CRCoverPageParser`. The payload keys are the column names
listed above. `advanced` is the line index after the last consumed line
(always returns the full length in practice).

`to_groups` and `cc_groups` are normalized to newline-delimited strings
(comma- or semicolon-separated inputs are split). `attachments_json` is
gzipped JSON via `storage/compression.py::gzip_json_dumps` so the column
matches the existing `tdoc_cr_change_details.details_json` pattern.

### Orchestrator (`parsers/ls/ls_parsers.py::LSParserBase`)

- `parser_version = "1.0.0"` (the orchestrator version — variant
  subclasses override with their own `parser_version`).
- `__init__(self, cover: LSCoverPageParser)` — variant-specific
  extractor injected at construction.
- `supports(tdoc_id, *, tdoc_type=None, spec=None, source=None) -> bool`:
  `return tdoc_type == "LS" and self._cover.supports_source(source)` —
  delegates the source predicate to the variant cover-page parser.
- `parse(markdown, *, tdoc_id, max_text_length=0, full=False) -> TDocCRParseResult`:
  raises `NotImplementedError("LSParserBase does not parse CR documents")`.
- `parse_ls(markdown, *, tdoc_id, max_text_length=0) -> TDocLSParserResult`:
  runs `is_ls_header_present`, then `self._cover.parse(lines, ...)`,
  builds `TDocLSDetails(tdoc_id=..., ftp_url=None, variant=self.VARIANT,
  ...)`. Returns `TDocLSParserResult(cover=details)`.

### Variant: 3GPP (`parsers/ls/variants/three_gpp.py`)

`ThreeGPPLSParser(LSParserBase)` is the v1 implementation:

- `VARIANT = "3gpp"`
- `parser_version = "1.0.0"`
- `__init__()` builds the 3GPP `LSCoverPageParser` with the eleven
  header-field regexes from the Field model table.
- `supports_source(source) -> bool`: returns `True` for every non-None
  source in v1 — the 3GPP variant is the broad catch-all for LS rows.
  (Future PRs tighten this to a curated allowlist of 3GPP TSG / WG
  short names.)

### Registry (`parsers/tdoc_parsers.py`)

`build_default_registry` registers variants **before** `TTCNCRParser`
so LS rows never fall through to `CRParser`. Order:
`ThreeGPPLSParser → TTCNCRParser → CRParser`. The `ieee` and `etsi`
stubs are **not** registered in v1 — they exist as code, not as
registry entries, so the lookup never returns them.

### Protocol extension (`parsers/tdoc_parsers.py`)

```python
@runtime_checkable
class TDocParser(Protocol):
    parser_version: str
    def supports(
        self,
        tdoc_id: str,
        *,
        tdoc_type: str | None = None,
        spec: str | None = None,
        source: str | None = None,
    ) -> bool: ...
    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
        full: bool = False,
    ) -> TDocCRParseResult: ...
    def parse_ls(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
    ) -> "TDocLSParserResult":
        raise NotImplementedError
```

The default `parse_ls` raises `NotImplementedError`; `CRParser` and
`TTCNCRParser` inherit it. Only `LSParserBase` subclasses override it.
The new `source` kwarg has a default of `None` for backward
compatibility — existing `supports()` callers keep working unchanged.
The `@runtime_checkable` decorator keeps existing `isinstance` checks
working.

## Storage

### ORM (`storage/db/orm.py`)

Add `TDocCrLSDetails` mirroring `TDocCrCoverPage`'s column shape, PK on
`ftp_url`, nullable text columns for the eleven header fields above,
`parser_version`, `extracted_at`. Registered in
`storage/db/create_schema.py::_TDOC_TABLES` so `db init` creates the
table on sqlite + postgres.

### Repository (`repository/protocols.py`)

```python
class LSParserRepository(Protocol):
    def upsert(self, details: TDocLSDetails) -> None: ...
    def get_by_url(self, ftp_url: str) -> TDocLSDetails | None: ...
    def get_by_tdoc_id(self, tdoc_id: str) -> list[TDocLSDetails]: ...
```

### SQL impl (`storage/repositories/tdoc_cr_ls_sql.py`)

Mirrors `SQLAlchemyTDocCrRepository`. `upsert` does `INSERT … ON
CONFLICT(ftp_url) DO UPDATE` (sqlite + postgres). `get_by_url` returns a
single row by PK; `get_by_tdoc_id` returns all revisions (LS files
typically have one revision, but the FTP-URL-is-row-identity invariant
is preserved).

### `TDocCrService` integration

Add `ls_repository: LSParserRepository` constructor arg
(defaults to `None`; factory builds `SQLAlchemyLSParserRepository`).
After a successful extract:

1. Resolve via `registry.resolve(tdoc_id, tdoc_type=tdoc_type,
   source=tdoc.source)` — the registry may return a CR parser, a TTCN
   parser, or an LS variant.
2. If `isinstance(parser, LSParserBase)`: call `parser.parse_ls(...)`
   and `ls_repository.upsert(TDocLSDetails(...))`. The CR sidecar
   writes are skipped — `parser.parse(...)` raises
   `NotImplementedError` for LS variants.
3. If `isinstance(parser, CRParserBase)` (covers `CRParser` and
   `TTCNCRParser`): the existing CR / TTCN / change-details writes fire
   unchanged. `parser.parse_ls(...)` raises `NotImplementedError`.

The LS parser is selected via the same registry dispatch — no separate
flag, no caller-side branching.

## Search / semantic projection

`services/search_service.py` projects the LS cover into the existing
FTS5 `cover_text` column:

```
"<title> <response_to_title> <to_groups> <cc_groups>"
```

This reuses the `cover_text` column already populated for CR rows; no
DDL change. `build_embed_text` in `services/embedding/embedder.py`
concatenates the same fields into the embed text.

## CLI surface

- `tdoc parse [filters]` — auto-dispatches to `LSParser` for
  `tdoc_type='LS'` rows. No new flag.
- `tdoc show --tdoc <id>` — when the row is LS, render an `ls` block
  with the structured fields. Markdown renderer emits `## LS` followed by
  `key: value` lines. JSON envelope gains `ls: {...}` (omit-when-null).
  `--compact` strips the block, same convention as the CR cover block.
- `tdoc list --type LS` — already supported via the shared filter
  grammar; no changes.

## Web surface

- `web/templates/tdoc_show.html` — add a new `LS Cover` card rendered
  **instead of** the CR cover card when `tdoc.tdoc_type == 'LS'`. Same
  styling; lists `title`, `response_to (doc + group)`, `release`,
  `work_item (name + code)`, `source`, `to_groups` (as tag
  list), `cc_groups`, `attachments` (table).
- `web/routes/tdocs.py` — add `ls_repo: LSParserRepository` to the
  deps, fetch via `ls_repo.get_by_url(ftp_url)`, render the card.
- JSON envelope (`?format=json`) — gains `ls: TDocLSDetails | None`.

## MCP

- `get_tdoc` returns the `ls` block when present. Output is byte-identical
  to the JSON route via the shared `_to_json` helper.

## Tests

### Unit

- `tests/unit/test_ls_header.py` — `is_ls_header_present` positive
  (template shape) and negative (CR-shaped, empty, missing title).
- `tests/unit/test_ls_cover_page.py` — `LSCoverPageParser.parse` on a
  synthetic LS markdown; field-by-field assertions including
  attachments parsing edge cases (empty, single, multi, missing
  description).
- `tests/unit/test_ls_parser.py` — full `LSParserBase.parse_ls` happy
  path + `LSHeaderMissingError` propagation + tsg fallback when
  header tsg is empty + `variant='3gpp'` is stamped on the result.
- `tests/unit/test_ls_registry_dispatch.py` — registry dispatch:
  `(tdoc_type='LS', source='3GPP TSG')` resolves to
  `ThreeGPPLSParser`; `(tdoc_type='LS', source='IEEE 802.11')` raises
  `LookupError`; `(tdoc_type='CR')` resolves to `CRParser` or
  `TTCNCRParser` unchanged.

### Integration

- `tests/integration/test_ls_sqlite.py` — `db init` creates the new
  table; `extract_many` writes the sidecar row; `tdoc show --tdoc` reads
  it back; JSON round-trip.
- `tests/integration/test_ls_search_sqlite.py` — LS title is searchable
  via FTS5 after `search index --rebuild`.

### Fixtures

- `tests/fixtures/ls/LS_sample_r5_240001.md` — synthetic LS markdown
  generated by `tests/fixtures/ls/_generate.py` from the
  `LS-Template-130.docx` template. Generated once, committed, used by
  the unit + integration tests.

## Error handling

- `LSHeaderMissingError` — raised by `parse_ls` when the header shape
  doesn't match. Surfaces to the CLI as a non-zero exit with the
  existing CR-style error message format.
- Empty `to_groups` / `cc_groups` — written as empty strings, not NULL,
  so the LIKE-search grammar works (`LIKE '%%'`).
- Missing `release` / `work_item_*` — nullable; written as NULL.
- `attachments_json` parse failure — gzip-encoded `[]`; warning logged
  with the offending raw line.

## Documentation sync

- `README.md` — add a one-line bullet to the features list.
- `AGENTS.md` — add an entry to the "Where to look" table:
  `Add an LS TDoc parser` → `parsers/ls/` (incl. `variants/`) +
  `models/tdoc_ls.py` + `storage/repositories/tdoc_cr_ls_sql.py`.
- `docs/cli.md` — add the new `ls` block under `tdoc show --tdoc` and
  `tdoc show --ftp-url`.
- `docs/code-map.md` — add the new symbols to the symbol table.
- `docs/architecture.md` — add the LS row to the layered diagram and
  the ORM schema section.

## Acceptance criteria

- [ ] `doc3gpp db init` creates `tdoc_cr_ls_details` on a fresh sqlite DB.
- [ ] `doc3gpp tdoc parse --from-path tests/fixtures/ls/LS_sample_r5_240001.md`
      exits 0, writes one row in `tdoc_cr_ls_details` with
      `variant='3gpp'`, and the subsequent
      `doc3gpp tdoc show --tdoc R5-240001` returns an `ls` block in
      JSON mode.
- [ ] `doc3gpp tdoc parse` on a CR row still writes only the CR sidecar
      and does not touch `tdoc_cr_ls_details`.
- [ ] `doc3gpp tdoc parse` on an LS row does not write to
      `tdoc_cr_cover_page`, `tdoc_cr_ttcn_details`, or
      `tdoc_cr_change_details`.
- [ ] `doc3gpp search query "LS on ..."` returns the LS row in the
      results after a `search index --rebuild`.
- [ ] The web TDoc detail page renders the LS Cover card for an LS row.
- [ ] `registry.resolve(tdoc_id, tdoc_type='LS', source='3GPP TSG')`
      returns `ThreeGPPLSParser` (asserted in a unit test).
- [ ] `registry.resolve(tdoc_id, tdoc_type='LS', source='IEEE 802.11')`
      raises `LookupError` in v1 (asserted in a unit test — IEEE / ETSI
      stubs are code, not registry entries).
- [ ] `parsers/ls/variants/three_gpp.py`,
      `parsers/ls/variants/ieee.py`, `parsers/ls/variants/etsi.py`
      exist; `ieee` and `etsi` are clearly marked as v2 stubs.
- [ ] `ruff check .` is clean.
- [ ] `./scripts/test_sqlite.sh` passes.
