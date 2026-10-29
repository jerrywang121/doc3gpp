# TDoc ZIP file inventory

## Goal and scope

Record the complete list of file entries in an original TDoc ZIP, regardless
of file type, and expose it consistently through TDoc detail views in the CLI,
web UI, HTTP, MCP, and the Python SDK. This inventory is distinct from the
existing `files` field, which lists auxiliary revision, review, and support
files found outside the ZIP in the meeting FTP directory.

Inventory collection is limited to the TDoc types the existing parser handles:
CR, WID new/revised, LS in/out, and discussion. Other TDoc types remain
unavailable; this feature does not extend their parsing eligibility or add a
standalone inventory-download command. For supported types the inventory is
recorded when the original ZIP is processed, even if subsequent document
conversion or parsing fails. No inventory is invented from the converted
Markdown documents.

## Inventory semantics

- The inventory contains the original, decoded central-directory filename of
  every file entry, in ZIP directory order. Preserve spelling, case, path
  separators, Unicode, and duplicate entries. Exclude directory-only entries.
- Include unsupported extensions, hidden and metadata files, Word lock files,
  and nested ZIP files as entries. Do not recurse into nested ZIPs and do not
  extract any members to the filesystem. In particular, do not inspect the
  internal ZIP structure of a DOCX as though it were the outer TDoc ZIP.
- Inventory names are untrusted data: retain them as inert strings, escape
  them in HTML, and never use them as filesystem paths or shell commands.
- A missing inventory is **unknown**, not an empty list. A successfully read
  ZIP with no file entries has a known empty list. The JSON representation is
  `zip_files: null` for unknown and `zip_files: []` for known empty.
- The inventory is independent of parsing completeness. Its presence does
  not indicate that Markdown extraction succeeded or that every member is
  convertible. `--source-file` continues to select only stored converted
  source documents, using that existing interface's exact-path rules.

## Collection and persistence

Introduce a dedicated main-database inventory table and repository keyed by
the normalized immutable ZIP URL, associated with its TDoc ID. Store the file
list losslessly, including order and duplicates. A separate table is needed
because a failed parse may not produce a `tdoc_extracts` row. Keep the
repository contract in `repository/`, its SQL implementation in
`storage/repositories/`, and domain values free of ORM objects. Update the
static TDoc schema registry to match the new ORM table.

During a supported database-anchored parse, obtain the source ZIP using the
existing download/cache behavior, then enumerate its central directory before
document conversion. Record a complete inventory once the ZIP directory
can be read, including when a later validation, conversion, parser, or
conversion-limit failure makes the parse fail. The original parsing error and
its existing failed/skipped classification must remain visible; saving an
inventory must not turn a failed parse into a success. If the ZIP cannot be
downloaded or its directory cannot be read, do not publish a partial inventory
or overwrite an existing complete inventory.

ZIP enumeration reads metadata only and must not bypass the existing source
download/size safeguards. Keep database writes in the existing serialized
pipeline writer lane, including the path where preparation returns a parsing
failure together with an inventory. Synchronous single-item extraction must
persist the same result. Preserve cancellation behavior and the existing
atomic content-bundle and index-update rules.

For canonical 3GPP URL parsing, apply the existing FK-aware persistence
rules: store an inventory only when the ZIP is associated with a stored,
eligible TDoc. Local-path and non-3GPP direct parsing retain their existing
in-memory-only behavior. Different immutable URLs for one TDoc retain
independent inventories; by-ID reads use the TDoc's current URL, while
by-URL reads use the exact normalized URL. When a download resolves a fallback
URL, associate the inventory with the URL of the ZIP actually used.

Existing cached ZIPs may have been downloaded from a fallback, but do not
record that provenance. When inventory collection needs such a cached ZIP,
do not guess its URL: re-download it through the existing candidate and size
checks. Record the actual source for new downloads and bind that provenance to
the cached bytes, so later parses can reuse the cache safely. Missing, invalid,
stale, or mismatched provenance is unknown and triggers the same re-download.
If the re-download fails, preserve any existing cached ZIP and inventory and
report the download failure; do not attribute unknown bytes to a URL.
Any other write to a ZIP-cache key that does not supply verified provenance
must invalidate that key's provenance, so stale metadata cannot be reused for
bytes written through a different cache path.

Creating the table is additive and idempotent through the existing main-schema
bootstrap (`doc3gpp db init --scope main`); no database reset or destructive
migration is required. Existing records start with an unknown inventory.
Re-parsing a supported TDoc with `--force` populates it, reusing a source
ZIP cache with verified provenance and downloading when needed, including
when an older cache entry has no provenance.
There is no bulk backfill and no inventory-only fetch action.

## User-facing behavior

Inventory detail reads query the stored inventory without downloading a ZIP,
re-parsing, or changing the inventory. The existing optional metadata auto-sync
for CLI `tdoc show --tdoc` remains unchanged when `sync.auto_sync` is enabled;
it may still sync meeting and TDoc metadata before the inventory lookup. A
supported TDoc whose inventory is unknown gets a clear unavailable message and
a forced re-parse hint. For an unsupported type, say unavailable without
suggesting a parse that cannot succeed. A known empty inventory is presented as
an empty ZIP file list, not as unavailable. Where an inventory exists despite
a parse failure, display the inventory without claiming the TDoc was parsed.

| Surface | Contract |
| --- | --- |
| CLI `tdoc show` | Table and Markdown output have a separate ZIP-files section; JSON always includes `zip_files` as an array of names or `null`. Both `--tdoc` and `--ftp-url` selectors use the corresponding URL inventory. `--format raw` remains raw Markdown and is unchanged. |
| Web detail | Show a separate ZIP-files section with the stored filenames or the appropriate unavailable/empty message; do not conflate it with auxiliary files. |
| HTTP detail JSON | `GET /tdocs/{tdoc_id}?format=json` and `GET /tdocs/by-url?ftp_url=...&format=json` expose the same `zip_files` value as CLI detail JSON. |
| MCP detail | `get_tdoc` exposes the same detail field and remains byte-consistent with the equivalent compact HTTP detail JSON. |
| Content JSON | HTTP content JSON and MCP `get_tdoc_content(format="json")` also include `zip_files` when the existing content service can return a response; they remain byte-consistent with one another. Inventory lookup does not make unavailable Markdown content available. |
| Python SDK | The shared TDoc detail and content domain values expose the optional inventory through repository-backed reads; no ORM values escape. |

Adding `zip_files` does not change the meaning of `files`, `documents`,
`document_count`, or `skipped_sources`. JSON consumers can distinguish
unknown from an empty ZIP without interpreting parser status.

## Tests and documentation

Use SDD and test-driven development. Add failing tests before implementation
for each behavior, then verify the tests pass. Cover:

1. Central-directory enumeration with DOCX, DOC, PDF, spreadsheets, hidden
   files, Word lock files, nested ZIPs, directory entries, Unicode, and
   duplicate names; no recursive extraction or filesystem access.
2. Persistence and retrieval by exact URL, multiple revisions, ordering,
   duplicates, unknown versus known empty, cache eviction, and an additive,
   repeatable schema upgrade for an existing database.
3. Supported CR and family parse flows, provenance-verified cache reuse and
   forced re-parse, legacy or mismatched provenance requiring a re-download,
   failed re-download preserving the old cache and inventory, conversion
   failure after ZIP inspection, corrupt or missing ZIP, existing
   failed/skipped classification, and serialized writes under concurrent
   preparation. Local and non-3GPP direct paths must not gain persistent
   side effects.
4. CLI table/Markdown/JSON and both selectors; web detail HTML and HTTP JSON;
   MCP detail and content JSON parity; SDK values; unavailable/empty states;
   and no inventory-triggered network access or parsing. Existing optional CLI
   metadata auto-sync remains covered separately. Verify raw Markdown output
   remains unchanged and auxiliary files remain separate.
5. Existing regression tests, lint, and the full offline SQLite test suite.

Update `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`, and any
other affected architecture/schema documentation in the same change set.
