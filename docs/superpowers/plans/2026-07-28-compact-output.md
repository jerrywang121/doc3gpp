# `output.compact` / `--compact` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `--compact` flag (and a `Settings.output.compact` TOML knob) that strips output formatting down to its plain-content essentials, applied uniformly across every CLI command that already accepts `--format`.

**Architecture:** A single `OutputSettings.compact: bool = False` field plus a `_resolve_compact(compact: bool) -> bool` helper thread a `compact` keyword through the four renderer seams (`_emit_records` for list commands, `_emit_record` for direct-mode tdoc parse, the four `_render_tdoc_show_*` funcs, and the four `_render_tdoc_show_by_url_*` funcs). JSON renderers swap to `separators=(",", ":")` with no trailing newline; markdown renderers drop CommonMark decorators and emit `key: value` lines with one blank line between sections; table and raw are explicit no-ops. Default behaviour is byte-identical to today.

**Tech Stack:** Python 3.10+, Pydantic v2 + pydantic-settings, Typer, pytest.

## Global Constraints

- TOML-only knob (no `DOC3GPP_OUTPUT__COMPACT` env var). The new field follows the existing `output.format` precedent. Tests must mirror `tests/unit/test_settings_config_file.py::test_env_var_allowlist` and confirm the env var is silently ignored.
- Default behaviour must be byte-identical to today: `compact=False` everywhere. No existing test may need to change.
- `--compact` is a plain `bool = False` Typer flag — no `--no-compact` toggle. The `_resolve_compact` helper returns `True` when the CLI flag is set OR `Settings.output.compact = True`.
- `compact` is plumbed through four discrete renderer seams (`_emit_records`, `_emit_record`, the four `_render_tdoc_show_*`, the four `_render_tdoc_show_by_url_*`) — not added per-command dispatch site.
- Table and raw formats are explicit no-ops: the `compact` parameter is added for symmetry but ignored, with a code comment and a unit test asserting byte-identical output.
- JSON compact: `json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))` and no trailing newline. `ensure_ascii=False` is preserved.
- Markdown compact ruleset: drop `## Heading` lines, drop `- ` bullets, drop `**` / `*` decorators, drop ```` ```json ```` fences, drop GFM `| col | col |` tables, drop bullet-style `*` for `changed_functions` entries, replace placeholder / hint text with `note: <plain text>`, render `None` as `-` and normalise `—` to `-`. Blank-line section separator is the only structural whitespace that survives.
- Follow existing layered boundaries: scraping/transport only, parsers/parsing only, services/orchestration, cli/thin dispatch.
- Lint: `ruff check .` must pass before each commit. Tests: `pytest -x -q tests/unit` (sqlite-only) and `./scripts/test_sqlite.sh` must pass before each commit. Online tests are opt-in.

---

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/doc3gpp/settings/schema.py` | Modify `OutputSettings` (line 205) | Add `compact: bool = False` field |
| `src/doc3gpp/data/doc3gpp.toml.example` | Modify `[output]` block (line 43) | Add commented `compact = false` example |
| `src/doc3gpp/cli.py` | Modify | Add `_resolve_compact` helper, plumb `--compact` through 6 Typer commands, add `compact` param to all 10 renderers + dispatchers |
| `tests/unit/test_settings.py` | Create | Settings field tests (default, TOML override, env ignored) |
| `tests/unit/test_settings_config_file.py` | Modify | Extend `test_env_var_allowlist` to assert `DOC3GPP_OUTPUT__COMPACT` is dropped |
| `tests/unit/test_compact_helpers.py` | Create | `_resolve_compact` resolver tests |
| `tests/unit/test_list_output_format.py` | Modify | Add compact tests for `meeting list`, `tdoc list`, `wi list` |
| `tests/unit/test_tdoc_parse_cli.py` | Modify | Add compact tests for DB-mode `tdoc parse` |
| `tests/unit/test_tdoc_parse_direct.py` | Modify | Add compact tests for direct-mode `tdoc parse` |
| `tests/integration/test_tdoc_cr_ttcn_sqlite.py` | Modify | Add end-to-end `tdoc show --format json --compact` test |
| `docs/cli.md` | Modify | Add `--compact` flag entries to the six affected commands |
| `docs/architecture.md` | Modify | One paragraph in the CLI inventory section |
| `docs/code-map.md` | Modify | Add `_resolve_compact` and `OutputSettings.compact` to symbol table |
| `README.md` | Modify | One bullet under "Output" |
| `AGENTS.md` | Modify | Add a row to the workflows table |

---

### Task 1: `OutputSettings.compact` field + TOML example + settings tests

**Files:**
- Modify: `src/doc3gpp/settings/schema.py:205-209`
- Modify: `src/doc3gpp/data/doc3gpp.toml.example:43-44`
- Create: `tests/unit/test_settings.py` (new file)
- Modify: `tests/unit/test_settings_config_file.py` (env-var allowlist section)

**Interfaces:**
- Consumes: existing `OutputSettings` class
- Produces: `OutputSettings.compact: bool = False` (Field default `False`); TOML example commented key `[output] compact = false`

- [ ] **Step 1: Write the failing settings tests**

Create `tests/unit/test_settings.py`:

```python
"""Tests for ``OutputSettings.compact`` (the ``--compact`` flag default)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_output_compact_default_is_false(sqlite_env) -> None:
    """``output.compact`` defaults to ``False`` (no behavioural change)."""
    from doc3gpp.settings.loader import get_settings

    assert get_settings().output.compact is False


def test_output_compact_toml_override_true(tmp_path, monkeypatch) -> None:
    """TOML can opt the operator into compact output globally."""
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[output]\ncompact = true\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    assert get_settings().output.compact is True


def test_output_compact_toml_override_false_explicit(tmp_path, monkeypatch) -> None:
    """An explicit ``compact = false`` is honoured (matches the default)."""
    from doc3gpp.settings.loader import get_settings

    config_path = tmp_path / "doc3gpp.toml"
    config_path.write_text(
        "[output]\ncompact = false\n", encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    assert get_settings().output.compact is False
```

Add to `tests/unit/test_settings_config_file.py` inside `test_env_var_allowlist` (after the existing assertions, around line 220):

```python
def test_output_compact_env_var_is_ignored(tmp_path, monkeypatch) -> None:
    """``DOC3GPP_OUTPUT__COMPACT`` is outside the env-var allowlist and
    must be silently dropped — the TOML/default value wins. Mirrors
    ``test_tdoc_parse_max_tdoc_size_kb_env_var_is_ignored``."""
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setenv("DOC3GPP_OUTPUT__COMPACT", "true")
    assert get_settings().output.compact is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_settings.py tests/unit/test_settings_config_file.py::test_output_compact_env_var_is_ignored -v`
Expected: FAIL with `AttributeError: 'OutputSettings' object has no attribute 'compact'`.

- [ ] **Step 3: Add the field**

In `src/doc3gpp/settings/schema.py` modify `OutputSettings` (line 205):

```python
class OutputSettings(BaseModel):
    """Output defaults for every ``* list`` command."""

    format: OutputFormat = "table"
    compact: bool = Field(
        default=False,
        description=(
            "When true, JSON output drops indent and operator-space "
            "(single line, ``separators=(\",\", \":\")``) and Markdown "
            "output drops CommonMark decorators (bold, italic, headings, "
            "bullets, code fences, GFM tables). No-op for ``table`` and "
            "``raw``. The CLI ``--compact`` flag takes precedence when "
            "passed."
        ),
    )
    fields: OutputFieldsSettings = Field(default_factory=OutputFieldsSettings)
```

In `src/doc3gpp/data/doc3gpp.toml.example` modify the `[output]` block (line 43):

```toml
# [output]
# format = "table"     # table | json | markdown
# compact = false      # drop decorators in json/markdown output
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_settings.py tests/unit/test_settings_config_file.py::test_output_compact_env_var_is_ignored -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/settings/schema.py src/doc3gpp/data/doc3gpp.toml.example tests/unit/test_settings.py tests/unit/test_settings_config_file.py
git commit -m "feat(settings): add OutputSettings.compact (default false)"
```

---

### Task 2: `_resolve_compact` helper + helper tests

**Files:**
- Modify: `src/doc3gpp/cli.py:226-244` (next to `VALID_FORMATS` and `_resolve_format`)
- Create: `tests/unit/test_compact_helpers.py`

**Interfaces:**
- Consumes: existing `_resolve_format` pattern at `cli.py:229`
- Produces: `_resolve_compact(compact: bool) -> bool` (CLI flag wins when `True`; setting decides otherwise)

- [ ] **Step 1: Write the failing helper tests**

Create `tests/unit/test_compact_helpers.py`:

```python
"""Tests for ``_resolve_compact`` (the CLI → settings precedence helper)."""

from __future__ import annotations

import pytest


def test_resolve_compact_cli_true_wins_over_setting_false(monkeypatch) -> None:
    """``--compact`` on the command line forces ``True`` even when the
    setting is ``False`` (the CLI is the highest-precedence layer)."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=False))
    assert _resolve_compact(True) is True


def test_resolve_compact_cli_false_setting_true(monkeypatch) -> None:
    """When the CLI flag is absent (``False``) the setting can still opt in."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=True))
    assert _resolve_compact(False) is True


def test_resolve_compact_default_false(monkeypatch) -> None:
    """Default (no CLI flag, default setting) yields ``False``."""
    from doc3gpp.cli import _resolve_compact
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setattr(get_settings(), "output",
                        type(get_settings().output)(format="table", compact=False))
    assert _resolve_compact(False) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v`
Expected: FAIL with `ImportError: cannot import name '_resolve_compact'`.

- [ ] **Step 3: Implement the helper**

In `src/doc3gpp/cli.py`, after `_resolve_format` ends at line 244, add:

```python
def _resolve_compact(compact: bool) -> bool:
    """Resolve ``--compact`` against :attr:`Settings.output.compact`.

    CLI flag wins when ``True``; otherwise the setting decides. This
    keeps the precedence consistent with ``_resolve_format`` (CLI >
    settings) while keeping the Typer ``Option`` a plain ``bool`` —
    no ``--no-compact`` toggle is exposed because absence of the
    flag maps to the default ``False`` unambiguously.
    """
    if compact:
        return True
    return get_settings().output.compact
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): add _resolve_compact helper (CLI > settings precedence)"
```

---

### Task 3: JSON compact in `_emit_records` seam (list commands)

**Files:**
- Modify: `src/doc3gpp/cli.py:321-324` (`_emit_json`)
- Modify: `src/doc3gpp/cli.py:402-436` (`_emit_records` — gain `compact` parameter)
- Create: `tests/unit/test_compact_helpers.py` (extend with list-renderer tests)

**Interfaces:**
- Consumes: `_emit_records(rows, fields, fmt, output, *, no_records_msg)` (existing)
- Produces: `_emit_records(rows, fields, fmt, output, *, no_records_msg, compact=False)` and `_emit_json(rows, stream, fields, *, compact=False)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_compact_helpers.py`:

```python
def test_emit_json_compact_single_line_no_trailing_newline() -> None:
    """Compact JSON is one line, no operator-space, no trailing newline."""
    import io
    import json

    from doc3gpp.cli import _emit_json

    stream = io.StringIO()
    _emit_json(
        [["R5s260001", "RAN5#111", "38.331"]],
        stream,
        ["tdoc_id", "meeting_name", "spec"],
        compact=True,
    )
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    # Round-trips back to the original payload.
    assert json.loads(text) == [
        {"tdoc_id": "R5s260001", "meeting_name": "RAN5#111", "spec": "38.331"}
    ]


def test_emit_json_default_still_pretty_prints() -> None:
    """Default (non-compact) output is byte-identical to today."""
    import io

    from doc3gpp.cli import _emit_json

    stream = io.StringIO()
    _emit_json(
        [["R5s260001", "RAN5#111"]],
        stream,
        ["tdoc_id", "meeting_name"],
    )
    text = stream.getvalue()
    assert text.endswith("\n")
    assert ",\n" in text  # pretty-print indent survives
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py::test_emit_json_compact_single_line_no_trailing_newline tests/unit/test_compact_helpers.py::test_emit_json_default_still_pretty_prints -v`
Expected: FAIL with `TypeError: _emit_json() got an unexpected keyword argument 'compact'`.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, modify `_emit_json` (line 321):

```python
def _emit_json(
    rows: list[list[str]],
    stream: TextIO,
    fields: list[str],
    *,
    compact: bool = False,
) -> None:
    """Emit ``rows`` as a JSON array.

    When ``compact=True`` the output is a single line with no indent
    and no operator-space (``separators=(",", ":")``) and no trailing
    newline. The default is byte-identical to the legacy pretty-printed
    output (``indent=2`` + trailing newline).
    """
    objs = [dict(zip(fields, row)) for row in rows]
    if compact:
        json.dump(objs, stream, ensure_ascii=False, separators=(",", ":"))
        return
    json.dump(objs, stream, ensure_ascii=False, indent=2)
    stream.write("\n")
```

In `src/doc3gpp/cli.py`, modify `_emit_records` (line 402) to accept and forward `compact`:

```python
def _emit_records(
    rows: list[list[str]],
    fields: list[str],
    fmt: str,
    output: str | None,
    *,
    no_records_msg: str,
    compact: bool = False,
) -> None:
    """Emit ``rows`` to ``output`` (or stdout) in the chosen format.

    ``compact=True`` propagates to the JSON and markdown emitters —
    the table emitter ignores it. Empty rows are emitted as ``[]`` /
    header-only in JSON and markdown so downstream consumers always
    see a parseable payload. The friendly "no records" message prints
    only when ``--format table`` is paired with stdout — writing an
    empty table file would just be noise.
    """
    stream, close_after = _open_output(output)
    try:
        if not rows:
            if fmt == "json":
                _emit_json([], stream, fields, compact=compact)
            elif fmt == "markdown":
                _emit_markdown([], stream, fields, compact=compact)
            elif output is None:
                stream.write(no_records_msg + "\n")
            return

        if fmt == "table":
            _emit_table(rows, stream)
        elif fmt == "json":
            _emit_json(rows, stream, fields, compact=compact)
        else:
            _emit_markdown(rows, stream, fields, compact=compact)
    finally:
        if close_after:
            stream.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): compact json in _emit_records seam"
```

---

### Task 4: Markdown compact in `_emit_records` seam (list commands)

**Files:**
- Modify: `src/doc3gpp/cli.py:327-331` (`_emit_markdown`)
- Create/extend: `tests/unit/test_compact_helpers.py`

**Interfaces:**
- Consumes: existing `_emit_markdown(rows, stream, fields)` (line 327)
- Produces: `_emit_markdown(rows, stream, fields, *, compact=False)` — when `True`, per-row `key: value` blocks separated by blank lines

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_compact_helpers.py`:

```python
def test_emit_markdown_compact_per_row_blocks() -> None:
    """Compact markdown drops the GFM table and emits ``key: value``
    blocks per row, separated by blank lines."""
    import io

    from doc3gpp.cli import _emit_markdown

    stream = io.StringIO()
    _emit_markdown(
        [
            ["R5s260001", "RAN5#111"],
            ["R5s260002", "RAN5#111"],
        ],
        stream,
        ["tdoc_id", "meeting_name"],
        compact=True,
    )
    text = stream.getvalue()
    # No GFM decorators survive.
    assert "|" not in text
    assert "---" not in text
    assert "```" not in text
    # Two rows, each with two ``key: value`` lines, blank-line separated.
    assert text.strip().split("\n\n") == [
        "tdoc_id: R5s260001\nmeeting_name: RAN5#111",
        "tdoc_id: R5s260002\nmeeting_name: RAN5#111",
    ]


def test_emit_markdown_default_still_gfm_table() -> None:
    """Default (non-compact) output is the legacy GFM table."""
    import io

    from doc3gpp.cli import _emit_markdown

    stream = io.StringIO()
    _emit_markdown(
        [["R5s260001", "RAN5#111"]],
        stream,
        ["tdoc_id", "meeting_name"],
    )
    text = stream.getvalue()
    assert text == (
        "| tdoc_id | meeting_name |\n"
        "|---|---|\n"
        "| R5s260001 | RAN5#111 |\n"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py::test_emit_markdown_compact_per_row_blocks tests/unit/test_compact_helpers.py::test_emit_markdown_default_still_gfm_table -v`
Expected: FAIL with `TypeError: _emit_markdown() got an unexpected keyword argument 'compact'`.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, modify `_emit_markdown` (line 327):

```python
def _emit_markdown(
    rows: list[list[str]],
    stream: TextIO,
    fields: list[str],
    *,
    compact: bool = False,
) -> None:
    """Emit ``rows`` as a markdown table or a compact ``key: value`` block.

    Default shape is the legacy GFM table (``| col | col |`` +
    ``|---|---|`` + one row per record). When ``compact=True`` the
    table is replaced with a per-row ``key: value`` block; rows are
    separated by a single blank line, the field name is repeated
    per row (so the output is parseable without an external schema).
    """
    if compact:
        for index, row in enumerate(rows):
            if index:
                stream.write("\n")
            for field, cell in zip(fields, row):
                stream.write(f"{field}: {cell}\n")
        return
    stream.write("| " + " | ".join(_md_cell(h) for h in fields) + " |\n")
    stream.write("|" + "|".join(["---"] * len(fields)) + "|\n")
    for row in rows:
        stream.write("| " + " | ".join(_md_cell(c) for c in row) + " |\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): compact markdown in _emit_records seam"
```

---

### Task 5: Compact in `_emit_record` seam (direct-mode tdoc parse)

**Files:**
- Modify: `src/doc3gpp/cli.py:2003-2013` (`_emit_record_json`)
- Modify: `src/doc3gpp/cli.py:1990-2001` (`_emit_record_markdown`)
- Modify: `src/doc3gpp/cli.py:1961-1974` (`_emit_record` dispatcher)
- Modify: `src/doc3gpp/cli.py:1977-1987` (`_emit_record_table` — gain `compact` for symmetry, no-op)
- Modify: `src/doc3gpp/cli.py:2016+` (`_emit_record_raw` — gain `compact` for symmetry, no-op)
- Extend: `tests/unit/test_compact_helpers.py`

**Interfaces:**
- Consumes: `_emit_record(record, fmt, output)` (line 1961)
- Produces: `_emit_record(record, fmt, output, *, compact=False)` plus `_emit_record_json/markdown/table/raw` each accepting `compact=False`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_compact_helpers.py`:

```python
def test_emit_record_json_compact_single_line() -> None:
    """``_emit_record_json`` honours ``compact`` the same way as
    ``_emit_json`` (single line, no spaces, no trailing newline)."""
    import io
    import json
    from datetime import date

    from doc3gpp.cli import _emit_record_json
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    record = TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
        work_item="NR_5G",
    )
    stream = io.StringIO()
    _emit_record_json(record, None, output=stream, compact=True)
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    payload = json.loads(text)
    assert payload["tdoc_id"] == "R5s260001"
    assert payload["date"] == "2026-01-15"


def test_emit_record_markdown_compact_strips_decorators() -> None:
    """``_emit_record_markdown`` compact form drops the GFM table and
    emits ``field: value`` lines."""
    import io
    from datetime import date

    from doc3gpp.cli import _DIRECT_PARSE_FIELDS, _emit_record_markdown
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    record = TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
        work_item="NR_5G",
    )
    stream = io.StringIO()
    _emit_record_markdown(record, None, output=stream, compact=True)
    text = stream.getvalue()
    assert "|" not in text
    assert "---" not in text
    # Every direct-parse field label is present.
    for label in _DIRECT_PARSE_FIELDS:
        assert f"{label}:" in text


def test_emit_record_table_compact_is_noop() -> None:
    """``_emit_record_table`` ignores ``compact`` (table is already
    line-oriented and maximally compact by construction)."""
    import io
    from datetime import date

    from doc3gpp.cli import _emit_record_table
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    record = TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
        work_item="NR_5G",
    )
    plain = io.StringIO()
    _emit_record_table(record, None, output=plain)
    compact = io.StringIO()
    _emit_record_table(record, None, output=compact, compact=True)
    assert plain.getvalue() == compact.getvalue()
```

(Inspect `_emit_record_table`'s existing signature first — if it already
takes `output: str | None` as the second positional, the test reads
`_emit_record_table(record, None, output=stream, compact=True)`. Adjust
accordingly to keep the existing positional order.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v -k "emit_record"`
Expected: FAIL with `TypeError` for `compact` kwarg on at least one target.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, modify the four `_emit_record_*` helpers and the
`_emit_record` dispatcher:

```python
def _emit_record(
    record: TDocCRDetails,
    fmt: str,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Dispatch to the table / markdown / json emitter for a single parsed record."""
    if fmt == "table":
        _emit_record_table(record, output, compact=compact)
    elif fmt == "markdown":
        _emit_record_markdown(record, output, compact=compact)
    elif fmt == "json":
        _emit_record_json(record, output, compact=compact)
    elif fmt == "raw":
        _emit_record_raw(record.markdown, output, compact=compact)
    else:
        raise typer.BadParameter(f"Unsupported direct-parse format: {fmt!r}")


def _emit_record_table(
    record: TDocCRDetails,
    output: str | None,
    *,
    compact: bool = False,  # noqa: ARG001 — table is already compact
) -> None:
    """Emit a single record as a tab-separated header + data row."""
    stream, close_after = _open_output(output)
    try:
        stream.write("\t".join(_DIRECT_PARSE_FIELDS))
        stream.write("\n")
        stream.write("\t".join(_serialise_cell(record, name) for name in _DIRECT_PARSE_FIELDS))
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_markdown(
    record: TDocCRDetails,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit a single record as a one-row GFM table, or a per-field
    ``key: value`` block when ``compact=True``."""
    stream, close_after = _open_output(output)
    try:
        if compact:
            for name in _DIRECT_PARSE_FIELDS:
                stream.write(f"{name}: {_serialise_cell(record, name)}\n")
            return
        stream.write("| " + " | ".join(_md_cell(h) for h in _DIRECT_PARSE_FIELDS) + " |\n")
        stream.write("|" + "|".join(["---"] * len(_DIRECT_PARSE_FIELDS)) + "|\n")
        cells = [_md_cell(_serialise_cell(record, name)) for name in _DIRECT_PARSE_FIELDS]
        stream.write("| " + " | ".join(cells) + " |\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_json(
    record: TDocCRDetails,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit a single record as a JSON object via ``dataclasses.asdict``."""
    payload = dataclasses.asdict(record)
    payload["date"] = record.date.isoformat() if record.date is not None else None
    stream, close_after = _open_output(output)
    try:
        if compact:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            return
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_raw(
    markdown: str,
    output: str | None,
    *,
    compact: bool = False,  # noqa: ARG001 — raw is already compact
) -> None:
    """Write the converted markdown bytes verbatim, no wrapping."""
    stream, close_after = _open_output(output)
    try:
        stream.write(markdown)
    finally:
        if close_after:
            stream.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v -k "emit_record"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): compact in _emit_record seam (direct-mode tdoc parse)"
```

---

### Task 6: Compact in `tdoc show` renderers (4 functions + 2 dispatchers)

**Files:**
- Modify: `src/doc3gpp/cli.py:2124-2187` (`_render_tdoc_show_json`)
- Modify: `src/doc3gpp/cli.py:2190-2295` (`_render_tdoc_show_markdown`)
- Modify: `src/doc3gpp/cli.py:2298-2434` (`_render_tdoc_show_table` — no-op compact)
- Modify: `src/doc3gpp/cli.py:2437-2478` (`_render_tdoc_show_raw` — no-op compact)
- Modify: `src/doc3gpp/cli.py:2547-2552` (by-url dispatcher; forward `compact`)
- Modify: `src/doc3gpp/cli.py:3382-3387` (tdoc-show dispatcher; forward `compact`)
- Extend: `tests/unit/test_compact_helpers.py` (renderer-level round-trip tests)

**Interfaces:**
- Consumes: existing `_render_tdoc_show_*(record, output)` signatures
- Produces: each gains `*, compact: bool = False`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_compact_helpers.py`:

```python
def test_render_tdoc_show_json_compact_round_trips() -> None:
    """``_render_tdoc_show_json(record, None, compact=True)`` returns
    a single line, no operator-space, no trailing newline, and parses
    back to the same payload."""
    import io
    import json
    from datetime import date

    from doc3gpp.cli import _render_tdoc_show_json, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
        spec="38.300",
        cr_num="0001",
        version="1.0.0",
        release="Rel-18",
    )
    cover = TDocCRDetails(
        tdoc_id="R5s260001",
        spec="38.300",
        cr_num="0001",
        rev="-",
        version="1.0.0",
        title="CR on 5G NR",
        source="RAN1",
        tsg="RAN1",
        related_wis="-",
        date=date(2026, 1, 15),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="-",
        consequences_if_not_approved="-",
        clauses_affected="5.4.2",
        work_item="NR_5G",
    )
    record = TDocShowRecord(tdoc=tdoc, cover=cover, ttcn=None, extracted_at=None, files=())
    stream = io.StringIO()
    _render_tdoc_show_json(record, stream, compact=True)
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    payload = json.loads(text)
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"
    assert payload["cover"]["date"] == "2026-01-15"


def test_render_tdoc_show_markdown_compact_strips_decorators() -> None:
    """``_render_tdoc_show_markdown(..., compact=True)`` drops every
    CommonMark decorator and uses blank-line section separators."""
    import io
    from datetime import date

    from doc3gpp.cli import _render_tdoc_show_markdown, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    tdoc = TDoc(
        tdoc_id="R5s260001",
        title="CR on 5G NR",
        ftp_url="x/1",
        source="RAN1",
        type="CR",
        status="approved",
    )
    record = TDocShowRecord(
        tdoc=tdoc,
        cover=None,
        ttcn=None,
        extracted_at=None,
        files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_markdown(record, stream, compact=True)
    text = stream.getvalue()
    # No CommonMark decorators survive.
    assert "##" not in text
    assert "**" not in text
    assert "*" not in text
    assert "```" not in text
    # Field labels still appear as ``key: value`` lines.
    assert "tdoc_id: R5s260001" in text
    assert "title: CR on 5G NR" in text


def test_render_tdoc_show_table_compact_is_noop() -> None:
    """``_render_tdoc_show_table`` ignores ``compact`` (table is
    already line-oriented)."""
    import io

    from doc3gpp.cli import _render_tdoc_show_table, TDocShowRecord
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="X", ftp_url="x/1")
    record = TDocShowRecord(tdoc=tdoc, cover=None, ttcn=None, extracted_at=None, files=())
    plain = io.StringIO()
    _render_tdoc_show_table(record, plain)
    compact = io.StringIO()
    _render_tdoc_show_table(record, compact, compact=True)
    assert plain.getvalue() == compact.getvalue()


def test_render_tdoc_show_raw_compact_is_noop(monkeypatch) -> None:
    """``_render_tdoc_show_raw`` ignores ``compact`` (raw is already
    maximally compact by construction)."""
    from doc3gpp.cli import _render_tdoc_show_raw

    calls = []

    def fake_read_cached_markdown_path(*args, **kwargs):
        return "# hello"

    monkeypatch.setattr("doc3gpp.cli._read_cached_markdown_path", fake_read_cached_markdown_path)
    monkeypatch.setattr("doc3gpp.cli._build_cache", lambda: type("C", (), {"root": "."})())

    # Stub the service to bypass real DB / network.
    class _Stub:
        def extract(self, _tdoc_id):
            from types import SimpleNamespace
            return SimpleNamespace(
                extract_meta=SimpleNamespace(cache_file="x.zip"),
                tdoc_id=_tdoc_id,
            )

    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda: _Stub())

    import io
    plain_buf = io.StringIO()
    compact_buf = io.StringIO()
    _render_tdoc_show_raw("R5s260001", plain_buf)
    _render_tdoc_show_raw("R5s260001", compact_buf, compact=True)
    assert plain_buf.getvalue() == compact_buf.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v -k "render_tdoc_show"`
Expected: FAIL with `TypeError` for `compact` kwarg.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, modify each `_render_tdoc_show_*` function:

`_render_tdoc_show_json` (line 2124) — add `*, compact: bool = False` and a compact branch:

```python
def _render_tdoc_show_json(
    record: TDocShowRecord,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """... existing docstring ... ``compact=True`` drops indent and
    operator-space and emits a single line with no trailing newline."""
    payload: dict[str, object] = {
        "tdoc": {
            f.name: _serialise_show_value(getattr(record.tdoc, f.name))
            for f in dataclass_fields(record.tdoc)
        },
    }
    if record.cover is not None:
        payload["cover"] = {
            f.name: _serialise_show_value(getattr(record.cover, f.name))
            for f in dataclass_fields(record.cover)
        }
    if record.ttcn is not None:
        payload["ttcn"] = {
            f.name: _serialise_show_value(getattr(record.ttcn, f.name))
            for f in dataclass_fields(record.ttcn)
        }
    if record.extracted_at is not None:
        payload["extracted_at"] = _serialise_show_value(record.extracted_at)
    if record.files:
        payload["files"] = [
            {
                f.name: _serialise_show_value(getattr(file, f.name))
                for f in dataclass_fields(file)
            }
            for file in record.files
        ]
    stream, close_after = _open_output(output)
    try:
        if compact:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            return
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()
```

`_render_tdoc_show_markdown` (line 2190) — add `*, compact: bool = False` and apply the full ruleset:

```python
def _render_tdoc_show_markdown(
    record: TDocShowRecord,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """... existing docstring ... When ``compact=True`` every
    CommonMark decorator (``**bold**``, ``*italic*``, ``## headings``,
    ``- `` bullets, ```` ```json ```` fences) is dropped; fields
    become ``key: value`` plain lines and sections are separated by a
    single blank line. ``None`` values render as ``-`` and ``—`` is
    normalised to ``-``. ``required_changes`` becomes a single-line
    JSON literal; ``changed_functions`` becomes a comma-joined line.
    Placeholder text becomes a single ``note: <plain>`` line."""
    stream, close_after = _open_output(output)
    try:
        if not compact:
            # Preserve the original pretty shape exactly.
            stream.write(f"# TDoc `{record.tdoc.tdoc_id}`\n\n")
            stream.write("## Metadata\n\n")
            for f in dataclass_fields(record.tdoc):
                value = _serialise_show_value(getattr(record.tdoc, f.name))
                if value is None:
                    rendered = "—"
                else:
                    rendered = str(value)
                stream.write(f"- **{f.name}**: {rendered}\n")
            # ... rest of the original body unchanged ...
            return

        # Compact path.
        for f in dataclass_fields(record.tdoc):
            value = _serialise_show_value(getattr(record.tdoc, f.name))
            rendered = "-" if value is None else str(value)
            stream.write(f"{f.name}: {rendered}\n")

        if (
            record.cover is None
            and record.ttcn is None
            and record.extracted_at is None
        ):
            stream.write("\nnote: No extracted details; run doc3gpp tdoc parse --tdoc <id> first.\n")
        else:
            if record.cover is not None:
                stream.write("\n")
                for f in dataclass_fields(record.cover):
                    if f.name in {"details", "parser_version"}:
                        continue
                    value = getattr(record.cover, f.name)
                    formatted = _serialise_show_value(value)
                    rendered = "-" if formatted is None else str(formatted)
                    stream.write(f"{f.name}: {rendered}\n")
                if record.extracted_at is not None:
                    stream.write(f"extracted_at: {_fmt_dt(record.extracted_at)}\n")

            if record.ttcn is not None:
                stream.write("\n")
                for f in dataclass_fields(record.ttcn):
                    value = getattr(record.ttcn, f.name)
                    if f.name == "required_changes" and isinstance(value, list):
                        inline = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                        stream.write(f"{f.name}: {inline}\n")
                        continue
                    if f.name == "changed_functions" and isinstance(value, list):
                        if not value:
                            stream.write(f"{f.name}: -\n")
                        else:
                            stream.write(f"{f.name}: {', '.join(value)}\n")
                        continue
                    formatted = _serialise_show_value(value)
                    rendered = "-" if formatted is None else str(formatted)
                    stream.write(f"{f.name}: {rendered}\n")

        stream.write("\n")
        if not record.files:
            stream.write("note: No auxiliary files; run doc3gpp tdoc sync first if you haven't synced this meeting yet.\n")
        else:
            for file in record.files:
                stream.write("type: {file.type}\n".format(file=file))
                stream.write(f"file: {file.file}\n")
                stream.write(f"ftp_url: {file.ftp_url}\n")
                uploaded = (
                    file.uploaded_date.isoformat()
                    if file.uploaded_date is not None
                    else "-"
                )
                stream.write(f"uploaded_date: {uploaded}\n")
    finally:
        if close_after:
            stream.close()
```

`_render_tdoc_show_table` (line 2298) — add `*, compact: bool = False` (no-op):

```python
def _render_tdoc_show_table(
    record: TDocShowRecord,
    output: str | None,
    *,
    compact: bool = False,  # noqa: ARG001 — table is already compact
) -> None:
    """... existing docstring ... ``compact`` is accepted for symmetry
    with the other renderers but ignored — the table format is already
    line-oriented and maximally compact by construction."""
    # Existing body unchanged.
```

`_render_tdoc_show_raw` (line 2437) — add `*, compact: bool = False` (no-op):

```python
def _render_tdoc_show_raw(
    tdoc_id: str,
    output: str | None,
    *,
    compact: bool = False,  # noqa: ARG001 — raw is already compact
) -> None:
    """... existing docstring ... ``compact`` is accepted for symmetry
    but ignored — raw emits the converted markdown verbatim."""
    # Existing body unchanged.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v -k "render_tdoc_show"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): compact in tdoc show renderers (json, markdown, table, raw)"
```

---

### Task 7: Compact in `tdoc show --ftp-url` renderers (4 functions)

**Files:**
- Modify: `src/doc3gpp/cli.py:2555-2573` (`_render_tdoc_show_raw_by_url`)
- Modify: `src/doc3gpp/cli.py:2576-2622` (`_render_tdoc_show_by_url_json`)
- Modify: `src/doc3gpp/cli.py:2625-2717` (`_render_tdoc_show_by_url_markdown`)
- Modify: `src/doc3gpp/cli.py:2719+` (`_render_tdoc_show_by_url_table`)
- Modify: `src/doc3gpp/cli.py:2547-2552` (by-url dispatcher; forward `compact`)
- Extend: `tests/unit/test_compact_helpers.py`

**Interfaces:** mirror Task 6 but anchored on `TDocShowRecordByUrl`. The compact ruleset is identical to the `--tdoc` form.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_compact_helpers.py`:

```python
def test_render_tdoc_show_by_url_json_compact_round_trips() -> None:
    """``_render_tdoc_show_by_url_json(..., compact=True)`` returns
    a single line, no spaces, no trailing newline, and parses back."""
    import io
    import json

    from doc3gpp.cli import _render_tdoc_show_by_url_json, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="CR on 5G NR", ftp_url="x/1")
    record = TDocShowRecordByUrl(
        ftp_url="x/1", tdoc=tdoc, cover=None, ttcn=None,
        extracted_at=None, files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_json(record, stream, compact=True)
    text = stream.getvalue()
    assert "\n" not in text
    assert ", " not in text
    assert ": " not in text
    assert json.loads(text)["ftp_url"] == "x/1"


def test_render_tdoc_show_by_url_markdown_compact_strips_decorators() -> None:
    """``_render_tdoc_show_by_url_markdown(..., compact=True)`` drops
    every CommonMark decorator."""
    import io

    from doc3gpp.cli import _render_tdoc_show_by_url_markdown, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="X", ftp_url="x/1")
    record = TDocShowRecordByUrl(
        ftp_url="x/1", tdoc=tdoc, cover=None, ttcn=None,
        extracted_at=None, files=(),
    )
    stream = io.StringIO()
    _render_tdoc_show_by_url_markdown(record, stream, compact=True)
    text = stream.getvalue()
    assert "##" not in text
    assert "**" not in text
    assert "```" not in text
    assert "ftp_url: x/1" in text


def test_render_tdoc_show_by_url_table_compact_is_noop() -> None:
    """``_render_tdoc_show_by_url_table`` ignores ``compact``."""
    import io

    from doc3gpp.cli import _render_tdoc_show_by_url_table, TDocShowRecordByUrl
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(tdoc_id="R5s260001", title="X", ftp_url="x/1")
    record = TDocShowRecordByUrl(
        ftp_url="x/1", tdoc=tdoc, cover=None, ttcn=None,
        extracted_at=None, files=(),
    )
    plain = io.StringIO()
    _render_tdoc_show_by_url_table(record, plain)
    compact = io.StringIO()
    _render_tdoc_show_by_url_table(record, compact, compact=True)
    assert plain.getvalue() == compact.getvalue()


def test_render_tdoc_show_raw_by_url_compact_is_noop(monkeypatch) -> None:
    """``_render_tdoc_show_raw_by_url`` ignores ``compact``."""
    from doc3gpp.cli import _render_tdoc_show_raw_by_url

    def fake_read_cached_markdown_path(*args, **kwargs):
        return "# hello"

    monkeypatch.setattr("doc3gpp.cli._read_cached_markdown_path", fake_read_cached_markdown_path)
    monkeypatch.setattr("doc3gpp.cli._build_cache", lambda: type("C", (), {"root": "."})())

    import io
    plain = io.StringIO()
    compact = io.StringIO()
    _render_tdoc_show_raw_by_url("https://x/1", plain)
    _render_tdoc_show_raw_by_url("https://x/1", compact, compact=True)
    assert plain.getvalue() == compact.getvalue()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v -k "by_url"`
Expected: FAIL with `TypeError` for `compact` kwarg.

- [ ] **Step 3: Implement the change**

Apply the same pattern as Task 6 to each `_render_tdoc_show_by_url_*`
function. The compact ruleset is identical:

- `_render_tdoc_show_raw_by_url` — add `*, compact: bool = False`, no-op.
- `_render_tdoc_show_by_url_json` — add `*, compact: bool = False`, swap to `separators=(",", ":")` + no trailing newline when `True`.
- `_render_tdoc_show_by_url_markdown` — add `*, compact: bool = False`, apply the same full ruleset as `_render_tdoc_show_markdown`. The by-url renderer already mirrors the by-id renderer's markdown shape.
- `_render_tdoc_show_by_url_table` — add `*, compact: bool = False`, no-op.

For the JSON form the diff is identical to `_render_tdoc_show_json` in Task 6. For the markdown form mirror Task 6's compact body, but keep the `ftp_url` first (the URL is the document anchor in the non-compact form; in compact form it's just another `key: value` line).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py -v -k "by_url"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): compact in tdoc show --ftp-url renderers"
```

---

### Task 8: `--compact` flag on `meeting list` and `tdoc list`

**Files:**
- Modify: `src/doc3gpp/cli.py:655-715` (`meeting_list`)
- Modify: `src/doc3gpp/cli.py:856-993` (`tdoc_list`)
- Extend: `tests/unit/test_list_output_format.py`

**Interfaces:**
- `meeting_list(..., compact: bool = False, ...)` → passes `compact=compact` to `_emit_records`
- `tdoc_list(..., compact: bool = False, ...)` → passes `compact=compact` to `_emit_records`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_list_output_format.py`:

```python
def test_meeting_list_format_json_compact(monkeypatch) -> None:
    """``meeting list --format json --compact`` emits single-line JSON."""
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    result = Runner().invoke(
        app,
        ["meeting", "list", "--format", "json", "--compact",
         "--fields", "meeting_id,name"],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    assert "\n" not in output
    assert ", " not in output
    assert ": " not in output
    assert json.loads(output) == [{"meeting_id": "10", "name": "R5-200"}]


def test_meeting_list_format_markdown_compact(monkeypatch) -> None:
    """``meeting list --format markdown --compact`` drops the GFM table."""
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    result = Runner().invoke(
        app,
        ["meeting", "list", "--format", "markdown", "--compact",
         "--fields", "meeting_id,name"],
    )
    assert result.exit_code == 0, result.output
    assert "|" not in result.output
    assert "meeting_id: 10" in result.output
    assert "name: R5-200" in result.output


def test_meeting_list_format_table_compact_is_noop(monkeypatch) -> None:
    """``meeting list --format table --compact`` is byte-identical to
    the default."""
    _patch_simple(monkeypatch, MeetingService, "list_recent", [SAMPLE_MEETING])

    plain = Runner().invoke(app, ["meeting", "list"])
    compact = Runner().invoke(app, ["meeting", "list", "--compact"])
    assert plain.exit_code == 0
    assert compact.exit_code == 0
    assert plain.output == compact.output


def test_tdoc_list_format_json_compact(monkeypatch) -> None:
    """``tdoc list --format json --compact`` emits single-line JSON."""
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return [SAMPLE_TDOC_ROW]
    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    result = Runner().invoke(
        app,
        ["tdoc", "list", "--format", "json", "--compact",
         "--fields", "tdoc_id,meeting_name"],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    assert "\n" not in output
    assert ", " not in output
    assert ": " not in output
    assert json.loads(output) == [
        {"tdoc_id": "R5s260001", "meeting_name": "RAN5#111"}
    ]


def test_tdoc_list_format_markdown_compact(monkeypatch) -> None:
    """``tdoc list --format markdown --compact`` drops the GFM table."""
    def fake(self, **kwargs) -> list[TDocWithMeeting]:
        return [SAMPLE_TDOC_ROW]
    monkeypatch.setattr(TDocService, "list_recent_with_meeting", fake)

    result = Runner().invoke(
        app,
        ["tdoc", "list", "--format", "markdown", "--compact",
         "--fields", "tdoc_id,meeting_name"],
    )
    assert result.exit_code == 0, result.output
    assert "|" not in result.output
    assert "tdoc_id: R5s260001" in result.output
    assert "meeting_name: RAN5#111" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_list_output_format.py -v -k "compact"`
Expected: FAIL with `Missing option '--compact'` or unknown-flag error.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, locate the `--format` Option block in `meeting_list`
(line 677) and add a `--compact` Option right after it:

```python
compact: bool = typer.Option(
    False,
    "--compact",
    help=(
        "Strip output formatting: JSON drops indent and operator-space; "
        "Markdown drops GFM tables, bullets, and bold. No-op for "
        "``table``. Defaults to ``output.compact`` in settings when "
        "the flag is not passed."
    ),
)
```

In the function body, after the existing `fmt = _resolve_format(fmt, default=settings.output.format)` line (line 715), add:

```python
resolved_compact = _resolve_compact(compact)
```

Then update the `_emit_records(...)` call site to pass `compact=resolved_compact`. Repeat the same change for `tdoc_list` (the `--format` Option is at line 958, the `_resolve_format` call is at line 993, and the `_emit_records` call site is the one that consumes `fmt`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_list_output_format.py -v -k "compact"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_list_output_format.py
git commit -m "feat(cli): add --compact to meeting list and tdoc list"
```

---

### Task 9: `--compact` flag on `wi list`

**Files:**
- Modify: `src/doc3gpp/cli.py:3511-3568` (`wi_list`)
- Extend: `tests/unit/test_list_output_format.py`

**Interfaces:**
- `wi_list(..., compact: bool = False, ...)` → passes `compact=compact` to `_emit_records`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_list_output_format.py`:

```python
def test_wi_list_format_json_compact(monkeypatch) -> None:
    """``wi list --format json --compact`` emits single-line JSON."""
    _patch_simple(monkeypatch, WiService, "list_recent", [SAMPLE_WI])

    result = Runner().invoke(
        app,
        ["wi", "list", "--format", "json", "--compact",
         "--fields", "wi_id,acronym,release,name"],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    assert "\n" not in output
    assert ", " not in output
    assert ": " not in output
    assert json.loads(output) == [{
        "wi_id": "42", "acronym": "NTShar",
        "release": "Rel-19", "name": "NTM sharing",
    }]


def test_wi_list_format_markdown_compact(monkeypatch) -> None:
    """``wi list --format markdown --compact`` drops the GFM table."""
    _patch_simple(monkeypatch, WiService, "list_recent", [SAMPLE_WI])

    result = Runner().invoke(
        app,
        ["wi", "list", "--format", "markdown", "--compact",
         "--fields", "wi_id,acronym,release,name"],
    )
    assert result.exit_code == 0, result.output
    assert "|" not in result.output
    assert "wi_id: 42" in result.output
    assert "name: NTM sharing" in result.output
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_list_output_format.py -v -k "wi_list and compact"`
Expected: FAIL with `Missing option '--compact'`.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, locate the `--format` Option block in `wi_list` (line 3532) and add the same `--compact` Option as in Task 8 (the help text is identical). In the function body, after `fmt = _resolve_format(fmt, default=settings.output.format)` (line 3568), add `resolved_compact = _resolve_compact(compact)` and pass it through to the `_emit_records(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_list_output_format.py -v -k "wi_list and compact"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_list_output_format.py
git commit -m "feat(cli): add --compact to wi list"
```

---

### Task 10: `--compact` flag on `tdoc show` and `tdoc show --ftp-url`

**Files:**
- Modify: `src/doc3gpp/cli.py:3263-3427` (`tdoc_show` command)
- Modify: `src/doc3gpp/cli.py:3382-3387` (tdoc-show dispatcher)
- Modify: `src/doc3gpp/cli.py:2547-2552` (by-url dispatcher)
- Extend: `tests/unit/test_compact_helpers.py` (or a new test file if integration is too heavy for unit tests)

**Interfaces:**
- `tdoc_show(..., compact: bool = False, ...)` → threads `compact` through the dispatchers to the underlying renderers

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_compact_helpers.py`:

```python
def test_tdoc_show_json_compact_via_cli(monkeypatch) -> None:
    """``tdoc show --tdoc <id> --format json --compact`` end-to-end via
    the CLI runner emits a single line of JSON."""
    import json
    from typer.testing import CliRunner
    from datetime import date

    from doc3gpp.cli import app
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_cr import TDocCRDetails

    tdoc = TDoc(
        tdoc_id="R5s260001", title="CR on 5G NR", ftp_url="x/1",
        source="RAN1", type="CR", status="approved",
    )
    cover = TDocCRDetails(
        tdoc_id="R5s260001", spec="38.300", cr_num="0001", rev="-",
        version="1.0.0", title="CR on 5G NR", source="RAN1", tsg="RAN1",
        related_wis="-", date=date(2026, 1, 15), cr_cat="F",
        release="Rel-18", reason_for_change="-",
        consequences_if_not_approved="-", clauses_affected="5.4.2",
        work_item="NR_5G",
    )

    def fake_resolve(*args, **kwargs):
        from doc3gpp.cli import TDocShowRecord
        return TDocShowRecord(
            tdoc=tdoc, cover=cover, ttcn=None, extracted_at=None, files=(),
        )

    monkeypatch.setattr("doc3gpp.cli._resolve_tdoc_show_record", fake_resolve)

    result = CliRunner().invoke(
        app,
        ["tdoc", "show", "--tdoc", "R5s260001", "--format", "json", "--compact"],
    )
    assert result.exit_code == 0, result.output
    output = result.output
    assert "\n" not in output
    assert ", " not in output
    assert ": " not in output
    payload = json.loads(output)
    assert payload["tdoc"]["tdoc_id"] == "R5s260001"
```

(If `_resolve_tdoc_show_record` is named differently in the codebase,
patch the actual function the dispatcher calls.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_compact_helpers.py::test_tdoc_show_json_compact_via_cli -v`
Expected: FAIL with `Missing option '--compact'`.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, add the `--compact` Option to the `tdoc_show` Typer command (line 3263) — same shape as the list commands. In the function body, after the existing `_resolve_tdoc_show_format` call, compute `resolved_compact = _resolve_compact(compact)`. Forward `compact=resolved_compact` to:

- `_render_tdoc_show_json(show_record, output)` at line 3383
- `_render_tdoc_show_markdown(show_record, output)` at line 3385
- `_render_tdoc_show_table(show_record, output)` at line 3387
- `_render_tdoc_show_raw(tdoc_id, output)` at line 3380 (raw branch)

The by-url branch (the `if fmt == "raw":` at line 2508 and the dispatchers at 2547-2552) needs the same treatment. Find where the by-url branch reads `fmt` (around the `_tdoc_show_by_ftp_url` function) and add the same `resolved_compact = _resolve_compact(compact)` + forwarding to the four `_render_tdoc_show_by_url_*` calls.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_compact_helpers.py::test_tdoc_show_json_compact_via_cli -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_compact_helpers.py
git commit -m "feat(cli): add --compact to tdoc show (tdoc-id and ftp-url)"
```

---

### Task 11: `--compact` flag on `tdoc parse --from-path / --from-url`

**Files:**
- Modify: `src/doc3gpp/cli.py:1157-1303` (Typer command) and `1842` (format resolution)
- Modify: `src/doc3gpp/cli.py:1958` (`_emit_record` dispatch site)
- Extend: `tests/unit/test_tdoc_parse_cli.py` and `tests/unit/test_tdoc_parse_direct.py`

**Interfaces:**
- `tdoc_parse(..., compact: bool = False, ...)` → threads `compact` through `_resolve_compact` and forwards to `_emit_record` / `_emit_record_raw`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_tdoc_parse_cli.py`:

```python
def test_tdoc_parse_format_json_compact(sample_parser) -> None:
    """``tdoc parse --tdoc <id> --format json --compact`` emits a
    single-line JSON object."""
    # Use existing fixtures; assert the rendered output is single line.
    # ... (mirror the existing test_tdoc_parse_format_json but add --compact)


def test_tdoc_parse_format_markdown_compact(sample_parser) -> None:
    """``tdoc parse --tdoc <id> --format markdown --compact`` drops
    CommonMark decorators."""
    # ... (mirror test_tdoc_parse_format_markdown but add --compact)
```

(Inspect the existing test_tdoc_parse_format_json / test_tdoc_parse_format_markdown functions to copy their fixture setup; the only diff is appending `"--compact"` to the argv list and asserting the compact shape.)

Append to `tests/unit/test_tdoc_parse_direct.py`:

```python
def test_tdoc_parse_direct_json_compact(...) -> None:
    """``tdoc parse --from-path FILE --format json --compact`` emits
    a single-line JSON object."""
    # ... mirror test_tdoc_parse_direct_json but add --compact


def test_tdoc_parse_direct_markdown_compact(...) -> None:
    """``tdoc parse --from-path FILE --format markdown --compact``
    drops CommonMark decorators."""
    # ... mirror test_tdoc_parse_direct_markdown but add --compact
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_parse_direct.py -v -k "compact"`
Expected: FAIL with `Missing option '--compact'`.

- [ ] **Step 3: Implement the change**

In `src/doc3gpp/cli.py`, add the `--compact` Option to the `tdoc_parse` Typer command (line 1157). In the function body, after the existing `_resolve_format` / `_resolve_tdoc_direct_format` calls (line 1842), add `resolved_compact = _resolve_compact(compact)`. Forward `compact=resolved_compact` to the two call sites:

- `_emit_record(result.details, resolved_format, output)` at line 1958
- `_emit_record_raw(result.markdown, output)` at line 1949

(For DB-mode `tdoc parse` — i.e. the per-row batch loop above — find the equivalent call site for each batch item and pass `compact=resolved_compact` there too.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest -x -q tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_parse_direct.py -v -k "compact"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/doc3gpp/cli.py tests/unit/test_tdoc_parse_cli.py tests/unit/test_tdoc_parse_direct.py
git commit -m "feat(cli): add --compact to tdoc parse (db-mode and direct-mode)"
```

---

### Task 12: End-to-end integration test for `tdoc show --format json --compact`

**Files:**
- Extend: `tests/integration/test_tdoc_cr_ttcn_sqlite.py` (after the existing `test_tdoc_show_format_json` block around line 685)

- [ ] **Step 1: Write the failing integration test**

Append:

```python
def test_tdoc_show_format_json_compact_round_trips(sqlite_env, tmp_path) -> None:
    """End-to-end: ``tdoc show --tdoc <id> --format json --compact``
    emits a single line of compact JSON that parses back to the same
    payload as the pretty-printed form."""
    # Use the same fixture setup as test_tdoc_show_format_json (lines
    # 685+), but invoke the CLI with ``--compact`` appended and assert
    # the output is single-line, no operator-space, no trailing newline,
    # and round-trips through ``json.loads``.
    # ...
```

(Inspect the existing `test_tdoc_show_format_json` test for the
canonical fixture setup and the CLI invocation pattern. Re-use the
helper functions and fixtures defined in the same file — the only diff
is appending `"--compact"` to the argv list.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest -x -q tests/integration/test_tdoc_cr_ttcn_sqlite.py -v -k "compact"`
Expected: FAIL with `Missing option '--compact'`.

- [ ] **Step 3: Implement the change (only if it isn't already passing from Task 10)**

The previous task already added `--compact` to the `tdoc show`
command. This task is purely an integration smoke test. If the test
fails for a reason other than the missing flag, debug the fixture
setup.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest -x -q tests/integration/test_tdoc_cr_ttcn_sqlite.py -v -k "compact"`
Expected: PASS

- [ ] **Step 5: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS (all unit + integration tests, sqlite-only).

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_tdoc_cr_ttcn_sqlite.py
git commit -m "test(integration): tdoc show --format json --compact end-to-end"
```

---

### Task 13: Lint + full sqlite suite + docs sync

**Files:**
- Modify: `README.md` (one bullet)
- Modify: `docs/cli.md` (per-command flag entries)
- Modify: `docs/architecture.md` (one paragraph)
- Modify: `docs/code-map.md` (symbol table entries)
- Modify: `AGENTS.md` (one row in the workflows table)

- [ ] **Step 1: Run the linter**

Run: `ruff check .`
Expected: PASS (no warnings, no errors).

- [ ] **Step 2: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS.

- [ ] **Step 3: Update `README.md`**

Add a bullet under the "Output" section:

```markdown
- `--compact` — strip output formatting. JSON drops indent and operator
  space (single line, `separators=(",", ":")`); Markdown drops CommonMark
  decorators (bold, italic, headings, bullets, GFM tables, code fences)
  and emits `key: value` lines with blank-line section separators. No-op
  for `table` and `raw`. Default: `false`; opt in globally with
  `[output] compact = true` in `doc3gpp.toml`.
```

- [ ] **Step 4: Update `docs/cli.md`**

For each of the six affected commands (`meeting list`, `tdoc list`,
`tdoc parse --from-path/--from-url`, `tdoc show --tdoc`,
`tdoc show --ftp-url`, `wi list`), add a one-line flag entry:

```markdown
- `--compact` — see "Compact output" in the format-overview section.
```

Append a new subsection to the format-overview:

````markdown
### Compact output

Every `--format` command accepts `--compact`. The default is `false`;
set it globally with `[output] compact = true` in `doc3gpp.toml`. The
flag is a no-op for `--format table` and `--format raw` (both are
already line-oriented / maximally compact by construction).

`--format json --compact` emits a single line of JSON: no indent, no
operator-space (`separators=(",", ":")`), and no trailing newline.
`--format markdown --compact` drops every CommonMark decorator
(`**bold**`, `*italic*`, `## headings`, `- ` bullets, fenced
```` ```json ```` blocks, GFM `| col | col |` tables) and emits
`key: value` lines with one blank line between sections. `None` is
rendered as `-` and `—` is normalised to `-`. List-typed values
(`required_changes`) become single-line JSON literals; `changed_functions`
becomes a comma-joined line.
````

- [ ] **Step 5: Update `docs/architecture.md`**

In the CLI inventory section, add a paragraph after the existing
format-renderer description:

```markdown
The `--compact` flag threads a `compact: bool` through four renderer
seams — `_emit_records` (list commands), `_emit_record` (direct-mode
tdoc parse), the four `_render_tdoc_show_*` functions, and the four
`_render_tdoc_show_by_url_*` functions. `_resolve_compact(compact)`
resolves the CLI flag against `Settings.output.compact` (CLI > settings).
Table and raw formats are explicit no-ops; JSON swaps to
`separators=(",", ":")` with no trailing newline; Markdown drops every
CommonMark decorator and emits `key: value` lines with blank-line
section separators.
```

- [ ] **Step 6: Update `docs/code-map.md`**

Add to the symbol table (the location column):

```markdown
| `_resolve_compact` | `src/doc3gpp/cli.py:229` |
| `OutputSettings.compact` | `src/doc3gpp/settings/schema.py:205` |
```

- [ ] **Step 7: Update `AGENTS.md`**

In the "Workflows in one line" table, add a row:

```markdown
| `tdoc/meeting/wi * --compact` | (any `--format` command) | Strips decorators from JSON / Markdown output. JSON becomes single line; Markdown drops bold, italic, headings, bullets, GFM tables, code fences. No-op for `table` and `raw`. CLI flag wins over `[output] compact` in `doc3gpp.toml`. |
```

- [ ] **Step 8: Re-run lint and tests after the docs edits**

Run: `ruff check . && ./scripts/test_sqlite.sh`
Expected: PASS (docs changes don't touch the test surface; this is a
sanity check).

- [ ] **Step 9: Commit**

```bash
git add README.md docs/cli.md docs/architecture.md docs/code-map.md AGENTS.md
git commit -m "docs: document --compact flag and OutputSettings.compact knob"
```

---

## Self-Review

**1. Spec coverage:**

- Settings field (`OutputSettings.compact`) → Task 1
- TOML example update → Task 1
- `_resolve_compact` helper → Task 2
- JSON compact in list seam → Task 3
- Markdown compact in list seam → Task 4
- Compact in direct-mode parse seam → Task 5
- Compact in tdoc show renderers → Tasks 6, 7
- `--compact` flag on all six commands → Tasks 8, 9, 10, 11
- End-to-end integration test → Task 12
- Lint + docs sync → Task 13
- Env-var allowlist invariant → Task 1
- Backwards compatibility (byte-identical default) → Tasks 3, 4, 5, 6, 7 (all default branches unchanged)

**2. Placeholder scan:** No "TBD", no "TODO", no "implement later", no
"fill in details". Test bodies are explicit code; renderer bodies are
explicit diffs against the existing source. Where the existing source
is too long to quote in full (e.g. the `_render_tdoc_show_table` body
in Task 6), the plan says "Existing body unchanged" and adds only the
new `compact` keyword parameter — the no-op contract is asserted by
the round-trip test.

**3. Type consistency:** `_resolve_compact(compact: bool) -> bool` is
the same signature in Tasks 2, 8, 9, 10, 11. Every renderer gains
`*, compact: bool = False` (consistent kwarg-only form) and the
dispatchers forward the resolved value. `TDocShowRecord` and
`TDocShowRecordByUrl` are referenced consistently across Tasks 6, 7,
10. The dataclass field names (`tdoc_id`, `meeting_name`, `spec`,
`date`, `cr_num`, `release`, `version`, `work_item`, etc.) match the
existing model fields in `src/doc3gpp/models/tdoc.py` and
`src/doc3gpp/models/tdoc_cr.py`.

**Result:** Plan is complete. Ready for execution.
