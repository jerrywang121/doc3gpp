# Testcase nested statuses payload — Design

**Date:** 2026-09-12
**Status:** approved
**Scope:** `testcase show` + `testcase list` on CLI, web (HTML + `?format=json`), and MCP.

## Problem

`testcase show` (and the `testcase list` projection) emit a two-key envelope,
`{"testcase": {...header...}, "statuses": [...]}`, with the status rows as a
sibling of the header. The identity of a row is `(testcase_id, group)`, so the
status belongs to the testcase object as a child, not as a detached sibling.
Additionally each status row repeats `group`, which is redundant once the
statuses are nested under their `(testcase_id, group)` parent.

Current `show --format json` shape:

```json
[{"testcase": {"testcase_id": "20.7", "group": "LTE", "...": null},
  "statuses": [{"group": "LTE", "path": "FDD", "gcf_ptcrb": null,
                "ttcn_status": "not available"}]}]
```

Current `list --format json` shape: flat header fields plus a compact
`statuses` **dict** (`path → ttcn_status`), losing `gcf_ptcrb` entirely.

## Decision

Approach 1 (chosen): flatten into the header object. One JSON object per
`(testcase_id, group)`; all header fields inline; `statuses` nested inside
as a list of per-path objects **without** `group`.

New `show` shape (all 8 header fields):

```json
[{"testcase_id": "20.7", "title": "IPsec tunnel ...", "ats": null,
  "feature": "MPSoWLAN", "release": "Rel-18", "wis": "...",
  "spec": "36.523-1", "group": "LTE",
  "statuses": [{"path": "FDD", "gcf_ptcrb": null,
                "ttcn_status": "not available"}]}]
```

New `list` shape: the `--fields`-selected header subset plus `statuses`
(same nested list of `{path, gcf_ptcrb, ttcn_status}`); `statuses` stays in
the default field set. This unifies `list` and `show`: both carry full
status rows, `list` is just field-selectable.

Rejected: (2) keep envelope, merge group up — keeps the wrapper the request
removes; (3) path-keyed map of objects — awkward in table/markdown, diverges
from the per-path row model.

## Changes

**Models** (`src/doc3gpp/models/testcase.py`):
- `TestCaseWithStatuses.statuses: dict[str, str | None]` →
  `list[TestCaseStatus]`. `TestCaseDetail` unchanged (already a nested list).

**Service** (`src/doc3gpp/services/testcase_service.py`):
- `list_recent` passes full status rows through instead of collapsing to
  `{row.path: row.ttcn_status}`.

**CLI** (`src/doc3gpp/cli.py`):
- `testcase show` JSON: flat per-`(id, group)` objects (header fields +
  nested `statuses`); docstring updated. Table/markdown: header block per
  group, then that group's status rows with columns
  `path, gcf_ptcrb, ttcn_status` (`TESTCASE_SHOW_STATUS_FIELDS` drops
  `group`).
- `testcase list` JSON: `statuses` serialised as the nested list (no dict
  special-case through `_coerce_cell`-style stringification; kept out of
  `_emit_records` like today). Table/markdown `statuses` cell renders
  `path=gcf/ttcn` pairs (replaces `_format_testcase_statuses`'
  `path=ttcn` rendering, since `gcf_ptcrb` is now present).
- `--fields` still selects header columns; `statuses` remains deselectable.

**Web** (`src/doc3gpp/web/routes/testcases.py`, `src/doc3gpp/web/render.py`,
templates):
- `GET /testcases?format=json`: `testcase_rows` emits the same flat+nested
  shape as CLI list (byte-identical).
- `GET /testcases/{id}?format=json`: flat+nested array (byte-identical to
  CLI show).
- `testcase_show.html`: drop the `Group` column from the per-group status
  table (group is on the parent header card).
- `partials/testcase_results.html`: statuses cell iterates status objects,
  not dict items.
- `_TESTCASE_STATUS_FIELDS` drops `group`.

**MCP** (`src/doc3gpp/web/mcp_server.py`):
- `get_testcase` returns the flat+nested array (byte-identical to CLI/HTTP).
- `list_testcases` returns the flat+nested rows via `render.testcase_rows`.
- `_TESTCASE_STATUS_FIELDS` drops `group`. Tool descriptions updated.

**Docs**: `docs/cli.md` (show/list JSON shapes), `docs/architecture.md`
(testcase workflow one-liners), `docs/web-server.md` (route + MCP payload
shapes), `docs/code-map.md` (model field change), `AGENTS.md` if the
one-liners name the old envelope.

## Compatibility

Breaking change to the JSON shape on both commands and all three surfaces
(CLI, HTTP, MCP): envelope keys removed, list `statuses` dict → list of
objects, `group` removed from status rows. No deprecation window; the
feature is pre-release on a feature branch. Consumers key on
`(testcase_id, group)` at the top level of each element.

## Testing

- Update JSON-shape assertions: `tests/integration/test_testcase_cli.py`
  (show multi-group + group-scoped + list shape), `test_web_routes.py`
  (`testcase_rows` + show route), `test_web_end_to_end.py`,
  `test_mcp_end_to_end.py` (show + list tools), `test_testcase_model.py`
  if it pins the dict type.
- New: list emits full `{path, gcf_ptcrb, ttcn_status}` objects (gcf
  survives the list path); status rows carry no `group` key anywhere.
- Gate: `./scripts/test_sqlite.sh` green + `ruff check` clean.
