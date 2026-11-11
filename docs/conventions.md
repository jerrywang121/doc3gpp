# Development conventions

These are contributor rules, not an operational manual. For runtime flows use
[processes](processes.md); settings are in [config](config.md), module locations
in the [code map](code-map.md), and boundaries in [architecture](architecture.md).

## Tests

- Features need focused unit tests with mocked external calls and relevant
  SQLite integration tests. Keep normal tests offline.
- `pyproject.toml` sets `pythonpath = ["src"]` and excludes `online` by default.
  Plain `python -m pytest` therefore imports the local source and runs offline.
- `./scripts/test_sqlite.sh` adds coverage and uses `-n auto` when xdist is
  available, sequential otherwise. Pass `-n 0` to force sequential execution.
- Run `uv sync --extra dev --extra extract --extra semantic` to cover optional
  extraction/vector paths.
  A pytest marker is not a dependency installer; inspect skip reasons.
- Run live tests explicitly with `python -m pytest -m online -rs`. Upstream
  availability/layout can change; do not make deterministic tests depend on it.
- Keep generated fixture bytes deterministic, including ZIP entry timestamps
  and gzip mtimes, so xdist workers collect identical parametrized tests.
- `.github/workflows/publish.yml` publishes releases. It is not an automated
  lint/test matrix; local validation remains necessary.

### Settings and engine isolation

Settings and engine getters are cached. Tests changing environment/configuration
must clear settings **and the relevant main/testcase/specdata engine caches**;
dispose engines before deleting files. Follow the shared fixtures in
[`tests/conftest.py`](../tests/conftest.py), not a new ad hoc global reset.
Use temporary config/database/cache directories and mock imported transport
bindings as well as their definitions. Otherwise an intended offline CLI test
can accidentally hit upstream during auto-sync.

## Lint and style

Ruff is the configured checker (`ruff check .`), with Python 3.10 and a
100-character line length. No mypy/pyright configuration is provided.
Keep the surrounding naming/style, use slotted domain dataclasses, and fix
types instead of introducing blanket ignore/escape hatches. Preserve intentional
ORM registration side-effect imports rather than deleting them as “unused”.

`repository/` contains abstractions; `storage/repositories/` contains SQL
implementations. `config.py` is a compatibility re-export: new code should use
`doc3gpp.settings`. Shared cache/export/compression helpers live at the
`storage/` root.

## Filter grammar

Token validation lives in `cli_filters.py`; parameterized SQL construction
lives in `storage/repositories/rich_filters.py`. The adapters share the grammar,
but **not every resource accepts every filter**; consult its command/tool schema.

| Text input | Meaning |
| --- | --- |
| `null` | SQL `IS NULL` |
| `not-null` | SQL `IS NOT NULL` |
| `!<pattern>` | Negated SQL `LIKE` pattern |
| Other text | Bound SQL `LIKE` pattern, with `%`/`_` wildcards |

Quote negation in shells, for example `--title '!%Sidelink%'`. SQL three-valued
logic still applies: negated LIKE does not automatically include nulls.
Exact-match fields, such as testcase group, are exceptions documented per command.

Date filters accept `null`, `not-null`, or a validated comparison such as
`">= '2026-01-01'"`, with operators `=`, `!=`, `<`, `<=`, `>`, `>=`.
Validate at the adapter boundary and bind values; never interpolate arbitrary
input into SQL. Compound meeting-name/title negation applies to the whole OR
group, not just one branch. TSG pattern normalization must preserve reserved
tokens and negation instead of uppercasing them indiscriminately.

The exact-ID meeting lookup validates TDoc shape before querying document
ranges. It requires a matching start prefix/number, an inclusive end when
present, and a non-null start. Do not treat arbitrary LIKE patterns as exact
IDs for range lookup or auto-sync candidate extraction.

## Architecture guardrails

- Update repository Protocols **and** SQL implementations together; preserve
  return types, filters, pagination, and batching contracts.
- Services orchestrate injected interfaces; adapters do not implement ORM
  domain queries. Composition belongs in factories/lifespan setup.
- Network I/O belongs in scraping; parsers/converters must be network-free.
- Keep `TDOC_CONTENT_STORES` the single TDoc family/type/completeness registry.
- Treat inventories, converted content, auxiliary files, parsed state, cache,
  and indexes as separate data products. Preserve `null` vs `[]` for inventory.
- A prepared aggregate's SQL transaction does not imply a cross-database,
  filesystem, or index transaction. Add failure-injection tests at boundaries.
- Shared pipeline writers serialize only work submitted to that runtime;
  neither the job cap nor SQLite WAL makes all writes globally serialized.
- Cancellation is cooperative. Drain submitted work before closing resources.
- Keep retry surfaces narrow: transient HTTP failures may retry; invalid URLs
  and programming errors should propagate.
- Maintain shared CLI/HTTP/MCP JSON contracts, field order where promised,
  Unicode handling, and safe HTML rendering. Test parity and hostile filenames.

## Documentation sync

Use [the documentation index](README.md) to choose one primary home:

| Topic | Primary home |
| --- | --- |
| Overview/install/minimal quick start | Root `README.md` |
| Commands/flags/output | `cli.md` |
| Discovery/precedence/settings/dependencies/secrets | `config.md` and packaged template |
| Runtime flows/skip rules/cache/pools/jobs | `processes.md` |
| Server/UI/HTTP operation | `web-server.md` |
| MCP clients/transports/tools | `mcp.md` |
| Layering/storage ownership | `architecture.md` |
| Domain/upstream semantics | `3gpp-knowledge.md` |
| Symbol-to-file navigation | `code-map.md` |
| Limitations / future work | `known-constraints.md` / `TODO.md` |

Update affected guides and `AGENTS.md` in the same behavior/interface change;
keep root README an overview, linking to details rather than repeating them.
Validate relative links/anchors, example configuration, and documented flags
against current implementation/help. Prefer symbol references over drifting
line numbers or hardcoded command/schema/test counts.

`superpowers/specs/` and `superpowers/plans/` are historical records. Do not
silently rewrite old designs as current reference; explain supersession in
the maintained guides. The packaged MCP skill is a task guide, not another
complete copy of configuration or HTTP reference material.

## Commits

Use Conventional Commits with an English present-tense subject, for example
`feat(tdoc):`, `fix(search):`, `docs:`, `test(cli):`. Branch names use
`area/topic`. Do not auto-commit or change branches without the user's request.
Run relevant verification and hand off the diff; a later commit requires a new
request. Shell scripts use `set -euo pipefail`.
