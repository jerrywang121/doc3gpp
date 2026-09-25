# Task 7 Report: Synchronize User Documentation

## Status

Implemented the approved Task 7 documentation updates only. No production
code, tests, schema registry, ORM, migration, or static schema documentation
was changed.

## Changed Files

- `docs/cli.md`
  - Documented parsed status as output-only data derived from non-null
    `spec_doc_sources.parsed_at` in the separate specdata database.
  - Documented that `parsed` is not a main `specs` / `spec_versions` column
    and is absent from `spec schema` output.
  - Added `spec list --parsed true|false`, including strict,
    case-insensitive values and filtering before pagination.
  - Added `parsed` after `rapporteurs` in the default list fields.
  - Documented comma-separated numeric-newest-first list JSON values,
    native JSON `null` for no parsed versions, and `-` in table/Markdown.
  - Documented native boolean `parsed` values on every `spec show` version
    in structured JSON.
  - Added concrete `--parsed true` and `--parsed false` CLI examples.
- `docs/web-server.md`
  - Added the `GET /specs` route and its `parsed=true|false` filter.
  - Documented the web `Any` / `true` / `false` Parsed control, strict
    validation, and filtering before pagination.
  - Documented the separate specdata source of truth and the absence of a
    main-table/schema field.
  - Documented native list `null` / version-string semantics and native
    per-version booleans.
  - Documented the MCP `list_specs(parsed=...)` argument and its parity.
- `README.md`
  - Added parsed-filter CLI examples.
  - Documented the default `rapporteurs, parsed` fields, specdata-derived
    status, non-schema nature of the field, list JSON null/version semantics,
    pre-pagination filtering, and show JSON booleans.
- `AGENTS.md`
  - Updated the spec source/workflow entry with the specdata parsed-status
    ledger and transient, non-schema output distinction.
  - Updated the spec workflow description with default `parsed` output,
    native null/boolean semantics, and pre-pagination filtering.
- `docs/code-map.md`
  - Updated the review date.
  - Added `SpecParsedStatusRepository` and clarified the separate specdata
    ledger boundary in the repository entries.
  - Updated `SpecService` to describe transient enrichment, filtering before
    pagination, and non-persistence of parsed output.

## Documentation Assertion Test

No new documentation assertion test was added. The existing
`tests/unit/test_settings_config_file.py` is the natural source-of-truth
check for the configured default spec field list, and Task 3 already extended
that check to require `parsed`.

## Verification Commands And Outputs

Focused existing configuration test:

```text
rtk pytest tests/unit/test_settings_config_file.py -q
Pytest: 37 passed
```

Documentation whitespace check before commit:

```text
rtk git diff --check
clean (no output)
```

Required documentation assertion search:

```text
rtk grep 'parsed' docs/cli.md docs/web-server.md README.md AGENTS.md docs/code-map.md src/doc3gpp/data/doc3gpp.toml.example
108 matches in 6 files
```

The RTK output summarized the long match list, but included the required
CLI, REST/web, MCP, README, agent-guide, code-map, and TOML references.

Post-commit diff check:

```text
rtk git diff --check HEAD^ HEAD
clean (no output)
```

Ruff was not run because no code files were changed.

Full SQLite suite required by the completion workflow:

```text
rtk ./scripts/test_sqlite.sh
1 failed, 2441 passed, 1 skipped, 109 warnings in 117.72s
```

The failure is the existing `tests/unit/test_settings.py::test_output_fields_default_spec`
assertion, which still expects the pre-Task-3 default list without `parsed`.
The focused `tests/unit/test_settings_config_file.py` check passed, and this
task does not modify tests or production settings by instruction.

## Commits

- `55682a3` (`docs: document parsed spec output`) — five requested user-facing
  documentation files.
- `a928467` (`docs: add Task 7 implementation report`) — this implementation
  report.

## Concerns

- The pre-existing untracked file
  `docs/superpowers/plans/2026-09-25-spec-parsed-output.md` remains in the
  worktree and was not modified or staged.
- The required full suite was run and has the pre-existing stale default
  assertion described above; the focused documentation/configuration test
  passed.
