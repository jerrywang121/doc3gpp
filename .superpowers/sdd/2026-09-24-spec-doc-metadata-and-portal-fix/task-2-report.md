# Task 2 Report

## Status

DONE_WITH_CONCERNS

## Files Changed

- `src/doc3gpp/parsers/docx_converter.py`
  - Recognizes alphanumeric section identifiers such as `7.2A.3` and `7.2A.3A`.
  - Recognizes table captions with 3GPP identifiers and colon, dash, or whitespace separators.
  - Collapses caption whitespace while retaining caption paragraphs in the block stream.
- `src/doc3gpp/parsers/spec_doc.py`
  - Adds a natural section sort key that compares numeric tokens numerically and alphabetic tokens case-insensitively.
  - Preserves front-matter and unnumbered-file ordering rules.
- `tests/unit/test_spec_doc_blocks.py`
  - Adds real in-memory `python-docx` tests for alphanumeric headings and table captions.
- `tests/unit/test_spec_doc_parser.py`
  - Adds alphanumeric file-ordering and TOC preservation tests.

No chunker, ORM, search, web, or unrelated files were modified. `HeadingBlock.section_no`, `TableBlock.table_no/table_title`, and TOC split fields remain unchanged.

## TDD Verification

### Red

Command:

```bash
rtk pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v
```

Result: `10 passed, 3 failed`.

The three expected failures were:

- `test_heading_accepts_alphanumeric_section_suffixes`: the numeric-only section regex returned `None`.
- `test_table_caption_accepts_3gpp_identifier_and_separator`: the numeric-only caption regex returned no table number.
- `test_order_numbered_files_handles_alphanumeric_sections`: the numeric-only `_first_section` path could not order the alphanumeric section.

### Green

Command:

```bash
rtk pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v
```

Result: `13 passed`.

Additional checks:

- `rtk ruff check src/doc3gpp/parsers/docx_converter.py src/doc3gpp/parsers/spec_doc.py tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py`: no findings.
- `rtk git diff HEAD^ HEAD --check`: clean.

The broader command `rtk pytest tests/unit/test_spec_doc_*.py -v` reports `34 passed, 5 failed`. The five failures are existing Task 3 chunker failures caused by the Task 1 `ChunkDraft` contract change before the chunker has been updated; they are outside this task and were not changed.

## Commit

- Hash: `b00a2eb`
- Message: `fix(spec-doc): recognize alphanumeric sections and tables`

## Self-Review Findings

- No Task 2 defects found in the focused review.
- The heading regex requires an identifier to start with a digit, permits alphanumeric dotted components, and still requires following whitespace.
- Caption parsing supports the requested identifier and separator forms, normalizes whitespace, and leaves caption paragraphs as `ParagraphBlock` entries.
- Natural sorting uses token types that are safe to compare in Python tuple ordering and keeps numeric section ordering natural (`7.2` before `7.10`).
- Existing numeric-section, front-matter, unnumbered-file, and TOC tests remain green.
- The commit contains only the four files specified by the Task 2 brief.

## Concerns

- The full spec-document unit-test glob is not green until the planned Task 3 chunker implementation updates its constructors and metadata handling for the Task 1 DTO contract. This was intentionally left untouched to preserve Task 2 scope.
- The approved plan file `docs/superpowers/plans/2026-09-24-spec-doc-metadata-and-portal-fix.md` remains pre-existing and untracked; it was not added to the Task 2 commit.

## Fix Round: Alphanumeric Hyphenated Table Suffixes

### Finding Addressed

The table-caption identifier grammar previously allowed only numeric suffixes after
the hyphen. It split identifiers such as `4.4.2-1A` and `4.6.1-4.0A` before the
suffix. The grammar now permits alphanumeric and dotted components after the
hyphen, while retaining the existing base identifier rules.

The real in-memory DOCX regression coverage now parameterizes colon, dash, and
whitespace separators for table captions, covers both reviewed identifiers, and
asserts that the caption remains a `ParagraphBlock` in the block stream.

### TDD Verification

Red command:

```bash
rtk pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v
```

Result: `13 passed, 2 failed`. The two new hyphenated-suffix cases failed with
the old numeric-only suffix grammar; for example, `4.4.2-1A` was parsed as
`4.4.2`.

Green command:

```bash
rtk pytest tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py -v
```

Result: `15 passed`.

Targeted lint:

```bash
rtk ruff check src/doc3gpp/parsers/docx_converter.py src/doc3gpp/parsers/spec_doc.py tests/unit/test_spec_doc_blocks.py tests/unit/test_spec_doc_parser.py
```

Result: no findings.

### Fix Commit

- Hash: `1836c50`
- Message: `fix(spec-doc): accept alphanumeric table suffixes`

### Remaining Concerns

- The broader `rtk pytest tests/unit/test_spec_doc_*.py -v` scope still has the
  previously recorded five Task 3 chunker failures caused by the Task 1 DTO
  contract; no unrelated chunker changes were made in this fix round.
- The pre-existing untracked plan file remains untracked and was not included in
  either fix commit.
