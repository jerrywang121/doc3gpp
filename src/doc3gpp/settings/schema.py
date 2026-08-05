"""Application configuration models.

Two layers of settings are exposed:

* :class:`Settings` is the top-level :class:`pydantic_settings.BaseSettings`.
  It reads environment variables from a **closed allowlist** —
  :data:`ALLOWED_ENV_VARS` — plus the optional ``.env`` file. Anything
  outside the allowlist is ignored, even when the framework's
  ``env_prefix`` / ``env_nested_delimiter`` would otherwise match it.
* Nested sub-models (:class:`OutputSettings`, :class:`OutputFieldsSettings`,
  :class:`CacheSettings`, :class:`TDocParseSettings`,
  :class:`SyncSettings`) carry values that are most naturally configured
  from a TOML file. They are **not** env-overridable; the TOML config
  file is the single source of truth for them.

The TOML config file discovery layer lives in
:mod:`doc3gpp.settings.config_source`; the ``$DOC3GPP_CONFIG`` env var
pinning that file is independent of :data:`ALLOWED_ENV_VARS` and remains
the only way to override the config file location from the environment.

Precedence (highest wins)::

    CLI flags  >  environment variables  >  config file (TOML)  >  defaults

The CLI layer (``doc3gpp.cli``) reads from ``get_settings()`` for its
non-flag defaults, so the same precedence applies transparently.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from humanfriendly import parse_timespan
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


_HUMAN_DELTA_RE = re.compile(r"^(?P<value>[+-]?\d+(?:\.\d+)?)(?P<unit>[smhd])$", re.IGNORECASE)


#: Closed allowlist of ``DOC3GPP_*`` environment variables that
#: :class:`Settings` will honour. Anything outside this set is silently
#: ignored by the env source — operators must use the TOML config file
#: (``doc3gpp config set ...`` or a hand-edited ``doc3gpp.toml``) for
#: the remaining fields.
#:
#: ``DOC3GPP_CONFIG`` (TOML config file location pin) is read directly
#: by :mod:`doc3gpp.settings.config_source` and is intentionally
#: **not** part of this allowlist.
ALLOWED_ENV_VARS: frozenset[str] = frozenset(
    {
        "DOC3GPP_DATABASE_URL",
        "DOC3GPP_DB_ECHO",
        "DOC3GPP_LOG_LEVEL",
        "DOC3GPP_HTTP_VERIFY",
        "DOC3GPP_CACHE__DIR",
        "DOC3GPP_SYNC__AUTO_SYNC",
    }
)


class FilteredEnvSettingsSource(EnvSettingsSource):
    """:class:`EnvSettingsSource` that only honours allowlisted env vars.

    The base class already filters by ``env_prefix`` (so only
    ``DOC3GPP_*`` is considered) and then maps each field to its
    env-var name via ``env_nested_delimiter`` (``__``). On top of that,
    this subclass drops every key that is not in
    :data:`ALLOWED_ENV_VARS`. The result: any ``DOC3GPP_*`` env var that
    is not on the allowlist is read as if it were unset, and the field
    falls back to whatever the TOML config file (or default factory)
    supplies.

    Keys in :attr:`env_vars` are lowercase (the parent class applies
    ``case_sensitive=False``), so the filter compares against
    :data:`ALLOWED_ENV_VARS` in lowercase form.
    """

    def __init__(self, settings_cls: type[BaseSettings], **kwargs: Any) -> None:
        super().__init__(settings_cls, **kwargs)
        # Parent populates ``self.env_vars`` with lowercase keys when
        # ``case_sensitive=False``; normalise both sides for comparison.
        allowed_lower = frozenset(name.lower() for name in ALLOWED_ENV_VARS)
        self.env_vars = {
            key: value for key, value in self.env_vars.items() if key in allowed_lower
        }


def env_var_for_dotted_key(key: str) -> str | None:
    """Render the ``DOC3GPP_*`` env-var name for a dotted ``key``.

    Returns ``None`` when ``key`` does not correspond to an allowlisted
    binding, so callers can detect "TOML-only" keys and skip the
    env-override hint. Shared between :mod:`doc3gpp.cli` and tests.
    """
    parts = key.split(".")
    if len(parts) == 1:
        name = f"DOC3GPP_{parts[0].upper()}"
    else:
        head = parts[0].upper()
        tail = "_".join(parts[1:]).upper()
        name = f"DOC3GPP_{head}__{tail}"
    return name if name in ALLOWED_ENV_VARS else None


def _parse_timedelta(value: object) -> timedelta:
    """Parse a non-negative timedelta from a human or ISO 8601 duration.

    Accepts:
        - human: ``24h``, ``30m``, ``90d``, ``15s`` (case-insensitive)
        - ISO 8601: ``P1D``, ``PT24H``, ``PT30M`` (also accepts lower-case ``pt24h``)
        - ``timedelta`` instances pass through unchanged

    Negative durations are rejected because these fields represent intervals
    and closed windows that must be non-negative.
    """
    parsed: timedelta
    if isinstance(value, timedelta):
        parsed = value
    elif not isinstance(value, str):
        raise TypeError(f"expected str or timedelta, got {type(value).__name__}")
    else:
        text = value.strip()
        if not text:
            raise ValueError("duration must not be empty")
        match = _HUMAN_DELTA_RE.match(text)
        if match:
            numeric = float(match.group("value"))
            unit = match.group("unit").lower()
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            parsed = timedelta(seconds=numeric * multipliers[unit])
        else:
            # Pydantic v2 already accepts ISO 8601 durations for timedelta
            # fields, but normalize through its parser for consistent error
            # messages.
            from pydantic import TypeAdapter

            try:
                parsed = TypeAdapter(timedelta).validate_strings(text)
            except ValueError as exc:
                raise ValueError(
                    f"invalid duration {value!r}; expected a human duration "
                    f"(e.g. 24h, 30m, 90d), an ISO 8601 duration (e.g. PT24H, P90D), "
                    f"or a timedelta object"
                ) from exc
    if parsed.total_seconds() < 0:
        raise ValueError(f"duration {value!r} must not be negative")
    return parsed

# Output formats accepted by every ``* list`` command. Mirrored in
# ``doc3gpp.cli.VALID_FORMATS`` so a typo in one place fails fast.
OutputFormat = Literal["table", "json", "markdown"]


class OutputFieldsSettings(BaseModel):
    """Default column lists for each ``* list`` command.

    Each list mirrors the hardcoded defaults that previously lived inside
    ``doc3gpp.cli``. Centralising them here lets users customise the
    default columns via the config file without touching code.
    """

    meeting: list[str] = Field(
        default_factory=lambda: [
            "meeting_id",
            "name",
            "location",
            "start_date",
            "end_date",
            "ftp_url",
            "start_doc",
            "end_doc",
        ]
    )
    tdoc: list[str] = Field(
        default_factory=lambda: [
            "tdoc_id",
            "meeting_name",
            "title",
            "source",
            "type",
            "status",
            "cr_cat",
            "spec",
            "version",
            "related_wis",
        ]
    )
    tsg: list[str] = Field(
        default_factory=lambda: ["tsg_name", "short_name", "description"]
    )
    wi: list[str] = Field(
        default_factory=lambda: ["wi_id", "acronym", "release", "name"]
    )


class OutputSettings(BaseModel):
    """Output defaults for every ``* list`` command."""

    format: OutputFormat = "table"
    compact: bool = Field(
        default=False,
        description=(
            "When true, JSON output drops indent and operator-space "
            "(single line, ``separators=(\",\", \":\")``) and Markdown "
            "output drops CommonMark decorators (bold, italic, headings, "
            "bullets, code fences, GFM tables). No-op for ``table`` and "
            "``raw``. The CLI ``--compact`` flag takes precedence when "
            "passed."
        ),
    )
    fields: OutputFieldsSettings = Field(default_factory=OutputFieldsSettings)


class TDocParseSettings(BaseModel):
    """Knobs for ``doc3gpp tdoc parse``.

    :attr:`max_batch` caps how many TDocs a single ``tdoc parse``
    invocation will actually process. Filters narrow the candidate
    set; matches above this ceiling are truncated and reported as
    ``remaining`` in the completion summary so the operator can
    re-run the same command (without ``--force``) to continue where
    the previous invocation stopped.

    The default of 100 is conservative — 3GPP meetings rarely exceed
    that for CR-type TDocs and most ad-hoc runs are well under it.
    Operators can raise it for big-batch sweeps via the TOML
    ``[tdoc_parse] max_batch`` key.

    :attr:`max_ftp_depth` controls how many folder levels
    ``tdoc parse --from-url <3gpp-folder> --recursive`` will descend.
    A value of ``0`` means only the root folder is scanned; the
    default of ``2`` scans the root plus two levels of subfolders.

    :attr:`max_tdoc_size_kb` caps the per-file source size (KB)
    applied at four gate points (``download_tdoc_zip`` pre-fetch +
    post-fetch, ``direct_parse_bytes``, and ``_tdoc_parse_local_batch``
    pre-read stat). Oversized files are routed to the existing skip
    bucket (``BatchExtractResult.skipped``) rather than the failure
    bucket — they represent a budget decision, not an upstream-side
    error. ``0`` disables the limit. TOML-only (the
    ``DOC3GPP_TDOC_PARSE__*`` env vars are outside the
    ``ALLOWED_ENV_VARS`` allowlist, matching the sibling knobs).
    """

    max_batch: int = Field(default=100, ge=1)
    max_ftp_depth: int = Field(default=2, ge=0, le=10)
    max_tdoc_size_kb: int = Field(
        default=1000,
        ge=0,
        description=(
            "Per-file cap in KB for TDoc sources (.zip or .docx). "
            "Files larger than this are skipped (size-limit skip "
            "bucket). 0 disables the limit."
        ),
    )
    body_change_enabled: bool = Field(
        default=True,
        description=(
            "Run the body-change extractor on non-TTCN CRs and persist "
            "the result to tdoc_cr_change_details. Disable to skip the "
            "extraction step entirely."
        ),
    )
    body_change_gap_window: int = Field(
        default=2,
        ge=0,
        le=20,
        description=(
            "Max number of plain (non-marker) lines that may sit between "
            "two <ins>/<del> lines and still count as the same change "
            "block. 0 = only consecutive marker lines count."
        ),
    )
    body_change_context_padding: int = Field(
        default=2,
        ge=0,
        le=50,
        description=(
            "Plain context lines captured before and after each change "
            "block. 0 = no context, only the marker lines + bridge."
        ),
    )


class SyncSettings(BaseModel):
    """Sync intervals, skip-rule windows, and auto-sync behavior.

    These values gate the ``meeting sync`` and ``tdoc sync`` commands
    so repeated invocations do not re-scrape unchanged upstream data.
    Durations may be written in TOML/env as human strings (``24h``,
    ``30m``, ``90d``) or ISO 8601 durations (``P1D``, ``PT30M``).

    When ``auto_sync`` is enabled, the read commands ``meeting list``,
    ``tdoc list``, ``tdoc show``, and DB-mode ``tdoc parse`` will
    internally trigger meeting and TDoc list syncs before querying.
    The same skip rules still apply.
    """

    auto_sync: bool = Field(
        default=True,
        description=(
            "Automatically sync meeting calendars and TDoc lists when "
            "running list/show/parse commands."
        ),
    )
    meeting_sync_interval: timedelta = Field(
        default=timedelta(hours=24),
        description="Minimum time between meeting calendar syncs for the same TSG.",
    )
    tdoc_list_sync_interval: timedelta = Field(
        default=timedelta(minutes=30),
        description="Minimum time between TDoc list syncs for the same meeting.",
    )
    tdoc_list_closed_window: timedelta = Field(
        default=timedelta(days=90),
        description="Meetings whose end date is older than this window are skipped.",
    )
    tdoc_list_url_template: str = Field(
        default="https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}",
        description=(
            "Portal URL template used to download a meeting's TDoc list XLSX. "
            "Must contain the '{meeting_id}' placeholder."
        ),
    )

    @field_validator("meeting_sync_interval", "tdoc_list_sync_interval", "tdoc_list_closed_window", mode="before")
    @classmethod
    def _validate_durations(cls, value: object) -> timedelta:
        return _parse_timedelta(value)


class CacheSettings(BaseModel):
    """Disk cache configuration for TDoc extraction artifacts.

    Two subtrees live under :attr:`dir`: ``zips/`` holds the raw 3GPP zip
    downloads and ``markdown/`` holds the python-docx output. The cache
    module evicts files in insertion order (oldest by ``st_ctime`` first)
    whenever the combined size of both subtrees exceeds
    :attr:`size_limit_mb` megabytes; ``0`` means unlimited. The
    :attr:`purge_confirm` flag is read by the CLI's ``cache purge``
    command to gate the destructive operation with an interactive prompt
    (skip the prompt with ``--yes`` or by setting
    ``cache.purge_confirm = false`` in the active TOML config — it is
    not exposed via environment variable; see
    :data:`ALLOWED_ENV_VARS`).
    """

    dir: Path = Field(
        default_factory=lambda: Path.home() / ".cache" / "doc3gpp" / "tdocs"
    )
    size_limit_mb: int = Field(default=1024, ge=0)  # 0 = unlimited
    purge_confirm: bool = Field(default=True)  # CLI guard for `cache purge`


#: The 8 FTS5 indexed columns of the ``tdoc_search`` virtual table, in
#: DDL order. Single source of truth for ``bm25_weights`` validation
#: and the per-column snippet selection in the search repo. Keep in
#: sync with the DDL in ``storage/db/create_schema.py``.
_SNIPPET_COLUMN_NAMES: tuple[str, ...] = (
    "title",
    "ftp_url",
    "meeting_title",
    "meeting_location",
    "wis",
    "cover_text",
    "change_text",
    "ttcn_text",
)


class SearchSettings(BaseModel):
    """Knobs for the FTS5 full-text search subsystem.

    Defaults match the conservative end: ``enabled`` and
    ``auto_index_on_parse`` both default to True so the index
    stays in sync with every successful ``tdoc parse`` until the
    operator opts out. ``rebuild_batch_size`` keeps the default
    CLI ``search index --rebuild`` manageable on huge DBs;
    ``snippet_tokens`` caps the FTS5 ``snippet(...)`` length.

    TOML-only (the ``DOC3GPP_SEARCH__*`` env vars are outside the
    :data:`ALLOWED_ENV_VARS` allowlist, matching the sibling
    knobs). The presence of FTS5 itself is gated by the new
    ``[search]`` pyproject extra; on sqlite builds without FTS5
    the runtime probe in
    :class:`~doc3gpp.storage.repositories.search_sql.SQLAlchemySearchIndexRepository`
    raises :class:`SearchUnavailableError` which the factory
    catches once at startup.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the search subsystem. False disables "
            "the CLI commands, the auto-index hook, and the "
            "tdoc_search DDL creation. doc3gpp continues to "
            "function normally otherwise."
        ),
    )
    auto_index_on_parse: bool = Field(
        default=True,
        description=(
            "When true, every successful tdoc parse calls "
            "SearchService.upsert_for_tdoc(tdoc_id) so the index "
            "stays in sync. Disable to manage the index manually."
        ),
    )
    rebuild_batch_size: int = Field(
        default=100,
        ge=1,
        description=(
            "TDocs per batch during `search index --rebuild`. "
            "Smaller values reduce peak memory and crash-recovery "
            "loss (cursor advances per batch); larger values "
            "finish faster."
        ),
    )
    snippet_tokens: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "Approximate number of tokens per FTS5 snippet() "
            "output. The CLI's --snippet-tokens flag overrides "
            "this for a single invocation."
        ),
    )
    bm25_weights: tuple[float, ...] = Field(
        default=(5.0, 0.0, 0.0, 1.0, 5.0, 5.0, 5.0, 5.0),
        description=(
            "Per-column BM25 weights applied via FTS5's "
            "bm25() function. Order MUST match the 8 indexed "
            "columns of the tdoc_search virtual table "
            "(see :data:`_SNIPPET_COLUMN_NAMES`). Columns with "
            "weight 0 are skipped in both ranking and snippet "
            "selection; columns with weight > 0 each get their "
            "own highlighted preview in query results."
        ),
    )

    @field_validator("bm25_weights", mode="before")
    @classmethod
    def _validate_bm25_weights_length(cls, value: object) -> object:
        if isinstance(value, (tuple, list)) and len(value) != 8:
            raise ValueError(
                f"bm25_weights must have exactly 8 entries (one per FTS5 "
                f"column), got {len(value)}"
            )
        return value

    search_fanout_factor: int = Field(
        default=4, ge=1, le=64,
        description=(
            "When `search query --sem-query` is used, the FTS5 path "
            "fetches limit * search_fanout_factor candidates before "
            "the semantic reranker truncates back to limit. Higher "
            "values give the reranker more to work with at the cost "
            "of more vector lookups per query. Only honored when "
            "--sem-query is supplied. Default 4. Range 1..64."
        ),
    )


class SemanticSearchSettings(BaseModel):
    """Configuration for the semantic (embedding + vector) search subsystem.

    TOML-only (no env overrides). The presence of the sqlite-vec
    extension is gated by the ``[semantic]`` pyproject extra; on
    builds without it the runtime probe raises
    :class:`VectorIndexUnavailableError` which the factory catches
    once at startup.

    As of the 2026-08-01 design revision, spaCy is no longer
    used; the FTS5 path runs the explicit ``--fts5-query`` string
    through :class:`doc3gpp.cli_filters.SearchQueryBuilder` without
    any stopword stripping.
    """

    enabled: bool = Field(default=True, description="Master switch.")
    auto_embed_on_parse: bool = Field(
        default=True,
        description="When true, every successful tdoc parse calls "
        "SemanticSearchService.index_for_tdoc(tdoc_id).",
    )
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="HuggingFace sentence-transformers repo id.",
    )
    chunk_size: int = Field(default=200, ge=1, description="Whitespace tokens per chunk.")
    chunk_overlap: int = Field(
        default=20, ge=0,
        description="Trailing tokens repeated at next chunk start. Must be < chunk_size.",
    )
    rrf_k: int = Field(default=60, ge=1, description="RRF k constant.")
    fts5_weight: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description=(
            "Blend weight for the FTS5 rank in RRF (0.0..1.0). The "
            "vector weight is 1 - fts5_weight. 0.0 is pure vector; "
            "1.0 is pure FTS5. Ignored when --fts5-query is omitted."
        ),
    )
    fanout_multiplier: int = Field(
        default=4, ge=1,
        description=(
            "Internal fan-out factor for the hybrid `search sem` path. "
            "When --fts5-query is supplied, each side fetches "
            "limit * fanout_multiplier candidates before RRF merge; "
            "ignored on the pure-vector path. Mirrors "
            "search.search_fanout_factor for `search query --sem-query`."
        ),
    )
    max_chunks_per_tdoc: int = Field(
        default=8, ge=1,
        description="Cap on chunks per TDoc to bound parse latency on long covers.",
    )

    @field_validator("chunk_overlap")
    @classmethod
    def _overlap_less_than_size(cls, v, info):
        size = info.data.get("chunk_size", 200)
        if v >= size:
            raise ValueError(f"chunk_overlap ({v}) must be < chunk_size ({size})")
        return v


class ServerSettings(BaseModel):
    """Knobs for ``doc3gpp server`` (FastAPI + uvicorn HTTP surface).

    :attr:`enabled` is the master switch — the CLI's ``server_app`` rejects
    every subcommand when this is ``False``. Defaults to ``False`` so a
    fresh install does not open a port without an explicit operator opt-in.

    :attr:`host` defaults to the loopback interface so the server is not
    reachable from the network until the operator binds a public address.

    :attr:`max_concurrent_jobs` caps how many job-worker slots run in
    parallel; bounded at 1..16 to keep the sqlite writer contention
    predictable on small machines.

    :attr:`log_retention` is the only duration-style field on this model;
    the validator routes it through :func:`humanfriendly.parse_timespan`
    so operators can write ``7d``, ``30d``, ``24h``, etc. The string is
    stored as-is so the worker can recompute the retention cutoff on
    every cleanup pass without losing precision.

    :attr:`pid_file` and :attr:`log_file` both default to ``None``; the
    server resolves them at startup to ``{cache.dir}/server.pid`` and
    ``{cache.dir}/server.log`` respectively so the CLI's ``start`` /
    ``stop`` / ``logs`` commands land on predictable paths without
    forcing every operator to set them explicitly.

    TOML-only (the ``DOC3GPP_SERVER__*`` env vars are outside the
    :data:`ALLOWED_ENV_VARS` allowlist, matching the sibling knobs).
    """

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the `doc3gpp server` HTTP surface. False "
            "rejects every `server` subcommand at the CLI gate so a "
            "fresh install does not open a port without an explicit "
            "operator opt-in via `[server] enabled = true`."
        ),
    )
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8765, ge=1, le=65535)
    max_concurrent_jobs: int = Field(default=1, ge=1, le=16)
    poll_interval_seconds: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        description=(
            "How often the job worker checks the ``jobs`` table for "
            "new ``QUEUED`` rows. Lower values mean faster pickup "
            "after a parse / sync / cache-purge request lands; "
            "higher values reduce DB load on idle installs. The old "
            "``cleanup_interval_seconds`` knob was reused as the poll "
            "cadence and produced 5-minute pickup delays — see the "
            "job worker module for the regression note."
        ),
    )
    cleanup_interval_seconds: int = Field(default=300, ge=10)
    log_retention: str = Field(
        default="7d",
        description=(
            "Retention window for completed-job log files. Parsed by "
            "humanfriendly.parse_timespan so values like `7d`, `24h`, "
            "`30m` are accepted."
        ),
    )
    cache_subdir: str | None = Field(
        default=None,
        description=(
            "Optional subdirectory under {cache.dir} for server-side "
            "artifacts (job bundles, transient uploads). None means "
            "use the cache root directly."
        ),
    )
    pid_file: str | None = Field(
        default=None,
        description=(
            "Path to the PID file written by `server start`. None "
            "defaults to {cache.dir}/server.pid at startup."
        ),
    )
    log_file: str | None = Field(
        default=None,
        description=(
            "Path to the structured log file written by the server "
            "worker. None defaults to {cache.dir}/server.log at startup."
        ),
    )

    @field_validator("log_retention", mode="before")
    @classmethod
    def _validate_log_retention(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError(f"log_retention must be a string, got {type(value).__name__}")
        text = value.strip()
        if not text:
            raise ValueError("log_retention must not be empty")
        from humanfriendly import InvalidTimespan

        try:
            parse_timespan(text)
        except (ValueError, InvalidTimespan) as exc:
            raise ValueError(
                f"invalid log_retention {value!r}; expected a humanfriendly "
                f"timespan (e.g. 7d, 24h, 30m)"
            ) from exc
        return text


class MCPSettings(BaseModel):
    """Knobs for the MCP sub-app mounted under ``doc3gpp server``.

    :attr:`enabled` defaults to ``True`` because MCP has no effect
    until :class:`ServerSettings.enabled` is also ``True``; the
    effective gate is the AND of the two. Keeping the per-side flag
    lets operators stage a config that disables MCP before turning
    the server on.

    :attr:`transport` is locked to ``streamable_http`` for v1 — the
    older SSE-only transport is deprecated upstream and the legacy
    stdio transport would conflict with the per-request lifecycle
    the HTTP surface expects. New transports become literals when
    they ship.

    :attr:`sse_queue_size` bounds the in-process queue length that
    fans events out to active MCP sessions; values below 10 cause
    dropouts under modest load, so the lower bound is 10.

    TOML-only (the ``DOC3GPP_MCP__*`` env vars are outside the
    :data:`ALLOWED_ENV_VARS` allowlist, matching the sibling knobs).
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Per-side MCP switch. Effective only when "
            "[server].enabled is also true. False disables the MCP "
            "mount without touching the rest of the server."
        ),
    )
    transport: Literal["streamable_http"] = Field(
        default="streamable_http",
        description="HTTP transport used by the MCP mount.",
    )
    sse_queue_size: int = Field(
        default=100,
        ge=10,
        description=(
            "Per-session event queue length. Bounded at >= 10 to avoid "
            "dropouts under modest load; raise for high-throughput "
            "agents."
        ),
    )


class Settings(BaseSettings):
    """Application configuration loaded from environment variables or .env.

    The flat fields at the root (``database_url``, ``db_echo``,
    ``log_level``, ``http_verify``) are populated from the
    :data:`ALLOWED_ENV_VARS` subset of ``DOC3GPP_*`` env vars.
    Nested sub-models (``output``, ``cache``, ``tdoc_parse``,
    ``sync``) come exclusively from the TOML config file via
    :func:`doc3gpp.settings.loader.get_settings`; the only nested
    env-override is ``DOC3GPP_CACHE__DIR`` (allowed) and
    ``DOC3GPP_SYNC__AUTO_SYNC`` (allowed). All other nested fields
    are TOML-only.
    """

    database_url: str = Field(
        default_factory=lambda: f"sqlite+pysqlite:///{Path.home()}/.local/share/doc3gpp/doc3gpp.db",
        validation_alias="DOC3GPP_DATABASE_URL",
    )
    db_echo: bool = Field(default=False, validation_alias="DOC3GPP_DB_ECHO")
    db_auto_migrate: bool = Field(default=True)
    log_level: str = Field(default="INFO", validation_alias="DOC3GPP_LOG_LEVEL")
    http_verify: bool = Field(default=False, validation_alias="DOC3GPP_HTTP_VERIFY")
    http_max_retries: int = Field(default=3, ge=0)
    http_retry_backoff: float = Field(default=0.5, ge=0.0)

    output: OutputSettings = Field(default_factory=OutputSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    tdoc_parse: TDocParseSettings = Field(default_factory=TDocParseSettings)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    semantic_search: SemanticSearchSettings = Field(default_factory=SemanticSearchSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)

    model_config = SettingsConfigDict(
        env_prefix="DOC3GPP_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Reorder sources so allowlisted env vars beat the TOML config file.

        pydantic-settings' default priority is ``init_args > env > .env >
        secrets > defaults``. We feed TOML data through ``init_settings``
        (in :func:`doc3gpp.settings.loader.get_settings`), so without this
        override the config file would shadow the env vars and silently
        break the documented ``CLI > env > file > defaults`` chain.
        Returning ``(filtered_env, init, dotenv, secret)`` flips init
        below env.

        The ``env_settings`` argument here is the framework-provided
        :class:`EnvSettingsSource`; we wrap it in
        :class:`FilteredEnvSettingsSource` so the allowlist
        (:data:`ALLOWED_ENV_VARS`) is the single source of truth for
        which env vars are honoured.
        """
        _ = env_settings  # noqa: F841 - replaced by the filtered source below
        filtered_env = FilteredEnvSettingsSource(
            settings_cls,
            case_sensitive=cls.model_config.get("case_sensitive"),
            env_prefix=cls.model_config.get("env_prefix"),
            env_nested_delimiter=cls.model_config.get("env_nested_delimiter"),
            env_ignore_empty=cls.model_config.get("env_ignore_empty"),
            env_parse_none_str=cls.model_config.get("env_parse_none_str"),
        )
        return (
            filtered_env,
            init_settings,
            dotenv_settings,
            file_secret_settings,
        )