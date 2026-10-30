# Presentation files in TDoc and spec-document ZIPs

## Intent and scope

Convert `.pptx` and legacy `.ppt` members of TDoc and spec-document ZIP packages
to Markdown. Store that Markdown compressed in SQL, make its text searchable,
and make the complete file available from the relevant detail view. Presentation
files are content documents, not a new source of CR, spec-section, table-of-
contents, or other domain metadata.

The requested presentation content is slide titles, text boxes, grouped text,
tables, and speaker notes. Text in images, charts, embedded objects, and other
unsupported graphical content is not OCRed or otherwise inferred. The scope is
presentations *inside ZIP packages*; it does not add a standalone-presentation
direct-parse interface or make otherwise ineligible TDoc types eligible.

## Conversion

Add one shared, parser-layer presentation converter used by both pipelines.
Read `.pptx` in memory with `python-pptx`, added to the existing optional
`[extract]` dependency set. Import it lazily so other SDK and CLI functionality
continues to work without the extraction extra. For `.ppt`, first convert to
`.pptx` with optional headless LibreOffice Impress, then use the same converter.
The legacy conversion uses a private temporary directory and LibreOffice profile,
a fixed input name instead of the archive path, the existing legacy-Office
conversion timeout, and process cleanup on errors and timeouts. Neither format
requires network access.

Produce deterministic Markdown in slide order, with a slide-number heading,
then text and tables in presentation shape order, recursing into grouped
shapes. Render tables as Markdown tables, escaping cell delimiters and retaining
cell line breaks. Put existing speaker-note text in a clearly labeled notes
block for its slide; do not create a notes slide merely to read it. Preserve
Unicode and the ZIP member's source identity. The conversion is textual, not a
promise of pixel-accurate layout or formatting fidelity. A successfully converted
empty presentation remains a known file even if it has no searchable text.

## TDoc ingestion and storage

Extend the existing ZIP-member enumeration, conversion dispatch, and process
pool to accept both presentation formats along with `.docx` and `.doc`.
Retain the existing TDoc ZIP path validation, deterministic member order,
duplicate rejection, document-count limit, and aggregate uncompressed-byte
limit; presentation members count toward both limits. Mixed Word/presentation
archives and presentation-only archives must work for eligible non-CR TDocs.

For WID, LS, and discussion TDocs, a presentation can be either the main member
or a supporting member. Extend the existing name-based main selection to all
four supported extensions, retaining its ambiguity errors rather than silently
choosing between multiple exact main-file candidates. CR TDocs still select a
structural Word CR cover as their main; every presentation is supporting content
and must never become a CR-cover candidate, even if its text resembles one.
Apply the same ZIP behavior to direct TDoc ZIP parsing where the TDoc family is
known, without broadening the standalone-file interface.

Store presentation Markdown in the existing compressed TDoc content tables:
CR annexes in `tdoc_cr_annexes`, and non-CR mains and annexes in their existing
family tables. Preserve the normalized ZIP source path, stable document identity,
hash, and content ordering. The existing FTS5 and optional semantic indexing
consume the stored presentation text like other content documents. Existing
source-specific TDoc content reads, including CR annex reads, remain SQL-first
and work after the derived Markdown cache is evicted.
For `tdoc show --tdoc ... --format raw --source-file ...` targeting an already
stored presentation in a complete extraction, read that SQL content before
attempting extraction, so a missing CR-main cache does not cause a download.
Keep the existing extraction-first behavior for other raw reads and for missing
or incomplete presentation content.

## Spec-document ingestion and storage

Extend spec ZIP enumeration and conversion to support mixed Word/presentation
and presentation-only packages. Apply the existing spec ZIP size limit and
preserve the existing source-file and deterministic file-order conventions.
The presentation's slide headings and tables are content only: they must not
create spec TOC entries or spec section/table metadata. Chunk the presentation
text using the existing chunk limits and preserve source identity, with
`sections` and `tables` unset for presentation chunks. Existing FTS5 and optional
semantic indexing then make slide and speaker-note text searchable.

Add a specdata SQL table for full presentation Markdown, keyed by `(spec_id,
version, source_file)` and carrying its file order and gzip-JSON-compressed
content. It is the authoritative full-file detail source, independent of the
ZIP and Markdown caches; overlapping search chunks cannot reliably reconstruct
the original Markdown. Reject duplicate presentation source paths in one spec
ZIP with a clear error rather than returning an ambiguous full-file detail.
The raw ZIP inventory continues to preserve its existing duplicate semantics.

On a successful re-parse, replace the version's presentation records and chunk
rows together in a transaction, including deleting old presentation records
when the new ZIP contains none. Conversion must finish before this replacement.
Keep the existing parsed-state and indexing lifecycle. The legacy `docx_count`
field counts all discovered supported members, including
presentations, and its historical name remains unchanged for compatibility.
Register the new table in the static schema registry and bootstrap it through
the additive, idempotent specdata schema initialization. Existing parsed ZIPs
are not silently backfilled; users run `doc3gpp db init --scope specdata` and
force a re-parse to populate presentation content.

## Detail access

TDoc presentation files use the existing content surfaces: the web content
view, the `get_tdoc_content` MCP tool, and `doc3gpp tdoc show --format raw
--source-file <path>`. Source selection is exact and ZIP-scoped.

For spec presentations, add a file listing/link on the existing version detail
page and a full-file Markdown view backed by the new SQL records. Expose the
same stored content through `doc3gpp spec doc show --spec <id> --version <v>
--source-file <path>`, an HTTP content route, and a `get_spec_doc_content` MCP
tool. Default the CLI to raw Markdown, with an explicit JSON format also
available. Provide a consistent JSON envelope containing the spec
ID, version, exact source file, and Markdown; HTTP and MCP JSON must match the
CLI's compact JSON representation. Detail reads do not download, extract, or
depend on cache files. Missing versions or source files return clear errors.
Escape source names and render Markdown safely in HTML.

## Failures and compatibility

Use the existing TDoc behavior: a failed presentation main fails the TDoc;
a failed presentation annex is reported in `skipped_sources`, leaves the
extraction incomplete, and is retried on a later normal parse. A CR presentation
is always an annex for this purpose. Spec-document conversion fails the version
parse if any supported member fails; it must not replace the previously stored
chunks or full-file records with partial results. Missing `python-pptx`, missing
LibreOffice for `.ppt`, invalid files, and conversion timeouts surface actionable
errors under these same main/annex rules.

Do not change existing Word conversion, CR metadata, or TDoc eligibility. Already
complete TDoc and spec-document extractions keep their existing skip rules;
`--force` is required to include presentation members previously ignored.
Existing `docx`-named internal pool functions and fields may retain their names
when preserving those interfaces, but documentation must describe all supported
formats and the historical counter semantics.

## Verification

Use test-driven development. Add focused conversion tests for slide ordering,
Unicode, text frames, groups, Markdown tables, speaker notes, empty decks,
malformed files, lazy dependencies, legacy conversion, and timeouts. Exercise
both serial and process-pool conversion and enforce ZIP limits and duplicate
handling. Integration tests cover mixed and presentation-only ZIPs, non-CR main
selection, CR annex routing, retryable failures, compressed SQL bytes, atomic
re-parse replacement, FTS5 and enabled semantic indexing, and exact file-detail
reads after cache eviction. Verify CLI, HTTP, MCP, and web output, including
JSON parity and HTML escaping. Run relevant existing Word/CR/spec regression
tests, the offline suite, and lint. Update `README.md`, `AGENTS.md`, and the
relevant CLI, architecture, and web documentation in the same change set.
