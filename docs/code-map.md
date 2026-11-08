# Code map

Use this map to find implementation entry points. Behavioral descriptions live
in [processes](processes.md), boundaries in [architecture](architecture.md), and
command syntax in [cli.md](cli.md). Paths below are relative to the repository.

## Adapters and configuration

| Symbols / concern | File |
| --- | --- |
| Typer apps, commands, rendering selection, direct-mode entry points | `src/doc3gpp/cli.py` |
| CLI input filters / auto-sync | `src/doc3gpp/cli_filters.py`, `src/doc3gpp/cli_auto_sync.py` |
| Server start/stop/status/logs/install | `src/doc3gpp/cli_server.py`, `src/doc3gpp/web/install.py` |
| Top-level `doc3gpp mcp` command / stdio runtime | `src/doc3gpp/cli.py`, `src/doc3gpp/web/mcp_stdio.py` |
| `Settings`, nested settings, `ALLOWED_ENV_VARS` | `src/doc3gpp/settings/schema.py` |
| `find_config_file`, `load_config_data` | `src/doc3gpp/settings/config_source.py` |
| Cached `get_settings` | `src/doc3gpp/settings/loader.py` |
| Config bootstrap/read-modify-write | `src/doc3gpp/settings/config_writer.py` |
| Default template / agent skill | `src/doc3gpp/data/doc3gpp.toml.example`, `src/doc3gpp/data/skills/doc3gpp/SKILL.md` |
| `build_*` composition | `src/doc3gpp/services/factory.py` |

## Metadata sync and domain parsing

| Concern | Entry points |
| --- | --- |
| `MeetingService`, `parse_3gpp_calendar` | `src/doc3gpp/services/meetings_service.py`, `src/doc3gpp/parsers/calendar_parser.py` |
| `TDocService`, `TDocSyncCoordinator`, `TDocFileService` | `src/doc3gpp/services/tdoc_service.py`, `src/doc3gpp/services/tdoc_sync_coordinator.py`, `src/doc3gpp/services/tdoc_file_service.py` |
| TDoc portal/FTP list parsing | `src/doc3gpp/parsers/tdoc_parser.py`, `src/doc3gpp/parsers/tdoc_file_parser.py` |
| `SpecService`, detail/bootstrap parsing | `src/doc3gpp/services/spec_service.py`, `src/doc3gpp/scraping/spec_source.py`, `src/doc3gpp/parsers/spec_parser.py` |
| WI service/parser | `src/doc3gpp/services/wi_service.py`, `src/doc3gpp/parsers/wi_parser.py` |
| `TestCaseService`, workbook/history parsing | `src/doc3gpp/services/testcase_service.py`, `src/doc3gpp/parsers/testcase_parser.py`, `src/doc3gpp/scraping/testcase_source.py` |
| HTTP transport / cache | `src/doc3gpp/scraping/client.py`, `src/doc3gpp/scraping/cache.py` |

## TDoc extraction and content

| Concern | Entry points |
| --- | --- |
| `TDocPipeline`, `PipelineRuntime`, permits/outcomes | `src/doc3gpp/services/tdoc_pipeline.py`, `src/doc3gpp/services/pipeline_runtime.py` |
| CR preparation/persistence | `src/doc3gpp/services/tdoc_cr_service.py` |
| WID/LS/discussion preparation/persistence | `src/doc3gpp/services/tdoc_family_service.py` |
| `TDOC_CONTENT_STORES`, routing/completeness | `src/doc3gpp/models/tdoc_content_families.py` |
| Direct source validation/extraction | `src/doc3gpp/parsers/direct_extractor.py` |
| ZIP member selection/inventory/conversion | `src/doc3gpp/parsers/tdoc_zip.py`, `src/doc3gpp/parsers/tdoc_family_main.py`, `src/doc3gpp/parsers/tdoc_zip_convert.py` |
| Shared Office conversion processes | `src/doc3gpp/services/docx_convert_pool.py` |
| Word / presentation conversion | `src/doc3gpp/parsers/word_converter.py`, `src/doc3gpp/parsers/docx_converter.py`, `src/doc3gpp/parsers/presentation_converter.py` |
| Cover/TTCN/body-change extraction | `src/doc3gpp/parsers/cr/cr_parsers.py`, `src/doc3gpp/parsers/cr/ttcn_functions.py`, `src/doc3gpp/parsers/cr/body_changes.py` |
| SQL-first content retrieval | `src/doc3gpp/services/tdoc_content_service.py` |
| Atomic extraction/content writes | `src/doc3gpp/storage/repositories/tdoc_content_sql.py`, `src/doc3gpp/storage/repositories/tdoc_cr_sql.py` |
| Inventory / TTCN / body sidecars | `src/doc3gpp/storage/repositories/tdoc_zip_inventory_sql.py`, `src/doc3gpp/storage/repositories/tdoc_cr_ttcn_sql.py`, `src/doc3gpp/storage/repositories/tdoc_cr_change_details_sql.py` |
| Detail DTOs and output | `src/doc3gpp/models/tdoc_show.py`, `src/doc3gpp/storage/export.py` |

## Spec documents and search

| Concern | Entry points |
| --- | --- |
| `SpecDocService`, fetch/parse/persist/content reads | `src/doc3gpp/services/spec_doc_service.py`, `src/doc3gpp/scraping/spec_doc_source.py` |
| TOC/chunks/presentations | `src/doc3gpp/models/spec_doc.py`, `src/doc3gpp/parsers/spec_doc.py`, `src/doc3gpp/parsers/spec_doc_chunker.py` |
| Specdata relational repositories | `src/doc3gpp/storage/repositories/spec_doc_sql.py` |
| Shared chunking / public search shapes | `src/doc3gpp/models/semantic_chunking.py`, `src/doc3gpp/models/search.py`, `src/doc3gpp/models/semantic_search.py`, `src/doc3gpp/models/unified_search.py` |
| Unified facade / index orchestration | `src/doc3gpp/services/tdoc_search_facade.py`, `src/doc3gpp/services/tdoc_index_service.py`, `src/doc3gpp/services/spec_doc_search_facade.py`, `src/doc3gpp/services/spec_doc_index_service.py` |
| FTS5 services | `src/doc3gpp/services/search_service.py`, `src/doc3gpp/services/spec_doc_search_service.py` |
| Vector services / remote embedder | `src/doc3gpp/services/semantic_search_service.py`, `src/doc3gpp/services/spec_doc_semantic_service.py`, `src/doc3gpp/services/embedding/remote_embedder.py` |
| FTS5 and presentation sidecars | `src/doc3gpp/storage/repositories/search_sql.py`, `src/doc3gpp/storage/repositories/tdoc_presentation_search_sql.py`, `src/doc3gpp/storage/repositories/spec_doc_search_sql.py` |
| Vector SQL / provenance | `src/doc3gpp/storage/repositories/vector_sql.py` |

## Persistence and web

| Concern | Entry points |
| --- | --- |
| Repository Protocols / domain DTOs | `src/doc3gpp/repository/protocols.py`, `src/doc3gpp/models/` |
| ORM/base/engines/bootstrap | `src/doc3gpp/storage/db/models.py`, `src/doc3gpp/storage/db/session.py`, `src/doc3gpp/storage/db/migrate.py` |
| Secondary metadata bases | `src/doc3gpp/storage/db/testcase_base.py`, `src/doc3gpp/storage/db/specdata_base.py` |
| Schema registry / compression | `src/doc3gpp/models/schema_info.py`, `src/doc3gpp/storage/compression.py` |
| App/lifespan/MCP mount | `src/doc3gpp/web/app.py` |
| `WebState`, `ServiceContainer`, `JobWorkerHandle` / dependencies | `src/doc3gpp/web/state.py`, `src/doc3gpp/web/deps.py` |
| HTTP adapters / shared output / errors | `src/doc3gpp/web/routes/`, `src/doc3gpp/web/render.py`, `src/doc3gpp/web/errors.py` |
| MCP tool registration and payloads | `src/doc3gpp/web/mcp_server.py` |
| Job kinds/SQL/worker/handlers | `src/doc3gpp/models/jobs.py`, `src/doc3gpp/storage/repositories/jobs_sql.py`, `src/doc3gpp/web/workers/job_worker.py`, `src/doc3gpp/web/workers/handlers.py` |
| Templates / HTMX / sync hub | `src/doc3gpp/web/templates/`, `src/doc3gpp/web/filters.py`, `src/doc3gpp/web/static/js/sync_hub.js` |

`server.enabled` gates the HTTP server and HTTP MCP mount, while `mcp.enabled`
gates both the HTTP mount and standalone `doc3gpp mcp` stdio. The MCP transport
setting selects the HTTP mount only.

## Tests and development

- `tests/conftest.py`: shared environment/settings/engine isolation.
- `tests/unit/`: parsers, services, config, contracts, and worker behavior.
- `tests/integration/`: SQLite repositories, CLI, HTTP/MCP parity, jobs, search.
- `tests/fixtures/`: source HTML/workbooks/Office archives.
- `scripts/test_sqlite.sh`: offline test profile with optional xdist.
- `scripts/dev_run.sh`: development bootstrap helper.
- `pyproject.toml`: dependencies/extras, pytest markers, Ruff/build configuration.
- `.github/workflows/publish.yml`: release packaging/publication, not a test matrix.
