# Spec Document Metadata and Portal Fix

## Context

The spec-document portal currently displays the table of contents and all
loaded chunks as expanded cards. Chunk metadata is split across four columns
(`section_no`, `section_title`, `table_no`, and `table_title`), so a chunk can
represent only one section and one table. The section recognizer also rejects
valid 3GPP identifiers such as `7.2A.3` and `7.2A.3A`, and the current table
caption recognizer misses table metadata in the cached 36.508 document.

The application is not deployed. This change therefore replaces the local
spec-document schema and performs one repair of the development database; it
does not add a runtime database migration or compatibility columns.

## Goals

- Make the version detail page compact by collapsing the TOC and each chunk by
  default.
- Make chunk summaries useful without expanding them: show the chunk ID,
  source file, and all extracted section/table metadata.
- Recognize alphanumeric section identifiers, including `7.2A.3` and
  `7.2A.3A`, in both TOC and chunk extraction.
- Store chunk metadata in exactly two text fields, `sections` and `tables`.
  Each field contains one combined identifier/title per line and can contain
  multiple entries.
- Preserve metadata for section/table context carried into a chunk by
  overlap, including the section that begins before the chunk's first new
  tokens.
- Allow `sections` and `tables` filters in FTS5 and semantic/hybrid search.
- Rename the public spec-document section filter to `sections` consistently in
  the CLI, web routes, MCP tools, services, and models.
- Make the search Query input occupy a full row.
- Correct the spec-document cache root to
  `~/.cache/doc3gpp/specs`, without changing the existing TDoc cache root.
- Re-extract every currently parsed spec/version pair for which a cached ZIP
  is available, including the current `36.508@19.2.0` development corpus.

## Non-goals

- No deployed-database migration framework or compatibility read path.
- No public `reparse` command and no automatic reparsing during `db init` or
  schema creation.
- No change to the TOC output contract: TOC rows continue to expose
  `section_no` and `title` separately.
- No changes to unrelated TDoc cache layout or TDoc search behavior.

## Portal UI

The TOC section remains a card, but its contents are wrapped in native
`<details>` markup with no `open` attribute. Its summary is the existing
"Table of contents" heading; the table and source-file summary remain inside
the card body.

Each chunk remains visually styled as a card and retains its `chunk-N` anchor.
The card becomes a native `<details>` element, collapsed by default. Its
summary contains:

- chunk ID and source file;
- `sections`, rendered with preserved line breaks when there are multiple
  entries; and
- `tables`, rendered the same way when present.

The expanded body contains the existing preformatted chunk text. Empty-result,
filter-empty, and pagination states remain unchanged. Native details elements
are used instead of JavaScript so HTMX pagination does not need state
management.

On the Spec Docs search page, the Query control spans the full grid row in
both normal and semantic modes. The semantic-only FTS5 query is placed on its
own control row. The filter form exposes `Sections` and `Tables` using those
exact parameter names.

Search result metadata displays the combined `sections` and `tables` values,
with line breaks preserved. Vector-only semantic results continue to show a
placeholder when no FTS hit supplies chunk metadata.

## Extraction and chunking

### Sections

The section recognizer accepts a section number beginning with digits followed
by dotted alphanumeric segments. This covers numeric sections and identifiers
such as `7.2A.3` and `7.2A.3A`. The parsed section number remains separate from
the heading title in the TOC model, while chunk metadata combines them as
`<section number> <title>`.

Section ordering uses a natural key that compares numeric portions numerically
and suffix letters lexically, avoiding integer conversion of an alphanumeric
segment.

### Tables

Table-caption parsing accepts the caption forms present in the source DOCX,
including alphanumeric/dotted table identifiers and the separators used by
3GPP captions. The parser normalizes the identifier and title into one
metadata value, `<table number> <title>`, while retaining the full table body
as the table block.

### Metadata carried by chunks

The chunker tracks ordered, deduplicated section and table metadata rather than
one scalar value. A chunk receives every section/table context represented by
its content in source order. When a chunk is built with overlap, metadata from
the overlapped source units is included as well. Consequently, if the first
part of a chunk is carried from a preceding section, that preceding section is
included in `sections` even when the chunk's first newly emitted unit belongs
to a later section.

Each metadata field is serialized as newline-delimited text, one entry per
line. Empty fields are stored as `NULL`; no deprecated split columns remain.

## Storage and search contract

`SpecDocChunkORM`, `ChunkDraft`, `SpecDocChunk`, and `SpecDocHit` replace the
old split fields with:

```text
sections: str | None
tables: str | None
```

The `spec_doc_chunks` table uses `sections` and `tables` text columns. Fresh
schema creation and the one-time local repair create only the new columns.

The FTS5 document contains the combined `sections` and `tables` fields. FTS
search returns those values directly and applies rich text filtering to both
fields. Semantic/hybrid search passes both filters through the FTS fan-out and
the vector-side chunk join. Embedding text includes the combined metadata so
metadata remains available to vector ranking as context.

The public filter object uses `sections` and `tables`; the old singular
`section` name is removed rather than aliased. The same names are used by:

- `doc3gpp spec doc search query` and `spec doc search sem`;
- web query parameters and form controls; and
- MCP `search_spec_docs` and `semantic_search_spec_docs` arguments.

CLI, JSON, HTML, and MCP serializers emit `sections` and `tables`. Default
spec-document output fields and the static schema registry are updated to the
new names.

## Cache and one-time repair

Spec-document artifacts use a dedicated root, `~/.cache/doc3gpp/specs` by
default. The service writes ZIPs below `specs/zips/<spec>/<version>.zip` and
markdown below `specs/markdown/<spec>/<version>/`. The existing TDoc cache
continues to use `~/.cache/doc3gpp/tdocs`.

Because this is a pre-deployment schema replacement, the repair is a
development-only operation and is not part of application startup:

1. Record all parsed `(spec_id, version)` pairs from the current specdata
   database.
2. Copy each available ZIP from the old development location
   `~/.cache/doc3gpp/tdocs/specs/...` into the new spec-document cache root.
   The old files are not deleted.
3. Recreate the local specdata database with the new relational, FTS5, and
   vector schema. The main and testcase databases are untouched.
4. Re-run the existing parse pipeline for every recorded pair whose ZIP is
   available. Each parse replaces its TOC/chunks and rebuilds its FTS/vector
   rows. A missing ZIP or parse failure is recorded in the repair output and
   does not prevent later pairs from being processed.
5. Verify that the repaired `36.508@19.2.0` corpus contains combined section
   and table metadata and that its FTS rows match the chunk rows.

The repair is performed once with a temporary development script/commands and
is not shipped as a migration, public command, or recurring startup hook.

## Testing

Tests are added before implementation for:

- alphanumeric section recognition and natural ordering;
- the actual table-caption shapes from the 36.508 DOCX fixture;
- multiple section/table metadata entries per chunk;
- overlap metadata carrying the preceding section into the next chunk;
- replacement ORM/repository fields and `sections`/`tables` filters;
- FTS and semantic/hybrid table filtering;
- CLI, web, JSON, and MCP output/filter names;
- collapsed TOC/chunk markup and full-width Query layout; and
- dedicated spec-document cache paths.

The final verification runs the full offline SQLite suite, Ruff, whitespace
checks, and direct database assertions for the repaired development corpus.
