# Spec-document ZIP file inventory

## Goal and scope

Extend the TDoc ZIP inventory feature to specification-document ZIPs. Show the
original ZIP file list alongside the corresponding spec, release, and version
in spec detail output. Collect the list only when that version is parsed; spec
metadata synchronization and detail reads must not download or inspect ZIPs.
The feature applies to CLI, web, HTTP JSON, MCP, and the Python SDK.

An inventory is separate from the converted DOCX files, TOC, chunks, and parse
status. It does not imply a successful parse. Do not add a standalone fetch,
inventory command, bulk backfill, new search filter, or new parsing eligibility.

## Identity and inventory semantics

- The existing spec-version identity is `(spec_id, version)`. Each such
  version has a canonical release attribute; store that release with the
  inventory and expose the list with the matching version and release. A
  release is not an independent extra primary-key dimension because the
  existing spec and spec-document data models do not support two releases for
  the same spec/version pair.
- Enumerate the decoded central-directory filename of every non-directory
  outer-ZIP entry, preserving archive order, duplicates, Unicode, case,
  separators, and names. Include all extensions, hidden and metadata files,
  lock files, and nested ZIP files as entries. Do not inspect nested archives
  or extract any member to enumerate the list. Reuse the existing TDoc ZIP
  enumeration behavior rather than deriving this list from parsed DOCX files.
- File names are untrusted: retain exact names in structured data, render them
  inertly in table/Markdown/compact output, escape them in HTML, and never use
  inventory entries as filesystem paths or commands.
- Unknown inventory is distinct from a known empty archive: structured output
  uses `zip_files: null` for unknown and `zip_files: []` for known empty.

## Collection and persistence

Introduce a dedicated `spec_doc_zip_inventory` table in the specdata database,
with `(spec_id, version)` as its primary key, nullable `release`, and a
losslessly serialized ordered list of filenames. Do not require a foreign key
to the source ledger: an inventory can exist even if no parse-source row was
created. Add an ORM-free inventory domain value, a repository Protocol, a SQL
implementation, and the matching static spec-document schema registry entry.
Inventory reads for a page of versions are batched rather than issuing one SQL
query per version.

In both synchronous CLI/SDK parsing and queued web/MCP parsing, use the
existing spec/version/release resolver, download and ZIP-cache rules, and ZIP
size limit. Once the source ZIP passes the existing download/size checks,
enumerate its central directory before DOCX selection or conversion. Carry the
result to the existing serialized writer lane with the prepared parse. Replace
the inventory for only that resolved `(spec_id, version)` on every actual
re-parse, including `--force`.

When enumeration succeeds but later DOCX selection, conversion, or parsing
fails, carry the inventory and the original exception to the serialized writer,
persist the inventory, then propagate the original error with its existing
failed/skipped classification. A readable empty or DOCX-free ZIP can therefore
have a known inventory even when parsing fails. If persisting an inventory
after a parse error also fails, log that failure and preserve the original
parse error. On the normal success path, an inventory persistence failure must
not be reported as a successful inventory-bearing parse. Neither inventory
presence nor this error path may set `parsed_at` or invent content. Preserve
the existing synchronous and queued forced-parse invalidation semantics,
progress, cancellation, and indexing behavior.

If the download fails, the ZIP is oversized, or the central directory cannot
be read, do not write a partial inventory or overwrite a previously known
inventory. Reads use stored data only, including after ZIP-cache eviction.
Already-parsed versions keep the existing immutable skip; they do not get an
inventory merely because a normal parse was requested. A forced re-parse is
required to populate older parsed versions, and follows the current spec-doc
behavior of re-downloading before parsing. Unparsed versions may use the
existing cached ZIP on their next normal parse.

The table is additive and idempotent through `doc3gpp db init --scope
specdata`; no reset is required. Old databases without the new table remain
readable: treat the specifically missing inventory table as unknown on detail
reads and show an upgrade/re-parse hint where appropriate; other database
errors remain errors. A parse whose preparation succeeds but cannot write an
inventory because the table is missing must fail with an actionable `db init
--scope specdata` hint rather than silently claiming the inventory was saved.
If preparation itself failed, preserve that original error and log the missing
table during the best-effort inventory write. No automatic backfill or download
is performed during schema initialization.

## User-facing behavior

Enrich the existing ORM-free `SpecVersion` values at the service boundary with
transient `zip_files`, using the separate specdata repository. The field is not
a main-database `spec_versions` column. The SDK's `SpecService.list_versions`
returns the same optional per-version inventory; normal pagination, version
filters, parsed status, release selection, and spec sync remain unchanged.

| Surface | Contract |
| --- | --- |
| CLI `doc3gpp spec show` | Add `zip_files` to each version in JSON as an array or `null`. Table, Markdown, and compact output show the list clearly for each version, distinguishing unknown from known empty and making untrusted filenames inert. `--version`, `--limit`, `--offset`, and `--no-wis-crs` retain their existing meanings. |
| Web spec detail | Show the inventory with each matching version, with an unavailable/forced-reparse hint for unknown and a distinct empty state. |
| Web spec-document version page | Show the same version's list independently of TOC/chunk presence or parse success; an inventory left by a failed conversion remains visible. |
| HTTP detail JSON | `GET /specs/{spec_id}?format=json` includes the same per-version `zip_files` values as CLI JSON, subject to the same selected versions. |
| MCP detail | `get_spec` returns the same per-version JSON values as the equivalent HTTP detail response. |
| Python SDK | Version values returned by `SpecService.list_versions` expose optional `zip_files` without leaking ORM objects. |

The existing spec-document version page is HTML-only; no new JSON endpoint is
required. Existing TOC/search payloads and list-spec default fields do not
change. Inventory detail reads do not invoke parsing, network fetches, or ZIP
cache reads.

## Verification and documentation

Use subagent-driven development and test-driven development. Add and run
failing tests before implementation, then verify the green results. Cover:

1. Central-directory semantics, including mixed extensions, directories,
   hidden and nested entries, Unicode, duplicates, empty archives, and inert
   rendering of unsafe names.
2. Repository round trips, release association and spec/version isolation;
   batched reads; unknown versus empty; cache eviction; and additive, repeatable
   initialization of existing specdata databases.
3. Normal and forced parsing, an existing parsed-version skip, cached ZIPs,
   unsupported-only/empty ZIPs, conversion failure after enumeration, invalid
   ZIP/download/size failure preserving a prior inventory, original error
   classification, and both synchronous and queued serialized-writer paths.
4. CLI table/Markdown/compact/JSON, spec detail HTML, spec-document version
   HTML, HTTP JSON, MCP and SDK parity; paging and version filtering; null and
   empty states; inventory visible despite parse failure; and absence of
   inventory-triggered network or cache reads.
5. Existing regression tests, Ruff, and the full offline SQLite test profile.

Update `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`, and any
affected architecture, schema, and code-map documentation in the same change
set. Follow the repository's no-auto-commit policy and keep all work on the
current branch.
