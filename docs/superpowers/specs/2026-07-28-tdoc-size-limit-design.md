# `tdoc_parse.max_tdoc_size_kb` — Design Spec

**Status:** Approved (pending user review of the written spec)
**Date:** 2026-07-28
**Branch:** main
**Author:** brainstorming session

## Goal

Add a configurable size limit that prevents `doc3gpp tdoc parse` from
processing TDoc source files (`.zip` blobs or bare `.docx`) larger than
`max_tdoc_size_kb`. Default `1000` (≈ 1 MiB). `0` disables the limit.

Apply uniformly across every parse path:

1. DB-mode `tdoc parse` (filter-driven batch)
2. `tdoc parse --from-path FILE`
3. `tdoc parse --from-path DIR --output DIR` (local batch)
4. `tdoc parse --from-url FILE`
5. `tdoc parse --from-url FOLDER` (URL batch)
6. `tdoc parse --from-url FOLDER --recursive` (URL batch, recursive)

Skip oversized files; never fail a batch because of one oversized file.

## Non-goals

- Per-TDoc size overrides.
- Sub-size budgets (markdown render time, docx expansion ratio,
  per-section byte caps).
- `Content-Length` HEAD-probe gating — the cache-stat pre-flight
  already saves the bandwidth on cache hits; on a cache miss the
  download is at most a few MB and the post-fetch guard catches it.
- Auto-tuning the limit from historical sizes.

## Settings

### `TDocParseSettings` (new field)

File: `src/doc3gpp/settings/schema.py:212` (`TDocParseSettings` class)

```python
class TDocParseSettings(BaseModel):
    """Knobs for ``doc3gpp tdoc parse``."""

    max_batch: int = Field(default=100, ge=1)
    max_ftp_depth: int = Field(default=2, ge=0, le=10)
    max_tdoc_size_kb: int = Field(
        default=1000,
        ge=0,
        description=(
            "Per-file cap in KB for TDoc sources (.zip or .docx). Files "
            "larger than this are skipped (size-limit skip bucket). "
            "0 disables the limit."
        ),
    )
```

- `ge=0` so the user-facing spec "set the value to 0 to disable the
  limit" is enforceable at the schema boundary.
- Internal value is always converted to bytes (`* 1024`) at the
  call-site so the rest of the code never deals with KB.
- Env var: `DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB` (auto-derived from
  the field name via pydantic-settings).
- TOML key: `tdoc_parse.max_tdoc_size_kb`.
- Allowlist: add `DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB` to
  `ALLOWED_ENV_VARS` in `src/doc3gpp/settings/schema.py`.

### `doc3gpp.toml.example`

Update the `[tdoc_parse]` block:

```toml
# [tdoc_parse]
# max_batch = 100
# max_ftp_depth = 2
# max_tdoc_size_kb = 1000  # skip .zip/.docx larger than this; 0 = unlimited
```

## CLI

### New flag

File: `src/doc3gpp/cli.py:1138` (`tdoc_parse` Typer command)

```python
max_tdoc_size_kb: int | None = typer.Option(
    None,
    "--max-tdoc-size-kb",
    min=0,
    help=(
        "Override tdoc_parse.max_tdoc_size_kb. Source files (.zip or "
        ".docx) larger than this many KB are skipped (size-limit skip "
        "bucket). 0 = unlimited."
    ),
)
```

Flag mirrors the existing `--max-depth` precedent (which overrides
`settings.tdoc_parse.max_ftp_depth`). Naming follows the TOML key
exactly.

### Resolution helper

```python
def _resolve_max_tdoc_size_bytes(
    *,
    override_kb: int | None,
    settings: "Settings",
) -> int:
    """Return the effective per-file cap in bytes; 0 = unlimited."""
    if override_kb is not None:
        return override_kb * 1024
    return settings.tdoc_parse.max_tdoc_size_kb * 1024
```

Add next to the existing `_resolve_url_batch_depth` helper
(`src/doc3gpp/cli.py:1124`).

### Dispatch

The CLI computes the resolved bytes value **once at the top of
`tdoc_parse`** and threads it through to:

- `build_tdoc_cr_service(max_tdoc_size_bytes=...)` factory call
- `_tdoc_parse_direct(max_tdoc_size_bytes=...)` for `--from-path FILE`
  and `--from-url FILE`
- `_tdoc_parse_local_batch(max_tdoc_size_bytes=...)` for `--from-path DIR`
- `_tdoc_parse_url_batch(max_tdoc_size_bytes=...)` for `--from-url FOLDER`

Single resolution point keeps behaviour consistent across every mode.

## Service layer

### `TDocCrService` constructor (new kwarg)

File: `src/doc3gpp/services/tdoc_cr_service.py:362` (`__init__`)

```python
def __init__(
    self,
    *,
    cache: TDocCacheLike,
    scraper_client: "ScraperClient",
    cr_repository: TDocCrDetailRepository,
    cr_ttcn_repository: TDocCrTTCNDetailRepository,
    tdoc_repository: TDocRepository,
    parser: TDocParser | None = None,
    parser_registry: TDocParserRegistry | None = None,
    max_tdoc_size_bytes: int = 0,
) -> None:
    ...
    self._max_tdoc_size_bytes = max_tdoc_size_bytes
```

Stored as an instance attribute. `0` (the default) means "no check" so
existing tests / callers that don't care continue to work without
changes.

### `services/factory.py`

`build_tdoc_cr_service` resolves the bytes value once via
`_resolve_max_tdoc_size_bytes(override_kb=None, settings=get_settings())`
and passes it to the constructor. The CLI passes its override via a
separate helper that bypasses the factory for non-default values
(or extends the factory with an `override_kb` kwarg — preferred; see
implementation plan).

## Gate points (four)

### 1. `download_tdoc_zip` — pre-download cache probe

File: `src/doc3gpp/scraping/tdoc_zip_source.py:224`

Before any HTTP fetch, stat the on-disk cache slot
`<cache.root>/zips/<cache_file>` (already computed by the caller via
`derive_cache_file(tdoc.ftp_url)`). If the file exists and its size
exceeds `max_tdoc_size_bytes` (when `> 0`), raise `TDocTooLargeError`
before any network work — bandwidth + parse time both saved.

The pre-flight is purely additive: cache miss falls through to the
existing download path unchanged.

### 2. `download_tdoc_zip` — post-fetch guard

After a successful download (cache write), stat the written file. If
over limit, raise `TDocTooLargeError`. Same exception type as the
pre-flight; the source string differs (`"download:<url>"` vs
`"cache:<path>"`) so the operator can tell which path caught it.

### 3. `direct_parse_bytes` — guard at the bytes-into-parse boundary

File: `src/doc3gpp/parsers/direct_extractor.py:351`

New optional kwarg `max_bytes: int = 0`. When `> 0` and
`len(payload) > max_bytes`, raise `TDocTooLargeError(filename=filename,
size=len(payload), limit=max_bytes)` before any unzip / python-docx
call.

Called by:

- `TDocCrService.extract_from_url` for non-3GPP URLs
- `TDocCrService.extract_from_url` for the FK-miss / no-tdoc-id branches
- `TDocCrService._extract_from_3gpp_url` after the happy-path download
  (defence-in-depth; the post-fetch guard in `download_tdoc_zip`
  should already have caught it, but the direct-parse path also
  writes its own zip slot and we want a uniform check)
- `_tdoc_parse_direct` for `--from-path` files (read `Path.stat().st_size`
  before calling `direct_parse_bytes`)

### 4. `_tdoc_parse_local_batch` — stat guard for directory trees

File: `src/doc3gpp/cli.py` (existing local-batch helper)

Before processing each file under the directory, stat it. If over
limit, log a warning and skip the file (no parse, no DB write). The
existing per-file emit already handles per-file outcomes; the size-skip
slot feeds the same emitter with a "size-limit skip" reason.

## Exception

### `TDocTooLargeError`

File: `src/doc3gpp/services/tdoc_cr_service.py` (next to
`TDocNotYetOnFTPError`)

```python
class TDocTooLargeError(Exception):
    """Raised when a TDoc's source file exceeds the configured size limit.

    Routed to the existing skip bucket (not the failure bucket)
    because "this file is too big for our parse budget" is an
    operational decision, not an upstream-side error.
    """

    def __init__(self, source: str, size: int, limit: int) -> None:
        super().__init__(
            f"TDoc source {source!r} is {size} bytes, "
            f"exceeds max_tdoc_size_kb limit ({limit} bytes)"
        )
        self.source = source
        self.size = size
        self.limit = limit
```

The `size` and `limit` are kept as attributes for the CLI summary
formatter.

## Skip routing

### `TDocCrService.extract_many` — batch path

File: `src/doc3gpp/services/tdoc_cr_service.py:647`

Extend the existing `except` chain:

```python
for raw_id in tdoc_ids:
    try:
        result = self.extract(raw_id, force=force, full=full)
    except TDocNotYetOnFTPError as exc:
        skipped[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
        continue
    except TDocTooLargeError as exc:
        logger.info(
            "Skipping TDoc %r: %s bytes exceeds limit %s",
            raw_id, exc.size, exc.limit,
        )
        skipped[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
        continue
    except (ValueError, LookupError, TDocZipDownloadError, TypeError) as exc:
        failures[raw_id.strip()] = f"{type(exc).__name__}: {exc}"
        continue
    successes[result.details.tdoc_id] = result
```

`TDocTooLargeError` slots into the existing `skipped` bucket. CLI
differentiates the two skip reasons by inspecting the reason string's
prefix.

### CLI summary (DB-mode)

File: `src/doc3gpp/cli.py:1625-1630` (existing summary block)

```python
size_skipped = sum(
    1 for tid, reason in batch.skipped.items()
    if reason.startswith("TDocTooLargeError:")
)
ftp_skipped = sum(
    1 for tid, reason in batch.skipped.items()
    if reason.startswith("TDocNotYetOnFTPError:")
)
typer.echo(
    f"Skipped (exceeds max_tdoc_size_kb={max_tdoc_size_kb}): "
    f"{size_skipped}"
)
typer.echo(f"Skipped (not yet on FTP):                 {ftp_skipped}")
```

Order: size-skip line first (operational), FTP-skip second
(upstream-lag).

### URL batch

File: `src/doc3gpp/services/tdoc_cr_service.py:793` (`extract_from_url_batch`)

Extend the per-file `try/except` to catch `TDocTooLargeError` before
the generic `except Exception`. Add a new `skipped: dict[str, str]`
attribute to `DirectParseBatchResult`:

```python
@dataclass(slots=True, frozen=True)
class DirectParseBatchResult:
    results: list[DirectParseResult]
    failures: dict[str, str]
    skipped: dict[str, str] = field(default_factory=dict)
```

CLI render (`_emit_url_batch_results`) prints both a "Skipped (size)"
and a "Skipped (FTP)" line; the first wins visually because it's the
operator's knob.

### Single-file direct path (`_tdoc_parse_direct`)

Catches `TDocTooLargeError` separately, emits a `WARNING` log, prints
`filename <size> bytes exceeds max_tdoc_size_kb limit (<limit> bytes)`
to stderr, exits 0 with no parse output — consistent with how the
non-3GPP URL branch surfaces a warning + emit today.

## Tests

### Unit (`tests/unit/`)

- `test_tdoc_parse_settings.py` (extend existing): default `1000`,
  `ge=0` enforces, `0` parses cleanly, env var
  `DOC3GPP_TDOC_PARSE__MAX_TDOC_SIZE_KB` overrides, TOML
  `[tdoc_parse] max_tdoc_size_kb` overrides.
- `test_tdoc_cr_service.py` (extend existing):
  - `extract_many` routes `TDocTooLargeError` into `skipped` not
    `failures`.
  - Constructor accepts `max_tdoc_size_bytes=0` as default; non-zero
    propagated to `download_tdoc_zip` and `direct_parse_bytes`.
- New `test_tdoc_zip_source.py` (extend): cache file present and
  oversized → `TDocTooLargeError` raised before network call; cache
  file present and within limit → download proceeds.
- New `test_direct_extractor.py` (extend): `direct_parse_bytes`
  raises `TDocTooLargeError` when `len(payload) > max_bytes`;
  `max_bytes=0` is a no-op.
- `test_tdoc_parse_cli.py` (extend): `--max-tdoc-size-kb 0` and
  `--max-tdoc-size-kb 500` flag plumbing; verify the new summary line
  counts size-skips separately.

### Integration (`tests/integration/`)

- `test_tdoc_parse_local_batch_sqlite.py` (new): a directory with one
  small file and one oversized file; the batch should emit the small
  file, skip the oversized one with a `TDocTooLargeError` reason, and
  exit 0.

## Documentation

- `docs/cli.md` — `tdoc parse` section: add the new flag, the
  new "Skipped (exceeds max_tdoc_size_kb)" summary line, and a
  one-paragraph "Size limit" subsection explaining the default,
  the `0`-disables semantics, and the four gate points.
- `docs/conventions.md` §"Settings caching" — add the new
  env-var allowlist entry.
- `doc3gpp.toml.example` — update the `[tdoc_parse]` block (see above).
- `AGENTS.md` — add the new field to the "Where to look" table;
  extend the `tdoc_parse` workflow one-liner to mention the new knob.

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| A legit small TDoc gets skipped because the zip wrapper is huge but the inner docx is tiny. | Document that the limit applies to the **zip / docx bytes on disk**, not the rendered output. Operators can re-run with `--max-tdoc-size-kb 0` for one-off large files. |
| Existing callers of `TDocCrService(...)` without `max_tdoc_size_bytes` break. | Default is `0` (no-op). Existing tests pass unchanged. |
| `Content-Length` lies about the size. | Post-fetch guard catches it before any conversion. |
| `_tdoc_parse_local_batch` silently skips the operator's only file. | Print a per-file `WARNING` line + count in the summary, exit 0 (consistent with non-3GPP URL behaviour). |
| Two skip reasons share the same bucket → user can't tell them apart. | CLI splits by prefix in the existing `skipped` dict; both summary lines printed. |