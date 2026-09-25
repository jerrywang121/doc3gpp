# Final Fix Report

## Status

Implemented and committed the final whole-branch review fixes in
`107cabc` (`fix(spec): close parsed output review gaps`).

The pre-existing untracked implementation plan
`docs/superpowers/plans/2026-09-25-spec-parsed-output.md` was not modified or
staged.

## Findings Addressed

### Consistent Spec Version Field Order

`spec show` CLI JSON now emits version keys in the same order already used by
the REST and MCP surfaces:

```text
version, parsed, release, ftp_url, meeting_id, meeting_name,
upload_date, pdf_url, crs
```

`--no-wis-crs` still removes only the header `wis` field and version `crs`
field; `parsed` remains immediately after `version`.

Coverage now includes:

- an exact CLI serialized key-order assertion;
- an integration assertion that CLI, REST, and MCP serialized version key
  orders are identical;
- updated table and Markdown assertions that account for the new position of
  `parsed`.

### Missing Specdata Parsed Ledger

`SQLAlchemySpecDocRepository.list_parsed_versions()` now treats the specific
missing `spec_doc_sources` table error as an empty parsed-status source while
re-raising unrelated SQL operational errors. Normal initialized specdata
behavior and the `spec_doc_sources.parsed_at IS NOT NULL` source-of-truth rule
are unchanged.

With a populated main database and no specdata schema:

- spec list returns normally with `parsed: null`;
- spec show returns normally with `parsed: false` on every version;
- `parsed=true` returns an empty list;
- the same behavior is covered through CLI, REST, and MCP.

### Documentation

Updated `docs/architecture.md` to describe the separate parsed-status reader,
transient enrichment, missing-ledger fallback, and filtering before
pagination. Updated `docs/code-map.md` to describe the factory wiring and
`SpecService` status-reader behavior.

## Changed Files

- `src/doc3gpp/cli.py`
  - normalized CLI `spec show` version field order;
- `src/doc3gpp/storage/repositories/spec_doc_sql.py`
  - handled an absent `spec_doc_sources` table as an empty status source;
- `tests/integration/test_spec_cli.py`
  - added field-order parity coverage and updated output-position assertions;
- `tests/integration/test_spec_doc_repo.py`
  - added direct missing-table repository coverage;
- `tests/integration/test_spec_parsed_output.py`
  - added main-only CLI/REST/MCP regression coverage;
- `docs/architecture.md`
  - documented status enrichment and pre-pagination filtering;
- `docs/code-map.md`
  - documented factory/service status-reader composition.

## TDD Evidence

The initial focused regression run was intentionally red:

```text
rtk pytest tests/integration/test_spec_cli.py::test_spec_show_json_uses_shared_version_field_order tests/integration/test_spec_doc_repo.py::test_list_parsed_versions_missing_table_is_empty tests/integration/test_spec_parsed_output.py -q
0 passed, 3 failed
```

The failures were the expected CLI order mismatch, missing-table
`OperationalError`, and main-only spec list crash. After the minimal fixes,
the initial focused regressions passed, and the final parity/missing-ledger
coverage passed across all three surfaces.

## Verification

Focused parsed-output, service, web, MCP, and repository tests:

```text
rtk pytest tests/integration/test_spec_cli.py tests/integration/test_spec_doc_repo.py tests/integration/test_spec_parsed_output.py tests/unit/test_spec_service.py tests/unit/test_services_factory.py tests/unit/test_web_routes.py tests/unit/web/test_mcp_server.py tests/integration/test_mcp_end_to_end.py -q
324 passed
```

Full offline SQLite suite:

```text
rtk ./scripts/test_sqlite.sh
2446 passed, 1 skipped, 109 warnings
```

The skipped test is the existing optional-dependency skip. The warnings are
the repository's existing pytest collection and datetime deprecation warnings.

Lint and diff checks:

```text
rtk ruff check .
clean, exit 0

rtk git diff --check
clean, exit 0
```

## Deferred Minor

The inherited `docs/cli.md` database scope text still lists
`main|testcase|all` for the `db` commands, while the current implementation
also supports `specdata`. This was pre-existing and unrelated to the parsed
output review findings. It was intentionally left unchanged rather than
expanding this fix wave into an unrelated CLI documentation rewrite.

## Concerns

- The separate specdata ledger remains intentionally transient enrichment; no
  main-table column, migration, or schema-registry field was added.
- Existing full-suite warnings remain as documented above.
