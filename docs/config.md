# Configuration

This is the settings reference. For command syntax see
[config commands](cli.md#config-commands); for runtime effects see
[processes](processes.md). The complete editable template is
[`doc3gpp.toml.example`](../src/doc3gpp/data/doc3gpp.toml.example), and validation
is implemented in [`settings/schema.py`](../src/doc3gpp/settings/schema.py).

## Discovery and precedence

The first matching TOML file wins; files are **not merged**:

1. `DOC3GPP_CONFIG`: an explicit file, or a directory containing `config.toml`.
2. `./doc3gpp.toml` in the current working directory (not a parent-directory search).
3. `$XDG_CONFIG_HOME/doc3gpp/config.toml`, defaulting to
   `~/.config/doc3gpp/config.toml`.

No discovered file means built-in defaults. An invalid explicit
`DOC3GPP_CONFIG` path raises an error rather than falling back; malformed TOML
also raises an error with the path. Unrelated extra keys are ignored, so check
spelling with `config show` rather than expecting every typo to fail validation.

For `get_settings()`, the actual source order, highest first, is:

1. Allowlisted **process environment** variables.
2. Values from the selected TOML file (passed as settings initializer values).
3. The current directory's `.env`, loaded by pydantic-settings.
4. Configured file-secret sources, if any.
5. Built-in defaults.

An explicit CLI option overrides its corresponding setting where the command
offers that option. There is no universal flag for every setting.

**Important `.env` caveat:** the closed allowlist is applied to process
environment variables, not the dotenv source. A `.env` entry for another
recognized nested setting can still affect fields absent from TOML. Do not rely
on the process allowlist to isolate an untrusted `.env`; see
[known constraints](known-constraints.md#configuration-and-security).

Settings and engines are cached within a process. Restart the server after
editing configuration; changes do not hot-reload existing services.

## Inspect and edit

```bash
doc3gpp config path
doc3gpp config show
doc3gpp config init --target user
doc3gpp config set sync.auto_sync false
doc3gpp config set tdoc_parse.max_batch 200 --dry-run
doc3gpp config set output.fields.tdoc '["tdoc_id","title","parsed"]'
```

- `config show` prints a source comment followed by resolved JSON. The embedding
  API key is redacted; treat the rest as potentially sensitive (for example a
  database URL can contain credentials).
- `config init --target auto|project|user` creates the packaged template. `auto`
  chooses `./doc3gpp.toml` when the working directory or a parent has project markers,
  otherwise the user file. Existing files require `--force`. Initialization
  refuses while `DOC3GPP_CONFIG` is set.
- The user bootstrap target is currently fixed at
  `~/.config/doc3gpp/config.toml`, even when `XDG_CONFIG_HOME` is customized.
  Discovery honors the custom XDG directory, so move the initialized file there
  or pin the created file with `DOC3GPP_CONFIG` before subsequent commands.
- `config set` requires an active file, validates the change, and atomically
  rewrites TOML. It does not preserve comments or original formatting.
- The CLI value `null` removes an optional key so its default applies. **TOML
  has no `null` literal**: omit optional settings instead of writing `key = null`.

### Process environment allowlist

| Variable | Setting |
| --- | --- |
| `DOC3GPP_DATABASE_URL` | `database_url` |
| `DOC3GPP_TESTCASE_DATABASE_URL` | `testcase_database_url` |
| `DOC3GPP_SPECDATA_DATABASE_URL` | `specdata_database_url` |
| `DOC3GPP_DB_ECHO` | `db_echo` |
| `DOC3GPP_LOG_LEVEL` | `log_level` |
| `DOC3GPP_HTTP_VERIFY` | `http_verify` |
| `DOC3GPP_CACHE__DIR` | `cache.dir` |
| `DOC3GPP_SYNC__AUTO_SYNC` | `sync.auto_sync` |
| `DOC3GPP_SEMANTIC_SEARCH__EMBEDDING_API_KEY` | `semantic_search.embedding_api_key` |
| `DOC3GPP_WORD_CONVERSION__TIMEOUT_SECONDS` | `word_conversion.timeout_seconds` |

Other process `DOC3GPP_*` variables are ignored. `DOC3GPP_CONFIG` and
`XDG_CONFIG_HOME` are separate discovery controls, not settings fields.
Use TOML for server, MCP, pool, search, and parse knobs.

## Databases and HTTP

| Key | Default | Meaning |
| --- | --- | --- |
| `database_url` | SQLite at `~/.local/share/doc3gpp/doc3gpp.db` | Main metadata, TDoc content, jobs, TDoc indexes |
| `testcase_database_url` | Omitted | Derived SQLite sibling `<main-stem>_testcase.db` |
| `specdata_database_url` | Omitted | Derived SQLite sibling `<main-stem>_specdata.db` |
| `db_echo` | `false` | SQLAlchemy statement logging |
| `db_auto_migrate` | `true` | Additive schema bootstrap behavior; not a general migration system |
| `log_level` | `"INFO"` | Application logging |
| `http_verify` | `false` | Verify upstream HTTP TLS certificates |
| `http_max_retries` | `3` | HTTP retry count |
| `http_retry_backoff` | `0.5` | Retry backoff base, seconds |

Use an explicit secondary URL when the main URL is not SQLite; sibling URL
derivation is SQLite-specific. In-memory URLs have special shared-engine
handling and are intended mainly for tests. Initialize all corpora with
`doc3gpp db init --scope all`; scopes are `main`, `testcase`, `specdata`, `all`.
See [database ownership](architecture.md#databases-and-ownership) and
[schema maintenance](processes.md#schema-and-recovery).

TLS verification is disabled by the current default. Enable `http_verify` when
your upstream certificate chain permits it. This setting governs outbound
requests; it does not add HTTPS or authentication to the local web server.

## Sync

Under `[sync]`:

| Key | Default | Scope |
| --- | --- | --- |
| `auto_sync` | `true` | Best-effort sync before selected CLI reads/parses |
| `meeting_sync_interval` | `"24h"` | Per-TSG meeting-calendar interval |
| `spec_sync_interval` | `"24h"` | Per-spec `last_synced_at` interval |
| `tdoc_list_sync_interval` | `"30m"` | Per-meeting TDoc-list interval |
| `tdoc_list_closed_window` | `"90d"` | Stop refreshing previously synced old meetings |
| `tdoc_list_url_template` | `https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}` | TDoc list export URL |

Durations accept human forms such as `30m`, `24h`, `90d`, or ISO 8601 durations.
Exact skip order, first-sync exceptions, and force behavior are in
[sync flows](processes.md#sync).

## Cache and extraction

| Section/key | Default | Meaning |
| --- | --- | --- |
| `cache.dir` | `~/.cache/doc3gpp/tdocs` | TDoc ZIP and derived Markdown cache root |
| `cache.size_limit_mb` | `1024` | Combined cache limit; `0` disables size eviction |
| `cache.purge_confirm` | `true` | CLI purge confirmation |
| `tdoc_parse.max_batch` | `100` | DB-selected TDocs per parse invocation; minimum `1` |
| `tdoc_parse.max_ftp_depth` | `2` | Default FTP-folder discovery depth, range `0..10` |
| `tdoc_parse.max_tdoc_size_kb` | `1000` | Per-source size cap; `0` means unlimited |
| `tdoc_parse.max_documents_per_zip` | `100` | Supported Office members per TDoc ZIP; `0` disables |
| `tdoc_parse.max_uncompressed_size_kb` | `102400` | Aggregate supported-member size cap; `0` disables |
| `tdoc_parse.body_change_enabled` | `true` | Extract non-TTCN CR body changes |
| `tdoc_parse.body_change_gap_window` | `2` | Plain lines allowed between tracked-change markers, `0..20` |
| `tdoc_parse.body_change_context_padding` | `2` | Context lines around body-change blocks, `0..50` |
| `word_conversion.timeout_seconds` | `300` | Timeout per legacy `.doc`/`.ppt` conversion; minimum `1` |

The archive limits count `.docx`, `.doc`, `.pptx`, and `.ppt`, despite historical
`docx` names in code. These are not limits on unrelated ZIP inventory entries.
Eviction and the distinction between SQL content and cached CR main content
are described in [cache lifecycle](processes.md#cache).

## Output

`[output] format = "table"` and `compact = false` are the defaults. Format
choices depend on the command. Compact JSON uses one line; compact Markdown
removes presentation markup; table/raw are unchanged. CLI options take priority.

`[output.fields]` has lists for `meeting`, `tdoc`, `tsg`, `wi`, `spec`,
`spec_doc`, `spec_doc_toc`, and `testcase`. Use `--fields` for a one-off
selection or `--fields all` where supported. The template lists default fields;
the [CLI reference](cli.md#common-list-output-options) defines per-command fields.

## Full-text search

Under `[search]` (TDoc FTS5):

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Enable the text-search subsystem |
| `auto_index_on_parse` | `true` | Update text indexes after extraction |
| `rebuild_batch_size` | `100` | Rebuild batch size |
| `snippet_tokens` | `8` | FTS preview size |
| `bm25_weights` | `[5, 0, 0, 1, 5, 5, 5, 5, 1]` | Weights in the column order below |
| `search_fanout_factor` | `4` | Oversampling before result deduplication |

Weight order: `title`, `ftp_url`, `meeting_title`, `meeting_location`, `wis`,
`cover_text`, `change_text`, `ttcn_text`, `content_text`. Legacy eight-value
settings receive a ninth content weight of `1`. A zero weight suppresses that
column's score contribution and preview, not its ability to match.

FTS5 is built into a compatible SQLite build; `[search]` adds no third-party
runtime dependency. Search requires SQLite FTS5, not a portable SQL backend.

## Semantic and hybrid search

Under `[semantic_search]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Enable semantic services when dependencies/configuration allow |
| `auto_embed_on_parse` | `true` | Embed TDoc content after parsing |
| `embedding_model` | `"embeddinggemma:300m"` | Remote model identifier |
| `embedding_base_url` | Omitted | OpenAI-compatible embedding API base URL; unset disables remote embedding |
| `embedding_api_key` | Omitted | Optional credential; prefer allowlisted environment variable |
| `embedding_timeout_s` | `30.0` | Request timeout |
| `embedding_batch_size` | `32` | Texts per remote batch |
| `chunk_size` | `512` | Whitespace-token budget |
| `chunk_overlap` | `24` | Overlap tokens; must be smaller than `chunk_size` |
| `rrf_k` | `60` | Reciprocal-rank fusion constant |
| `fts5_weight` | `0.5` | Persisted hybrid text/vector blend, not a query flag |
| `fanout_multiplier` | `8` | Vector candidate oversampling |
| `max_chunks_per_tdoc` | `8` | Metadata chunks per TDoc |
| `max_content_chunks_per_tdoc` | `16` | Shared content-chunk cap across a TDoc's documents |

Example (adapt the URL/model to your embedding server):

```toml
[semantic_search]
embedding_base_url = "http://127.0.0.1:11434/v1"
embedding_model = "embeddinggemma:300m"
```

Install `[semantic]`; changing the model or chunk policy can require rebuilding
embeddings. Embedding sends extracted text to the configured endpoint: consider
confidentiality and network policy before enabling it. For secret writes,
`config set` normally permits plaintext API keys only in the user config;
project files require the explicit override documented in
[config set](cli.md#doc3gpp-config-set). Writes use restrictive permissions where
appropriate, but an environment variable or secret manager is preferable.

## Specification documents

Under `[spec_doc]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `cache_dir` | `~/.cache/doc3gpp/specs` | Separate spec-document cache |
| `max_zip_size_kb` | `0` | Downloaded ZIP cap; `0` disables |
| `max_chunk_chars` | `1500` | Character budget in addition to the token budget |
| `chunk_overlap` | Omitted | Inherits `semantic_search.chunk_overlap` |
| `auto_index_on_parse` | `true` | Update spec-document FTS5 after parse |
| `auto_embed_on_parse` | `true` | Update spec-document vectors when configured |
| `bm25_weights` | `[5, 5, 5, 1, 1, 1]` | `text`, `sections`, `tables`, `spec_id`, `version`, `release` |

Specdata is separate from the main and testcase databases. The TDoc cache purge
commands do not purge this separate cache. See
[specification parsing](processes.md#specification-document-parsing).

## Pools, server, and MCP

Under `[pipeline]`:

| Key | Default | Range |
| --- | --- | --- |
| `prepare_workers` | `min(32, (cpu_count or 1) + 4)` | `1..32` |
| `docx_workers` | `min(4, cpu_count or 1)` | `0..32`; `0`/`1` convert inline |

Under `[server]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Permit HTTP server commands and its MCP mount; does not gate stdio |
| `host` / `port` | `"127.0.0.1"` / `13999` | Bind address |
| `max_concurrent_jobs` | `3` | Active jobs, range `1..16`; not item workers |
| `poll_interval_seconds` | `1.0` | Queue pickup cadence, `0.05..60` |
| `progress_interval_seconds` | `10.0` | Periodic progress throttle, `0.1..60`; forced lines bypass |
| `cleanup_interval_seconds` | `300` | Job retention cleanup cadence, minimum `10` |
| `log_retention` | `"7d"` | Retention for terminal job rows/logs in SQL, not rotation of `server.log` |
| `cache_subdir` | Omitted | Optional server artifact subdirectory setting |
| `pid_file` | Omitted | Defaults to `{cache.dir}/server.pid` |
| `log_file` | Omitted | Defaults to `{cache.dir}/server.log` |

Under `[mcp]`:

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Enable `doc3gpp mcp` stdio and the HTTP MCP mount (the latter also requires `server.enabled`) |
| `transport` | `"streamable_http"` | HTTP MCP mount only: `streamable_http` or legacy `sse`; stdio is unaffected |
| `sse_queue_size` | `100` | Bounded event queue setting, minimum `10` |
| `allowed_origins` | `["http://127.0.0.1", "http://localhost"]` | MCP transport origin allowlist |

See [pool and job behavior](processes.md#pools-and-background-jobs),
[server operation](web-server.md), and [MCP transport security](mcp.md#security).
Binding to another address does not automatically change the MCP SDK's allowed
host policy or provide authentication.
HTTP and stdio both start a job worker; use only one worker process per database
to avoid interference with job ownership and recovery. See
[runtime constraints](known-constraints.md#pools-jobs-and-deployment).

## Optional dependencies

| Extra / external program | Use |
| --- | --- |
| `[cli]` | Typer CLI and progress display |
| `[extract]` | `python-docx` and `python-pptx` for Word/presentation extraction |
| `[search]` | Convenience extra; FTS5 comes from SQLite |
| `[semantic]` | `sqlite-vec`; remote embedding service still required |
| `[web]` | FastAPI/Uvicorn, templates, Markdown rendering, MCP SDK |
| `[all]` | All runtime extras, including web |
| `[dev]` | Test/lint tools plus CLI/web; add `extract,semantic` for their coverage |
| LibreOffice Writer | Legacy `.doc` conversion |
| LibreOffice Impress | Legacy `.ppt` conversion |

For Debian/Ubuntu, install the needed legacy converters with:

```bash
sudo apt-get install libreoffice-writer libreoffice-impress
```

On Windows, install LibreOffice with Writer/Impress and make its `program`
directory (containing `soffice`) available on `PATH`. Restart the terminal or
service after changing `PATH`. Legacy conversion uses isolated temporary
directories/profiles and the configured timeout; no Office automation server or
persistent conversion daemon is required. Native `.docx`/`.pptx` conversion does
not require LibreOffice. See [extraction limits](known-constraints.md#extraction-and-content).
