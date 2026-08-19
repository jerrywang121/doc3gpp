# TDoc show — URL-anchored read on web + MCP

**Date:** 2026-08-19
**Status:** Approved design
**Branch:** new branch off `main` (e.g. `web-mcp-tdoc-show-by-url`)

## Goal

The CLI's `doc3gpp tdoc show --ftp-url <url>` reads six URL-keyed
sources (`tdocs`, `tdoc_cr_cover_page`, `tdoc_cr_ttcn_details`,
`tdoc_files`, `tdoc_extracts`, `tdoc_cr_change_details`), anchors
on the URL rather than on a parent TDoc, and emits a
`TDocShowRecordByUrl` DTO. The web and MCP surfaces only support the
`tdoc_id` anchor (`GET /tdocs/{tdoc_id}` and the MCP `get_tdoc` tool);
operators wanting to look up "what does the DB have for this URL?"
have to fall back to the CLI.

This change adds URL-anchored reads to both surfaces so an operator
with a 3GPP URL can hit the same `/tdocs/by-url` page from a browser
or call the same `get_tdoc(ftp_url=...)` MCP tool — and both
return the byte-identical JSON envelope `doc3gpp tdoc show --ftp-url
<url> --format json` already emits.

The CLI is unchanged. The composition logic in
`TDocShowRecordByUrl.from_ftp_url` (`src/doc3gpp/models/tdoc_show.py:163`)
is already battle-tested (8 callers in `cli.py`, plus
`tests/unit/test_tdoc_show_record.py`) and is the single source of
truth for the URL-anchored read.

## Section 1 — Architecture

```
Browser tab / MCP client
    │
    │   ?ftp_url=<url>      OR     tdoc_id=<id> (existing)
    │                                     │
    ▼                                     ▼
GET /tdocs/by-url?ftp_url=...       GET /tdocs/{tdoc_id}  (existing)
MCP get_tdoc(ftp_url=...)           MCP get_tdoc(tdoc_id=...)  (existing)
    │                                     │
    │                                     ▼
    │                              TDocShowRecord.from_tdoc_id  (existing)
    │
    ▼
parsers.normalizers.normalize_ftp_path(<raw url>)
    │  full https://www.3gpp.org/ftp/...  or  bare TSG_RAN/WG5/.../file.zip
    │  → canonical relative path the DB stores as ftp_url.
    │
    ▼
TDocShowRecordByUrl.from_ftp_url(ftp_url, repos)
    │
    │  repos.tdoc.get_by_ftp_url           (tdocs)
    │  repos.cr.get_by_url                 (tdoc_cr_cover_page)
    │  repos.cr.get_extract_meta_by_url    (tdoc_extracts)
    │  repos.cr_ttcn.get_by_url            (gated on cover is not None)
    │  repos.cr_change_details.get_by_url  (tdoc_cr_change_details)
    │  repos.file.get_by_ftp_url           (tdoc_files)
    ▼
TDocShowRecordByUrl(ftp_url, tdoc?, cover?, ttcn?, changes?,
                    extracted_at?, files?)  →  render via existing
                                              helpers / new polymorphic
                                              tdoc_show.html
```

Auto-sync is **never** triggered in URL mode (CLI parity — there's
no parent TDoc or meeting to anchor a sync on). The boundary
normalisation happens once per request, before the five repo reads;
the normalised value is what gets surfaced as `record.ftp_url` in
the rendered output.

The HTTP JSON response is byte-identical to `doc3gpp tdoc show
--ftp-url <url> --format json` (both surfaces use the same
`render.to_jsonable(record)` helper). The MCP tool returns the same
JSON string via `_to_json`. The HTTP `?format=json` route returns
the same JSON via `JSONResponse(content=to_jsonable(record))`.

### Why a new route, not a query param on `GET /tdocs/{tdoc_id}`

The existing `GET /tdocs/{tdoc_id}` anchors on a path param; adding
a `?ftp_url=` query param that XORs with the path param creates a
mutually-exclusive-pair on the same route, which FastAPI doesn't
have a built-in idiom for. A separate `GET /tdocs/by-url` route
keeps the URL-anchored read bookmarkable (`/tdocs/by-url?ftp_url=...`
copies/pastes cleanly into a chat) and avoids the routing ambiguity.

### Why extend `get_tdoc` instead of adding a new tool

The MCP tool catalogue already has 24 entries; adding a second
URL-shaped tool doubles the surface area for what is logically one
read operation (anchor on TDoc **or** URL, get the same five-table
composition). A second optional `ftp_url` arg with an XOR validator
mirrors the CLI's `--tdoc` / `--ftp-url` XOR exactly.

## Section 2 — Surface details

### `GET /tdocs/by-url`

| Method | Route | Description |
| --- | --- | --- |
| GET | `/tdocs/by-url` | URL-anchored read; renders `tdoc_show.html` polymorphed on `TDocShowRecordByUrl`. |
| GET | `/tdocs/by-url?format=json` | Returns `TDocShowRecordByUrl` as JSON (byte-identical to `doc3gpp tdoc show --ftp-url <url> --format json`). |

The `ftp_url` query param is **required**. Behaviour:

- Absent → `400 invalid_filter` ("`ftp_url` query param is required").
- Empty / whitespace-only after `normalize_ftp_path` (e.g.
  `?ftp_url=`, `?ftp_url=/`, `?ftp_url=ftp://`) → `400 invalid_filter`
  ("`ftp_url` normalised to an empty path").
- After normalisation, the URL is passed to
  `TDocShowRecordByUrl.from_ftp_url`. Every repo lookup that returns
  `None` is fine (record fields stay `None`); only when **every** of
  `tdoc`, `cover`, `ttcn`, `changes`, `extracted_at`, `files` is empty
  do we raise `TDocUrlNotFoundError` → `404 not_found`.
- The route reuses `_build_show_repos(request, file_repo)` (same five
  repos the existing `show_tdoc` route uses).
- `has_cached_zip` is computed off `record.ftp_url` instead of
  `record.tdoc.ftp_url`. The cache key is `derive_cache_file(ftp_url)`
  — the same function the existing `tdoc_show` route uses.
- The Parse card is omitted entirely in URL mode (no parent TDoc to
  filter a `parse_tdocs` job on; the user has to run
  `doc3gpp tdoc parse --from-url <url>` for fresh data).
- The existing `/tdocs/{tdoc_id}/content` and `/tdocs/{tdoc_id}/download`
  routes stay path-anchored. In URL mode the cached-source-zip link
  is replaced with a direct `https://www.3gpp.org/ftp/<record.ftp_url>`
  anchor.

The route is registered with `include_in_schema=False` (consistent
with the rest of the tdocs router) and added to the OpenAPI summary
table in `docs/web-server.md`.

### MCP `get_tdoc` — extended signature

```python
@server.tool(
    name="get_tdoc",
    description=(
        "Get a single tdoc by id (or by FTP URL), including its cover-page, "
        "TTCN sidecar, and extract metadata. Exactly one of `tdoc_id` or "
        "`ftp_url` must be supplied. In URL mode the response surfaces every "
        "row across tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
        "tdoc_files, and tdoc_extracts whose ftp_url matches; auto-sync is "
        "never triggered."
    ),
)
@_mcp_error_guard
def get_tdoc(
    tdoc_id: Annotated[
        str | None,
        Field(description="Canonical tdoc id (e.g. 'R5-260013'). Mutually exclusive with ftp_url."),
    ] = None,
    ftp_url: Annotated[
        str | None,
        Field(
            description=(
                "3GPP FTP URL (full URL or relative path) — surfaces every row "
                "across the four URL-keyed tables whose ftp_url matches. "
                "Mutually exclusive with tdoc_id."
            )
        ),
    ] = None,
) -> str:
    ...
```

Behaviour:

- Both `tdoc_id` and `ftp_url` supplied (or neither) →
  `InvalidFilterError("Provide exactly one of tdoc_id or ftp_url")`
  → MCP `-32602` invalid-params error.
- `ftp_url` mode: `normalize_ftp_path(ftp_url)`, raise
  `InvalidFilterError("ftp_url normalised to an empty path")` on
  empty result, build a `TDocShowRepos` (same five concrete repos
  the existing tool uses), call
  `TDocShowRecordByUrl.from_ftp_url(ftp_url, repos)`.
- If every repo returns null, raise `TDocUrlNotFoundError(ftp_url)`
  (a `LookupError` subclass). `_mcp_error_guard` already maps
  `LookupError` to MCP error code `-32602` (invalid-params), the
  same code `TDocNotFoundError` uses today — verify this in
  `web/mcp_server.py::_mcp_error_guard` during implementation
  and add an explicit mapping if one is missing).
- Render via `render.to_jsonable(record)` (same as the CLI's
  `--format json` path).
- `_mcp_error_guard` and `_to_json` are reused unchanged.

### New exception — `TDocUrlNotFoundError`

In `src/doc3gpp/services/tdoc_cr_service.py` (next to
`TDocNotFoundError`):

```python
class TDocUrlNotFoundError(LookupError):
    """No row matches the requested FTP URL across any of the five repos."""

    def __init__(self, ftp_url: str) -> None:
        self.ftp_url = ftp_url
        super().__init__(
            f"No stored rows match ftp_url {ftp_url!r}. The URL was looked "
            "up against tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
            "tdoc_extracts, and tdoc_files; none matched. The upstream "
            "document may not have been ingested yet — run 'doc3gpp tdoc "
            "sync' on the parent meeting, or 'doc3gpp tdoc parse --from-url "
            f"{ftp_url}' to populate the URL-keyed tables."
        )
```

Registered in `src/doc3gpp/web/errors.py::map_domain_error` as a 404
(same shape as `TDocNotFoundError`).

### Template changes — `src/doc3gpp/web/templates/tdoc_show.html`

Single template, polymorphic on the record shape. The existing
sections (`## Cover page`, `## TTCN`, `## Required changes`,
`## Extracted changes`, `## Extracted at`, `## Auxiliary files`)
already gate on their respective attributes (`record.cover`,
`record.ttcn`, etc.) — they work as-is for both shapes. The changes:

| Section | Change |
| --- | --- |
| `<title>` + `<h1>` | When `record.tdoc is None`, render `TDoc <code>{{ record.ftp_url }}</code>` instead of `TDoc <code>{{ record.tdoc.tdoc_id }}</code>`. |
| `## TDoc` card | Skip the whole `<section class="card">` block when `record.tdoc is None`. Show a small "No parent tdocs row matches this URL" placeholder card instead. |
| Parse card | Omit entirely when `record.tdoc is None`. |
| `/content` + `/download` buttons | Replace with a single "Open on 3GPP FTP" anchor linking to `https://www.3gpp.org/ftp/<record.ftp_url>` when `record.tdoc is None`. |
| Auxiliary files empty placeholder | When `record.tdoc is None`, the hint reads `Run doc3gpp tdoc parse --from-url <url> to populate auxiliary rows.` instead of the existing tdoc-id hint. |
| `## XLSX metadata` card | Already gates on `record.tdoc.<field>` so it's naturally skipped in URL mode. No change. |

The cover-without-parent case (a URL with a `tdoc_cr_cover_page`
row but no parent `tdocs` row) renders the cover card and surfaces
the small note text in the TDoc card placeholder, mirroring the
CLI's `_render_tdoc_show_markdown_full` `tdoc_missing_note` wording.

No new template files. No template inheritance changes.

### Error / status mapping

| Situation | HTTP response | MCP response | HTTP envelope | MCP error code |
|---|---|---|---|---|
| `ftp_url` query param missing | 400 | n/a (tool requires either) | `{"error": "invalid_filter", "message": "ftp_url query param is required"}` | `-32602` |
| `ftp_url` empty after normalisation | 400 | same | `{"error": "invalid_filter", "message": "ftp_url normalised to an empty path"}` | `-32602` |
| Both `tdoc_id` and `ftp_url` supplied to MCP tool | n/a | error | n/a | `-32602` |
| Neither supplied to MCP tool | n/a | error | n/a | `-32602` |
| URL resolves to no rows anywhere | 404 | error | `{"error": "not_found", "message": "..."}` | `-32602` (via existing `LookupError` mapping) |
| URL resolves to cover row but no parent TDoc | 200 | 200 | normal envelope, TDoc card replaced with placeholder | normal text |
| URL resolves to auxiliary files but no cover / tdoc | 200 | 200 | normal envelope, only files card populated | normal text |

The `LookupError` → 404 mapping for HTTP is already in
`web/errors.py::map_domain_error` (the existing `TDocNotFoundError`
uses it). `TDocUrlNotFoundError` slots in next to it.

## Section 3 — Touched files

- `src/doc3gpp/services/tdoc_cr_service.py` — add `TDocUrlNotFoundError`.
- `src/doc3gpp/web/errors.py` — register `TDocUrlNotFoundError` in
  `map_domain_error` as 404.
- `src/doc3gpp/web/routes/tdocs.py` — add `show_tdoc_by_url` route,
  register in the same `router`. Reuse `_build_show_repos`.
- `src/doc3gpp/web/templates/tdoc_show.html` — make the template
  polymorphic as described above.
- `src/doc3gpp/web/mcp_server.py` — extend the existing `get_tdoc`
  tool with the `ftp_url` arg + XOR validation.

No schema migration, no service-layer changes, no model-layer
changes (the `TDocShowRecordByUrl.from_ftp_url` classmethod is
already in `models/tdoc_show.py` and unchanged).

## Section 4 — Testing

### Unit — `tests/unit/web/test_routes.py`

New tests in `show_tdoc_by_url`:

- `test_show_tdoc_by_url_404_when_no_rows` — `GET /tdocs/by-url?ftp_url=...`
  against an empty DB → 404, envelope has `error: not_found`.
- `test_show_tdoc_by_url_400_on_missing_param` → 400, `error: invalid_filter`.
- `test_show_tdoc_by_url_400_on_empty_after_normalize` — `?ftp_url=`
  and `?ftp_url=/` and `?ftp_url=ftp://` all 400.
- `test_show_tdoc_by_url_full_url_matches_bare_path` — seed the
  `tdocs` row with `ftp_url = "TSG_RAN/WG5/.../file.zip"`, hit
  `?ftp_url=https://www.3gpp.org/ftp/TSG_RAN/WG5/.../file.zip` and
  assert 200.
- `test_show_tdoc_by_url_json_byte_matches_cli` — seed a complete
  record (tdocs + cover + ttcn + files), hit `?format=json`,
  `json.loads(result)` byte-equal to
  `TDocShowRecordByUrl.from_ftp_url(ftp_url, repos)` rendered via
  `render.to_jsonable`.
- `test_show_tdoc_by_url_no_parent_tdoc_renders_placeholder` — seed
  only `tdoc_cr_cover_page`; HTML contains the placeholder text.
- `test_show_tdoc_by_url_parse_card_omitted_in_url_mode` — HTML does
  not contain `#parse-form` when no parent TDoc.
- `test_show_tdoc_by_url_no_auto_sync` — set `settings.sync.auto_sync=True`,
  mount a spy on `trigger_auto_sync`; assert it is not called (URL
  mode never triggers auto-sync).
- `test_show_tdoc_by_url_lone_extracted_at_renders` —
  seed only a `tdoc_extracts` row (so `extracted_at` is populated
  but `tdoc`, `cover`, `ttcn`, `changes`, `files` are all `None`/`[]`).
  The route must render 200 with the "Extracted at" card: the
  "all six sources empty" rule includes `extracted_at`, so a lone
  `extracted_at` keeps the record alive (CLI parity — the CLI's
  `_tdoc_show_by_ftp_url` all-empty check includes `extracted_at`).

### Unit — `tests/unit/web/test_mcp_server.py`

New tests for the extended `get_tdoc` tool:

- `test_mcp_get_tdoc_by_url_returns_json_envelope` — URL mode
  returns a JSON string whose `json.loads(...)` matches the
  `TDocShowRecordByUrl.from_ftp_url` payload (same byte-match as
  the CLI `--format json`).
- `test_mcp_get_tdoc_xor_validator_rejects_both` — both `tdoc_id`
  and `ftp_url` → invalid-params error.
- `test_mcp_get_tdoc_xor_validator_rejects_neither` — both missing
  → invalid-params error.
- `test_mcp_get_tdoc_404_on_no_rows` — empty DB → LookupError, MCP
  error envelope.
- `test_mcp_get_tdoc_url_normalisation` — full URL and bare path
  resolve the same record.
- `test_mcp_get_tdoc_existing_tdoc_id_path_unchanged` — regression:
  the existing `tdoc_id` path still works (no behavioural change).

### Integration — `tests/integration/test_web_end_to_end.py`

Add a parity block:

- `test_get_tdoc_by_url_byte_parity_with_cli` — full-stack via
  `TestClient`: seed a complete record, hit
  `GET /tdocs/by-url?ftp_url=<url>&format=json`, assert the JSON
  bytes equal the bytes the CLI's `tdoc_show --ftp-url <url>
  --format json` would emit. Reuses the existing parity-test
  helper. Marks the `MCP/HTTP byte parity` set so the test
  framework catches regressions.

### Integration — `tests/integration/test_mcp_end_to_end.py`

- Add `"get_tdoc"` to the registered-tool-names set if not already
  (it's already in the existing set; we only need to ensure the
  new `ftp_url` arg is in the tool schema). Add a parity check
  that the MCP `get_tdoc(ftp_url=...)` response equals the HTTP
  `GET /tdocs/by-url?ftp_url=...&format=json` response.

### Docs sync (per `docs/conventions.md` §"Documentation sync")

- `docs/web-server.md`:
  - Add `GET /tdocs/by-url` row to the HTTP routes table.
  - Update the MCP tools description of `get_tdoc` to mention the
    `ftp_url` arg and the URL-mode semantics.
  - Mention the polymorphic `tdoc_show.html` in the "TDoc detail
    page" prose section.
- `AGENTS.md` — extend the "Workflows in one line" entry for
  `tdoc show --ftp-url` to note that the same composition is now
  reachable via `GET /tdocs/by-url` and MCP `get_tdoc(ftp_url=...)`.

CLI docs (`docs/cli.md`, `README.md`) — unchanged.

## Section 5 — Anti-patterns to avoid

- **Don't** add a second `_tdoc_show_by_url` template that
  duplicates 80% of `tdoc_show.html`. Make the existing template
  polymorphic; the cover / TTCN / files cards already gate on their
  respective attributes.
- **Don't** branch the existing `show_tdoc` route on a query param
  XOR with the path param. Add a separate route — FastAPI doesn't
  have a clean idiom for the XOR, and a separate route keeps the
  bookmarkable URL shape.
- **Don't** add a second MCP tool (`get_tdoc_by_url`). Extend the
  existing tool with a second optional arg + XOR validator; mirrors
  the CLI's UX exactly.
- **Don't** re-implement the URL classifier / normalisation. Import
  `normalize_ftp_path` from `parsers.normalizers`; the CLI path
  uses the same function.
- **Don't** add `/tdocs/by-url/content` or `/tdocs/by-url/download`
  routes. The URL is the row identity, not a tdoc_id — there's no
  path-anchored cache lookup to add. The user reads cached content
  via `tdoc parse --from-url <url>` (which writes the cache) or
  via `tdoc show --tdoc <id>` once they know the tdoc_id.
- **Don't** trigger auto-sync in URL mode. There's no parent TDoc
  / meeting to anchor a sync on, and the CLI parity contract is
  "URL mode never triggers auto-sync".
- **Don't** bypass `_mcp_error_guard` for the new arg. Every
  existing error path goes through it.
- **Don't** change `TDocShowRecordByUrl.from_ftp_url`'s contract.
  The web and MCP surfaces compose via the same classmethod the
  CLI uses — that's the byte-parity guarantee.

## Section 6 — Scope check

Single implementation plan, single PR. Three edited files in
`src/` (`web/routes/tdocs.py`, `web/mcp_server.py`,
`web/templates/tdoc_show.html`), two small additions
(`services/tdoc_cr_service.py`, `web/errors.py`), no schema
migration, no service-layer changes, no model-layer changes.
Three new test blocks (`tests/unit/web/test_routes.py`,
`tests/unit/web/test_mcp_server.py`, two integration test files)
plus one integration parity block. Three doc touch-ups.

CLI behaviour is unchanged. The composition logic
(`TDocShowRecordByUrl.from_ftp_url`) is unchanged. The five repo
reads are unchanged.
