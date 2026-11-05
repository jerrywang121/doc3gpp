# doc3gpp agent guide

A Python 3.10+ SDK/CLI for 3GPP metadata and extracted documents, with optional
web/HTTP/MCP adapters. This file is a contributor entry point, not a duplicate
command/settings/process reference. Follow the current implementation and
preserve unrelated working-tree changes.

## Navigation

| Need | Primary reference / implementation |
| --- | --- |
| Start using the project | [README.md](README.md), [docs index](docs/README.md) |
| Commands/flags/output | [docs/cli.md](docs/cli.md), `src/doc3gpp/cli.py`, `cli_server.py` |
| Settings/defaults/discovery/secrets | [docs/config.md](docs/config.md), `settings/schema.py`, `config_source.py`, `config_writer.py`, packaged TOML template |
| Sync/auto-sync/parse/cache/chunk/index/query/pools/jobs | [docs/processes.md](docs/processes.md) |
| Layer boundaries/storage ownership | [docs/architecture.md](docs/architecture.md) |
| Symbols/files | [docs/code-map.md](docs/code-map.md) |
| Upstream/domain rules | [docs/3gpp-knowledge.md](docs/3gpp-knowledge.md) |
| Testing/filter grammar/commit/doc rules | [docs/conventions.md](docs/conventions.md) |
| HTTP/UI/operation | [docs/web-server.md](docs/web-server.md), `web/routes/`, templates/renderers |
| MCP/tool contracts/transports | [docs/mcp.md](docs/mcp.md), `web/mcp_server.py` |
| Limitations/backlog | [docs/known-constraints.md](docs/known-constraints.md), [docs/TODO.md](docs/TODO.md) |

Source paths in the table after `src/doc3gpp/` are package-relative.
Historical `docs/superpowers/{specs,plans}/` are rationale, not current API docs.

## Structure and rules

- `models/`: slotted domain dataclasses; never leak ORM entities.
- `repository/`: Protocols. Change contracts and SQL implementations together.
- `services/`: orchestration over injected interfaces; factories compose concrete
  repositories. No direct service-to-ORM queries.
- `scraping/`: transport only, no domain HTML parsing.
- `parsers/`: pure parsing/conversion/chunking, no network.
- `storage/db/`, `storage/backends/`, `storage/repositories/`: persistence.
- `settings/`: validated, process-cached configuration.
- `cli.py`, `cli_server.py`, `web/`: thin adapters/composition; share payloads.

Three logical databases: main metadata/TDoc content/jobs/indexes, testcase,
and specdata. Use their own engines/bases and `db init --scope` selectors.
Additive schema initialization is not a general migration system or backfill.
Keep `models/schema_info.py` synchronized with ORM columns; transient output
fields are not necessarily schema columns.

Important change entry points:

- TDoc family routing/completeness: `models/tdoc_content_families.py`, the single
  registry used by discovery, extraction, content, search, and schema reporting.
- Batch extraction: `services/tdoc_pipeline.py` + `pipeline_runtime.py`;
  one prepared-bundle writer, bounded preparation, discovery-ordered results.
- CR/family content: `services/tdoc_cr_service.py`, `tdoc_family_service.py`,
  `storage/repositories/tdoc_content_sql.py`. Preserve per-URL atomic bundles.
- Conversion: Word/presentation converters and shared `DocxConvertPool`;
  historical `docx` names include `.docx`, `.doc`, `.pptx`, `.ppt`.
- Inventories: outer-ZIP entries only; all extensions/order/duplicates;
  preserve unknown `null` versus known-empty `[]` independently of content.
- Spec docs: `SpecDocService`, specdata repositories, shared chunker; full
  presentation reads use exact source paths and SQL, not cache/network.
- Search: unified facades/index services and FTS/vector repositories; preserve
  matched-source/chunk provenance, mode-specific scores, JSON parity.
- Jobs: `models/jobs.py`, `web/workers/handlers.py`, `job_worker.py`, job routes,
  `web/state.py`; cancellation is cooperative and queue ownership single-process.
- MCP: `web/mcp_server.py`; `get_skill` returns packaged raw Markdown, other
  matching JSON reads share HTTP payloads. Register tools, not duplicate queries.

See the linked process/constraint guides before changing skip rules, force,
cache recovery, semantic freshness, or resource ownership. In particular,
do not confuse SQL completeness with derived-cache availability or make a
snapshot content read silently download/parse.

## Development and validation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,extract,semantic]"
ruff check .
./scripts/test_sqlite.sh
# Explicit live-network tests only:
python -m pytest -m online -rs
```

Build: hatchling. Core: SQLAlchemy 2, Pydantic 2/settings, httpx, BeautifulSoup,
lxml, openpyxl. Optional CLI/extraction/semantic/web extras are described in
[config.md](docs/config.md#optional-dependencies). `[dev]` alone does not include
every extraction/vector dependency. Tests import `src` and exclude online by
default; the script uses xdist when installed (`-n 0` forces sequential).

Use shared test fixtures to clear cached settings and all relevant engines.
Mock auto-sync transports to avoid unintended network calls. Do not claim
passing verification without a fresh run, and do not auto-commit.

Update affected maintained docs and this guide in the same interface/behavior
change. Keep README short, put details in their primary guide, and verify
relative links, example TOML, command help, and schema/render parity.
