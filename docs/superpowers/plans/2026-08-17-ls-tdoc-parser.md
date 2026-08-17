# LS TDoc Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated Liaison Statement (LS) TDoc parser that extracts the eleven header fields from the [3GPP LS template](https://www.3gpp.org/ftp/Information/All_Templates/LS_Template.zip) into a new `tdoc_cr_ls_details` sidecar table, surfaces them through CLI / web / MCP, and indexes them for FTS5 + semantic search. The parser dispatch is `tdoc_type="LS"` + a per-variant `tdocs.source` predicate so future non-3GPP LS formats slot in as new variants without changing the framework.

**Architecture:** New `parsers/ls/` subpackage mirroring `parsers/cr/` — shared header detection + variant cover-page parser + `LSParserBase` orchestrator. Variants live in `parsers/ls/variants/` (subclass-per-format). A single `tdoc_cr_ls_details` table holds every variant (nullable columns + a `variant` tag column). `TDocCrService` resolves via the existing registry and branches on `isinstance(parser, LSParserBase)` to call the new `parse_ls` method.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0 ORM, dataclasses(slots=True, frozen=True), pytest, ruff.

## Global Constraints

These are project-wide and apply to every task:

- **Layered architecture**: `models/` → `repository/` → `services/` → `web/`/`cli.py`. Each layer depends only on the layer below. Parsers stay pure (no I/O). Storage stays behind Protocol boundaries.
- **Dataclass model**: `@dataclass(slots=True, frozen=True)` for new domain objects in `models/`. Never expose ORM attributes — round-trip through ORM ↔ dataclass via `_details_to_orm` / `_orm_to_details` helpers.
- **Protocol-first repos**: every new repository gets a `Protocol` in `repository/protocols.py` and a SQL impl in `storage/repositories/`. Tests inject either.
- **Sidecar pattern**: every sidecar table keys on `ftp_url` (PK), with FK `tdoc_id → tdocs.tdoc_id ON DELETE CASCADE`. Bookkeeping columns: `parser_version`, `extracted_at`. One row per immutable URL.
- **Compression helpers**: `storage/compression.py::compress_json` / `decompress_json` for any JSON-list column (gzip + UTF-8 JSON).
- **No new CLI flag**: `tdoc parse` auto-dispatches via the registry. Variant selection is invisible to the CLI.
- **Lint**: `ruff check .` clean before each commit.
- **Tests**: `./scripts/test_sqlite.sh` passes before any push.
- **Docs sync**: update `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/code-map.md`, `docs/architecture.md` in the same PR as the implementation that changes the surface.
- **Conventional commits**: subject line `<scope>: <imperative>` (e.g. `feat(ls): add LSParser orchestrator`).
- **Python imports**: `from __future__ import annotations` at the top of every new module.
- **Frozen dataclasses**: when mutating after construction, use `dataclasses.replace(...)`. Direct assignment to frozen fields raises `FrozenInstanceError`.
- **Parser tests**: no covering test for a parser is acceptable only when a downstream integration test exercises the same code path. Prefer unit tests.

---

## File Structure

New / modified files for the change. Tasks reference these by path.

| File | Responsibility |
|---|---|
| `src/doc3gpp/models/tdoc_ls.py` | NEW — `TDocLSDetails`, `TDocLSParserResult` dataclasses |
| `src/doc3gpp/parsers/ls/__init__.py` | NEW — re-exports |
| `src/doc3gpp/parsers/ls/header.py` | NEW — `is_ls_header_present`, `LSHeaderMissingError` |
| `src/doc3gpp/parsers/ls/cover_page.py` | NEW — `LSCoverPageParser` (3GPP extractor + `supports_source`) |
| `src/doc3gpp/parsers/ls/ls_parsers.py` | NEW — `LSParserBase` orchestrator |
| `src/doc3gpp/parsers/ls/variants/__init__.py` | NEW — re-exports |
| `src/doc3gpp/parsers/ls/variants/three_gpp.py` | NEW — `ThreeGPPLSParser` |
| `src/doc3gpp/parsers/ls/variants/ieee.py` | NEW — `IEEELSParser` v2 stub |
| `src/doc3gpp/parsers/ls/variants/etsi.py` | NEW — `ETSILSParser` v2 stub |
| `src/doc3gpp/parsers/tdoc_parsers.py` | MODIFY — extend Protocol: `source` kwarg, `parse_ls` default method |
| `src/doc3gpp/storage/db/models.py` | MODIFY — add `TDocCrLSDetailOrm` |
| `src/doc3gpp/storage/db/create_schema.py` | MODIFY — register new table |
| `src/doc3gpp/repository/protocols.py` | MODIFY — add `LSParserRepository` Protocol |
| `src/doc3gpp/storage/repositories/tdoc_cr_ls_sql.py` | NEW — `SQLAlchemyLSParserRepository` |
| `src/doc3gpp/services/factory.py` | MODIFY — `build_ls_repository()` |
| `src/doc3gpp/services/tdoc_cr_service.py` | MODIFY — 4th sidecar write (LSBase branch), registry resolves with `source` |
| `src/doc3gpp/services/search_service.py` | MODIFY — project LS fields into `cover_text` when row has LS sidecar |
| `src/doc3gpp/services/embedding/embedder.py` | MODIFY — concatenate LS fields into embed text |
| `src/doc3gpp/models/tdoc_show.py` | MODIFY — add `ls: TDocLSDetails \| None` to `TDocShowRecord` / `TDocShowRecordByUrl`; `TDocShowRepos` gains `ls` repo |
| `src/doc3gpp/web/routes/tdocs.py` | MODIFY — fetch LS sidecar, render LS Cover card / ls envelope |
| `src/doc3gpp/web/templates/tdoc_show.html` | MODIFY — add LS Cover card |
| `src/doc3gpp/web/mcp_server.py` | MODIFY — `get_tdoc` includes `ls` |
| `src/doc3gpp/cli.py` | MODIFY — `tdoc show --tdoc` / `--ftp-url` emit `ls` block |
| `tests/fixtures/ls/LS_sample_r5_240001.md` | NEW — synthesized 3GPP LS |
| `tests/fixtures/ls/_generate.py` | NEW — helper that renders `LS_sample_r5_240001.md` from the template (run once, committed) |
| `tests/unit/test_ls_header.py` | NEW |
| `tests/unit/test_ls_cover_page.py` | NEW |
| `tests/unit/test_ls_parser.py` | NEW |
| `tests/unit/test_ls_registry_dispatch.py` | NEW |
| `tests/integration/test_ls_sqlite.py` | NEW |
| `tests/integration/test_ls_search_sqlite.py` | NEW |
| `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/code-map.md`, `docs/architecture.md` | MODIFY — docs sync |

---

## Task 1: Models — `TDocLSDetails` + `TDocLSParserResult`

**Files:**
- Create: `src/doc3gpp/models/tdoc_ls.py`
- Modify: `src/doc3gpp/models/__init__.py` (re-export)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `TDocLSDetails(tdoc_id: str | None = None, ftp_url: str | None = None, variant: str = '3gpp', title: str | None = None, response_to_doc: str | None = None, response_to_title: str | None = None, response_to_group: str | None = None, release: str | None = None, work_item_name: str | None = None, work_item_code: str | None = None, source: str | None = None, to_groups: str = '', cc_groups: str = '', attachments: tuple[dict[str, str], ...] = (), parser_version: str = '1.0.0', extracted_at: datetime | None = None)`
  - `TDocLSParserResult(cover: TDocLSDetails | None = None)`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tdoc_ls_model.py`:

```python
from datetime import datetime, timezone
import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails, TDocLSParserResult


def test_default_construction_is_valid():
    d = TDocLSDetails()
    assert d.tdoc_id is None
    assert d.ftp_url is None
    assert d.variant == "3gpp"
    assert d.title is None
    assert d.response_to_doc is None
    assert d.response_to_title is None
    assert d.response_to_group is None
    assert d.release is None
    assert d.work_item_name is None
    assert d.work_item_code is None
    assert d.source is None
    assert d.to_groups == ""
    assert d.cc_groups == ""
    assert d.attachments == ()
    assert d.parser_version == "1.0.0"
    assert d.extracted_at is None


def test_empty_string_ftp_url_is_rejected():
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocLSDetails(ftp_url="   ")


def test_empty_string_tdoc_id_is_rejected():
    with pytest.raises(ValueError, match="non-empty tdoc_id"):
        TDocLSDetails(tdoc_id="   ")


def test_none_string_fields_pass_through():
    d = TDocLSDetails(
        tdoc_id="R5-240001",
        ftp_url="tsg/ls/R5-240001.doc",
        title="LS on foo",
        response_to_doc="R5-234567",
        response_to_title="foo",
        response_to_group="RAN WG2",
        release="Rel-17",
        work_item_name="5G_eHealth",
        work_item_code="WI-123456",
        source="3GPP TSG",
        attachments=({"doc_number": "TR 38.901 v0.1.0", "description": ""},),
    )
    assert d.title == "LS on foo"
    assert d.attachments[0]["doc_number"] == "TR 38.901 v0.1.0"


def test_parse_result_default_is_none_cover():
    r = TDocLSParserResult()
    assert r.cover is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tdoc_ls_model.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.models.tdoc_ls'`

- [ ] **Step 3: Create the module**

Create `src/doc3gpp/models/tdoc_ls.py`:

```python
"""Domain objects for the LS TDoc sidecar.

The :class:`TDocLSDetails` dataclass mirrors the ``tdoc_cr_ls_details``
SQL table — one row per immutable ``ftp_url`` — and carries the
eleven header fields extracted from a 3GPP LS markdown body plus
bookkeeping columns. The :class:`TDocLSParserResult` is the parser's
output envelope; ``cover`` holds the parsed details, ``None`` when
the parser declined (header missing).

Like the CR sidecar models, the parser emits ``None`` for ``ftp_url``
and ``tdoc_id`` because the body extractor has no download URL or
TDoc id of its own; the service layer fills them in via
:func:`dataclasses.replace` before persistence. An empty string is
still a programmer error — the validation only relaxes for ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TypedDict


class LSAttachment(TypedDict):
    """One attachment row from the LS P17 line."""

    doc_number: str
    description: str


@dataclass(slots=True, frozen=True)
class TDocLSDetails:
    """Structured header fields for an LS TDoc, plus bookkeeping.

    Attributes:
        ftp_url: Immutable download URL the row is keyed on. ``None``
            in the parser; non-empty on the persisted row.
        tdoc_id: Canonical TDoc identifier (FK into ``tdocs.tdoc_id``).
            ``None`` in the parser; non-empty on the persisted row.
        variant: Format variant tag (e.g. ``"3gpp"``). Defaults to
            ``"3gpp"``. Future variants (e.g. ``"ieee"``, ``"etsi"``)
            register distinct parsers and stamp their own value here.
        title: P3 ``Title:`` cell, minus the ``LS on`` prefix.
        response_to_doc: P4 regex group 1 — original LS-out doc number.
        response_to_title: P4 regex group 2 — original LS title.
        response_to_group: P4 regex group 3 — original LS group.
        release: P5 ``Release:`` cell.
        work_item_name: P6 regex group 1 — work item name.
        work_item_code: P6 regex group 2 — work item code.
        source: P8 ``Source:`` cell — submitting organisation name(s).
        to_groups: P9 ``To:`` cell, newline-delimited.
        cc_groups: P10 ``Cc:`` cell, newline-delimited.
        attachments: P17 ``Attachments:`` cells, parsed as
            ``LSAttachment`` records. Stored as gzip-JSON on the table.
        parser_version: Parser version string.
        extracted_at: Server-side UTC timestamp, populated by the
            repository's ``upsert`` method.
    """

    ftp_url: str | None = None
    tdoc_id: str | None = None
    variant: str = "3gpp"
    title: str | None = None
    response_to_doc: str | None = None
    response_to_title: str | None = None
    response_to_group: str | None = None
    release: str | None = None
    work_item_name: str | None = None
    work_item_code: str | None = None
    source: str | None = None
    to_groups: str = ""
    cc_groups: str = ""
    attachments: tuple[dict[str, str], ...] = ()
    parser_version: str = "1.0.0"
    extracted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.ftp_url is not None:
            stripped = self.ftp_url.strip()
            if not stripped:
                raise ValueError("TDocLSDetails requires a non-empty ftp_url")
            if stripped != self.ftp_url:
                object.__setattr__(self, "ftp_url", stripped)
        if self.tdoc_id is not None:
            stripped_id = self.tdoc_id.strip()
            if not stripped_id:
                raise ValueError("TDocLSDetails requires a non-empty tdoc_id")
            if stripped_id != self.tdoc_id:
                object.__setattr__(self, "tdoc_id", stripped_id)


@dataclass(slots=True, frozen=True)
class TDocLSParserResult:
    """Output envelope for an LS parser invocation.

    ``cover`` is ``None`` when the parser declined (header missing or
    the variant extractor chose not to fire). All other LS sidecar
    slots (TTCN, body changes) are absent for LS rows.
    """

    cover: TDocLSDetails | None = None


__all__ = ["LSAttachment", "TDocLSDetails", "TDocLSParserResult"]
```

- [ ] **Step 4: Re-export from the models package**

Modify `src/doc3gpp/models/__init__.py` — append to `__all__`:

```python
from doc3gpp.models.tdoc_ls import LSAttachment, TDocLSDetails, TDocLSParserResult

__all__ = [...existing..., "LSAttachment", "TDocLSDetails", "TDocLSParserResult"]
```

(Read the file first; preserve the existing alphabetical `__all__` ordering.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_tdoc_ls_model.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/models/tdoc_ls.py tests/unit/test_tdoc_ls_model.py src/doc3gpp/models/__init__.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/models/tdoc_ls.py src/doc3gpp/models/__init__.py tests/unit/test_tdoc_ls_model.py
git commit -m "feat(ls): add TDocLSDetails and TDocLSParserResult models"
```

---

## Task 2: Shared LS header detection (`parsers/ls/header.py`)

**Files:**
- Create: `src/doc3gpp/parsers/ls/__init__.py`
- Create: `src/doc3gpp/parsers/ls/header.py`
- Create: `tests/unit/test_ls_header.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `class LSHeaderMissingError(ValueError)` with `snippet: str` attribute
  - `def is_ls_header_present(markdown: str) -> tuple[bool, str]`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ls_header.py`:

```python
import pytest

from doc3gpp.parsers.ls.header import LSHeaderMissingError, is_ls_header_present


HEADER_LINES = [
    "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001",
    "",
    "Title:\tLS on 5G_eHealth WI status update",
    "Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3",
    "Release:\tRelease 17",
    "Work Item:\t5G_eHealth (WI-123456)",
    "",
    "Source:\t3GPP TSG RAN WG2",
    "To:\tRAN WG3",
    "Cc:\tSA WG2, CT WG1",
    "",
]


def test_positive_detection_for_3gpp_template_shape():
    md = "\n".join(HEADER_LINES) + "\n\n1\tOverall description\n…"
    present, blob = is_ls_header_present(md)
    assert present is True
    assert "LS on 5G_eHealth" in blob


def test_negative_when_no_title_line():
    lines = [l for l in HEADER_LINES if not l.startswith("Title:")]
    md = "\n".join(lines) + "\n"
    present, _ = is_ls_header_present(md)
    assert present is False


def test_negative_when_title_does_not_start_with_ls_on():
    lines = [l.replace("LS on", "Update on") for l in HEADER_LINES]
    md = "\n".join(lines) + "\n"
    present, _ = is_ls_header_present(md)
    assert present is False


def test_negative_for_cr_shaped_document():
    cr = "\n".join([
        "| CHANGE REQUEST |",
        "|---|---|---|---|",
        "| 38.300 | CR | 1234 | rev | 1 | Current version: 17.1.0 |",
    ])
    present, _ = is_ls_header_present(cr)
    assert present is False


def test_negative_for_empty_markdown():
    present, _ = is_ls_header_present("")
    assert present is False


def test_detection_requires_one_of_source_to_cc():
    lines = [l for l in HEADER_LINES if not (l.startswith("Source:") or l.startswith("To:") or l.startswith("Cc:"))]
    md = "\n".join(lines) + "\n"
    present, _ = is_ls_header_present(md)
    assert present is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ls_header.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.parsers.ls'`

- [ ] **Step 3: Create the package and module**

Create `src/doc3gpp/parsers/ls/__init__.py`:

```python
"""LS TDoc parsers.

Mirrors the layout of :mod:`doc3gpp.parsers.cr`. Shared header
detection lives in :mod:`doc3gpp.parsers.ls.header`; the
:class:`LSCoverPageParser` lives in :mod:`doc3gpp.parsers.ls.cover_page`;
the :class:`LSParserBase` orchestrator lives in
:mod:`doc3gpp.parsers.ls.ls_parsers`. Subclass-per-format variants
live in :mod:`doc3gpp.parsers.ls.variants`.
"""
```

Create `src/doc3gpp/parsers/ls/header.py`:

```python
"""Shared header detection for LS TDoc parsers.

The 3GPP LS template carries a recognisable header shape — a tabbed
``Meeting`` / ``TDoc`` first line, a ``Title:`` cell whose value
starts with ``LS on`` (case-insensitive), and at least one of
``Source:`` / ``To:`` / ``Cc:`` cells. Variants (IEEE, ETSI, …) keep
their own header detection but share the same error contract via
:class:`LSHeaderMissingError`.

The detection works on raw markdown because the LS template is
already markdown-shaped when the converter hands it to the parser.
"""

from __future__ import annotations

import re

_LS_TITLE_PREFIX = re.compile(r"^\s*LS\s+on\b", re.IGNORECASE)
_FIRST_LINE_TAB = re.compile(
    r"^3GPP\s+TSG\b.*\bMeeting\b.*\t.*\bTDoc\b", re.IGNORECASE
)
_ANY_OF_SOURCE_TO_CC = re.compile(r"^(?:Source|To|Cc):", re.IGNORECASE)


class LSHeaderMissingError(ValueError):
    """Raised when an LS-marked markdown body lacks the LS header shape."""

    def __init__(self, message: str, snippet: str = "") -> None:
        super().__init__(message)
        self.snippet = snippet


def is_ls_header_present(markdown: str) -> tuple[bool, str]:
    """Return ``(present, header_blob)``.

    ``header_blob`` is the leading 100-line slice that the detector
    scanned — useful for error messages and the
    :class:`LSHeaderMissingError` snippet.
    """
    if not markdown:
        return False, ""

    lines = markdown.splitlines()
    head = lines[:100]
    blob = "\n".join(head)

    first_match = bool(_FIRST_LINE_TAB.search(blob))
    if not first_match:
        for line in head:
            if "\t" in line and "Meeting" in line and "TDoc" in line:
                first_match = True
                break

    title_match = False
    for line in head:
        if line.startswith(("Title:", "Title :")) and _LS_TITLE_PREFIX.search(
            line.split(":", 1)[1]
        ):
            title_match = True
            break

    any_destination = any(_ANY_OF_SOURCE_TO_CC.match(line) for line in head)

    return (first_match and title_match and any_destination), blob


__all__ = ["LSHeaderMissingError", "is_ls_header_present"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ls_header.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Lint**

Run: `ruff check src/doc3gpp/parsers/ls/ tests/unit/test_ls_header.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/parsers/ls/__init__.py src/doc3gpp/parsers/ls/header.py tests/unit/test_ls_header.py
git commit -m "feat(ls): add shared LS header detection"
```

---

## Task 3: 3GPP cover-page extractor (`parsers/ls/cover_page.py`)

**Files:**
- Create: `src/doc3gpp/parsers/ls/cover_page.py`
- Create: `tests/unit/test_ls_cover_page.py`

**Interfaces:**
- Consumes: `lines: Sequence[str]` (markdown split into lines), `max_text_length: int = 0`
- Produces:
  - `class LSCoverPageParser`:
    - `name = "ls_cover_page"`
    - `VARIANT = "3gpp"`
    - `def supports_source(source: str | None) -> bool`
    - `def parse(self, lines, *, max_text_length=0, full=False) -> tuple[bool, dict, int]`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ls_cover_page.py`:

```python
import pytest

from doc3gpp.parsers.ls.cover_page import LSCoverPageParser


_LS_LINES = [
    "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001",
    "",
    "Title:\tLS on 5G_eHealth WI status update",
    "Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3",
    "Release:\tRelease 17",
    "Work Item:\t5G_eHealth (WI-123456)",
    "",
    "Source:\t3GPP TSG RAN WG2",
    "To:\tRAN WG3, RAN WG4",
    "Cc:\tSA WG2",
    "",
    "Attachments:\tTR 38.901 v0.1.0 [draft]. ",
]


def test_supports_source_true_for_non_none():
    assert LSCoverPageParser.supports_source("3GPP TSG") is True
    assert LSCoverPageParser.supports_source("IEEE 802.11") is True


def test_supports_source_false_for_none():
    assert LSCoverPageParser.supports_source(None) is True  # 3GPP is the catch-all


def test_parse_extracts_all_eleven_fields():
    ok, payload, advanced = LSCoverPageParser().parse(_LS_LINES)
    assert ok is True
    assert advanced == len(_LS_LINES)
    assert payload["title"] == "LS on 5G_eHealth WI status update"
    assert payload["response_to_doc"] == "R5-234567"
    assert payload["response_to_title"] == "5G_eHealth WI status"
    assert payload["response_to_group"] == "RAN WG3"
    assert payload["release"] == "Release 17"
    assert payload["work_item_name"] == "5G_eHealth"
    assert payload["work_item_code"] == "WI-123456"
    assert payload["source"] == "3GPP TSG RAN WG2"
    assert "RAN WG3" in payload["to_groups"]
    assert "RAN WG4" in payload["to_groups"]
    assert "SA WG2" in payload["cc_groups"]


def test_to_groups_normalises_comma_separated_to_newlines():
    lines = [l.replace("RAN WG3, RAN WG4", "RAN WG3, RAN WG4") for l in _LS_LINES]
    _, payload, _ = LSCoverPageParser().parse(lines)
    assert payload["to_groups"] == "RAN WG3\nRAN WG4"


def test_parse_handles_missing_response_to():
    lines = [l for l in _LS_LINES if not l.startswith("Response to:")]
    _, payload, _ = LSCoverPageParser().parse(lines)
    assert payload["response_to_doc"] is None
    assert payload["response_to_title"] is None
    assert payload["response_to_group"] is None


def test_parse_handles_missing_work_item_code():
    lines = [l.replace("(WI-123456)", "(no-code)") for l in _LS_LINES]
    _, payload, _ = LSCoverPageParser().parse(lines)
    assert payload["work_item_name"] == "5G_eHealth"
    assert payload["work_item_code"] == "no-code"


def test_attachments_parsed_as_list():
    lines = _LS_LINES + [
        "Attachments:\tTR 38.901 v0.1.0 [draft].",
        "Attachments:\tTS 38.300 v17.1.0.",
    ]
    _, payload, _ = LSCoverPageParser().parse(lines)
    attachments = payload["attachments"]
    assert isinstance(attachments, list)
    assert {"doc_number": "TR 38.901 v0.1.0", "description": "draft"} in attachments
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ls_cover_page.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.parsers.ls.cover_page'`

- [ ] **Step 3: Create the cover-page extractor**

Create `src/doc3gpp/parsers/ls/cover_page.py`:

```python
"""3GPP LS header-field extractor.

:class:`LSCoverPageParser` walks the first ~50 lines of a 3GPP LS
markdown body, extracts the eleven structured fields from the
template (title, response-to triple, release, work-item pair, source,
to / cc groups, attachments list), and returns them as a dict that
the :class:`LSParserBase` orchestrator maps onto a
:class:`TDocLSDetails`.

Non-3GPP variants inherit the class shape but override the regex
patterns and the ``supports_source`` predicate. v1 ships the 3GPP
implementation only — ``ieee`` and ``etsi`` stubs live as siblings
under :mod:`doc3gpp.parsers.ls.variants`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)


_TITLE_RE = re.compile(r"^Title:\s*(.*)$", re.IGNORECASE)
_RESPONSE_TO_RE = re.compile(
    r"^Response to:\s*LS\s+(\S+)\s+on\s+(.+?)\s+from\s+(.+?)\s*$",
    re.IGNORECASE,
)
_RELEASE_RE = re.compile(r"^Release:\s*(.*)$", re.IGNORECASE)
_WORK_ITEM_RE = re.compile(
    r"^Work Item:\s*(.+?)\s*\(([^)]*)\)\s*$", re.IGNORECASE
)
_SOURCE_RE = re.compile(r"^Source:\s*(.*)$", re.IGNORECASE)
_TO_RE = re.compile(r"^To:\s*(.*)$", re.IGNORECASE)
_CC_RE = re.compile(r"^Cc:\s*(.*)$", re.IGNORECASE)
_ATTACHMENTS_RE = re.compile(r"^Attachments:\s*(.*)$", re.IGNORECASE)


def _normalise_groups(raw: str) -> str:
    """Split comma/semicolon-separated destination strings into newlines."""
    if not raw:
        return ""
    parts = re.split(r"[,;]\s*", raw.strip())
    return "\n".join(p for p in parts if p)


def _parse_attachments(raw: str) -> list[dict[str, str]]:
    """Parse a single Attachments: line into {doc_number, description} dicts.

    Format: ``DocNumber(s) [Description e.g. Draft TS 29.414 v0.1.0].``
    """
    if not raw:
        return []
    cleaned = raw.rstrip(" .").strip()
    if not cleaned:
        return []
    out: list[dict[str, str]] = []
    for chunk in re.split(r"\s*;\s*|\s{2,}", cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(?P<doc>\S+)\s*\[(?P<desc>[^\]]*)\]\s*$", chunk)
        if m:
            out.append(
                {"doc_number": m.group("doc").strip(),
                 "description": m.group("desc").strip()}
            )
        else:
            out.append({"doc_number": chunk, "description": ""})
    return out


class LSCoverPageParser:
    """3GPP LS header extractor.

    Implements the :class:`doc3gpp.parsers.tdoc_parsers.SectionParser`
    Protocol. Returns ``(True, payload, len(lines))`` when the lines
    look like an LS header; ``payload`` keys match the column names
    in the ``tdoc_cr_ls_details`` table (plus ``attachments`` as a
    list of dicts, ready for gzip-JSON storage).
    """

    name = "ls_cover_page"
    VARIANT = "3gpp"

    @staticmethod
    def supports_source(source: str | None) -> bool:
        """Return ``True`` for every non-None source.

        The 3GPP variant is the broad catch-all for LS rows in v1.
        Future PRs tighten this to a curated allowlist of 3GPP TSG /
        WG short names (``R5``, ``RAN WG2``, …). ``None`` source
        returns ``True`` so unannotated rows still hit the parser —
        the sidecar write fails closed if the variant stamp is later
        judged unsafe.
        """
        return source is None or bool(source.strip())

    def parse(
        self,
        lines: Sequence[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, dict, int]:
        payload: dict = {
            "title": None,
            "response_to_doc": None,
            "response_to_title": None,
            "response_to_group": None,
            "release": None,
            "work_item_name": None,
            "work_item_code": None,
            "source": None,
            "to_groups": "",
            "cc_groups": "",
            "attachments": [],
        }
        attachments: list[dict[str, str]] = []

        for line in lines:
            if (m := _TITLE_RE.match(line)):
                payload["title"] = m.group(1).strip() or None
                continue
            if (m := _RESPONSE_TO_RE.match(line)):
                payload["response_to_doc"] = m.group(1).strip()
                payload["response_to_title"] = m.group(2).strip()
                payload["response_to_group"] = m.group(3).strip()
                continue
            if (m := _RELEASE_RE.match(line)):
                payload["release"] = m.group(1).strip() or None
                continue
            if (m := _WORK_ITEM_RE.match(line)):
                payload["work_item_name"] = m.group(1).strip() or None
                payload["work_item_code"] = m.group(2).strip() or None
                continue
            if (m := _SOURCE_RE.match(line)):
                payload["source"] = m.group(1).strip() or None
                continue
            if (m := _TO_RE.match(line)):
                payload["to_groups"] = _normalise_groups(m.group(1))
                continue
            if (m := _CC_RE.match(line)):
                payload["cc_groups"] = _normalise_groups(m.group(1))
                continue
            if (m := _ATTACHMENTS_RE.match(line)):
                attachments.extend(_parse_attachments(m.group(1)))

        payload["attachments"] = attachments

        if max_text_length > 0:
            for field in ("title", "response_to_title", "source"):
                v = payload.get(field)
                if isinstance(v, str) and len(v) > max_text_length:
                    logger.warning(
                        "Truncating LS %s to %d characters",
                        field, max_text_length,
                    )
                    payload[field] = v[:max_text_length]

        if not payload["title"]:
            logger.warning(
                "LS header missing Title; leaving tdoc_cr_ls_details.title as None"
            )

        return True, payload, len(lines)


__all__ = ["LSCoverPageParser"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_ls_cover_page.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Lint**

Run: `ruff check src/doc3gpp/parsers/ls/cover_page.py tests/unit/test_ls_cover_page.py`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/parsers/ls/cover_page.py tests/unit/test_ls_cover_page.py
git commit -m "feat(ls): add 3GPP LSCoverPageParser"
```

---

## Task 4: LS parser orchestrator + 3GPP variant stub

**Files:**
- Create: `src/doc3gpp/parsers/ls/ls_parsers.py`
- Create: `src/doc3gpp/parsers/ls/variants/__init__.py`
- Create: `src/doc3gpp/parsers/ls/variants/three_gpp.py`
- Create: `src/doc3gpp/parsers/ls/variants/ieee.py`
- Create: `src/doc3gpp/parsers/ls/variants/etsi.py`
- Create: `tests/unit/test_ls_parser.py`

**Interfaces:**
- Consumes: `LSCoverPageParser` (variant-specific), `TDocLSDetails`, `TDocLSParserResult`
- Produces:
  - `class LSParserBase(TDocParser)` with `parser_version`, `__init__(cover)`, `supports(...)`, `parse(...)` (raises), `parse_ls(...)`
  - `class ThreeGPPLSParser(LSParserBase)` with `VARIANT = "3gpp"`, `parser_version = "1.0.0"`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_ls_parser.py`:

```python
from datetime import datetime, timezone

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.parsers.ls.header import LSHeaderMissingError
from doc3gpp.parsers.ls.ls_parsers import LSParserBase
from doc3gpp.parsers.ls.variants.three_gpp import ThreeGPPLSParser


_LS_MD = """3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001

Title:	LS on 5G_eHealth WI status update
Response to:	LS R5-234567 on 5G_eHealth WI status from RAN WG3
Release:	Release 17
Work Item:	5G_eHealth (WI-123456)

Source:	3GPP TSG RAN WG2
To:	RAN WG3
Cc:	SA WG2

Attachments:	TR 38.901 v0.1.0 [draft].
"""


def test_three_gpp_parser_stamps_variant_and_happy_path():
    parser = ThreeGPPLSParser()
    result = parser.parse_ls(_LS_MD, tdoc_id="R5-240001")
    assert result.cover is not None
    assert result.cover.tdoc_id == "R5-240001"
    assert result.cover.variant == "3gpp"
    assert result.cover.title == "LS on 5G_eHealth WI status update"
    assert result.cover.work_item_code == "WI-123456"


def test_supports_requires_ls_tdoc_type():
    parser = ThreeGPPLSParser()
    assert parser.supports("R5-240001", tdoc_type="LS", source="3GPP TSG") is True
    assert parser.supports("R5-240001", tdoc_type="CR") is False
    assert parser.supports("R5-240001", tdoc_type=None) is False


def test_parse_method_raises_for_ls_variant():
    parser = ThreeGPPLSParser()
    with pytest.raises(NotImplementedError, match="does not parse CR"):
        parser.parse(_LS_MD, tdoc_id="R5-240001")


def test_missing_header_raises_ls_header_missing_error():
    parser = ThreeGPPLSParser()
    bad_md = "| CHANGE REQUEST |\n| 38.300 | CR | 1234 | rev | 1 |\n"
    with pytest.raises(LSHeaderMissingError):
        parser.parse_ls(bad_md, tdoc_id="R5-240001")


def test_ls_parser_base_is_abstract():
    """LSParserBase can be subclassed with a custom cover extractor."""
    class _Stub:
        VARIANT = "stub"
        name = "stub_cover"

        @staticmethod
        def supports_source(source):
            return True

        def parse(self, lines, *, max_text_length=0, full=False):
            return True, {"title": "stub-title"}, len(lines)

    base = LSParserBase(cover=_Stub())
    assert base.parse_ls("dummy markdown", tdoc_id="X-1").cover.title == "stub-title"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_ls_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'doc3gpp.parsers.ls.ls_parsers'`

- [ ] **Step 3: Create the orchestrator**

Create `src/doc3gpp/parsers/ls/ls_parsers.py`:

```python
"""LS parser orchestrator (:class:`LSParserBase`).

Mirrors :class:`doc3gpp.parsers.cr.cr_parsers.CRParserBase`. Holds a
variant-specific :class:`LSCoverPageParser` injected at construction;
the orchestrator does the header detection, runs the cover extractor,
and assembles a :class:`TDocLSDetails`. ``parse()`` raises
``NotImplementedError`` — the service never calls it for LS rows,
because the existing CR sidecar writes assume a ``TDocCRParseResult``
and an LS row has nothing to write there.
"""

from __future__ import annotations

import logging
from typing import Any

from doc3gpp.models.tdoc_cr import TDocCRParseResult
from doc3gpp.models.tdoc_ls import TDocLSDetails, TDocLSParserResult
from doc3gpp.parsers.ls.header import LSHeaderMissingError, is_ls_header_present
from doc3gpp.parsers.tdoc_parsers import TDocParser

logger = logging.getLogger(__name__)


class LSParserBase(TDocParser):
    """Orchestrator for LS-family parsers.

    Subclasses bind ``VARIANT`` and ``parser_version`` and inject a
    variant-specific cover extractor at construction. The orchestrator
    itself does no header-detection work beyond delegating to
    :func:`is_ls_header_present`.
    """

    parser_version: str = "1.0.0"
    VARIANT: str = ""

    def __init__(self, cover: Any) -> None:
        self._cover = cover

    def supports(
        self,
        tdoc_id: str,
        *,
        tdoc_type: str | None = None,
        spec: str | None = None,
        source: str | None = None,
    ) -> bool:
        if tdoc_type != "LS":
            return False
        return bool(self._cover.supports_source(source))

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
        full: bool = False,
    ) -> TDocCRParseResult:
        raise NotImplementedError(
            "LSParserBase does not parse CR documents; use parse_ls()"
        )

    def parse_ls(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
    ) -> TDocLSParserResult:
        present, header_blob = is_ls_header_present(markdown)
        if not present:
            raise LSHeaderMissingError(
                "Markdown does not contain a recognisable LS header "
                "(tabbed Meeting/TDoc line, 'LS on' title, and "
                "Source/To/Cc cell); this does not look like an LS "
                "TDoc.",
                snippet=header_blob[:100],
            )

        lines = markdown.splitlines()
        _ok, payload, _advanced = self._cover.parse(
            lines, max_text_length=max_text_length
        )

        final_tdoc_id = (tdoc_id or "").strip() or None
        details = TDocLSDetails(
            tdoc_id=final_tdoc_id,
            ftp_url=None,
            variant=self.VARIANT,
            title=payload.get("title"),
            response_to_doc=payload.get("response_to_doc"),
            response_to_title=payload.get("response_to_title"),
            response_to_group=payload.get("response_to_group"),
            release=payload.get("release"),
            work_item_name=payload.get("work_item_name"),
            work_item_code=payload.get("work_item_code"),
            source=payload.get("source"),
            to_groups=payload.get("to_groups") or "",
            cc_groups=payload.get("cc_groups") or "",
            attachments=tuple(payload.get("attachments") or ()),
            parser_version=self.parser_version,
            extracted_at=None,
        )
        return TDocLSParserResult(cover=details)


__all__ = ["LSParserBase"]
```

- [ ] **Step 4: Create the variant stubs**

Create `src/doc3gpp/parsers/ls/variants/__init__.py`:

```python
"""LS parser variants — subclass-per-format.

v1 ships :class:`ThreeGPPLSParser`; :class:`IEEELSParser` and
:class:`ETSILSParser` exist as code-level seams for future work but
are not registered in :func:`build_default_registry`.
"""
```

Create `src/doc3gpp/parsers/ls/variants/three_gpp.py`:

```python
"""3GPP LS variant — v1 implementation."""

from __future__ import annotations

from doc3gpp.parsers.ls.cover_page import LSCoverPageParser
from doc3gpp.parsers.ls.ls_parsers import LSParserBase

__all__ = ["ThreeGPPLSParser"]


class ThreeGPPLSParser(LSParserBase):
    """LS parser for documents produced by 3GPP working groups.

    Binds :class:`LSCoverPageParser` as the cover extractor and stamps
    ``VARIANT = "3gpp"`` on the persisted row.
    """

    parser_version = "1.0.0"
    VARIANT = "3gpp"

    def __init__(self) -> None:
        super().__init__(cover=LSCoverPageParser())
```

Create `src/doc3gpp/parsers/ls/variants/ieee.py`:

```python
"""IEEE LS variant — v2 stub.

The class is intentionally not registered in
:func:`doc3gpp.parsers.tdoc_parsers.build_default_registry`; it exists
to make the seam for future work explicit. The cover extractor is a
placeholder — replace with an IEEE-specific :class:`LSCoverPageParser`
subclass when the format is documented.
"""

from __future__ import annotations

from typing import Any

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.parsers.ls.ls_parsers import LSParserBase

__all__ = ["IEEELSParser"]


class IEEELSParser(LSParserBase):
    """Placeholder for IEEE-style LS documents. Not registered in v1."""

    parser_version = "0.0.0"
    VARIANT = "ieee"

    def __init__(self) -> None:
        super().__init__(cover=_IEEECoverPlaceholder())

    def parse_ls(self, markdown: str, *, tdoc_id: str, max_text_length: int = 0):  # type: ignore[override]
        raise NotImplementedError(
            "IEEELSParser is a v2 stub; register in "
            "build_default_registry once the IEEE LS header format "
            "is documented."
        )


class _IEEECoverPlaceholder:
    """Minimal stand-in until the IEEE LS header is documented."""

    VARIANT = "ieee"
    name = "ieee_cover_placeholder"

    @staticmethod
    def supports_source(source: str | None) -> bool:
        return source is not None and "ieee" in source.lower()

    def parse(self, lines, *, max_text_length: int = 0, full: bool = False):
        return True, {}, len(lines)
```

Create `src/doc3gpp/parsers/ls/variants/etsi.py`:

```python
"""ETSI LS variant — v2 stub. See :mod:`doc3gpp.parsers.ls.variants.ieee`."""

from __future__ import annotations

from doc3gpp.parsers.ls.ls_parsers import LSParserBase

__all__ = ["ETSILSParser"]


class ETSILSParser(LSParserBase):
    """Placeholder for ETSI TB-style LS documents. Not registered in v1."""

    parser_version = "0.0.0"
    VARIANT = "etsi"

    def __init__(self) -> None:
        super().__init__(cover=_ETSICoverPlaceholder())

    def parse_ls(self, markdown: str, *, tdoc_id: str, max_text_length: int = 0):  # type: ignore[override]
        raise NotImplementedError(
            "ETSILSParser is a v2 stub; register in "
            "build_default_registry once the ETSI TB LS header "
            "format is documented."
        )


class _ETSICoverPlaceholder:
    VARIANT = "etsi"
    name = "etsi_cover_placeholder"

    @staticmethod
    def supports_source(source: str | None) -> bool:
        return source is not None and "etsi" in source.lower()

    def parse(self, lines, *, max_text_length: int = 0, full: bool = False):
        return True, {}, len(lines)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_ls_parser.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/parsers/ls/ tests/unit/test_ls_parser.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/parsers/ls/ls_parsers.py src/doc3gpp/parsers/ls/variants/ tests/unit/test_ls_parser.py
git commit -m "feat(ls): add LSParserBase orchestrator and 3GPP variant"
```

---

## Task 5: Protocol extension (`source` kwarg + `parse_ls` default)

**Files:**
- Modify: `src/doc3gpp/parsers/tdoc_parsers.py:33-50`
- Modify: `src/doc3gpp/parsers/tdoc_parsers.py:60-74` (resolve signature)

**Interfaces:**
- Consumes: existing `TDocParser` Protocol
- Produces:
  - `TDocParser.supports(..., source=None)` — extended kwarg
  - `TDocParserRegistry.resolve(..., source=None)` — passes through
  - `TDocParser.parse_ls(...)` — default raises `NotImplementedError`
  - `TDocParser.parse(...)` — unchanged

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tdoc_parser_protocol.py`:

```python
import pytest

from doc3gpp.models.tdoc_cr import TDocCRParseResult
from doc3gpp.models.tdoc_ls import TDocLSParserResult
from doc3gpp.parsers.cr.cr_parsers import CRParser
from doc3gpp.parsers.tdoc_parsers import TDocParserRegistry, build_default_registry


def test_default_registry_resolves_ls_to_three_gpp():
    reg = build_default_registry()
    parser = reg.resolve("R5-240001", tdoc_type="LS", source="3GPP TSG")
    from doc3gpp.parsers.ls.variants.three_gpp import ThreeGPPLSParser
    assert isinstance(parser, ThreeGPPLSParser)


def test_default_registry_ls_with_unknown_source_raises():
    reg = build_default_registry()
    with pytest.raises(LookupError):
        reg.resolve("R5-240001", tdoc_type="LS", source="IEEE 802.11")


def test_default_registry_still_resolves_cr_to_cr_parser():
    reg = build_default_registry()
    parser = reg.resolve("R5-240001", tdoc_type="CR")
    assert isinstance(parser, CRParser)


def test_cr_parser_parse_ls_raises_not_implemented():
    parser = CRParser()
    with pytest.raises(NotImplementedError):
        parser.parse_ls("dummy", tdoc_id="X-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tdoc_parser_protocol.py -v`
Expected: FAIL — `build_default_registry` does not register `ThreeGPPLSParser` yet, and `resolve` does not accept `source`.

- [ ] **Step 3: Extend the Protocol**

In `src/doc3gpp/parsers/tdoc_parsers.py`, replace the file contents with:

```python
"""TDoc parser abstractions and default registry.

This module defines the :class:`TDocParser` and :class:`SectionParser`
Protocols and a small registry that lets the rest of the app resolve a
parser for a given TDoc without importing concrete implementations.
"""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from doc3gpp.models.tdoc_cr import TDocCRParseResult


T = TypeVar("T")


@runtime_checkable
class SectionParser(Protocol[T]):
    """Parses one named section of a CR markdown body."""

    name: str

    def parse(
        self,
        lines: list[str],
        *,
        max_text_length: int = 0,
        full: bool = False,
    ) -> tuple[bool, T, int]: ...


@runtime_checkable
class TDocParser(Protocol):
    """Parses a CR or LS markdown body.

    Concrete parsers expose ``parse`` (CR-family) or ``parse_ls``
    (LS-family). The default ``parse_ls`` raises
    :class:`NotImplementedError`; only ``LSParserBase`` subclasses
    override it. The ``source`` kwarg on :meth:`supports` enables
    variant dispatch for LS-family parsers (3GPP vs IEEE vs ETSI) —
    it is ignored by the CR-family parsers.
    """

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


class TDocParserRegistry:
    """Resolves a :class:`TDocParser` for a given TDoc id / type / spec / source."""

    def __init__(self) -> None:
        self._parsers: list[TDocParser] = []

    def register(self, parser: TDocParser) -> None:
        """Add ``parser`` to the registry."""
        self._parsers.append(parser)

    def resolve(
        self,
        tdoc_id: str,
        *,
        tdoc_type: str | None = None,
        spec: str | None = None,
        source: str | None = None,
    ) -> TDocParser:
        """Return the first registered parser that supports the inputs.

        Raises:
            LookupError: no registered parser supports the inputs.
        """
        for parser in self._parsers:
            if parser.supports(
                tdoc_id,
                tdoc_type=tdoc_type,
                spec=spec,
                source=source,
            ):
                return parser
        raise LookupError(f"No TDoc parser registered for {tdoc_id!r}")


def build_default_registry() -> TDocParserRegistry:
    """Return a registry with the built-in CR and LS parsers.

    Registration order is most-specific first: the 3GPP LS variant
    wins for ``tdoc_type='LS'`` rows from 3GPP sources; the TTCN CR
    variant wins for ``tdoc_type='CR'`` rows whose id matches the
    TTCN suffix; everything else falls through to the generic CR
    parser.
    """
    from doc3gpp.parsers.cr.cr_parsers import CRParser, TTCNCRParser
    from doc3gpp.parsers.ls.variants.three_gpp import ThreeGPPLSParser

    registry = TDocParserRegistry()
    registry.register(ThreeGPPLSParser())
    registry.register(TTCNCRParser())
    registry.register(CRParser())
    return registry
```

(Note: `TDocLSParserResult` is referenced as a forward string in the
Protocol default method body — at runtime the Protocol is structural,
not nominal, so the string annotation is fine. If `ruff` complains,
add `from doc3gpp.models.tdoc_ls import TDocLSParserResult` below the
existing imports and drop the quotes.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_tdoc_parser_protocol.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full unit suite**

Run: `pytest tests/unit -q`
Expected: PASS — the existing `CRParser.supports` signature accepts
`source=None` via the new kwarg, so no call site breaks.

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/parsers/tdoc_parsers.py tests/unit/test_tdoc_parser_protocol.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/parsers/tdoc_parsers.py tests/unit/test_tdoc_parser_protocol.py
git commit -m "feat(parsers): add source kwarg and parse_ls to TDocParser Protocol"
```

---

## Task 6: ORM + schema registration

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py` (append `TDocCrLSDetailOrm`)
- Modify: `src/doc3gpp/storage/db/create_schema.py` (register in `_TDOC_TABLES`)

**Interfaces:**
- Produces: `class TDocCrLSDetailOrm` with PK `ftp_url`, FK `tdoc_id`, columns matching `TDocLSDetails` plus `extracted_at` server-default

- [ ] **Step 1: Locate the file**

Read `src/doc3gpp/storage/db/models.py`. Find the `TDocCrCoverPageOrm` class (or its equivalent in this codebase — `TDocCrDetailOrm` per codegraph). Use the same pattern: PK `ftp_url`, FK `tdoc_id`, bookkeeping columns.

- [ ] **Step 2: Write the failing test**

Create `tests/integration/test_ls_orm_sqlite.py`:

```python
from sqlalchemy import inspect

from doc3gpp.storage.db.models import TDocCrLSDetailOrm
from doc3gpp.storage.db.session import get_engine, get_session_factory


def test_table_created_with_expected_columns():
    get_session_factory()  # force init
    engine = get_engine()
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("tdoc_cr_ls_details")}
    expected = {
        "ftp_url", "tdoc_id", "variant",
        "title", "response_to_doc", "response_to_title", "response_to_group",
        "release", "work_item_name", "work_item_code",
        "source", "to_groups", "cc_groups", "attachments_json",
        "parser_version", "extracted_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_pks_and_fks():
    insp = inspect(get_engine())
    pk = insp.get_pk_constraint("tdoc_cr_ls_details")
    assert pk["constrained_columns"] == ["ftp_url"]
    fks = insp.get_foreign_keys("tdoc_cr_ls_details")
    tdoc_fk = [f for f in fks if f["referred_table"] == "tdocs"]
    assert tdoc_fk, "no FK to tdocs"
    assert "tdoc_id" in tdoc_fk[0]["constrained_columns"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/integration/test_ls_orm_sqlite.py -v`
Expected: FAIL — `TDocCrLSDetailOrm` does not exist yet.

- [ ] **Step 4: Add the ORM class**

In `src/doc3gpp/storage/db/models.py`, after `TDocCrCoverPageOrm` (or the existing `TDocCrDetailOrm`), append:

```python
class TDocCrLSDetailOrm(Base):
    """Sidecar row for LS header extraction.

    One row per immutable ``ftp_url``; FK ``tdoc_id`` cascades from
    ``tdocs.tdoc_id``. The ``attachments_json`` column stores the
    parsed attachments as gzip-compressed UTF-8 JSON via
    :func:`doc3gpp.storage.compression.compress_json`. ``variant``
    tags the format family so the show record and search index can
    branch on it without re-running the parser.
    """

    __tablename__ = "tdoc_cr_ls_details"

    ftp_url: Mapped[str] = mapped_column(Text, primary_key=True)
    tdoc_id: Mapped[str] = mapped_column(
        Text, ForeignKey("tdocs.tdoc_id", ondelete="CASCADE"), nullable=False
    )
    variant: Mapped[str] = mapped_column(Text, nullable=False, default="3gpp", server_default="3gpp")
    title: Mapped[str | None] = mapped_column(Text)
    response_to_doc: Mapped[str | None] = mapped_column(Text)
    response_to_title: Mapped[str | None] = mapped_column(Text)
    response_to_group: Mapped[str | None] = mapped_column(Text)
    release: Mapped[str | None] = mapped_column(Text)
    work_item_name: Mapped[str | None] = mapped_column(Text)
    work_item_code: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    to_groups: Mapped[str | None] = mapped_column(Text)
    cc_groups: Mapped[str | None] = mapped_column(Text)
    attachments_json: Mapped[bytes | None] = mapped_column(LargeBinary)
    parser_version: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 5: Register in `_TDOC_TABLES`**

Read `src/doc3gpp/storage/db/create_schema.py`. Append `TDocCrLSDetailOrm` to the `_TDOC_TABLES` tuple (or list — match the existing pattern).

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/integration/test_ls_orm_sqlite.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Lint**

Run: `ruff check src/doc3gpp/storage/db/ tests/integration/test_ls_orm_sqlite.py`
Expected: clean

- [ ] **Step 8: Commit**

```bash
git add src/doc3gpp/storage/db/models.py src/doc3gpp/storage/db/create_schema.py tests/integration/test_ls_orm_sqlite.py
git commit -m "feat(storage): add tdoc_cr_ls_details ORM and schema registration"
```

---

## Task 7: LSParserRepository Protocol + SQL impl

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py`
- Create: `src/doc3gpp/storage/repositories/tdoc_cr_ls_sql.py`
- Create: `tests/integration/test_ls_sqlite_repo.py`

**Interfaces:**
- Produces:
  - `class LSParserRepository(Protocol)`:
    - `upsert(details: TDocLSDetails) -> None`
    - `get_by_url(ftp_url: str) -> TDocLSDetails | None`
    - `get_by_tdoc_id(tdoc_id: str) -> list[TDocLSDetails]`
    - `get_by_variant(ftp_url: str, variant: str) -> TDocLSDetails | None`

- [ ] **Step 1: Write the failing test**

Create `tests/integration/test_ls_sqlite_repo.py`:

```python
import datetime as dt

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.storage.db.models import TDoc, TDocCrLSDetailOrm
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository


@pytest.fixture
def repo():
    return SQLAlchemyLSParserRepository()


@pytest.fixture
def tdoc_row():
    sf = get_session_factory()
    with sf() as s:
        s.add(TDoc(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc", tdoc_type="LS", source="3GPP TSG"))
        s.commit()
    yield "R5-240001"


def test_upsert_then_get_by_url(repo, tdoc_row):
    repo.upsert(TDocLSDetails(
        tdoc_id=tdoc_row,
        ftp_url="tsg/ls/R5-240001.doc",
        variant="3gpp",
        title="LS on foo",
        source="3GPP TSG",
        attachments=( {"doc_number": "TR 38.901 v0.1.0", "description": "draft"}, ),
    ))
    got = repo.get_by_url("tsg/ls/R5-240001.doc")
    assert got is not None
    assert got.title == "LS on foo"
    assert got.variant == "3gpp"
    assert got.attachments[0]["doc_number"] == "TR 38.901 v0.1.0"


def test_get_by_tdoc_id_returns_all_revisions(repo, tdoc_row):
    for i in range(2):
        repo.upsert(TDocLSDetails(
            tdoc_id=tdoc_row,
            ftp_url=f"tsg/ls/R5-240001-{i}.doc",
            variant="3gpp",
            title=f"rev {i}",
        ))
    rows = repo.get_by_tdoc_id(tdoc_row)
    assert len(rows) == 2


def test_get_by_variant_filters(repo, tdoc_row):
    repo.upsert(TDocLSDetails(
        tdoc_id=tdoc_row, ftp_url="tsg/ls/R5-240001-a.doc",
        variant="3gpp", title="a",
    ))
    repo.upsert(TDocLSDetails(
        tdoc_id=tdoc_row, ftp_url="tsg/ls/R5-240001-b.doc",
        variant="ieee", title="b",
    ))
    assert repo.get_by_variant("tsg/ls/R5-240001-a.doc", "3gpp").title == "a"
    assert repo.get_by_variant("tsg/ls/R5-240001-b.doc", "ieee").title == "b"
    assert repo.get_by_variant("tsg/ls/R5-240001-a.doc", "ieee") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_ls_sqlite_repo.py -v`
Expected: FAIL — module does not exist yet.

- [ ] **Step 3: Add the Protocol**

In `src/doc3gpp/repository/protocols.py`, append after the existing CR Protocols:

```python
class LSParserRepository(Protocol):
    """Storage operations used by the LS service layer."""

    def upsert(self, details: TDocLSDetails) -> None:
        """Insert or update the LS sidecar row keyed by ``details.ftp_url``."""
        ...

    def get_by_url(self, ftp_url: str) -> TDocLSDetails | None:
        """Return the LS row whose URL matches, or ``None``.

        ``ftp_url`` is the relative path (PK); callers that hold a
        full upstream URL must normalise via
        :func:`doc3gpp.scraping.ftp_source.normalize_ftp_path` first.
        """
        ...

    def get_by_tdoc_id(self, tdoc_id: str) -> list[TDocLSDetails]:
        """Return every LS row for ``tdoc_id``, ordered by ``ftp_url`` ASC."""
        ...

    def get_by_variant(
        self, ftp_url: str, variant: str
    ) -> TDocLSDetails | None:
        """Return the LS row matching both URL and variant tag."""
        ...
```

(Read the file first; add `from doc3gpp.models.tdoc_ls import TDocLSDetails`
near the top if the existing imports don't cover it.)

- [ ] **Step 4: Create the SQL impl**

Create `src/doc3gpp/storage/repositories/tdoc_cr_ls_sql.py`:

```python
"""SQLAlchemy-backed implementation of :class:`LSParserRepository`.

Stores LS header extractions in ``tdoc_cr_ls_details`` — one row per
immutable ``ftp_url`` with a foreign-key ``tdoc_id`` into
``tdocs.tdoc_id`` (``ON DELETE CASCADE``). The ``attachments_json``
column holds the parsed attachments as gzip-compressed UTF-8 JSON via
:func:`doc3gpp.storage.compression.compress_json`. The ``variant``
column tags the format family (``"3gpp"``, ``"ieee"``, ``"etsi"``)
so the show record and search index can branch on it.
"""

from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.storage.compression import compress_json, decompress_json
from doc3gpp.storage.db.models import TDocCrLSDetailOrm
from doc3gpp.storage.db.session import get_session_factory

logger = logging.getLogger(__name__)


class SQLAlchemyLSParserRepository:
    """SQLAlchemy implementation of :class:`LSParserRepository`."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._ensured = False

    def _ensure_table_exists(self) -> None:
        if self._ensured:
            return
        from doc3gpp.storage.db.base import Base
        from doc3gpp.storage.db.session import get_engine

        try:
            with self._session_factory() as session:
                session.execute(text("SELECT 1 FROM tdoc_cr_ls_details LIMIT 0"))
        except OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "doesn't exist" in msg:
                Base.metadata.create_all(bind=get_engine())
                with self._session_factory() as session:
                    session.execute(text("SELECT 1 FROM tdoc_cr_ls_details LIMIT 0"))
            else:
                raise
        self._ensured = True

    def upsert(self, details: TDocLSDetails) -> None:
        self._ensure_table_exists()
        if not details.ftp_url:
            raise ValueError(
                "TDocLSDetails requires a non-empty ftp_url for URL-keyed upsert"
            )
        ftp_url = details.ftp_url
        with self._session_factory() as session:
            row = session.get(TDocCrLSDetailOrm, ftp_url)
            if row is None:
                row = TDocCrLSDetailOrm(ftp_url=ftp_url, tdoc_id=details.tdoc_id)
                session.add(row)
            else:
                row.tdoc_id = details.tdoc_id
            _details_to_orm(row, details)
            session.commit()

    def get_by_url(self, ftp_url: str) -> TDocLSDetails | None:
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrLSDetailOrm, ftp_url)
        if row is None:
            return None
        return _orm_to_details(row)

    def get_by_tdoc_id(self, tdoc_id: str) -> list[TDocLSDetails]:
        self._ensure_table_exists()
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(TDocCrLSDetailOrm)
                    .where(TDocCrLSDetailOrm.tdoc_id == tdoc_id)
                    .order_by(TDocCrLSDetailOrm.ftp_url.asc())
                ).all()
            )
        return [_orm_to_details(r) for r in rows]

    def get_by_variant(
        self, ftp_url: str, variant: str
    ) -> TDocLSDetails | None:
        self._ensure_table_exists()
        with self._session_factory() as session:
            row = session.get(TDocCrLSDetailOrm, ftp_url)
        if row is None or row.variant != variant:
            return None
        return _orm_to_details(row)


def _details_to_orm(target: TDocCrLSDetailOrm, details: TDocLSDetails) -> None:
    """Copy :class:`TDocLSDetails` fields onto an ORM instance.

    Excludes ``ftp_url`` (PK) and ``tdoc_id`` (FK, handled by
    :meth:`upsert`).
    """
    target.variant = details.variant
    target.title = details.title
    target.response_to_doc = details.response_to_doc
    target.response_to_title = details.response_to_title
    target.response_to_group = details.response_to_group
    target.release = details.release
    target.work_item_name = details.work_item_name
    target.work_item_code = details.work_item_code
    target.source = details.source
    target.to_groups = details.to_groups or None
    target.cc_groups = details.cc_groups or None
    target.attachments_json = (
        compress_json([dict(a) for a in details.attachments])
        if details.attachments
        else None
    )
    target.parser_version = details.parser_version


def _orm_to_details(row: TDocCrLSDetailOrm) -> TDocLSDetails:
    """Reconstruct a :class:`TDocLSDetails` from an ORM row."""
    raw = decompress_json(row.attachments_json) if row.attachments_json else []
    if not isinstance(raw, list):
        raw = []
    attachments: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        attachments.append(
            {
                "doc_number": str(entry.get("doc_number", "")),
                "description": str(entry.get("description", "")),
            }
        )
    return TDocLSDetails(
        ftp_url=row.ftp_url,
        tdoc_id=row.tdoc_id,
        variant=row.variant,
        title=row.title,
        response_to_doc=row.response_to_doc,
        response_to_title=row.response_to_title,
        response_to_group=row.response_to_group,
        release=row.release,
        work_item_name=row.work_item_name,
        work_item_code=row.work_item_code,
        source=row.source,
        to_groups=row.to_groups or "",
        cc_groups=row.cc_groups or "",
        attachments=tuple(attachments),
        parser_version=row.parser_version,
        extracted_at=row.extracted_at,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/integration/test_ls_sqlite_repo.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/storage/repositories/tdoc_cr_ls_sql.py src/doc3gpp/repository/protocols.py tests/integration/test_ls_sqlite_repo.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/tdoc_cr_ls_sql.py tests/integration/test_ls_sqlite_repo.py
git commit -m "feat(storage): add LSParserRepository Protocol and SQL impl"
```

---

## Task 8: Factory wiring

**Files:**
- Modify: `src/doc3gpp/services/factory.py`

**Interfaces:**
- Produces: `build_ls_repository() -> LSParserRepository` helper

- [ ] **Step 1: Locate and read the factory**

Find `build_tdoc_cr_repository` / `build_tdoc_cr_ttcn_repository` and add `build_ls_repository` next to them. Mirror the same shape: optional `session_factory` for tests.

- [ ] **Step 2: Add the helper**

In `src/doc3gpp/services/factory.py`, after `build_tdoc_cr_ttcn_repository`:

```python
def build_ls_repository(
    session_factory=None,
) -> LSParserRepository:
    """Construct an :class:`LSParserRepository` for the LS sidecar table."""
    return SQLAlchemyLSParserRepository(session_factory=session_factory)
```

(Add `from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository`
near the top if missing.)

- [ ] **Step 3: Run the existing test suite**

Run: `pytest tests/ -q -x`
Expected: PASS — the factory change is additive; nothing imports `build_ls_repository` yet.

- [ ] **Step 4: Lint**

Run: `ruff check src/doc3gpp/services/factory.py`
Expected: clean

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/services/factory.py
git commit -m "feat(services): add build_ls_repository factory helper"
```

---

## Task 9: TDocCrService LS sidecar write

**Files:**
- Modify: `src/doc3gpp/services/tdoc_cr_service.py`
- Create: `tests/unit/test_tdoc_cr_service_ls.py`

**Interfaces:**
- Produces: `TDocCrService.__init__` gains `ls_repository: LSParserRepository | None`. The `extract_*` paths call `parser.parse_ls(...)` when the resolved parser is `LSParserBase`, then upsert.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_tdoc_cr_service_ls.py` (this is a unit test that uses a stub repo + the real registry):

```python
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.services.tdoc_cr_service import TDocCrService


@pytest.fixture
def ls_repo_stub():
    stub = MagicMock()
    stub.upsert = MagicMock()
    return stub


_LS_MD = """3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001

Title:	LS on 5G_eHealth WI status update
Response to:	LS R5-234567 on 5G_eHealth WI status from RAN WG3
Source:	3GPP TSG RAN WG2
To:	RAN WG3
"""


def test_ls_sidecar_is_written_for_ls_rows(ls_repo_stub, tmp_path, monkeypatch):
    # Stub the rest of the service dependencies to avoid hitting the
    # network / cache / FTS5.
    cache = MagicMock()
    scraper = MagicMock()
    scraper.fetch_bytes = MagicMock(return_value=_LS_MD.encode("utf-8"))
    cr_repo = MagicMock()
    cr_repo.upsert = MagicMock()
    cr_ttcn_repo = MagicMock()
    cr_change_repo = MagicMock()
    tdoc_repo = MagicMock()
    tdoc_repo.get = MagicMock(return_value=MagicMock(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc", tdoc_type="LS", source="3GPP TSG"))

    svc = TDocCrService(
        cache=cache, scraper_client=scraper,
        cr_repository=cr_repo, cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_repo,
        tdoc_repository=tdoc_repo,
        ls_repository=ls_repo_stub,
    )

    svc.extract_from_bytes(
        _LS_MD.encode("utf-8"), tdoc_id="R5-240001",
        ftp_url="tsg/ls/R5-240001.doc", tdoc_type="LS", source="3GPP TSG",
    )

    ls_repo_stub.upsert.assert_called_once()
    details = ls_repo_stub.upsert.call_args[0][0]
    assert isinstance(details, TDocLSDetails)
    assert details.variant == "3gpp"
    assert details.title == "LS on 5G_eHealth WI status update"


def test_ls_sidecar_is_not_written_for_cr_rows(ls_repo_stub):
    cache = MagicMock()
    scraper = MagicMock()
    cr_repo = MagicMock()
    cr_repo.upsert = MagicMock()
    cr_ttcn_repo = MagicMock()
    cr_change_repo = MagicMock()
    tdoc_repo = MagicMock()

    svc = TDocCrService(
        cache=cache, scraper_client=scraper,
        cr_repository=cr_repo, cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_repo,
        tdoc_repository=tdoc_repo,
        ls_repository=ls_repo_stub,
    )

    # CR markdown (header has CHANGE REQUEST) — should NOT touch ls_repo
    cr_md = "| CHANGE REQUEST |\n| 38.300 | CR | 1234 | rev | 1 | Current version: 17.1.0 |\n"
    svc.extract_from_bytes(
        cr_md.encode("utf-8"), tdoc_id="R5-240099",
        ftp_url="tsg/cr/R5-240099.doc", tdoc_type="CR", source="Ericsson",
    )

    ls_repo_stub.upsert.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_tdoc_cr_service_ls.py -v`
Expected: FAIL — service signature doesn't accept `ls_repository` or `source` yet.

- [ ] **Step 3: Read the existing service**

Read `src/doc3gpp/services/tdoc_cr_service.py`. Identify:

- The `__init__` signature (around line 377 per codegraph).
- The three extract entry points: `extract_many`, `extract_from_url`, `extract_from_bytes`.
- The block where the registry resolves the parser and the existing CR sidecar writes fire.

The exact line numbers depend on the current file — read first.

- [ ] **Step 4: Extend the service**

Add `ls_repository` to `__init__` (defaults to `None`); thread it through the three entry points. After the existing CR sidecar write block in each entry point, add:

```python
from doc3gpp.parsers.ls.ls_parsers import LSParserBase

if isinstance(parser, LSParserBase) and self._ls_repository is not None:
    ls_result = parser.parse_ls(markdown, tdoc_id=tdoc_id)
    if ls_result.cover is not None:
        details = dataclasses.replace(
            ls_result.cover,
            tdoc_id=tdoc_id,
            ftp_url=ftp_url,
        )
        self._ls_repository.upsert(details)
```

For `extract_many` (DB-mode batch), the service reads the tdoc row first; pull `tdoc_type` and `source` from the row and pass them to the registry's `resolve(...)` call.

Threading `source` into `extract_from_url` / `extract_from_bytes`: add `source: str | None = None` keyword to both signatures; pass it to the registry.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_tdoc_cr_service_ls.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Run the full unit + integration suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS — no existing test should break because the new
behaviour is gated on `isinstance(parser, LSParserBase)` and the
LS parser only matches `tdoc_type='LS'`.

- [ ] **Step 7: Lint**

Run: `ruff check src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ls.py`
Expected: clean

- [ ] **Step 8: Commit**

```bash
git add src/doc3gpp/services/tdoc_cr_service.py tests/unit/test_tdoc_cr_service_ls.py
git commit -m "feat(services): write tdoc_cr_ls_details sidecar for LS rows"
```

---

## Task 10: Search index projection

**Files:**
- Modify: `src/doc3gpp/services/search_service.py`
- Modify: `src/doc3gpp/services/embedding/embedder.py`
- Create: `tests/integration/test_ls_search_sqlite.py`

**Interfaces:**
- Produces: when an LS row has a sidecar, `cover_text` and the embed text include the LS fields.

- [ ] **Step 1: Locate the cover-text projection**

Read `src/doc3gpp/services/search_service.py`. Find where `cover_text` is built for CR rows (it concatenates cover fields). The same code path handles LS rows because the LS sidecar fields are projected into `TDocCRDetails.title` or a parallel projection helper — confirm by reading.

The pattern from the recent `summary_of_change` commit: each new field gets its own projection line.

- [ ] **Step 2: Write the failing test**

Create `tests/integration/test_ls_search_sqlite.py`:

```python
import datetime as dt

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchRepository


@pytest.fixture
def ls_row():
    sf = get_session_factory()
    with sf() as s:
        s.add(TDoc(
            tdoc_id="R5-240001",
            ftp_url="tsg/ls/R5-240001.doc",
            tdoc_type="LS",
            source="3GPP TSG",
            title="LS on 5G_eHealth WI status update",
            uploaded_date=dt.date(2026, 8, 1),
        ))
        s.commit()
    yield "R5-240001"


def test_search_index_includes_ls_title(ls_row):
    from doc3gpp.storage.repositories.tdoc_cr_ls_sql import SQLAlchemyLSParserRepository
    SQLAlchemyLSParserRepository().upsert(TDocLSDetails(
        tdoc_id=ls_row,
        ftp_url="tsg/ls/R5-240001.doc",
        variant="3gpp",
        title="LS on 5G_eHealth WI status update",
        source="3GPP TSG",
        to_groups="RAN WG3\nRAN WG4",
        response_to_title="5G_eHealth WI status",
    ))
    repo = SQLAlchemySearchRepository()
    repo.upsert(ls_row)
    # Verify FTS5 returns the LS row when querying its title
    hits = repo.search("5G_eHealth", limit=10)
    assert any(h["tdoc_id"] == ls_row for h in hits)
```

- [ ] **Step 3: Add the projection**

In the function that builds `cover_text` (in `search_service.py`), add the LS branch:

```python
ls_row = self._ls_repository.get_by_url(tdoc.ftp_url) if tdoc.ftp_url else None
if ls_row is not None:
    parts.extend(filter(None, [
        ls_row.title,
        ls_row.response_to_title,
        ls_row.to_groups.replace("\n", " "),
        ls_row.cc_groups.replace("\n", " "),
    ]))
```

(In `embedding/embedder.py`, mirror the same logic in `build_embed_text`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_ls_search_sqlite.py -v`
Expected: PASS

- [ ] **Step 5: Run the full integration suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS

- [ ] **Step 6: Lint**

Run: `ruff check src/doc3gpp/services/search_service.py src/doc3gpp/services/embedding/embedder.py tests/integration/test_ls_search_sqlite.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/services/search_service.py src/doc3gpp/services/embedding/embedder.py tests/integration/test_ls_search_sqlite.py
git commit -m "feat(search): project LS fields into cover_text and embed text"
```

---

## Task 11: Show record + CLI renderers

**Files:**
- Modify: `src/doc3gpp/models/tdoc_show.py`
- Modify: `src/doc3gpp/cli.py`

**Interfaces:**
- Produces: `TDocShowRecord.ls: TDocLSDetails | None` (omit-when-null); `TDocShowRecordByUrl.ls: TDocLSDetails | None` (omit-when-null); `TDocShowRepos.ls: LSParserRepository`. CLI renderers emit the `ls` block in JSON / Markdown / table modes.

- [ ] **Step 1: Write the failing test**

Read `src/doc3gpp/models/tdoc_show.py` to find `TDocShowRecord`, `TDocShowRecordByUrl`, and `TDocShowRepos`. Add `ls` fields with `default=None`.

Create `tests/unit/test_tdoc_show_record_ls.py`:

```python
from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.models.tdoc_show import TDocShowRecord


def test_show_record_carries_ls_field():
    rec = TDocShowRecord(tdoc=None, cover=None, ttcn=None, changes=None, files=())
    assert rec.ls is None


def test_show_record_ls_can_be_set():
    details = TDocLSDetails(
        tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc",
        variant="3gpp", title="LS on foo",
    )
    rec = TDocShowRecord(tdoc=None, cover=None, ttcn=None, changes=None, files=(), ls=details)
    assert rec.ls.title == "LS on foo"
```

- [ ] **Step 2: Extend `TDocShowRecord` / `TDocShowRecordByUrl` / `TDocShowRepos`**

```python
@dataclass(slots=True)
class TDocShowRecord:
    ...
    ls: TDocLSDetails | None = None


@dataclass(slots=True)
class TDocShowRecordByUrl:
    ...
    ls: TDocLSDetails | None = None


@dataclass(slots=True)
class TDocShowRepos:
    tdoc: TDocRepository
    cr: TDocCrRepository
    cr_ttcn: TDocCrTTCNDetailRepository
    cr_change_details: TDocCrChangeDetailsRepository
    file: TDocFileRepository
    ls: LSParserRepository  # NEW
```

In `TDocShowRecord.from_tdoc_id` and `TDocShowRecordByUrl.from_ftp_url`, populate `ls` via `repos.ls.get_by_url(tdoc.ftp_url)`.

- [ ] **Step 3: Extend the CLI renderers**

In `src/doc3gpp/cli.py`, find `_render_tdoc_show_json` / `_render_tdoc_show_markdown` / `_render_tdoc_show_table` (and the `_by_url` siblings). Add an `ls` branch that emits a block when `record.ls is not None`. Follow the existing `cover` block pattern. For Markdown, render `## LS` then `key: value` lines. For table, add `[LS Cover]` heading. `--compact` strips it.

- [ ] **Step 4: Run tests + lint**

Run: `pytest tests/unit/test_tdoc_show_record_ls.py tests/unit/test_compact_helpers.py -v`
Run: `ruff check src/doc3gpp/models/tdoc_show.py src/doc3gpp/cli.py tests/unit/test_tdoc_show_record_ls.py`
Expected: PASS, clean

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/models/tdoc_show.py src/doc3gpp/cli.py tests/unit/test_tdoc_show_record_ls.py
git commit -m "feat(cli): render LS block in tdoc show"
```

---

## Task 12: Web LS Cover card

**Files:**
- Modify: `src/doc3gpp/web/routes/tdocs.py`
- Modify: `src/doc3gpp/web/templates/tdoc_show.html`

**Interfaces:**
- Produces: web route deps gain `ls_repo: LSParserRepository`; template renders an LS Cover card when `tdoc.tdoc_type == 'LS'`.

- [ ] **Step 1: Read the existing cover card**

Read `src/doc3gpp/web/templates/tdoc_show.html` to find the existing CR Cover card markup. Mirror its structure for the LS variant.

- [ ] **Step 2: Add the LS Cover card**

Append after the CR Cover card:

```html
{% if tdoc.tdoc_type == 'LS' and ls %}
<section class="card">
  <h2>LS Cover</h2>
  <dl>
    <dt>Title</dt><dd>{{ ls.title or '—' }}</dd>
    <dt>Response to</dt><dd>
      {% if ls.response_to_doc %}<code>{{ ls.response_to_doc }}</code>{% endif %}
      {% if ls.response_to_group %} from {{ ls.response_to_group }}{% endif %}
    </dd>
    <dt>Release</dt><dd>{{ ls.release or '—' }}</dd>
    <dt>Work Item</dt><dd>
      {{ ls.work_item_name or '—' }}
      {% if ls.work_item_code %} (<code>{{ ls.work_item_code }}</code>){% endif %}
    </dd>
    <dt>Source</dt><dd>{{ ls.source or '—' }}</dd>
    <dt>To</dt><dd>
      {% for g in (ls.to_groups or '').splitlines() if g %}
        <span class="tag">{{ g }}</span>
      {% endfor %}
    </dd>
    <dt>Cc</dt><dd>
      {% for g in (ls.cc_groups or '').splitlines() if g %}
        <span class="tag">{{ g }}</span>
      {% endfor %}
    </dd>
    <dt>Attachments</dt><dd>
      {% if ls.attachments %}
        <ul>
        {% for a in ls.attachments %}
          <li><code>{{ a.doc_number }}</code>{% if a.description %} — {{ a.description }}{% endif %}</li>
        {% endfor %}
        </ul>
      {% else %}—{% endif %}
    </dd>
  </dl>
</section>
{% elif tdoc.tdoc_type == 'LS' %}
<section class="card"><h2>LS Cover</h2><p>No LS sidecar parsed yet. Run <code>doc3gpp tdoc parse --tdoc {{ tdoc.tdoc_id }}</code>.</p></section>
{% endif %}
```

Gate the CR Cover card on `tdoc.tdoc_type != 'LS'` so the two are mutually exclusive.

- [ ] **Step 3: Add the route dep**

In `src/doc3gpp/web/routes/tdocs.py`, add:

```python
from doc3gpp.repository.protocols import LSParserRepository

def get_ls_repository(...) -> LSParserRepository:
    return build_ls_repository()
```

Wire it into the TDoc detail route + JSON envelope handler. The JSON envelope gains `"ls": {...}` (omit-when-null).

- [ ] **Step 4: Write the failing test**

Create `tests/unit/test_web_routes_ls.py`:

```python
from fastapi.testclient import TestClient


def test_ls_cover_card_renders_for_ls_row(monkeypatch):
    # Stub the repos to return an LS sidecar.
    ...
```

(Stub the relevant repos; assert the rendered HTML contains "LS Cover" and the LS fields.)

- [ ] **Step 5: Run tests + lint**

Run: `pytest tests/unit/test_web_routes_ls.py tests/unit/test_web_routes.py -v`
Run: `ruff check src/doc3gpp/web/ tests/unit/test_web_routes_ls.py`
Expected: PASS, clean

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/ tests/unit/test_web_routes_ls.py
git commit -m "feat(web): render LS Cover card on TDoc detail page"
```

---

## Task 13: MCP `get_tdoc` includes `ls`

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py`

**Interfaces:**
- Produces: `get_tdoc` returns the `ls` block when present.

- [ ] **Step 1: Add the repo + read path**

In `get_tdoc` (around line 272 per codegraph), add `ls=SQLAlchemyLSParserRepository()` to the `TDocShowRepos(...)` constructor. The `TDocShowRecord.from_tdoc_id` already populates `ls` once the dataclass has the field — verify and run.

- [ ] **Step 2: Run the MCP test suite**

Run: `pytest tests/unit/test_mcp_*.py -v -k tdoc`
Expected: PASS — `get_tdoc` JSON shape gains `ls: {...}` for LS rows, omitted otherwise.

- [ ] **Step 3: Lint**

Run: `ruff check src/doc3gpp/web/mcp_server.py`
Expected: clean

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py
git commit -m "feat(mcp): include ls block in get_tdoc"
```

---

## Task 14: Synthesised LS fixture

**Files:**
- Create: `tests/fixtures/ls/_generate.py`
- Create: `tests/fixtures/ls/LS_sample_r5_240001.md`

**Interfaces:**
- Produces: a committed `.md` fixture synthesised from the 3GPP LS template. Generated once, then committed; the generator script is documentation of how the fixture was built.

- [ ] **Step 1: Write the generator**

Create `tests/fixtures/ls/_generate.py`:

```python
"""Render ``LS_sample_r5_240001.md`` from the 3GPP LS template.

Run once to refresh the fixture; the rendered file is the canonical
fixture used by the unit + integration tests.
"""

from pathlib import Path

FIXTURE = """3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001

Title:\tLS on 5G_eHealth WI status update
Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3
Release:\tRelease 17
Work Item:\t5G_eHealth (WI-123456)

Source:\t3GPP TSG RAN WG2
To:\tRAN WG3, RAN WG4
Cc:\tSA WG2

Attachments:\tTR 38.901 v0.1.0 [draft].\tTS 38.300 v17.1.0.

1\tOverall description
…  (body omitted; the parser only inspects the header)

"""


def main() -> None:
    out = Path(__file__).parent / "LS_sample_r5_240001.md"
    out.write_text(FIXTURE, encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Generate and commit the fixture**

Run: `python tests/fixtures/ls/_generate.py`
Run: `git add tests/fixtures/ls/`
Run: `git commit -m "test(fixtures): add synthesised 3GPP LS sample"`

---

## Task 15: End-to-end integration test (`--from-path`)

**Files:**
- Create: `tests/integration/test_ls_e2e_sqlite.py`

**Interfaces:**
- Produces: end-to-end test that runs `tdoc parse --from-path`, writes the LS sidecar, then `tdoc show --tdoc` returns the `ls` block.

- [ ] **Step 1: Write the test**

```python
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def doc3gpp_cmd(tmp_path_factory):
    # Use a per-test sqlite DB so each run is isolated.
    db = tmp_path_factory.mktemp("db") / "test.db"
    cfg = tmp_path_factory.mktemp("cfg") / "doc3gpp.toml"
    cfg.write_text(f"""
[db]
url = "sqlite:///{db}"
""", encoding="utf-8")
    return ["doc3gpp", "--config", str(cfg)]


def test_end_to_end_ls_parse(doc3gpp_cmd):
    subprocess.run([*doc3gpp_cmd, "db", "init"], check=True)
    fixture = Path("tests/fixtures/ls/LS_sample_r5_240001.md")
    subprocess.run(
        [*doc3gpp_cmd, "tdoc", "parse", "--from-path", str(fixture), "--tdoc", "R5-240001",
         "--ftp-url", "tsg/ls/R5-240001.doc", "--type", "LS"],
        check=True,
    )
    result = subprocess.run(
        [*doc3gpp_cmd, "tdoc", "show", "--tdoc", "R5-240001", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    assert '"ls"' in result.stdout
    assert '"5G_eHealth"' in result.stdout
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/integration/test_ls_e2e_sqlite.py -v`
Expected: PASS

- [ ] **Step 3: Lint**

Run: `ruff check tests/integration/test_ls_e2e_sqlite.py`
Expected: clean

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_ls_e2e_sqlite.py
git commit -m "test(ls): cover end-to-end tdoc parse --from-path + tdoc show"
```

---

## Task 16: Documentation sync

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/cli.md`
- Modify: `docs/code-map.md`
- Modify: `docs/architecture.md`

- [ ] **Step 1: Update README.md**

Add a one-line bullet to the features list: "Parse 3GPP Liaison Statement (LS) TDoc headers (`tdoc_cr_ls_details` sidecar)."

- [ ] **Step 2: Update AGENTS.md**

Add to the "Where to look" table:

```
| Add an LS TDoc parser | `parsers/ls/` (incl. `variants/`) + `models/tdoc_ls.py` + `storage/repositories/tdoc_cr_ls_sql.py` |
```

- [ ] **Step 3: Update docs/cli.md`

Add the `ls` block under `tdoc show --tdoc` and `tdoc show --ftp-url`. Same omit-when-null convention as the `cover` block.

- [ ] **Step 4: Update docs/code-map.md`

Add the new symbols: `TDocLSDetails`, `TDocLSParserResult`, `LSParserBase`, `ThreeGPPLSParser`, `LSCoverPageParser`, `SQLAlchemyLSParserRepository`.

- [ ] **Step 5: Update docs/architecture.md`

Add the LS row to the layered diagram and the ORM schema section. Mention the multi-variant framework under "Future variants" with IEEE / ETSI as v2 hooks.

- [ ] **Step 6: Lint + test**

Run: `ruff check .`
Run: `./scripts/test_sqlite.sh`
Expected: clean, PASS

- [ ] **Step 7: Commit**

```bash
git add README.md AGENTS.md docs/cli.md docs/code-map.md docs/architecture.md
git commit -m "docs: sync README/AGENTS/cli/code-map/architecture for LS parser"
```

---

## Task 17: Full verification

- [ ] **Step 1: Lint**

Run: `ruff check .`
Expected: clean

- [ ] **Step 2: Full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS

- [ ] **Step 3: Manual smoke**

Run:
```bash
doc3gpp db init --target project
doc3gpp tdoc parse --from-path tests/fixtures/ls/LS_sample_r5_240001.md \
    --tdoc R5-240001 --ftp-url tsg/ls/R5-240001.doc --type LS
doc3gpp tdoc show --tdoc R5-240001 --format json | jq '.ls.title'
doc3gpp search query "5G_eHealth" --quiet
```
Expected: title is `LS on 5G_eHealth WI status update`; search returns the LS row.

- [ ] **Step 4: Acceptance criteria check**

Verify every checkbox in the spec's Acceptance Criteria section is satisfied.

- [ ] **Step 5: Final commit (no-op if previous commits covered it)**

```bash
git status   # should be clean
```

---

## Self-Review Notes (post-write)

- All spec sections mapped: 16 implementation tasks + 1 verification task.
- No `TBD` / `TODO` / "implement later" placeholders.
- `TDocLSDetails`, `TDocLSParserResult`, `LSParserBase`, `ThreeGPPLSParser`, `SQLAlchemyLSParserRepository` names match across tasks.
- `parser_version` defaults match across tasks (`"1.0.0"` for v1).
- `source` kwarg added to Protocol in Task 5; threaded through `extract_*` in Task 9.
- `variant` column on ORM (Task 6) matches the dataclass default (Task 1).
- Test files are concrete and runnable; no `test_xxx_to_be_determined` placeholders.
