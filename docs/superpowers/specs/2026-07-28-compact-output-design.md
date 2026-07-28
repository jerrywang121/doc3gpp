# `output.compact` / `--compact` — Design Spec

**Status:** Approved (pending user review of the written spec)
**Date:** 2026-07-28
**Branch:** main
**Author:** brainstorming session

## Goal

Add an opt-in `--compact` flag (and a `Settings.output.compact` TOML/env
knob) that strips output formatting down to its plain-content essentials,
applied uniformly across every CLI command that already accepts
`--format`.

Two format families are affected:

- **JSON**: drop the pretty-print indent and the `, ` / `: ` operator
  spacing so the payload becomes a single line.
- **Markdown**: drop the CommonMark decorators (`**bold**`, `*italic*`,
  `## headings`, `- ` bullets, fenced ```` ```json ```` blocks, GFM
  `| col | col |` tables) so the output is just `key: value` lines with
  one blank line between sections.

Table and raw formats already produce line-oriented / maximally-compact
output, so `--compact` is a no-op for them — the flag is accepted but
ignored. Help text and a unit test make the no-op explicit so future
readers don't chase a ghost.

## Non-goals

- Adding new output formats (CSV, YAML, NDJSON, etc.) — only the
  existing `table` / `json` / `markdown` / `raw` quartet.
- Per-command compact thresholds or finer granularity (e.g. "compact
  markdown but keep bold") — one compact variant per format.
- Auto-detection of "compact needed" (e.g. when stdout is a pipe) — the
  flag is always explicit.
- Changing the default `Settings.output.format` semantics.

## Renderer architecture (ground truth)

Today the format rendering code lives in three discrete seams. The
implementation only has to plumb `compact` through these seams — not
add a per-command renderer.

| Seam | File:line | What it covers |
| --- | --- | --- |
| `_emit_records(rows, fields, fmt, output, *, no_records_msg)` | `src/doc3gpp/cli.py:402` | All **list** commands (`meeting list`, `tdoc list`, `wi list`). Dispatches to `_emit_table` / `_emit_json` / `_emit_markdown`. |
| `_emit_record(record, fmt, output)` | `src/doc3gpp/cli.py:1961` | Direct-mode `tdoc parse --from-path / --from-url`. Dispatches to `_emit_record_table` / `_emit_record_markdown` / `_emit_record_json` / `_emit_record_raw`. |
| `_render_tdoc_show_*` (4 funcs) | `src/doc3gpp/cli.py:2124, 2190, 2298, 2437` | `tdoc show --tdoc` (json, markdown, table, raw). |
| `_render_tdoc_show_by_url_*` (4 funcs) | `src/doc3gpp/cli.py:2555, 2576, 2625, 2719` | `tdoc show --ftp-url` (raw, json, markdown, table). |

The `tdoc_show` and `tdoc_show_by_url` families each have their own
explicit `if fmt == "json": _render_tdoc_show_json(...)` dispatchers
(`cli.py:3382` and `cli.py:2547` respectively); `compact` is passed
through both the dispatcher and the underlying renderer.

## Settings

### `OutputSettings` (new field)

File: `src/doc3gpp/settings/schema.py` (`OutputSettings` class,
alongside the existing `format: str = "table"` field)

```python
class OutputSettings(BaseModel):
    """Knobs for ``doc3gpp`` CLI output rendering."""

    format: str = Field(default="table")
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
```

- `compact` is `False` by default so existing shell scripts and tests
  keep working byte-for-byte.
- TOML key: `output.compact`.
- Env var: TOML-only, consistent with `output.format` (the
  `DOC3GPP_OUTPUT__*` namespace is intentionally outside the
  `ALLOWED_ENV_VARS` allowlist — see
  `tests/unit/test_settings_config_file.py::test_env_var_allowlist`).
  No allowlist change required.

### `doc3gpp.toml.example`

Update the `[output]` block:

```toml
# [output]
# format = "table"        # table | json | markdown | raw
# compact = false         # drop decorators in json/markdown output
```

## CLI

### New flag

A Typer `Option` is added to every command that already exposes
`--format`. The flag is a plain `bool = False` (absence means "not
compact"). A small helper `_resolve_compact(compact: bool) -> bool`
reads the runtime `Settings` instance and returns the effective
boolean — when the CLI flag is `False` the setting can still opt in,
and when the CLI flag is `True` it always wins.

```python
compact: bool = typer.Option(
    False,
    "--compact",
    help=(
        "Strip output formatting: JSON drops indent and operator-space; "
        "Markdown drops bold, italics, headings, bullets, code fences, "
        "and GFM tables. No-op for ``table`` and ``raw``. Defaults to "
        "``output.compact`` in settings when the flag is not passed."
    ),
)
```

### `_resolve_compact` helper

Added next to `_resolve_format` (`cli.py:229`).

```python
def _resolve_compact(compact: bool) -> bool:
    """Resolve ``--compact`` against ``Settings.output.compact``.

    CLI flag wins when ``True``; otherwise the setting decides.
    """
    if compact:
        return True
    return get_settings().output.compact
```

### Inventory of affected commands

| Command | Typer fn site | Format-resolver site | Renderer seam |
| --- | --- | --- | --- |
| `meeting list` | `cli.py:655` | `cli.py:715` | `_emit_records` (`cli.py:402`) |
| `tdoc list` | `cli.py:856` | `cli.py:993` | `_emit_records` (`cli.py:402`) |
| `tdoc parse --from-path / --from-url` | `cli.py:1157` | `cli.py:1842` | `_emit_record` (`cli.py:1961`) |
| `tdoc show --tdoc` | `cli.py:3263` | `cli.py:3427` | dispatch at `cli.py:3382` → 4 standalone renderers (`cli.py:2124–2434`) |
| `tdoc show --ftp-url` | inside `cli.py:3263` | inline (URL mode skips `_resolve_format`) | dispatch at `cli.py:2547` → 4 standalone renderers (`cli.py:2555–2782`) |
| `wi list` | `cli.py:3511` | `cli.py:3568` | `_emit_records` (`cli.py:402`) |

Each call site gets:

1. The `--compact` Option declaration (a plain `bool = False` — no
   `--no-compact` toggle; absence means "not compact").
2. A call to `_resolve_compact(compact)` immediately after the existing
   `_resolve_format(fmt, …)` line.
3. The resolved value is passed as `compact=<bool>` to the relevant
   renderer (existing renderers get a new `compact: bool = False`
   keyword argument).

`tdoc sync`, `meeting sync`, `wi sync` do **not** accept `--format` and
do not get a `--compact` flag. If a future command gains a format flag
the same pattern applies; no central registry is required.

`tdoc show --ftp-url` currently has no `_resolve_format` call (its
`fmt` is bound directly via the same Typer Option as the `--tdoc`
form). For this command `_resolve_compact` is invoked at the same
`cli.py:2547` dispatch site where `fmt` is consumed.

## Renderer changes

### JSON renderers

| Renderer | File:line | Compact change |
| --- | --- | --- |
| `_emit_json` (list path) | `cli.py:321` | When `compact=True`: `json.dump(objs, stream, ensure_ascii=False, separators=(",", ":"))` and no trailing newline. |
| `_emit_record_json` (direct path) | `cli.py:2003` | Same `compact` arg, same behaviour. |
| `_render_tdoc_show_json` | `cli.py:2124` | Same `compact` arg, same behaviour. |
| `_render_tdoc_show_by_url_json` | `cli.py:2576` | Same `compact` arg, same behaviour. |

All four gain a `compact: bool = False` parameter. The dict-walk that
builds the payload is unchanged — compactness is a serialisation
concern, not a data-model concern. `ensure_ascii=False` is preserved in
both modes so non-ASCII filenames stay readable.

### Markdown renderers

| Renderer | File:line | Compact change |
| --- | --- | --- |
| `_emit_markdown` (list path) | `cli.py:327` | When `compact=True`: emit a `key: value` block per row (field label per line, value per line) with blank-line separators between rows. No GFM table, no `| ... |` syntax. |
| `_emit_record_markdown` (direct path) | `cli.py:1990` | Same `compact` arg; emit a `key: value` block per `_DIRECT_PARSE_FIELDS` entry. |
| `_render_tdoc_show_markdown` | `cli.py:2190` | Same `compact` arg, full ruleset below. |
| `_render_tdoc_show_by_url_markdown` | `cli.py:2625` | Same `compact` arg, full ruleset below. |

Compact ruleset (applies to the two `tdoc show` markdown renderers; the
two list/direct renderers implement the row-block subset):

1. Drop `## Heading` lines — sections are still separated by a single
   blank line, but the heading text itself is replaced by an implicit
   `key: value` style block.
2. Drop `- ` bullets — fields become plain `key: value` lines.
3. Drop `**` and `*` decorators around inline emphasis.
4. Drop the ```` ```json ```` fences around list-typed values
   (`required_changes`); the list is rendered as a single-line JSON
   literal, e.g. `required_changes: [{"module": "...", "function": "..."}]`.
5. Drop the bullet-style `*` for `changed_functions` entries; the
   list is comma-joined on one line:
   `changed_functions: module_a.fn1, module_a.fn1, module_b.fn2`.
6. Replaces placeholder / hint text (e.g. `_No extracted details; run
   \`doc3gpp tdoc parse --tdoc <id>\` first._`) with a single
   `note: <plain text>` line — the backtick and underscore decorators
   are dropped.
7. Renders `None` values as `-` (matching the existing markdown
   convention); `—` (em-dash) is also normalised to `-` in compact
   mode for ASCII-friendliness.

The blank-line section separator is the only structural whitespace that
survives. This matches the user's stated intent: "key: value plain
lines, blank-line between sections".

### Table and raw renderers

The `compact: bool = False` parameter is added for symmetry but is
ignored. Help text, code comments, and a unit test make the no-op
explicit. No output change for these formats.

`tdoc show --format raw` already emits the rendered markdown verbatim
(maximally compact by construction); `tdoc show --ftp-url --format raw`
and `_emit_record_raw` are likewise unaffected.

## Compact shape examples

### `tdoc show --tdoc R5s260001 --format json --compact`

```json
{"tdoc":{"tdoc_id":"R5s260001","title":"CR on 5G NR","meeting_id":1,"ftp_url":"https://www.3gpp.org/ftp/TSG/WG1/R5s260001.zip","source":"RAN1","type":"CR","status":"approved","spec":"38.300","cr_num":"0001","version":"1.0.0","release":"Rel-18"},"cover":{"ftp_url":"https://www.3gpp.org/ftp/TSG/WG1/R5s260001.zip","spec":"38.300","cr_num":"0001","rev":"-","version":"1.0.0","title":"CR on 5G NR","source":"RAN1","tsg":"RAN1","related_wis":"-","date":"2026-01-15","cr_cat":"F","release":"Rel-18","reason_for_change":"-","consequences_if_not_approved":"-","clauses_affected":"5.4.2"},"extracted_at":"2026-07-28T14:32:11"}
```

### `tdoc show --tdoc R5s260001 --format markdown --compact`

```
tdoc_id: R5s260001
title: CR on 5G NR
meeting_id: 1
ftp_url: https://www.3gpp.org/ftp/TSG/WG1/R5s260001.zip
source: RAN1
type: CR
status: approved
spec: 38.300
cr_num: 0001
version: 1.0.0
release: Rel-18

spec: 38.300
cr_num: 0001
rev: -
version: 1.0.0
title: CR on 5G NR
source: RAN1
tsg: RAN1
related_wis: -
date: 2026-01-15
cr_cat: F
release: Rel-18
reason_for_change: -
consequences_if_not_approved: -
clauses_affected: 5.4.2
extracted_at: 2026-07-28T14:32:11

testcase: TC_5G_NR_01
ue: true
ss: false
ats_version: 1.0.0
ttcn_release: Rel-18
test_suite: NR_NR5G
required_changes: [{"module":"NAS_5G","function":"handleRegistration"}]
changed_functions: NAS_5G.handleRegistration, NAS_5G.handleDeregistration
```

### `tdoc list --meeting-id 1 --format json --compact`

```json
[{"tdoc_id":"R5s260001","meeting_id":1,"title":"…","ftp_url":"…","source":"RAN1","type":"CR","status":"approved","spec":"38.300","cr_num":"0001","uploaded_date":"2026-01-15"}]
```

### `tdoc list --meeting-id 1 --format markdown --compact`

```
tdoc_id: R5s260001
meeting_id: 1
title: CR on 5G NR
ftp_url: https://www.3gpp.org/ftp/TSG/WG1/R5s260001.zip
source: RAN1
type: CR
status: approved
spec: 38.300
cr_num: 0001
uploaded_date: 2026-01-15

tdoc_id: R5s260002
meeting_id: 1
title: …
```

(The list compact form repeats the field labels for every row —
intentional, since dropping the field-name prefix would make the
output unparseable without a schema.)

## Testing strategy

### New unit tests

File: `tests/unit/test_compact_output.py`

- `test_resolve_compact_cli_true_overrides_settings_false` — CLI
  `--compact` with `Settings.output.compact = False` returns `True`.
- `test_resolve_compact_cli_false_setting_true` — CLI absent
  (`False`) with `Settings.output.compact = True` returns `True`.
- `test_resolve_compact_default_false` — CLI absent + setting `False`
  → `False`.
- `test_emit_json_compact_single_line` — output is one line, ends
  without `\n`, contains no `, ` or `: `, parses back as JSON.
- `test_emit_markdown_compact_no_decorators` — assert no `|`, no
  `---`, no ```` ``` ```` in the output; assert one blank line
  between rows.
- `test_render_tdoc_show_json_compact_round_trips` — output parses as
  JSON, has exactly one line, contains no `, ` or `: `, ends without
  `\n`.
- `test_render_tdoc_show_markdown_compact_strips_decorators` — assert
  no `**`, no `## `, no `- `, no ```` ``` ```` in the output; assert
  one blank line between sections.
- `test_render_tdoc_show_markdown_compact_placeholder` — when neither
  cover nor TTCN exists, the placeholder becomes
  `note: No extracted details; run doc3gpp tdoc parse --tdoc <id> first.`
  (no backticks, no underscore-italics).
- `test_render_tdoc_show_table_compact_is_noop` — output byte-identical
  to `compact=False`.
- `test_render_tdoc_show_raw_compact_is_noop` — same.
- `test_render_tdoc_show_by_url_json_compact` — same shape as
  `--tdoc` form, payload keyed by `ftp_url`.
- `test_render_tdoc_show_by_url_markdown_compact` — same.
- `test_render_tdoc_show_by_url_table_compact_is_noop`.
- `test_render_tdoc_show_by_url_raw_compact_is_noop`.
- `test_tdoc_list_json_compact` — array-of-records form, single line,
  no spaces.
- `test_tdoc_list_markdown_compact` — GFM table collapses to per-row
  `key: value` blocks separated by blank lines.
- `test_meeting_list_json_compact` — same shape as tdoc list.
- `test_meeting_list_markdown_compact` — same.
- `test_wi_list_json_compact` — same.
- `test_wi_list_markdown_compact` — same.
- `test_tdoc_parse_direct_json_compact` — exercises the
  `_emit_record_json` path used by `--from-path/--from-url`.
- `test_tdoc_parse_direct_markdown_compact` — same for markdown.
- `test_tdoc_parse_direct_table_compact_is_noop`.
- `test_tdoc_parse_direct_raw_compact_is_noop`.
- `test_settings_output_compact_default` — TOML-less bootstrap yields
  `False`; TOML `output.compact = true` yields `True`; `DOC3GPP_OUTPUT__*`
  env vars are not honoured (allowlist unchanged).
- `test_settings_output_compact_toml_round_trip` — write the example
  TOML, re-read, assert value matches.

### Existing tests to extend (one new test per file)

- `tests/unit/test_tdoc_parse_cli.py`: `test_tdoc_parse_format_json_compact`,
  `test_tdoc_parse_format_markdown_compact`.
- `tests/unit/test_tdoc_parse_direct.py`:
  `test_tdoc_parse_direct_json_compact`,
  `test_tdoc_parse_direct_markdown_compact`.
- `tests/integration/test_tdoc_cr_ttcn_sqlite.py`:
  `test_tdoc_show_format_json_compact_round_trips` — end-to-end via
  the CLI runner, with sqlite backing.

No online (`-m online`) or mysql tests — `--compact` is pure CLI
behaviour, fully mockable.

## Documentation sync

- `README.md`: one bullet under "Output" describing `--compact`.
- `docs/cli.md`: per-command flag entries for every command listed
  above; a one-line summary in the format-overview section.
- `docs/architecture.md`: one paragraph in the CLI inventory section
  referencing `_render_*` and `_resolve_compact`.
- `docs/code-map.md`: add `_resolve_compact` and the
  `Settings.output.compact` field to the symbol table.
- `doc3gpp.toml.example`: add `compact = false` to `[output]`.
- `AGENTS.md`: add a row to the "Workflows in one line" table noting
  that the flag is accepted on every `--format` command.

## Backwards compatibility

- Default behaviour is unchanged: `compact=False` → existing output is
  byte-identical to today.
- `Settings.output.compact = false` is the new default.
- No new dependencies, no schema migrations, no DB changes.
- All existing tests must pass without modification.
- Operators who today pipe output to `jq .field` keep working (the
  default still produces valid pretty-printed JSON); operators who pipe
  to `wc -c` or `tr -d ' '` get a denser baseline by opting in.

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Breaking consumer shell scripts that grep the JSON. | Default unchanged; new flag is opt-in. `AGENTS.md` and the help text call out the contract. |
| Hiding a field by accident when stripping `**` / `##`. | Existing field names contain no `*` or `#`; new test asserts every field name survives the compact path. |
| JSON list fields (`required_changes`) becoming ambiguous in compact markdown. | Render as a single-line JSON literal — still parseable, no fence needed. |
| `tdoc show --format raw --compact` confusion. | `raw` is already maximally compact — explicit no-op + help text and a unit test asserting byte-identical output. |
| `_emit_record` (parse direct-mode) forgotten. | The same flag is plumbed through the same `_resolve_compact` helper; the new tests cover it. |
| Drift between `cli.py` and the help text / `cli.md`. | The help string is single-sourced in the Typer `Option`; `docs/cli.md` is updated in the same change set per the "Documentation sync" convention in `docs/conventions.md`. |
