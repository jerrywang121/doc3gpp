# DB-mode LS support for `tdoc parse` — design

**Status:** approved
**Date:** 2026-08-18
**Branch target:** `feat/tdoc-parse-ls-db-mode`
**Follows on:** `docs/superpowers/specs/2026-08-17-ls-tdoc-parser-design.md`

## Background

`doc3gpp tdoc parse --tdoc R5-260017` returns **"No TDoc matched the provided filters."**
even though `doc3gpp tdoc list --tdoc R5-260017` shows the row (`type="LS in"`). Two
cooperating bugs cause this:

1. `cli.py:1673` hardcodes `"tdoc_type": tdoc_type or "CR"`. When the operator omits
   `--type`, the SQL filter silently restricts matches to `type='CR'` only.
2. `TDocCrService.extract()` (`src/doc3gpp/services/tdoc_cr_service.py:668-678`)
   explicitly raises `TDocTypeUnsupportedError` for any row whose parser resolves
   to `LSParserBase`, even though the LS sidecar (`tdoc_cr_ls_details`) and the
   direct-mode LS parse path are fully implemented.

The LS sidecar (`services/tdoc_cr_service.py:_extract_from_3gpp_url`,
`parsers/ls/ls_parsers.py:LSParserBase`,
`storage/repositories/tdoc_cr_ls_sql.py`) is wired and tested for direct-mode
(`--from-url`, `--from-path`). What's missing is letting DB-mode
(`--tdoc`, `--meeting-id`, `--meeting`, …) drive the same dispatch.

The user-reported goal is two-fold:

* `doc3gpp tdoc parse --tdoc <id>` (no `--type`) extracts the row regardless of
  `tdocs.type`, dispatching CR → `CRParser`/`TTCNCRParser` and LS →
  `ThreeGPPLSParser` (and future LS variants).
* `doc3gpp tdoc parse --from-url <3gpp-ftp-url>` continues to auto-sync (TSG →
  meeting → tdoc), and once the parent row lands in `tdocs`, the same
  `tdoc.type`-driven dispatch decides CR vs LS. **No new behaviour** — the
  direct-mode path already handles LS via `_extract_from_3gpp_url`.

## Goals (in scope)

1. `tdoc parse --tdoc <id>` (no `--type`) successfully extracts rows of **any**
   `tdocs.type` present in the table, dispatching CR / LS by `tdoc.type`.
2. `tdoc parse --meeting-id <id>` and `tdoc parse --meeting <name>` route the
   same way for any matched row.
3. `tdoc parse --from-url <3gpp-url>` continues to auto-sync, lands the parent
   row in `tdocs`, then routes via the same dispatch — no behaviour change
   expected, but the auto-sync contract is reaffirmed and documented.
4. The DB cache short-circuit honours `tdoc_type`: CR probes
   `tdoc_cr_cover_page`, LS probes `tdoc_cr_ls_details`.
5. The batch summary (`tdoc parse <filters>`) renders two parallel "Extracted"
   groups (CR + LS) with their respective `from_cache` counts.
6. `tdoc show --tdoc <id>` keeps rendering the existing `LS Cover` card from
   the sidecar — no render change.
7. Unit + integration tests; existing "LS row raises `TDocTypeUnsupportedError`"
   expectations are updated to "LS row writes the LS sidecar".

## Non-goals (out of scope for v1)

* New LS sidecar fields; new LS variants beyond `3gpp` (the variant framework
  from `2026-08-17-ls-tdoc-parser-design.md` is untouched).
* Removing the existing direct-mode flows (`--from-url`, `--from-path`,
  uninitialised DB). They remain canonical for those flags; DB-mode is now an
  *additional* entry point for LS rows, not a replacement.
* `--type ls` filter removal — it stays as an explicit SQL filter for
  `tdoc list` / `tdoc parse` (and is now strictly optional instead of
  implicitly enforced).
* `tdoc show` HTTP / MCP / web renderers (`tdoc_show.html`, MCP `get_tdoc`) —
  already LS-aware; the sidecar row is the source of truth.
* Search index projections — `SearchService.upsert_for_tdoc` and the
  SemanticSearchService already project `response_to_title` / `to_groups` / etc.
  for LS sidecars (verified in the existing LS design). No change.

## Field model

Unchanged. The eleven LS header fields from
`docs/superpowers/specs/2026-08-17-ls-tdoc-parser-design.md` §"Field model" remain
the sidecar contract. DB-mode writes the same `tdoc_cr_ls_details` row.

## Approach

### 1. Drop the CR default at the filter boundary (`cli.py:1673`)

```python
list_kwargs: dict[str, object] = {
    "tdoc_id": normalised_tdoc,
    "meeting_like": meeting,
    "meeting_id": meeting_id,
    "tdoc_type": tdoc_type,            # was: tdoc_type or "CR"
    ...
}
```

When `--type` is omitted the filter is `None` and `_apply_text_filter`
no-ops; every `tdocs` row becomes a candidate. The
`exclude_parsed=not force` flag still applies — extended to exclude both
`tdoc_cr_cover_page` and `tdoc_cr_ls_details` rows in non-`--force` mode
(see step 4).

The `--type` flag itself stays as an opt-in narrower (e.g. `--type LS` to
restrict a batch to LS rows). The CLI's existing `_warn_on_ignored_filter_flags`
helpers stay untouched; filter-arg validation in `_any_filter_set` still
requires at least one of `--tdoc` / `--meeting-id` / `--meeting` / `--type` /
etc.

### 2. Remove the `TDocTypeUnsupportedError` raise in `TDocCrService.extract`

Replace lines 668-681 of `src/doc3gpp/services/tdoc_cr_service.py`:

```python
parser = self._resolve_parser(
    normalised,
    tdoc_type=tdoc.type,
    source=tdoc.source,
)
```

…with the same two-branch pattern already in
`_extract_from_3gpp_url` (`src/doc3gpp/services/tdoc_cr_service.py:1264-1307`),
minus the cache-probe prelude (replaced by a new LS-aware cache probe, see
step 4):

* **LS branch** (`isinstance(parser, LSParserBase)`):
  * `ls_result = parser.parse_ls(markdown, tdoc_id=normalised)`.
  * If `ls_result.cover is not None`, build a `TDocLSDetails` via
    `dataclasses.replace(..., ftp_url=stored_ftp_url)`.
  * Upsert via `self._ls_repository.upsert(details)`. If `_ls_repository
    is None`, raise `TDocTypeUnsupportedError` (factory should always
    inject — see step 6).
  * No `tdoc_cr_cover_page` / `tdoc_cr_ttcn_details` / `tdoc_extracts`
    rows.
  * Run `_index_after_parse(normalised)` and
    `_embed_after_parse(normalised)`.
  * Return a new `LSResult(details=details, extract_meta=meta,
    from_cache=False)` (or `from_cache=True` on a cache hit — see
    step 4).
* **CR branch**: unchanged from the existing implementation.

The existing `parser.parse(markdown, ...)` call at the bottom of
`extract()` is replaced by an `if/else` that dispatches the parser method
(`parse_ls` vs `parse`) based on the resolved parser type.

### 3. Adapt `ExtractResult` / `BatchExtractResult` / CLI summary

Add a sibling dataclass `LSResult` next to `ExtractResult`:

```python
@dataclass(slots=True, frozen=True)
class LSResult:
    details: TDocLSDetails
    extract_meta: TDocExtractMeta
    from_cache: bool
```

Extend `BatchExtractResult`:

```python
@dataclass(slots=True, frozen=True)
class BatchExtractResult:
    successes: dict[str, ExtractResult]
    ls_successes: dict[str, LSResult] = field(default_factory=dict)
    failures: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)
```

`extract_many` populates `ls_successes` when an LS row succeeds and leaves
`successes` for CR rows. The CLI batch summary renders two parallel
"Extracted" groups when either dict is non-empty. The renderer follows the
pattern at `src/doc3gpp/cli.py:1709-1758` — a new `_print_parse_group`
call for "Extracted LS" with a slim column set (`tdoc_id`, `meeting`,
`title`, `variant`, `parser_version`).

### 4. DB cache probe for LS rows in `TDocCrService.extract`

Before the existing zip-download + cover-page cache probe
(`src/doc3gpp/services/tdoc_cr_service.py:596-642`), when the resolved
parser is `LSParserBase`, probe the LS sidecar:

```python
if not force:
    for candidate in candidates:
        cached = self._ls_repository.get_by_url(
            normalize_ftp_path(candidate)
        )
        if cached is not None:
            meta = self._repo.get_extract_meta_by_url(
                normalize_ftp_path(candidate)
            )
            if meta is not None:
                return LSResult(
                    details=cached, extract_meta=meta, from_cache=True,
                )
```

A cache hit returns immediately — same contract as the CR branch.

The `exclude_parsed` filter in `SQLAlchemyTDocRepository.list`
(`src/doc3gpp/storage/repositories/tdoc_sql.py:174-179`) is extended to
also exclude rows present in `tdoc_cr_ls_details`:

```python
if exclude_parsed:
    stmt = stmt.where(
        ~select(TDocCrDetailOrm.tdoc_id)
        .where(TDocCrDetailOrm.tdoc_id == TDocORM.tdoc_id)
        .exists()
        & ~select(TDocCrLSDetailOrm.tdoc_id)
        .where(TDocCrLSDetailOrm.tdoc_id == TDocORM.tdoc_id)
        .exists()
    )
```

### 5. Reaffirm the `--from-url` auto-sync contract

`src/doc3gpp/cli.py:1547-1560` already calls `collect_tdoc_candidates_for_url`
+ `trigger_auto_sync` for 3GPP-URL paths. Once the parent row lands,
`extract_from_url` calls `_resolve_parser` with the stored
`row.type`/`row.source`
(`src/doc3gpp/services/tdoc_cr_service.py:933-936` → `_extract_from_3gpp_url`),
which already dispatches CR / LS correctly. **No code change to the
direct-mode flow.**

Document the contract:

```
tdoc parse --from-url <3gpp-url>
  → collect_tdoc_candidates_for_url   (BFS basename extract)
  → trigger_auto_sync(...)            (TSG sync → meeting sync → tdoc sync,
                                       same skip rules as explicit sync;
                                       non-3GPP URLs never trigger)
  → extract_from_url                  (FK probe; falls through to
                                       _extract_from_3gpp_url)
  → _extract_from_3gpp_url            (download + render + parse +
                                       persist sidecar)
  → parser dispatched by tdoc.type    (CR or LS, same registry)
```

The DB-mode entry (`tdoc parse --tdoc <id>`) and the URL-mode entry
(`tdoc parse --from-url <url>`) now produce identical DB state for any
given TDoc id. The only difference is which flag the operator used — DB
short-circuit probes apply in both cases (the cover-page probe in DB
mode + the URL-keyed probe in direct mode both honour `from_cache=True`).

### 6. Wiring guarantees

`services/factory.py:build_tdoc_cr_service` already injects `ls_repository`
via `build_ls_repository()` (line 254). We add a runtime assertion:

```python
if self._ls_repository is None:
    raise RuntimeError(
        "TDocCrService requires an ls_repository; "
        "construct via services.factory.build_tdoc_cr_service()"
    )
```

…at the top of the LS branch in `extract()`. This guards against test
helpers that build the service without the new dep (a small fixture
update in `tests/unit/test_tdoc_cr_service.py` adds the dep; the new test
file `tests/unit/test_tdoc_cr_service_db_mode_ls.py` adds the dep at
construction).

## Architecture / layering

| Layer | Change |
|---|---|
| `cli.py` | drop default-CR at line 1673; new `_print_parse_group` line for "Extracted LS"; auto-sync contract reaffirmed (no behaviour change) |
| `services/tdoc_cr_service.py` | new `LSResult` dataclass; `extract` branches on `LSParserBase`; new LS-aware DB cache probe; `extract_many` populates `ls_successes`; `BatchExtractResult` gains `ls_successes` |
| `storage/repositories/tdoc_sql.py` | `list()` `exclude_parsed` sub-query gains an OR-clause against `tdoc_cr_ls_details` |
| `storage/repositories/tdoc_cr_ls_sql.py` | **(unchanged)** — already implements `upsert` / `get_by_url` |
| `storage/db/models.py` | import `TDocCrLSDetailOrm` (already imported elsewhere; one import site change) |
| `docs/cli.md`, `docs/architecture.md`, `docs/code-map.md`, `AGENTS.md`, `docs/web-server.md` | replace stale "DB-mode LS extraction is not yet supported" notes |

## Error handling

* `LSHeaderMissingError` (raised by `ThreeGPPLSParser.parse_ls`) routes to
  `BatchExtractResult.failures` with the existing per-id exception-class
  prefix format.
* `_resolve_parser` `LookupError` for an unrecognised `tdoc.type` (legacy
  `DRAFT`, `TS`, etc.) — caught by `extract`'s outer try, re-raised as
  `TDocTypeUnsupportedError(tdoc_id, observed_type=tdoc.type)`. The CLI's
  `typer.BadParameter` translation at `cli.py:3041` stays accurate.
* `LSHeaderMissingError` is raised **after** the cache probe, so an LS row
  already in `tdoc_cr_ls_details` returns `from_cache=True` even when the
  markdown on disk has been edited. This matches the existing CR cache
  contract.
* Existing `TDocNotFoundError` / `TDocNotYetOnFTPError` / `TDocTooLargeError`
  / `TDocZipDownloadError` propagation through `extract_many` is unchanged.

## Testing strategy

1. **Unit (mocked repos)** — new `tests/unit/test_tdoc_cr_service_db_mode_ls.py`:
   * `extract()` writes the LS sidecar + skips cover-page sidecar when
     `tdoc.type == "LS in"` and the markdown parses as an LS.
   * `extract()` returns `from_cache=True` when `ls_repo.get_by_url` hits.
   * `extract()` raises `LSHeaderMissingError` when the rendered markdown
     is unrecognisable (no DB writes).
   * `extract_many` populates `ls_successes["R5-260017"]` and leaves
     `successes={}`.

2. **CLI** — `tests/unit/test_tdoc_parse_cli.py`:
   * Replace the `LS in` → `TDocTypeUnsupportedError` expectation with an
     LS-success assertion in the batch-math test (line ~598).
   * Add `--type LS` filter test (now an explicit narrower).

3. **Integration** — extend `tests/integration/test_tdoc_cr_sqlite.py`:
   * Rewrite `test_ls_row_raises_TDocTypeUnsupportedError` (line 437) to
     assert: LS row writes to `tdoc_cr_ls_details` via
     `tdoc parse --tdoc <id>`; cache hit on a second call; parser-version
     stamped correctly.
   * Add `exclude_parsed` test: an LS row already in `tdoc_cr_ls_details`
     is excluded from a non-`--force` batch.

4. **Optional online smoke** — left out of the test surface (LS markdown
   rendering depends on the upstream docx + python-docx installation that
   may not be available in CI); operators can verify by running
   `doc3gpp tdoc parse --tdoc R5-260017` against a live 3GPP FTP.

## Open questions

* **Return-type shape**: `ExtractResult | LSResult` (tagged union) vs two
  parallel dataclasses with a sibling `ls_successes` dict. **Pick: parallel
  dataclasses** — the CLI already prints parallel groups, and the web/MCP
  layers never read `ExtractResult`, they consult sidecar rows directly.
  Less invasive.

## Out-of-scope follow-ups

* If the operator wants to limit a batch to LS rows explicitly they pass
  `--type LS` (no flag change needed).
* A `--family ls`/`cr` shorthand filter is **deferred** — can ride on the
  same rich-filter grammar in `docs/cli.md` if it becomes a request.
