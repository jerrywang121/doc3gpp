# Spec-Document Fetch Command Removal Design

## Goal

Remove the redundant public `doc3gpp spec doc fetch` command. Spec-document
acquisition remains an internal step of parsing, so users continue to run
`doc3gpp spec doc parse` for the supported end-to-end workflow.

## Scope

The change applies to every current public surface and its contract:

- Remove the `fetch` subcommand from the Typer `spec doc` CLI app.
- Remove current documentation and examples that present `spec doc fetch` as
  a supported command.
- Verify that the web application exposes parsing, but no standalone
  spec-document fetch route or control.
- Verify that MCP exposes parsing, but no standalone spec-document fetch tool.
- Add or update tests that assert the old CLI command is unavailable and that
  parse still performs the internal fetch step.

Historical design and implementation-plan documents remain unchanged. They
describe the original implementation history rather than the current public
contract.

## Non-Goals

- Do not remove or rename `SpecDocService.fetch()`.
- Do not remove or rename `fetch_spec_doc_zip()`.
- Do not change ZIP cache layout, download recording, version resolution, or
  size-limit handling.
- Do not change the web/MCP parse job payload, progress behavior, or error
  mapping.
- Do not change spec-document TOC, FTS5, semantic-search, or schema surfaces.

## Behavior

`SpecDocService.parse()` remains the single public workflow for downloading
and processing a spec document. It continues to call the internal
`SpecDocService.fetch()` method before parsing the cached ZIP.

Normal parsing keeps its existing fetch-if-missing behavior:

1. Resolve the requested spec version.
2. Use the ZIP cache when present; otherwise download and record the source.
3. Convert DOCX files, build the TOC, chunk content, and write parse caches.
4. Record the parsed ledger row and perform best-effort search indexing.

`doc3gpp spec doc parse --force` retains its current explicit refresh
semantics. The CLI passes `force=True` through `parse_many()` and `parse()` to
the internal fetch step, which re-downloads the ZIP and then re-parses it.
Removing the public fetch command must not turn `--force` into a cache-only
operation.

The removed invocation must fail as an unknown Typer command, with no alias,
redirect, or compatibility shim:

```text
doc3gpp spec doc fetch
```

## Public Surface Audit

### CLI

Delete the `spec_doc_fetch` command registration and its implementation.
Update current command inventories, examples, and help-oriented documentation
so the `spec doc` command list begins with `parse`, followed by `toc`, search,
and schema commands.

### Web

No standalone spec-document fetch route exists in the current web surface.
Keep the existing parse job route and worker behavior unchanged. Verify that
route tests and user-facing templates contain no spec-document fetch action or
link.

### MCP

No standalone spec-document fetch tool exists in the current MCP surface.
Keep the existing parse tool and its job payload unchanged. Verify MCP tool
discovery contains no spec-document fetch tool or legacy fetch name.

### Internal service and transport

Keep `SpecDocService.fetch()` as an internal orchestration method because
`parse()` depends on it. Keep `fetch_spec_doc_zip()` as the network transport
function. Their unit/integration coverage remains valuable even though the
CLI no longer exposes download-only behavior.

## Documentation Updates

Update only current-contract documentation and examples, including:

- `README.md`
- `AGENTS.md`
- `docs/cli.md`
- `docs/architecture.md`

The update removes the standalone fetch command from inventories, workflows,
examples, and command descriptions. Internal references to the fetch method or
transport remain when they explain parse internals.

## Testing Strategy

Add or update coverage for the following:

- The CLI help no longer lists `fetch` under `spec doc`.
- Invoking `doc3gpp spec doc fetch` exits with the normal unknown-command
  error.
- Normal parse still fetches a missing ZIP through the service's internal
  fetch path.
- `parse --force` passes force through to fetching and parsing, causing a
  re-download followed by re-parsing.
- Existing web route and MCP discovery tests continue to demonstrate that
  parse is available and no standalone fetch surface is introduced.
- Existing service/transport tests for cache hits, downloads, source recording,
  and size limits remain unchanged unless their wording refers to the removed
  public command.

## Acceptance Criteria

- `doc3gpp spec doc fetch` is not a registered CLI command.
- `doc3gpp spec doc parse` still downloads missing documents automatically.
- `doc3gpp spec doc parse --force` still re-downloads and re-parses.
- No web route or MCP tool provides standalone spec-document fetching.
- Current documentation does not instruct users to invoke `spec doc fetch`.
- Internal fetch functionality and cache/download behavior are preserved.
- Focused tests and the full offline SQLite suite pass, and Ruff/diff checks
  are clean.
