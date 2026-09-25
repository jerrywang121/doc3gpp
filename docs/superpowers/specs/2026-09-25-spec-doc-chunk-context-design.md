# Spec-Document Chunk Context Design

## Goal

Correct spec-document chunk context so that each chunk contains only the
section and table metadata represented by its own content, while preserving
the rendered section headings and table captions in the chunk text.

The existing public metadata contract remains unchanged: chunks expose the
newline-delimited `sections` and `tables` fields. This change corrects how
those fields and chunk text are produced; it does not change the database
schema or search API names.

## Current Problems

The chunker currently treats headings as metadata-only state and accumulates
every heading/table label encountered while building a chunk. Consequently,
metadata from earlier sections can remain attached to later chunks even when
the later chunk starts in a different section. The converter also returns
heading and table-caption information in block structures that the chunker
does not emit into `ChunkDraft.text`.

For example, a chunk that starts with content under `3.3 Abbreviations` and
then crosses into `4.1 Environmental conditions`, `4.1.1 Temperature`, and
`4.1.2 Voltage` must expose only those represented contexts, not `Foreword`,
`Introduction`, `1 Scope`, `2 References`, `3.1 Definitions`, or `3.2
Symbols` merely because they occurred earlier in the document.

The same rule applies to table metadata: a chunk includes only table labels
for table content present in that chunk, plus labels for table content copied
into it as overlap.

## Design

### Source Units

`chunk_blocks` remains the single place that turns structured blocks into
chunking units. The existing `HeadingBlock`, `ParagraphBlock`, and
`TableBlock` types remain unchanged.

Each emitted unit carries:

- rendered text;
- the section metadata represented by that unit, if any;
- the table metadata represented by that unit, if any;
- file order and source filename;
- whether the unit is atomic for table-row handling.

Heading units use `HeadingBlock.raw`, preserving the existing Markdown
rendering such as `### 3.3 Abbreviations`. Paragraph units preserve their
original text. Table units preserve their complete GFM content. The adjacent
caption paragraph remains a separate rendered unit in the existing block
stream, so it remains visible in chunk text rather than being discarded; its
table label is associated with the caption/table content for metadata
purposes.

When a table is split row-wise, every resulting table unit retains the table
label. The Markdown header is emitted once at the start of each resulting
chunk, not once before every row.

### Metadata Scope

Metadata is derived from the source units that actually contribute text to a
chunk. It is not a document-level running history.

At a normal chunk boundary:

- flush the current text and its ordered, deduplicated metadata;
- start the next chunk with empty metadata;
- add only metadata from units assigned to the new chunk.

This means a heading contributes its own label only when the rendered heading
itself is in a chunk, while content following a heading carries that section's
context through its own source-unit metadata. An earlier section does not
remain in later chunks after the chunk boundary unless its actual text is
copied there as overlap.

### Overlap

When `chunk_overlap` is enabled, the chunker prepends the exact trailing
non-table tokens copied from the preceding chunk. If the requested overlap
would copy any table header or row tokens, no overlap is added at that
boundary. This preserves row integrity and prevents a table row from being
duplicated in the next chunk. Metadata is prepended only for copied tokens;
no metadata is copied merely because it was present somewhere in the
preceding chunk.

The overlap metadata is calculated from per-token source-unit metadata. This
continues to support paragraph overlap while preventing unrelated earlier
sections or tables from leaking into the following chunk.

Metadata values remain ordered by first appearance and deduplicated within a
chunk. Empty metadata remains `None`.

### Rendered Content

The resulting `ChunkDraft.text` retains:

- heading Markdown from `HeadingBlock.raw`;
- paragraph text;
- table-caption paragraph text;
- GFM table content.

Existing paragraph/table separators and table atomicity rules are preserved.
No heading or table-caption text is removed solely to produce metadata.

`_render_blocks_markdown` and the block converter retain their current public
behavior. This change is limited to the chunking representation and its
tests unless implementation details require a narrowly scoped helper.

## Tests

Add or update focused unit tests covering:

1. A sequence of many headings produces only the section metadata represented
   by each chunk, with no historical metadata leak.
2. A chunk beginning with content under a section includes that immediate
   section context and any later contexts represented in the same chunk.
3. Heading Markdown remains in chunk text.
4. Table caption text and GFM table content remain in chunk text.
5. Table metadata follows the same normal-boundary and overlap rules as
   section metadata.
6. Existing paragraph splitting, table row splitting, deduplication, empty
   metadata, file tags, and character limits remain valid.

The local repaired corpus for `36.508@19.2.0` must be regenerated through
the normal spec-document parse service after implementation. The repair must
continue to reset only the specdata database and retain the existing external
backup.

## Verification

Run the focused spec-document parser/chunker tests, then the full SQLite test
suite, Ruff, and `git diff --check`. Inspect representative repaired rows to
confirm that headings/captions are present in `text` and that `sections` and
`tables` no longer contain unrelated historical context.
