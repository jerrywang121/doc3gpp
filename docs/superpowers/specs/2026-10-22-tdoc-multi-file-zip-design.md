# Multi-file TDoc ZIP extraction and source-aware search

Date: 2026-10-22  
Status: **Approved for planning — implementation not yet approved**  
Branch: `feat/tdoc-multifile-markdown`  
Workspace: current checkout, as requested; no worktree.

## 1. Purpose and confirmed requirements

A TDoc ZIP downloaded from 3GPP can contain a main Word document and
supporting documents. Convert every supported document to its own Markdown
body without multiplying the TDoc's cover-page or extraction metadata.
FTS5 results must identify the particular source file within the ZIP, and
raw-content retrieval must be able to return all of those Markdown bodies.

The user confirmed:

- Exactly one main document contains the CR cover page. Other documents
  provide supporting information.
- Keep one parent extraction metadata record per immutable ZIP `ftp_url`,
  retaining the existing URL identity rather than creating metadata per file.
- Store document Markdown in SQL, not solely in an evictable cache.
- Work on the feature branch in the current checkout.
- Use Superpowers subagent-driven development (SDD) and test-driven
  development (TDD) during implementation.
- Review this specification before implementation planning or implementation.

The contracts below are proposals for this review, not additional decisions
already approved by the user.

## 2. Scope and non-goals

In scope:

- Multiple `.docx` members inside one TDoc ZIP, including subdirectories.
- One main document's existing CR cover, TTCN, and body-change metadata.
- Durable per-document Markdown, source identity, and deterministic ordering.
- Database, direct-URL, local-file, folder/batch, and queued parsing paths.
- Source-aware FTS5 results and preservation of those references in hybrid search.
- CLI, HTTP, and MCP retrieval of all documents or one selected source.
- Existing single-document behavior, legacy artifact handling, and reprocessing.

Not in scope:

- New converters for binary `.doc`, PDF, spreadsheets, or nested ZIP attachments.
- Expanding database-mode parsing beyond the currently supported CR TDoc types.
- Multiple independent CR covers inside one ZIP, or merging conflicting covers.
- Per-document embedding generation or redesigning semantic-only search.
- Repurposing `tdoc_files`, unrelated sync changes, or unrelated test repairs.

## 3. Existing assumptions to replace

`parsers/cr/helpers.py::extract_docx_from_zip` selects one Word member.
`TDocCrService._load_or_render_markdown` writes one Markdown member, while
`_decompress_markdown` returns the first decodable member. Calling those
functions repeatedly would reuse or overwrite the same cache entry.

`DirectParseResult` and prepared direct results carry a scalar Markdown body.
The CR parser requires a cover page, so it cannot be applied indiscriminately
to supporting documents.

The current TDoc FTS5 index contains one row per TDoc. It indexes metadata
and extracted CR fields, not complete Markdown. Hybrid fusion is keyed by
`tdoc_id`; passing it ungrouped document hits would lose source information.

## 4. Ownership and durable content model

```text
tdocs
  └── tdoc_extracts                         one row per ZIP ftp_url
        ├── tdoc_documents: main.docx       one content row per source
        └── tdoc_documents: annex/info.docx
```

### Parent metadata

Retain `tdoc_extracts.ftp_url` as the primary key and `tdoc_id` as the logical
reference. Keep `cache_file`, timestamps, and parser version at this level.
`doc_filename` becomes explicitly the full source path of the **main** document.
It is not an inventory of every document.

Add an independent `extraction_version` integer and `document_count` integer.
Existing rows migrate to version `1` with count `0`; complete multi-file
extractions use version `2`. Layout completeness must not be inferred from
the presence of a cover row or from the CR parser version.

### Document content

Add a `tdoc_documents` table in the main database:

| Field | Purpose |
| --- | --- |
| `document_id` | Stable primary key, SHA-256 of the UTF-8 compact JSON array `[normalized_ftp_url, source_file]`, using `ensure_ascii=False`. |
| `ftp_url` | FK to `tdoc_extracts.ftp_url`, with cascade deletion. |
| `source_file` | Full, case-preserving member path within the source ZIP. |
| `markdown_file` | Relative member name within the derived Markdown ZIP. |
| `file_order` | Main document first, followed by supporting files in deterministic path order. |
| `markdown_text` | Complete converted Markdown; authoritative content for retrieval and index rebuilding. |
| `content_hash` | SHA-256 of the UTF-8 Markdown for index freshness checks. |

Enforce uniqueness of `(ftp_url, source_file)` and `(ftp_url, file_order)`.
Different folders or ZIP URLs may contain the same basename without collision.
The main document is identified by the parent's `doc_filename`, avoiding a
second independently writable main-document flag.

These are content/provenance rows, not additional TDoc or CR metadata records.
`tdoc_files` continues to describe remotely discovered auxiliary files; it
does not describe members inside a ZIP and receives no new inner-file rows.

Domain values remain ORM-free dataclasses. Add document and extraction-bundle
contracts to `repository/protocols.py`; concrete SQL and transaction ownership
remain in `storage/repositories/`. Wire dependencies through the factory and
web dependency helpers rather than constructing repositories in adapters.

## 5. Extraction and main-document selection

Use one shared preparation flow across database and direct parsing:

1. Resolve the ZIP URL/TDoc identity where applicable and obtain source bytes.
2. Enumerate supported `.docx` members, excluding directories, `__MACOSX`, and
   Word lock files. Do not recursively parse embedded attachment ZIPs.
3. Validate member identities and convert each supported document independently.
4. Apply the existing CR-cover structural detector to the converted bodies.
5. Require exactly one main-document candidate. Run the existing CR parser
   only for that candidate, including its TTCN/body-change extraction.
6. Build an ordered document collection and the one parent metadata bundle.
7. Persist the complete bundle on the existing serialized writer, then invoke
   the existing best-effort indexing hooks.

A supporting document without a CR header is valid content. It must not be
rejected merely because it is not itself a CR. No supporting document may
overwrite the main cover or contribute to the main document's TTCN/body-change
sidecars. Its full text remains searchable independently.

Zero or multiple main candidates are explicit archive failures. Filenames
and alphabetical order are not sufficient evidence for selecting a cover.
A standalone direct-mode `.docx` is a one-document collection and retains
the current requirement for a valid CR cover.

Keep parallelism at the existing archive/preparation level; process an
archive's documents sequentially rather than creating a nested worker pool. (Superseded
for DOCX conversion by `2026-10-28-spec-doc-parse-speed-design.md`, which converts members
through one shared process pool.)
Prepared values and direct results carry a document collection instead of a
single `markdown` string. Update all consumers, including worker result
rendering and local/folder raw-output paths, together.

Preserve existing direct-mode persistence and FK rules: local files,
non-3GPP URLs, and 3GPP sources without a stored owning TDoc remain in-memory
results where they do today. They do not create parent TDoc rows. Their
document values use a separate ephemeral identity namespace based on the
source payload hash and member path, not a fabricated stored FTP URL.
Existing URL discovery and auto-sync ordering are unchanged.

### Failure and safety policy

- Unsupported attachment formats are ignored with source-named warnings.
  An archive with no supported main document fails clearly.
- Failure to read or convert any supported document fails the archive as a
  whole. Do not mark a partially converted collection complete.
- Preserve an earlier successful SQL extraction if a later forced attempt fails.
- Reject absolute/traversing member paths and duplicate normalized identities;
  treat ZIP separators consistently and never extract member paths directly
  into the filesystem.
- Retain the existing downloaded-source byte cap and skipped/failed classifier.
  Add TOML-only `tdoc_parse.max_documents_per_zip` (default `100`) and
  `tdoc_parse.max_uncompressed_size_kb` (default `102400`, aggregate supported
  member bytes); `0` disables the respective limit. Enforce declared and actual
  byte limits while reading. Resource-limit violations use the skip bucket.
- Report archive outcome using existing buckets, with source-specific failure
  detail. Progress may mention member counts without changing job identities.

## 6. Persistence and cache behavior

Add a repository-level atomic replacement operation for the main cover,
optional TTCN/body-change sidecars, extraction parent, and document collection.
It must replace removed documents and clear obsolete optional main sidecars
on successful reprocessing, not leave rows from an earlier extraction.
Set version `2` and `document_count` only in that successful transaction.
Do not implement transaction coordination through ORM access in a service.

Retain the existing source ZIP location:

```text
<cache root>/zips/<cache_file>
```

The derived artifact remains one real ZIP at:

```text
<cache root>/markdown/<cache_file>
```

It contains one `.md` member per document plus a versioned JSON manifest.
Use collision-free members such as
`documents/<document_id>/<sanitized-source-stem>.md`. The manifest records
ZIP URL, main source, ordered source-to-Markdown mappings, and content hashes.
The reader must select Markdown explicitly rather than decoding the first
ZIP member; a manifest must never be mistaken for document content.

Construct the complete artifact before installing it through the cache's
atomic write path. Preparation may install a derived cache artifact before
SQL persistence; a later SQL failure does not make that artifact authoritative.
SQL is the source of truth once document rows exist, so no cross-filesystem
transaction is required. Cache eviction or purge does not delete SQL content
or change extraction completeness. Cache limits/status still count artifacts,
not individual Markdown members.

Auto-indexing occurs after the SQL transaction commits. An index failure does
not invalidate successfully stored content; freshness reporting and rebuilds
must make the missing index work recoverable from SQL without downloading.

## 7. FTS5 indexing and result provenance

### One logical parent result, multiple searchable source rows

Extend the existing TDoc FTS5 table rather than introducing a second ranking
system. Store:

- One `metadata` row per TDoc, retaining the existing eight indexed fields.
- One `document` row per stored document, with its Markdown in a new indexed
  `document_text` column and the existing indexed metadata fields empty.
- Unindexed provenance: row kind, document ID, source ZIP URL, source path,
  and document content hash.

This keeps unparsed TDocs searchable by metadata and avoids making a title
match falsely appear to be a match in every supporting document. The metadata
row uses the canonical TDoc URL when resolving its CR sidecars. Document rows
cover all stored extraction URLs belonging to the logical TDoc.

`upsert(tdoc_id)` atomically replaces that TDoc's metadata and document index
rows from SQL. Removal deletes both kinds. Schema bootstrap must explicitly
migrate/recreate the virtual table; `CREATE ... IF NOT EXISTS` is insufficient.

### Ranking, filtering, and snippets

Preserve current parent-level filters and public pagination semantics. Group
matching rows by `tdoc_id` **before** applying parent limit/offset. Rank a parent
by its best matching row's BM25 score, with deterministic TDoc-ID tie-breaking.
Use SQL grouping/windowing or bounded batch iteration rather than assuming
the current fanout factor can contain every matching document.

An FTS expression must match one metadata or document row. Do not satisfy
phrases or conjunctions by stitching together text from different source
documents. Existing matching across the eight metadata fields remains;
cross-row conjunction is not a new capability of this feature.

Keep current metadata previews. Document matches expose separate previews
bound to their own source identity. Add `matched_documents` to shared TDoc
search rows; each entry contains:

```text
document_id, ftp_url, source_file, score, previews
```

Return the matching documents for each returned parent in BM25 order, with
source-identity tie-breaking. A metadata-only hit has an empty list. The
top-level `ftp_url` retains its existing canonical-parent meaning; consumers
must use the document entry's URL for a source-specific link.

Append a ninth BM25 weight for `document_text`, default `1.0`. Accept existing
eight-element configurations by appending that default; write/document new
configurations with nine values. Update validation, SQL score arguments,
snippet binding, settings templates, and schema tests together.

`row_count` continues to describe logical indexed TDocs, not physical FTS rows.
Index freshness additionally checks document IDs/content hashes and extraction
versions; unchanged `uploaded_date` is not proof that document indexing is
complete. Resume/stale-only rebuilds retain parent-level cursors and rebuild
each selected parent's complete row set.

### Hybrid and semantic-only behavior

Feed grouped TDoc hits into the existing TDoc-level reciprocal-rank fusion.
Carry `matched_documents` through fusion instead of discarding it. Hybrid
ranking remains parent-level; the source list describes its FTS matches.
Semantic-only and filter-only results return an empty source-match list.
Existing vector text/chunk identity is unchanged in this feature; do not
claim supporting-document semantic coverage without embedding those documents.

## 8. Content retrieval and public surfaces

Introduce a shared content service returning an ordered collection, using
SQL document content first. Support either a canonical TDoc selector or an
exact ZIP `ftp_url`, plus an optional full `source_file` selector. A source
selector is resolved only within that ZIP; it is not a filesystem path.

Structured content has this shape:

```json
{
  "tdoc_id": "R5-123456",
  "ftp_url": ".../R5-123456.zip",
  "extraction_version": 2,
  "complete": true,
  "documents": [
    {"document_id": "...", "source_file": "main.docx", "is_main": true, "markdown": "..."},
    {"document_id": "...", "source_file": "annex/info.docx", "is_main": false, "markdown": "..."}
  ]
}
```

Selecting a source returns the same envelope with one document. An unknown
source is a clear not-found error, not a fallback to the main document.
`complete` describes the stored extraction, not the number of documents
selected in this response. Inconsistent version-2 parent/document rows produce
an incomplete-extraction error with a re-parse hint; do not conceal corruption
by falling back to a stale derived cache.

### CLI

- Extend `tdoc show --format raw` with optional `--source-file <full-member-path>`.
  Retain the existing mutually exclusive `--tdoc` and `--ftp-url` selectors.
  Reject `--source-file` with non-raw metadata formats rather than ignoring it.
- A single selected/available document emits its Markdown unchanged, retaining
  the current trailing-newline behavior.
- Multiple documents emit separate named Markdown blocks, main first. Each
  starts with `<!-- doc3gpp-source: <JSON-encoded source_file string> -->` and
  is followed by that document's unmodified body. Do not concatenate unnamed
  bodies. Escape `>` as `\u003e` inside that JSON string so a source name cannot
  close the comment. Use the same renderer for direct and batch raw outputs.
- `--output` continues to write the rendered text stream to one destination;
  a caller needing one original body uses `--source-file`.
- Existing structured `tdoc show` metadata remains metadata, not an implicit
  dump of every Markdown body.

### HTTP and MCP

- Extend `GET /tdocs/{tdoc_id}/content` with `source_file` and `format=json`.
  Retain HTML and Markdown modes, defaulting to HTML.
- Add `GET /tdocs/by-url/content?ftp_url=...` for exact-URL retrieval, registering
  it before parameterized TDoc routes.
- Extend MCP `get_tdoc_content` to accept exactly one of `tdoc_id`/`ftp_url`,
  optional `source_file`, and `format=markdown|html|json`; retain its Markdown
  default. JSON byte-matches the equivalent HTTP payload using shared serializers.
- Markdown mode uses the same single-body/named-block rules as the CLI.
  HTML displays distinct source-labelled sections and selected-source links.
- Search results link each matching source using its exact URL and source path,
  correctly URL-encoded. TDoc content pages expose main/supporting file choices.

Content reads on HTTP/MCP and the CLI URL selector do not trigger sync,
downloads, or conversion. Preserve the CLI TDoc selector's existing ability
to invoke extraction when content needs populating. Unknown selectors remain
not-found errors; unavailable/unreadable legacy content retains a parse hint.

## 9. Legacy data and completeness

Normal parse discovery/count/preview must consider version-1 extractions
pending, even if a cover row exists. Version-2 skip checks require a matching
document count and exactly one stored main source. `--force` replaces the
complete collection using the current source-ZIP cache/download rules.

Reprocessing reads all members from the original ZIP; it may reuse a cached
source ZIP. Do not backfill by treating the old one-file Markdown artifact as
evidence that other members did not exist. Rebuilding FTS5 alone cannot recover
previously ignored supporting documents.

Keep legacy plain UTF-8, gzip, and single-member Markdown ZIP decoding for
transition reads. Where legacy metadata identifies the source, an available
legacy body may be returned as a one-document collection with version `1`
and `complete=false`. CLI text reads warn on stderr; HTTP text reads carry
`X-Doc3gpp-Extraction-Complete: false`. MCP text-only reads of incomplete legacy
content return an explicit error directing the caller to JSON or re-parsing,
rather than adding warning text to the original body. If provenance cannot be
established, report that re-parsing is required instead of inventing a filename.
Legacy fallback never marks an extraction version `2` or supplies complete
multi-file index content. SQL-backed content always takes precedence over cache.

No whole-corpus destructive reset or new standalone repair command is required.
Document the normal filtered parse/force workflow for upgrading the corpus.

## 10. Verification requirements for the later implementation

Use TDD per implementation task: demonstrate each new regression failing,
implement the minimum behavior, run the relevant tests, then refactor. Use
SDD implementer/reviewer handoffs only after the reviewed specification and
implementation plan are approved. No feature code or new tests are written
as part of this specification-only change.

Required unit/integration coverage:

- Main plus multiple supporting DOCX members; main not first alphabetically.
- Duplicate basenames across directories and stable source IDs/order.
- Supporting text without a CR header; no supporting CR-sidecar writes.
- Zero/multiple main candidates, corrupt supported members, unsupported files,
  path/identity validation, and resource-limit skip behavior.
- Atomic SQL replacement/rollback, removed documents, sidecar cleanup, cascade
  deletion, and unchanged parent metadata cardinality.
- Multi-entry Markdown ZIP/manifest and legacy decoders; cache purge leaves
  SQL raw retrieval and FTS rebuilding functional.
- A term appearing only in a supporting document finds the parent and points
  to that exact source; metadata-only unparsed TDocs remain searchable.
- Multiple matching documents do not distort parent pagination, result count,
  ranking, or hybrid source references. Eight-weight configuration compatibility.
- Search-to-source raw round trips, exact URL selection, source not found,
  single/multi raw rendering, HTTP/MCP JSON parity, and no implicit URL-read sync.
- Database/direct/local/folder/job parsing and concurrent pipeline indexing.
- Version-1 pending discovery and explicit reprocessing into complete version-2
  content; index status/rebuild detect missing content despite unchanged dates.

Implementation documentation updates include `README.md`, `AGENTS.md`, relevant
architecture/CLI/web/conventions/code-map documentation, schema registry, and
both settings examples. New settings follow the repository's TOML-only policy.

### Recorded pre-existing baseline issue

Before this draft, a baseline run reported 2,666 passed, 1 skipped, and one
failure in
`tests/integration/test_auto_sync_sqlite.py::test_tdoc_list_auto_sync_triggers_tdoc_sync`.
The test leaves the calendar module's imported `ScraperClient` unmocked and
received a live HTTP 403. A one-off in-memory mock-binding correction made
that individual test pass without repository edits. The user chose to record
the issue and continue the feature without repairing unrelated tests.

Further test execution and implementation wait until the design/planning
review gates; this baseline is context, not evidence of feature correctness.

## 11. Review and handoff

Review focus: the proposed source-aware parent-level search contract, named
multi-document raw output, compatibility behavior, and archive safety defaults.
The SQL-backed content choice and one-main-document rule are already confirmed.

After user approval of this written spec, create an implementation plan with
the writing-plans skill and request plan review. Only then execute using SDD
and TDD. Do not auto-commit the spec, plan, or implementation; repository policy
requires a separate user request to commit.
