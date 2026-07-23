from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date, datetime
from pathlib import Path
from typing import Any, TextIO

import typer
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from doc3gpp.config import get_settings
from doc3gpp.settings.schema import Settings, env_var_for_dotted_key
from doc3gpp.cli_auto_sync import (
    _build_meeting_url,
    collect_tdoc_candidates_for_url,
    trigger_auto_sync,
)
from doc3gpp.cli_filters import parse_tdoc_id, validate_date_filter
from doc3gpp.cli_url_helpers import (
    _looks_like_3gpp_file_url,
    _looks_like_3gpp_folder_url,
    is_3gpp_ftp_url,
)
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.sync import BulkSyncOutcome
from doc3gpp.models.tdoc_cr import (
    DirectParseBatchResult,
    TDocCRDetails,
    TDocCRTTCNDetails,
)
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi
from doc3gpp.parsers.docx_converter import PythonDocxNotInstalledError
from doc3gpp.scraping.cache import CacheStatus, TDocCache
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.parsers.direct_extractor import (
    NotAFolderError,
    extract_tdoc_id_from_filename,
)
from doc3gpp.parsers.cr.header import is_ttcn_tdoc
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.scraping.tdoc_zip_source import canonicalise_tdoc_id
from doc3gpp.services.factory import (
    build_meeting_service,
    build_tdoc_cr_repository,
    build_tdoc_cr_service,
    build_tdoc_cr_ttcn_repository,
    build_tdoc_file_repository,
    build_tdoc_repository,
    build_tdoc_service,
    build_tdoc_sync_coordinator,
    build_tsg_service,
    build_wi_service,
)
from doc3gpp.services.tdoc_cr_service import (
    TDocNotFoundError,
    TDocTypeUnsupportedError,
    TDocZipDownloadError,
    _read_cached_markdown_path,
)
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
)
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.settings.config_source import find_config_file, load_config_data
from doc3gpp.settings.config_writer import (
    ConfigValidationError,
    load_default_template,
    patch_dotted,
    prune_empty_tables,
    read_toml,
    resolve_echo_subtree,
    resolve_init_target,
    validate_against_settings,
    walk_known_dotted_keys,
    write_toml,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine

app = typer.Typer(help="doc3gpp command line tools")
db_app = typer.Typer(help="database commands")
meeting_app = typer.Typer(help="meeting commands")
tdoc_app = typer.Typer(help="tdoc commands")
tsg_app = typer.Typer(help="tsg reference data commands")
wi_app = typer.Typer(help="wi commands")
config_app = typer.Typer(help="inspect the resolved configuration")
cache_app = typer.Typer(help="TDoc extraction cache commands")
app.add_typer(db_app, name="db")
app.add_typer(meeting_app, name="meeting")
app.add_typer(tdoc_app, name="tdoc")
app.add_typer(tsg_app, name="tsg")
app.add_typer(wi_app, name="wi")
app.add_typer(config_app, name="config")
app.add_typer(cache_app, name="cache")

logger = logging.getLogger(__name__)

DEFAULT_TSG = "r5"


def _configure_logging() -> None:
    try:
        settings = get_settings()
        level = getattr(logging, settings.log_level.upper(), logging.INFO)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        # Malformed active TOML must not abort every CLI command before
        # the operator can run `doc3gpp config set` to repair it.
        # ``load_config_data`` wraps the underlying TOMLDecodeError in
        # a ValueError, so both shapes are caught here. The error is
        # logged so it stays visible, then we fall back to INFO.
        logging.getLogger(__name__).warning(
            "active TOML config is malformed: %s; falling back to default log level", exc
        )
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(level)
    logger.debug("Logging configured at %s", logging.getLevelName(level))


@app.callback(invoke_without_command=True)
def main_callback(ctx: typer.Context) -> None:
    _configure_logging()
    if ctx.invoked_subcommand is None:
        typer.echo(app.get_help(ctx))


def _ensure_tsg_ready(tsg_service: TsgService) -> TsgService:
    """Auto-seed the TSG reference table on a fresh install."""
    if tsg_service.count() == 0:
        logger.info("TSG reference table is empty; seeding default TSG list")
        tsg_service.seed_defaults()
    return tsg_service


def _validate_tsg_short_name(tsg: str, service: TsgService) -> str:
    """Return the canonical short name or raise typer.BadParameter."""
    canonical = tsg.upper()
    if not service.is_known_short_name(canonical):
        known = service.known_short_names()
        known_list = ", ".join(known) if known else "(no TSGs registered)"
        raise typer.BadParameter(
            f"Unknown TSG short name '{tsg}'. Known short names: {known_list}. "
            f"Run 'doc3gpp tsg list' for the full reference."
        )
    return canonical


def _parse_field_selection(
    requested: str | None,
    allowed_fields: list[str],
    default_fields: list[str],
) -> list[str]:
    """Resolve a ``--fields`` comma-separated option into a concrete field list.

    Semantics:
      - Empty / ``None`` input ⇒ fall back to ``default_fields``.
      - The literal token ``all`` (case-insensitive) ⇒ return every entry in
        ``allowed_fields`` in declaration order.
      - Otherwise ⇒ validate each requested token against ``allowed_fields``
        and raise ``typer.BadParameter`` listing unknown names. The error
        message format matches what ``meeting list`` / ``tdoc list`` /
        ``tsg list`` produced before this helper was extracted, so existing
        tests still match.
    """
    if not requested:
        return default_fields

    selected = [f.strip() for f in requested.split(",") if f.strip()]
    if any(f.lower() == "all" for f in selected):
        return list(allowed_fields)

    invalid = [f for f in selected if f not in allowed_fields]
    if invalid:
        valid_list = ", ".join(allowed_fields)
        raise typer.BadParameter(
            f"Unknown field(s): {', '.join(invalid)}. Valid fields: {valid_list}"
        )
    return selected


def _auto_wrap_like(pattern: str) -> str:
    """Auto-wrap a SQL ``LIKE`` pattern when no wildcards are present.

    SQL ``LIKE`` matches the literal string when the pattern has no ``%`` or
    ``_``; that surprises users who pass ``--meeting RAN5#111`` and expect
    substring matching. Wrapping such patterns in ``%...%`` makes the common
    case ergonomic while still allowing explicit wildcards when needed.
    """
    if "%" in pattern or "_" in pattern:
        return pattern
    return f"%{pattern}%"


def _tdoc_field(item: TDocWithMeeting, name: str) -> object | None:
    """Resolve a CLI field name against a :class:`TDocWithMeeting` DTO.

    ``meeting_name`` is a top-level attribute on the DTO (computed via
    JOIN); every other field lives on ``item.tdoc``. Centralising the
    routing keeps the print loop free of ``isinstance`` checks and lets
    field names mirror ``dataclass_fields(TDoc)`` + ``"meeting_name"``.
    """
    if name == "meeting_name":
        return item.meeting_name
    return getattr(item.tdoc, name, None)


VALID_FORMATS: tuple[str, ...] = ("table", "json", "markdown")


def _resolve_format(fmt: str | None, default: str = "table") -> str:
    """Resolve ``--format`` against an injected default and reject unknown values.

    ``default`` comes from :attr:`Settings.output.format` so the config
    file (or env var) can change the default output format without code
    changes. The CLI flag, when present, still wins.
    """
    if fmt is None or fmt == "":
        return default
    normalized = fmt.strip().lower()
    if normalized not in VALID_FORMATS:
        valid = ", ".join(VALID_FORMATS)
        raise typer.BadParameter(
            f"Unknown format {fmt!r}. Choose from: {valid}."
        )
    return normalized


# ``tdoc show`` adds ``raw`` to the standard set because it can emit the
# converted .docx markdown (the artefact the parser otherwise consumes).
# Keeping the constant local to this command avoids leaking the option
# onto ``* list`` where it doesn't make sense.
_TDOC_SHOW_FORMATS: tuple[str, ...] = ("table", "json", "markdown", "raw")


def _resolve_tdoc_show_format(fmt: str | None, default: str = "table") -> str:
    """Resolve ``--format`` for ``tdoc show``.

    Mirrors :func:`_resolve_format` but accepts ``"raw"`` as well. The
    default still flows from :attr:`Settings.output.format` so a user
    who configures ``DOC3GPP_OUTPUT__FORMAT=json`` gets JSON from this
    command too.
    """
    if fmt is None or fmt == "":
        return default
    normalized = fmt.strip().lower()
    if normalized not in _TDOC_SHOW_FORMATS:
        valid = ", ".join(_TDOC_SHOW_FORMATS)
        raise typer.BadParameter(
            f"Unknown format {fmt!r}. Choose from: {valid}."
        )
    return normalized


VALID_PURGE_SCOPES: tuple[str, ...] = ("markdown", "zips", "all")


def _resolve_cache_purge_scope(scope: str) -> str:
    """Resolve ``--scope`` for ``cache purge``.

    Normalises whitespace + case and validates against
    :data:`VALID_PURGE_SCOPES`. ``"markdown"`` is the default scope at
    the Typer layer (the cheap artefacts); ``"zips"`` targets the
    3GPP-served blobs alone; ``"all"`` is the original wipe-both
    behaviour. Unknown values raise :class:`typer.BadParameter` so
    Typer can render a clean error and a non-zero exit.
    """
    normalized = scope.strip().lower()
    if normalized not in VALID_PURGE_SCOPES:
        valid = ", ".join(VALID_PURGE_SCOPES)
        raise typer.BadParameter(
            f"Unknown --scope {scope!r}. Choose from: {valid}."
        )
    return normalized


def _open_output(path: str | None) -> tuple[TextIO, bool]:
    """Open ``path`` for writing, or return ``(sys.stdout, False)`` for stdout.

    ``None`` and the literal ``"-"`` both resolve to stdout. The second
    return value tells the caller whether to close the stream afterwards.
    """
    if path is None or path == "-":
        return sys.stdout, False
    return Path(path).open("w", encoding="utf-8", newline=""), True


def _md_cell(value: str) -> str:
    """Escape a markdown table cell.

    Pipes break column alignment inside GitHub-flavored tables, so the
    few values that contain them need a backslash to render correctly.
    """
    return value.replace("|", "\\|")


def _emit_table(rows: list[list[str]], stream: TextIO) -> None:
    for row in rows:
        stream.write("\t".join(row))
        stream.write("\n")


def _emit_json(rows: list[list[str]], stream: TextIO, fields: list[str]) -> None:
    objs = [dict(zip(fields, row)) for row in rows]
    json.dump(objs, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def _emit_markdown(rows: list[list[str]], stream: TextIO, fields: list[str]) -> None:
    stream.write("| " + " | ".join(_md_cell(h) for h in fields) + " |\n")
    stream.write("|" + "|".join(["---"] * len(fields)) + "|\n")
    for row in rows:
        stream.write("| " + " | ".join(_md_cell(c) for c in row) + " |\n")


def _build_cache() -> TDocCache:
    """Construct a :class:`TDocCache` from the active settings.

    Centralises the cache construction so the ``cache status`` and
    ``cache purge`` commands share the exact same root + size-limit
    translation that the ``TDocCrService`` factory uses internally.
    The size limit is converted from megabytes (the ``CacheSettings``
    unit) to bytes (the ``TDocCache`` unit) once, here.
    """
    settings = get_settings()
    return TDocCache(
        root=settings.cache.dir,
        size_limit_bytes=settings.cache.size_limit_mb * 1024 * 1024,
    )


def _fmt_bytes(n: int) -> str:
    """Render a byte count as a short human-readable string.

    Uses simple thresholds so the format stays predictable across
    environments; ``0`` is rendered as ``"unlimited"`` for clarity in
    the ``cache status`` table when the configured ceiling is off.
    """
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.1f} GB"


def _format_cache_status_row(label: str, value: str) -> str:
    """Render a single ``label: value`` line padded for the status table."""
    return f"{label:<12} {value}"


def _emit_cache_status(status: CacheStatus, stream: TextIO) -> None:
    """Write a plain-text ``cache status`` table to ``stream``.

    No ``--format`` flag for this initial cut — the table is short
    enough that markdown / JSON variants are not worth the surface
    area. ``limit_bytes`` of ``0`` renders as ``"unlimited"`` so an
    unset cap is unambiguous.
    """
    stream.write(_format_cache_status_row("file_count:", str(status.file_count)) + "\n")
    stream.write(_format_cache_status_row("total_bytes:", _fmt_bytes(status.total_bytes)) + "\n")
    limit_display = "unlimited" if status.limit_bytes == 0 else _fmt_bytes(status.limit_bytes)
    stream.write(_format_cache_status_row("limit_bytes:", limit_display) + "\n")
    stream.write(_format_cache_status_row("zips:", str(status.zips)) + "\n")
    stream.write(_format_cache_status_row("markdown:", str(status.markdown)) + "\n")


def _truncate_for_display(value: str | None, limit: int = 200) -> str:
    """Truncate a long string for ``tdoc show`` display.

    Long free-text fields (``reason_for_change``,
    ``consequences_if_not_approved``) routinely run to many hundreds
    of characters; the display helper caps them at ``limit`` chars and
    appends an ellipsis so the column layout doesn't blow up.
    """
    if value is None:
        return "-"
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "..."


def _emit_records(
    rows: list[list[str]],
    fields: list[str],
    fmt: str,
    output: str | None,
    *,
    no_records_msg: str,
) -> None:
    """Emit ``rows`` to ``output`` (or stdout) in the chosen format.

    Empty rows are emitted as ``[]`` / header-only in JSON and markdown so
    downstream consumers always see a parseable payload. The friendly
    "no records" message prints only when ``--format table`` is paired
    with stdout — writing an empty table file would just be noise.
    """
    stream, close_after = _open_output(output)
    try:
        if not rows:
            if fmt == "json":
                _emit_json([], stream, fields)
            elif fmt == "markdown":
                _emit_markdown([], stream, fields)
            elif output is None:
                stream.write(no_records_msg + "\n")
            return

        if fmt == "table":
            _emit_table(rows, stream)
        elif fmt == "json":
            _emit_json(rows, stream, fields)
        else:
            _emit_markdown(rows, stream, fields)
    finally:
        if close_after:
            stream.close()


@cache_app.command("status")
def cache_status() -> None:
    """Print cache size, file count, limit, and per-subdir breakdown.

    Read-only: does not trigger eviction even when over the configured
    limit. Use ``doc3gpp cache purge`` to free space.
    """
    cache = _build_cache()
    snapshot = cache.status()
    _emit_cache_status(snapshot, sys.stdout)


@cache_app.command("purge")
def cache_purge(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    scope: str = typer.Option(
        "markdown",
        "--scope",
        help=(
            "Which subtree to purge: 'markdown' (default — only the "
            "rendered markdown sidecars), 'zips' (only the 3GPP-served "
            "zip blobs), or 'all' (both)."
        ),
    ),
) -> None:
    """Delete cached files in the markdown subtree, the zips subtree, or both.

    By default (``--scope markdown``) only the rendered markdown sidecars
    are removed; the 3GPP-served zip blobs (the expensive downloads) are
    preserved. Pass ``--scope zips`` to wipe only the zip subtree or
    ``--scope all`` to wipe both subtrees (the original wipe-everything
    behaviour).

    Prompts for confirmation by default; pass ``--yes`` to skip. The
    prompt can also be disabled globally by setting
    ``cache.purge_confirm = false`` in the active TOML config (it is
    not exposed via environment variable — see
    ``ALLOWED_ENV_VARS`` in ``src/doc3gpp/settings/schema.py``).
    """
    resolved_scope = _resolve_cache_purge_scope(scope)
    settings = get_settings()
    if settings.cache.purge_confirm and not yes:
        prompts = {
            "markdown": "Delete all cached markdown?",
            "zips": "Delete all cached zips?",
            "all": "Delete all cached zips and markdown?",
        }
        typer.confirm(prompts[resolved_scope], abort=True)
    cache = _build_cache()
    if resolved_scope == "all":
        deleted = cache.purge()
        noun = "file" if deleted == 1 else "files"
        typer.echo(f"Deleted {deleted} {noun} from cache.")
    elif resolved_scope == "zips":
        deleted = cache.purge_subdir("zips")
        noun = "zip file" if deleted == 1 else "zip files"
        typer.echo(f"Deleted {deleted} {noun} from cache.")
    else:  # markdown
        deleted = cache.purge_subdir("markdown")
        noun = "markdown file" if deleted == 1 else "markdown files"
        typer.echo(f"Deleted {deleted} {noun} from cache.")


@db_app.command("check")
def db_check() -> None:
    """Validate database connectivity for configured backend."""

    logger.info("Checking database connectivity")
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    settings = get_settings()
    typer.echo(f"Database connection OK: {settings.database_url}")


@db_app.command("init")
def db_init() -> None:
    """Create schema for current backend and seed the TSG reference table.

    Re-running this command is safe: the TSG seed is upsert-based, so existing
    rows are refreshed in place rather than duplicated.
    """

    logger.info("Initializing database schema")
    create_schema()
    tsg_service = build_tsg_service()
    seeded = tsg_service.seed_defaults()
    logger.info("Seeded %s TSG reference records", seeded)
    typer.echo(f"Database schema initialized; seeded {seeded} TSG records")


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Delete the SQLite database file and recreate the schema.

    Destructive: all data is wiped. SQLite URLs only; MySQL/PostgreSQL are
    rejected. Prompts for confirmation unless ``--yes`` is passed. After
    reset the ``tsgs`` reference table is re-seeded.
    """

    settings = get_settings()
    parsed = make_url(settings.database_url)

    if not parsed.drivername.startswith("sqlite"):
        raise typer.BadParameter(
            f"'db reset' only supports SQLite backends "
            f"(configured URL: {settings.database_url}). "
            "Use the backend-native schema reset for MySQL or PostgreSQL."
        )

    db_file: Path | None = None
    if parsed.database and parsed.database != ":memory:":
        db_file = Path(parsed.database)

    if db_file is not None and db_file.exists():
        if not yes:
            typer.confirm(
                f"Delete SQLite database file at {db_file}?",
                abort=True,
            )
        logger.info("Deleting SQLite database file %s", db_file)
        db_file.unlink()
        # Also remove any SQLite journal sidecar files (-wal, -shm, -journal)
        # so a half-written WAL from a previous session does not survive
        # the reset and confuse the new schema.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = db_file.with_name(db_file.name + suffix)
            if sidecar.exists():
                sidecar.unlink()
                logger.debug("Removed SQLite sidecar %s", sidecar)
        typer.echo(f"Deleted {db_file}")
    else:
        typer.echo("No existing SQLite file to delete.")

    # SQLAlchemy cached the engine from the pre-delete file path; clear it
    # so create_schema() opens a fresh connection to the (now empty) file.
    get_engine.cache_clear()

    logger.info("Recreating database schema")
    create_schema()
    tsg_service = build_tsg_service()
    seeded = tsg_service.seed_defaults()
    logger.info("Seeded %s TSG reference records", seeded)
    typer.echo(f"Database reset complete; seeded {seeded} TSG records")


@meeting_app.command("sync")
def meeting_sync(
    tsg: str | None = typer.Option(
        None,
        help=(
            "TSG name for which the 3GPP meeting calendar to sync. "
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass the sync interval skip rule.",
    ),
) -> None:
    """Fetch and store meetings calendar from 3GPP site.

    Valid --tsg values are:
    `R1`, `R2`, `R3`, `R4`, `R5`, `RT`, `RP`,
    `C1`, `C3`, `C4`, `C6`, `CP`,
    `S1`, `S2`, `S3`, `S4`, `S5`, `S6`, `SP`

    When no ``--tsg`` is given, every distinct TSG 
    found in the local meetings table is synced.
    """
    create_schema()
    tsg_service = _ensure_tsg_ready(build_tsg_service())
    service = build_meeting_service()

    if tsg is None:
        tsgs = service.list_distinct_tsgs()
        if not tsgs:
            logger.info("No stored meetings with a TSG found; nothing to sync")
            typer.echo("No stored meetings with a TSG found; nothing to sync.")
            return
        logger.info("Starting meeting sync for %s stored TSG(s): %s", len(tsgs), ", ".join(tsgs))
    else:
        tsgs = [_validate_tsg_short_name(tsg, tsg_service)]
        logger.info("Starting meeting sync for TSG %s", tsgs[0])

    for tsg_short in tsgs:
        if not tsg_service.is_known_short_name(tsg_short):
            logger.warning("Skipping unknown TSG '%s' found in meetings table", tsg_short)
            typer.echo(f"Skipping unknown TSG '{tsg_short}' found in meetings table.")
            continue
        meeting_url = _build_meeting_url(tsg_short)
        outcome = service.sync(meeting_url, tsg=tsg_short, force=force)
        typer.echo(outcome.reason)


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat(sep=" ", timespec="seconds")


@meeting_app.command("list")
def meeting_list(
    limit: int = typer.Option(20, min=1, max=500),
    offset: int = typer.Option(
        0, min=0, help="Number of rows to skip before applying --limit (pagination)."
    ),
    tsg: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter meeting TSG short name (supports % and _)",
    ),
    name: str | None = typer.Option(None, help="SQL LIKE pattern to filter meeting name (supports % and _)") ,
    location: str | None = typer.Option(None, help="SQL LIKE pattern to filter meeting location (supports % and _)") ,
    year: int | None = typer.Option(None, help="Filter meetings by end_date year"),
    tdoc: str | None = typer.Option(
        None,
        help="Find the meeting containing this TDoc (e.g. 'R5-260013'), case-insensitive.",
    ),
    fields: str | None = typer.Option(
        None,
        help="Comma-separated list of fields to include (or 'all' for all fields).",
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: table (default, tab-separated), json, or markdown.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write results to FILE instead of stdout. Pass '-' for stdout.",
    ),
) -> None:
    """List meetings from database with optional filtering and pagination.

    Filter optional flags are combinable; the query is ANDed together:
      --tsg, --name, --location, --year, --tdoc
    See docs/cli.md for full semantics and examples.

    Output fields can be selected with ``--fields``. Defaults are
    ``meeting_id, name, location, start_date, end_date, ftp_url,
    start_doc, end_doc``; ``title`` and ``tsg`` are also available.

    """
    allowed_fields = [
        "meeting_id",
        "name",
        "title",
        "location",
        "start_date",
        "end_date",
        "ftp_url",
        "start_doc",
        "end_doc",
        "tsg",
    ]

    settings = get_settings()
    default_fields = settings.output.fields.meeting

    out_fields = _parse_field_selection(fields, allowed_fields, default_fields)
    fmt = _resolve_format(fmt, default=settings.output.format)

    # Validate --tdoc against the CR-shape regex before the database is
    # touched so the operator sees a clear error at the CLI boundary.
    parsed_tdoc_id: tuple[str, int] | None = None
    if tdoc is not None:
        try:
            parsed_tdoc_id = parse_tdoc_id(tdoc)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

    # Canonicalise the TSG pattern to upper case so it matches the
    # upper-case values stored by `meeting sync --tsg`.
    if tsg is not None:
        tsg = tsg.upper()

    logger.info(
        "Listing meetings limit=%s offset=%s tsg=%s name=%s location=%s "
        "year=%s tdoc=%s",
        limit, offset, tsg, name, location, year, tdoc,
    )
    service = build_meeting_service()
    trigger_auto_sync(
        auto_sync_enabled=settings.sync.auto_sync,
        meeting_service=service,
        tdoc_sync_coordinator=build_tdoc_sync_coordinator(),
        tsg=tsg,
        tdoc=tdoc,
    )
    records = service.list_recent(
        limit=limit,
        offset=offset,
        tsg=tsg,
        name_like=name,
        location_like=location,
        year=year,
        tdoc_id=parsed_tdoc_id,
    )

    rows: list[list[str]] = []
    for item in records:
        assert isinstance(item, Meeting)
        vals: list[str] = []
        for f in out_fields:
            v = getattr(item, f, None)
            if v is None:
                vals.append("-")
                continue

            if f in ("start_date", "end_date"):
                vals.append(v.isoformat())
            else:
                vals.append(str(v))

        rows.append(vals)

    _emit_records(
        rows=rows,
        fields=out_fields,
        fmt=fmt,
        output=output,
        no_records_msg="No meetings found",
    )


def _echo_bulk_sync_outcome(outcome: BulkSyncOutcome) -> None:
    """Render a bulk TDoc sync result and exit non-zero if every meeting failed."""
    if outcome.total == 0:
        typer.echo("No stored meetings with TDocs found; nothing to sync.")
        return

    typer.echo(f"TDoc bulk sync: {outcome.total} meeting(s) processed")
    typer.echo(f"  Synced:  {outcome.synced_count}")
    typer.echo(f"  Skipped: {outcome.skipped_count}")
    typer.echo(f"  Failed:  {outcome.failed_count}")

    if outcome.failures:
        typer.echo("Failed meetings:")
        for failure in outcome.failures:
            typer.echo(
                f"  meeting_id={failure.meeting_id}  "
                f"{failure.error}  {failure.reason}"
            )

    if outcome.failed_count == outcome.total:
        raise typer.Exit(code=1)


@tdoc_app.command("sync")
def tdoc_sync(
    meeting_id: int | None = typer.Option(
        None, help="Meeting ID from the meeting database (see `doc3gpp meeting sync`) to resolve the FTP URL"
    ),
    meeting: str | None = typer.Option(
        None,
        help="Meeting name from the meeting database (see `doc3gpp meeting sync`) to resolve the FTP URL",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass the sync interval skip rules (closed window, interval, FTP mtime).",
    ),
) -> None:
    """Fetch TDocs List for a 3GPP meeting and store them in DB.

    When neither ``--meeting-id`` nor ``--meeting`` is given, every
    distinct ``meeting_id`` currently stored in the ``tdocs`` table is
    synced individually. The existing per-meeting guard rules still
    apply, and ``--force`` bypasses them for every meeting in the run.
    """

    coordinator = build_tdoc_sync_coordinator()

    if meeting_id is not None and meeting is not None:
        raise typer.BadParameter("Specify exactly one of --meeting-id or --meeting.")

    if meeting_id is None and meeting is None:
        logger.info("Starting bulk TDoc sync for all tracked meetings")
        outcome = coordinator.sync_all_tracked_meetings(force=force)
        _echo_bulk_sync_outcome(outcome)
        return

    try:
        if meeting_id is not None:
            logger.info("Starting TDoc sync for meeting ID %s", meeting_id)
            outcome = coordinator.sync_for_meeting_id(meeting_id, force=force)
        else:
            logger.info("Starting TDoc sync for meeting name %s", meeting)
            outcome = coordinator.sync_for_meeting_name(meeting, force=force)
    except MeetingNotFoundError as exc:
        logger.error("Meeting not found: %s", exc)
        raise typer.BadParameter(str(exc)) from None
    except MeetingMissingFtpUrlError as exc:
        logger.error("Meeting has no FTP URL stored: %s", exc)
        raise typer.BadParameter(str(exc)) from None

    typer.echo(outcome.reason)


@tdoc_app.command("list")
def tdoc_list(
    limit: int = typer.Option(20, min=1, max=500),
    offset: int = typer.Option(
        0, min=0, help="Number of rows to skip before applying --limit (pagination)."
    ),
    tdoc: str | None = typer.Option(
        None,
        "--tdoc",
        help="SQL LIKE pattern on tdoc_id (or null/not-null/!pattern).",
    ),
    meeting: str | None = typer.Option(
        None,
        "--meeting",
        help="SQL LIKE pattern on meeting name (or null/not-null/!pattern).",
    ),
    meeting_id: int | None = typer.Option(
        None,
        "--meeting-id",
        help="Exact numeric meeting ID; combinable with every filter.",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="SQL LIKE pattern on source (or null/not-null/!pattern).",
    ),
    spec: str | None = typer.Option(
        None,
        "--spec",
        help="SQL LIKE pattern on spec (or null/not-null/!pattern).",
    ),
    wi: str | None = typer.Option(
        None,
        "--wi",
        help="SQL LIKE pattern on related_wis (or null/not-null/!pattern).",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        help="SQL LIKE pattern on title (or null/not-null/!pattern).",
    ),
    cr_cat: str | None = typer.Option(
        None,
        "--cr-cat",
        help="SQL LIKE pattern on cr_cat (or null/not-null/!pattern).",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="SQL LIKE pattern on status (or null/not-null/!pattern).",
    ),
    type: str | None = typer.Option(
        None,
        "--type",
        help="SQL LIKE pattern on type (or null/not-null/!pattern).",
    ),
    revision_of: str | None = typer.Option(
        None,
        "--revision-of",
        help="SQL LIKE pattern on is_revision_of (or null/not-null/!pattern).",
    ),
    revised_to: str | None = typer.Option(
        None,
        "--revised-to",
        help="SQL LIKE pattern on revised_to (or null/not-null/!pattern).",
    ),
    ftp_url: str | None = typer.Option(
        None,
        "--ftp-url",
        help="SQL LIKE pattern on ftp_url (or null/not-null/!pattern).",
    ),
    release: str | None = typer.Option(
        None,
        "--release",
        help="SQL LIKE pattern on release (or null/not-null/!pattern).",
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        help="SQL LIKE pattern on version (or null/not-null/!pattern).",
    ),
    cr_num: str | None = typer.Option(
        None,
        "--cr-num",
        help="SQL LIKE pattern on cr_num (or null/not-null/!pattern).",
    ),
    cr_pack: str | None = typer.Option(
        None,
        "--cr-pack",
        help="SQL LIKE pattern on cr_pack (or null/not-null/!pattern).",
    ),
    uploaded_date: str | None = typer.Option(
        None,
        "--uploaded-date",
        help="Filter on uploaded_date: null/not-null or '<op> YYYY-MM-DD'.",
    ),
    fields: str | None = typer.Option(
        None,
        "--fields",
        help="Comma-separated fields to include in the output, or 'all'.",
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: table|json|markdown.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write results to PATH instead of stdout. Pass '-' for stdout.",
    ),
) -> None:
    """List stored TDocs from the database.

    Filter optional flags are combinable; the query is ANDed together:
      --tdoc, --meeting-id, --meeting, --status, --cr-cat, --spec, --wi,
      --revision-of, --revised-to, --title, --ftp-url, --release, --version,
      --cr-num, --cr-pack, --source, --type, --uploaded-date
    See docs/cli.md for full semantics and examples.
    """

    # Reject malformed --uploaded-date before the database is touched so the
    # operator sees a clear error at the CLI boundary. Mirrors the guard in
    # ``tdoc parse``.
    if uploaded_date is not None:
        try:
            validate_date_filter(uploaded_date)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

    # ``meeting_name`` is a top-level attribute on ``TDocWithMeeting``; the
    # rest live on ``TDocWithMeeting.tdoc``.
    allowed_fields = [f.name for f in dataclass_fields(TDoc)] + ["meeting_name"]
    settings = get_settings()
    default_fields = settings.output.fields.tdoc

    out_fields = _parse_field_selection(fields, allowed_fields, default_fields)
    fmt = _resolve_format(fmt, default=settings.output.format)

    logger.info(
        "Listing %s recent TDocs (offset=%s) with filters tdoc=%s meeting=%s "
        "meeting_id=%s source=%s spec=%s wi=%s title=%s cr_cat=%s status=%s "
        "type=%s revision_of=%s revised_to=%s ftp_url=%s release=%s version=%s "
        "cr_num=%s cr_pack=%s uploaded_date=%s",
        limit,
        offset,
        tdoc,
        meeting,
        meeting_id,
        source,
        spec,
        wi,
        title,
        cr_cat,
        status,
        type,
        revision_of,
        revised_to,
        ftp_url,
        release,
        version,
        cr_num,
        cr_pack,
        uploaded_date,
    )

    service = build_tdoc_service()
    trigger_auto_sync(
        auto_sync_enabled=settings.sync.auto_sync,
        meeting_service=build_meeting_service(),
        tdoc_sync_coordinator=build_tdoc_sync_coordinator(),
        meeting_id=meeting_id,
        meeting_name=meeting,
        tdoc=tdoc,
    )
    records = service.list_recent_with_meeting(
        limit=limit,
        offset=offset,
        tdoc_id=tdoc,
        meeting_like=_auto_wrap_like(meeting) if meeting else None,
        meeting_id=meeting_id,
        # Rich-filter surface — supports `null` / `not-null` / LIKE.
        source=source,
        spec=spec,
        wi=wi,
        title=title,
        cr_cat=cr_cat,
        status=status,
        tdoc_type=type,
        revision_of=revision_of,
        revised_to=revised_to,
        ftp_url=ftp_url,
        release=release,
        version=version,
        cr_num=cr_num,
        cr_pack=cr_pack,
        uploaded_date=uploaded_date,
    )

    rows: list[list[str]] = []
    for item in records:
        assert isinstance(item, TDocWithMeeting)
        vals: list[str] = []
        for f in out_fields:
            v = _tdoc_field(item, f)
            if v is None:
                vals.append("-")
                continue

            if f in ("reservation_date", "uploaded_date") and v is not None:
                vals.append(v.isoformat())
            else:
                vals.append(str(v))

        rows.append(vals)

    _emit_records(
        rows=rows,
        fields=out_fields,
        fmt=fmt,
        output=output,
        no_records_msg="No TDocs found",
    )


def _extract_failure_hints() -> str:
    """Return the friendly error-name list for the ``tdoc parse`` summary.

    Used in the exception handler at the batch level so an
    operator-facing error message lists the per-item error categories
    that ``TDocCrService.extract_many`` catches internally (rather than
    letting one obscure the others). Kept as a module-local helper so
    the doctring lives next to its only caller.
    """
    return (
        "TDocZipDownloadError, PythonDocxNotInstalledError, "
        "TDocTypeUnsupportedError, TDocNotFoundError, CRHeaderMissingError"
    )


def _normalise_cli_tdoc_id(raw: str) -> str:
    """Return the canonical form of ``raw`` for a CLI ``--tdoc`` argument.

    The database stores TDoc IDs in their canonical case (``R5s260213``);
    a CLI user typing ``r5s260213`` would otherwise fail the PK lookup.
    For CR-shape IDs :func:`canonicalise_tdoc_id` returns the canonical
    form; non-CR shapes (LS / DRAFT / etc.) have no canonical mapping,
    so the input is returned whitespace-stripped and the user is on the
    hook for typing it exactly as the DB has it.
    """
    canonical = canonicalise_tdoc_id(raw)
    return canonical if canonical is not None else raw.strip()


def _normalise_cli_ftp_url(raw: str) -> str:
    """Normalise a CLI ``--ftp-url`` argument for DB lookup.

    Accepts both full URLs (``https://www.3gpp.org/ftp/TSG_RAN/...``)
    and bare relative paths (``TSG_RAN/...``); delegates to
    :func:`normalize_ftp_path` so both forms collapse to the same
    canonical key the database stores. Empty input is rejected so
    the caller never silently no-ops on whitespace.
    """
    if not raw or not raw.strip():
        raise typer.BadParameter("Empty --ftp-url argument.")
    return normalize_ftp_path(raw)


def _resolve_url_batch_depth(
    *,
    recursive: bool,
    max_depth: int | None,
    settings: "Settings",
) -> int:
    """Return the effective recursion depth for a URL-folder batch parse."""
    if max_depth is not None:
        return max_depth
    if recursive:
        return settings.tdoc_parse.max_ftp_depth
    return 0


@tdoc_app.command("parse")
def tdoc_parse(
    tdoc: str | None = typer.Option(
        None,
        "--tdoc",
        help="SQL LIKE pattern on tdoc_id (or null/not-null/!pattern).",
    ),
    meeting_id: int | None = typer.Option(
        None,
        "--meeting-id",
        help="Exact numeric meeting ID; combinable with every filter.",
    ),
    meeting: str | None = typer.Option(
        None,
        "--meeting",
        help="SQL LIKE pattern on meeting name (or null/not-null/!pattern).",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="SQL LIKE pattern on status (or null/not-null/!pattern).",
    ),
    cr_cat: str | None = typer.Option(
        None,
        "--cr-cat",
        help="SQL LIKE pattern on cr_cat (or null/not-null/!pattern).",
    ),
    spec: str | None = typer.Option(
        None,
        "--spec",
        help="SQL LIKE pattern on spec (or null/not-null/!pattern).",
    ),
    wi: str | None = typer.Option(
        None,
        "--wi",
        help="SQL LIKE pattern on related_wis (or null/not-null/!pattern).",
    ),
    revision_of: str | None = typer.Option(
        None,
        "--revision-of",
        help="SQL LIKE pattern on is_revision_of (or null/not-null/!pattern).",
    ),
    revised_to: str | None = typer.Option(
        None,
        "--revised-to",
        help="SQL LIKE pattern on revised_to (or null/not-null/!pattern).",
    ),
    title_filter: str | None = typer.Option(
        None,
        "--title",
        help="SQL LIKE pattern on title (or null/not-null/!pattern).",
    ),
    ftp_url: str | None = typer.Option(
        None,
        "--ftp-url",
        help="SQL LIKE pattern on ftp_url (or null/not-null/!pattern).",
    ),
    release: str | None = typer.Option(
        None,
        "--release",
        help="SQL LIKE pattern on release (or null/not-null/!pattern).",
    ),
    version: str | None = typer.Option(
        None,
        "--version",
        help="SQL LIKE pattern on version (or null/not-null/!pattern).",
    ),
    cr_num: str | None = typer.Option(
        None,
        "--cr-num",
        help="SQL LIKE pattern on cr_num (or null/not-null/!pattern).",
    ),
    cr_pack: str | None = typer.Option(
        None,
        "--cr-pack",
        help="SQL LIKE pattern on cr_pack (or null/not-null/!pattern).",
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help="SQL LIKE pattern on source (or null/not-null/!pattern).",
    ),
    tdoc_type: str | None = typer.Option(
        None,
        "--type",
        help="SQL LIKE pattern on type (defaults to 'CR'; null/not-null/!pattern).",
    ),
    uploaded_date: str | None = typer.Option(
        None,
        "--uploaded-date",
        help="Filter on uploaded_date: null/not-null or '<op> YYYY-MM-DD'.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "DB parse: re-fetch and re-parse every match. "
            "Local batch parse: overwrite existing output files."
        ),
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Forward full=True to the parser (parser to extract info beyond cover page, e.g. TTCN corrections).",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the batch parse confirmation prompt.",
    ),
    from_url: str | None = typer.Option(
        None,
        "--from-url",
        help="Download and parse a URL (3GPP file or folder; folder batch writes cache/DB).",
    ),
    from_path: str | None = typer.Option(
        None,
        "--from-path",
        help="Parse a local .docx/.zip file, or every .docx/.zip under a directory tree.",
    ),
    recursive: bool = typer.Option(
        False,
        "--recursive",
        "-r",
        help="Descend into subfolders for --from-path or --from-url folder batch.",
    ),
    max_depth: int | None = typer.Option(
        None,
        "--max-depth",
        min=0,
        max=10,
        help="Override tdoc_parse.max_ftp_depth for --from-url folder batch (implies --recursive).",
    ),
    direct_format: str | None = typer.Option(
        None,
        "--format",
        help="Output format for direct/local-batch mode: table|json|markdown|raw.",
    ),
    direct_output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write output to PATH (file when source is a file, folder in batch mode). Default stdout",
    ),
) -> None:
    """Parse Tdoc from the DB table, online file, a local file, or a folder tree.

    The command has five modes; See docs/cli.md for full semantics and examples.

    General TDoc parse output options:
      --force (re-fetch/re-parse/over-write every match), 
      --yes (skip confirmation prompt),
      --format table|json|markdown|raw 
        (raw output covered markdown of the Tdoc, without extracting fields),
      --full (extracting full Tdoc, otherwise only extracting cover page fields)

    DB TDoc parse filters options (for selecting which TDocs to parse):
      --tdoc, --meeting-id, --meeting, --status, --cr-cat, --spec, --wi,
      --revision-of, --revised-to, --title, --ftp-url, --release, --version,
      --cr-num, --cr-pack, --source, --type, --uploaded-date

    Local TDoc parse:
      --from-path PATH [--output PATH] [--format table|json|markdown|raw] [--full]
      (PATH may be a single .docx/.zip file or a directory of them)

    Online TDoc parse:
      --from-url URL [--output PATH] [--format table|json|markdown|raw] [--full]
      (URL may be a single 3GPP .docx/.zip file or a 3GPP FTP folder; folder
      batch scans .docx/.zip files, recurses with --recursive, and always
      writes cache/DB for matching TDoc ids)
    """
    if from_url is not None or from_path is not None:
        _validate_source_mode_flags(from_url, from_path)
        _warn_on_ignored_filter_flags(
            tdoc=tdoc,
            meeting_id=meeting_id,
            meeting=meeting,
            status=status,
            cr_cat=cr_cat,
            spec=spec,
            wi=wi,
            revision_of=revision_of,
            revised_to=revised_to,
            title=title_filter,
            ftp_url=ftp_url,
            release=release,
            version=version,
            cr_num=cr_num,
            cr_pack=cr_pack,
            source=source,
            tdoc_type=tdoc_type,
            uploaded_date=uploaded_date,
            force=force,
            yes=yes,
            from_path=from_path,
            from_path_is_file=Path(from_path).is_file() if from_path else False,
        )
        if from_path is not None:
            input_path = Path(from_path)
            if not input_path.exists():
                raise typer.BadParameter(f"--from-path does not exist: {from_path}")
            if input_path.is_file():
                _tdoc_parse_direct(
                    from_path=str(input_path),
                    from_url=None,
                    fmt=direct_format,
                    output=direct_output,
                    full=full,
                )
            elif input_path.is_dir():
                if direct_output is None:
                    raise typer.BadParameter(
                        "--output is required when --from-path is a directory."
                    )
                _tdoc_parse_local_batch(
                    from_path=str(input_path),
                    output=direct_output,
                    fmt=direct_format,
                    recursive=recursive,
                    force=force,
                    full=full,
                )
            else:
                raise typer.BadParameter(
                    f"--from-path is neither a file nor a directory: {from_path}"
                )
        else:
            effective_depth = _resolve_url_batch_depth(
                recursive=recursive,
                max_depth=max_depth,
                settings=get_settings(),
            )
            tdoc_service = build_tdoc_cr_service()
            if is_3gpp_ftp_url(from_url):
                candidates = collect_tdoc_candidates_for_url(
                    from_url,
                    tdoc_service=tdoc_service,
                    max_depth=effective_depth,
                )
                if candidates:
                    trigger_auto_sync(
                        auto_sync_enabled=get_settings().sync.auto_sync,
                        meeting_service=build_meeting_service(),
                        tdoc_sync_coordinator=build_tdoc_sync_coordinator(),
                        tdoc_ids=candidates,
                    )
            if not is_3gpp_ftp_url(from_url) or _looks_like_3gpp_file_url(from_url):
                _tdoc_parse_direct(
                    from_path=None,
                    from_url=from_url,
                    fmt=direct_format,
                    output=direct_output,
                    full=full,
                )
            elif _looks_like_3gpp_folder_url(from_url):
                _tdoc_parse_url_batch(
                    from_url=from_url,
                    output=direct_output,
                    fmt=direct_format,
                    max_depth=effective_depth,
                    force=force,
                    full=full,
                )
            else:
                try:
                    batch = tdoc_service.extract_from_url_batch(
                        from_url,
                        max_depth=effective_depth,
                        force=force,
                        full=full,
                    )
                except NotAFolderError:
                    _tdoc_parse_direct(
                        from_path=None,
                        from_url=from_url,
                        fmt=direct_format,
                        output=direct_output,
                        full=full,
                    )
                else:
                    _emit_url_batch_results(
                        batch=batch,
                        root_url=from_url,
                        output=direct_output,
                        fmt=direct_format,
                    )
        return

    trigger_auto_sync(
        auto_sync_enabled=get_settings().sync.auto_sync,
        meeting_service=build_meeting_service(),
        tdoc_sync_coordinator=build_tdoc_sync_coordinator(),
        meeting_id=meeting_id,
        meeting_name=meeting,
        tdoc=tdoc,
    )

    filter_args: dict[str, object] = {
        "tdoc": tdoc,
        "meeting_id": meeting_id,
        "meeting": meeting,
        "status": status,
        "cr_cat": cr_cat,
        "spec": spec,
        "wi": wi,
        "revision_of": revision_of,
        "revised_to": revised_to,
        "title": title_filter,
        "ftp_url": ftp_url,
        "release": release,
        "version": version,
        "cr_num": cr_num,
        "cr_pack": cr_pack,
        "source": source,
        "tdoc_type": tdoc_type,
        "uploaded_date": uploaded_date,
    }
    if not _any_filter_set(filter_args):
        raise typer.BadParameter(
            "Specify at least one filter (--tdoc, --meeting-id, --meeting, "
            "--status, --cr-cat, --spec, --wi, --revision-of, --revised-to, "
            "--title, --ftp-url, --release, --version, --cr-num, --cr-pack, "
            "--source, --type, --uploaded-date)."
        )

    if uploaded_date is not None:
        try:
            validate_date_filter(uploaded_date)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

    if meeting_id is not None:
        looked_up = build_meeting_service().get_by_id(meeting_id)
        if looked_up is None:
            raise typer.BadParameter(
                f"Unknown meeting_id {meeting_id}. "
                f"Run 'doc3gpp meeting list' to see stored meetings."
            )

    max_batch = get_settings().tdoc_parse.max_batch
    tdoc_repo = build_tdoc_repository()
    normalised_tdoc = _normalise_cli_tdoc_id(tdoc) if tdoc else None
    list_kwargs: dict[str, object] = {
        "tdoc_id": normalised_tdoc,
        "meeting_like": meeting,
        "meeting_id": meeting_id,
        "tdoc_type": tdoc_type or "CR",
        "status": status,
        "cr_cat": cr_cat,
        "spec": spec,
        "wi": wi,
        "revision_of": revision_of,
        "revised_to": revised_to,
        "title": title_filter,
        "ftp_url": ftp_url,
        "release": release,
        "version": version,
        "cr_num": cr_num,
        "cr_pack": cr_pack,
        "source": source,
        "uploaded_date": uploaded_date,
    }
    matches = tdoc_repo.list_with_meeting(
        limit=max_batch,
        offset=0,
        exclude_parsed=not force,
        **list_kwargs,
    )
    if not matches:
        if not force:
            # Normal mode dropped parsed rows before the limit; an empty
            # pending result may still mean "every raw match is already
            # parsed". Probe the raw set with limit=1 to disambiguate.
            raw_exists = tdoc_repo.list_with_meeting(
                limit=1, offset=0, exclude_parsed=False, **list_kwargs,
            )
            if raw_exists:
                typer.echo("Nothing to extract — every match is already parsed.")
                raise typer.Exit(code=0)
        typer.echo("No TDoc matched the provided filters.")
        raise typer.Exit(code=1)

    columns = _BASE_PARSE_COLUMNS + _active_extra_columns(filter_args)
    cr_repo = build_tdoc_cr_repository()
    if force:
        # Force mode: SQL returns every match, so we probe parsed status
        # per id to feed the reparse / newly-parsed summary math and to
        # render the "Already parsed" preview group.
        parsed_ids = {
            m.tdoc.tdoc_id for m in matches if cr_repo.get(m.tdoc.tdoc_id)
        }
        already_parsed = [m for m in matches if m.tdoc.tdoc_id in parsed_ids]
        to_parse = list(matches)
    else:
        # Normal mode: SQL excluded already-parsed rows before the limit,
        # so every returned row is guaranteed pending. No N+1 lookups.
        parsed_ids = set()
        already_parsed = []
        to_parse = list(matches)

    truncated = len(matches) == max_batch
    if truncated:
        typer.echo(
            f"Warning: {max_batch} TDocs matched but max_batch={max_batch} "
            f"may have truncated the result; the repository returned the "
            f"first {max_batch} only.\n"
            f"  - Raise DOC3GPP_TDOC_PARSE__MAX_BATCH (or "
            f"[tdoc_parse] max_batch in TOML) to ingest them all at once.\n"
            f"  - Re-run the same command (without --force) to continue "
            f"with the remaining TDocs."
        )

    _print_parse_group("To parse", to_parse, columns)
    if already_parsed:
        suffix = " (with --force, these will be re-extracted)" if force else ""
        _print_parse_group(
            f"Already parsed in tdoc_cr_details{suffix}",
            already_parsed,
            columns,
        )

    if not to_parse:
        typer.echo("Nothing to extract — every match is already parsed.")
        raise typer.Exit(code=0)

    if not yes:
        proceed = typer.confirm(
            f"Extract {len(to_parse)} TDoc(s)?", default=False,
        )
        if not proceed:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)

    tdoc_ids = [m.tdoc.tdoc_id for m in to_parse]
    dispatched_set = set(tdoc_ids)
    logger.info(
        "Starting TDoc parse for %d id(s) (force=%s, full=%s)",
        len(tdoc_ids), force, full,
    )
    service = build_tdoc_cr_service()
    try:
        batch = service.extract_many(tdoc_ids, force=force, full=full)
    except PythonDocxNotInstalledError as exc:
        typer.echo(
            "python-docx is not installed; install with `pip install doc3gpp[extract]`.",
            err=True,
        )
        typer.echo(f"hint: {exc}", err=True)
        raise typer.Exit(code=1) from None

    failures: list[str] = []
    for raw_id in tdoc_ids:
        normalised = raw_id.strip()
        if normalised in batch.successes:
            result = batch.successes[normalised]
            typer.echo(
                f"{normalised}: spec={result.details.spec} "
                f"cr_num={result.details.cr_num} "
                f"title={result.details.title}"
            )
        elif normalised in batch.failures:
            typer.echo(f"{normalised}: FAILED - {batch.failures[normalised]}")
            failures.append(normalised)
        else:
            typer.echo(f"{normalised}: FAILED - extract error (no diagnostic)")
            failures.append(normalised)

    success_set = set(batch.successes.keys())
    skipped = len(parsed_ids - dispatched_set)
    re_parsed = len(parsed_ids & success_set)
    newly_parsed = len(success_set - parsed_ids)
    typer.echo("---")
    typer.echo(f"Skipped (already parsed before this run): {skipped}")
    typer.echo(f"Re-parsed (with --force):                  {re_parsed}")
    typer.echo(f"Newly parsed:                              {newly_parsed}")
    typer.echo(f"Failures:                                  {len(failures)}")
    if truncated:
        typer.echo(
            f"Remaining (truncated by max_batch={max_batch}): "
            f"at least 1 — re-run the same command (without --force) "
            f"to continue."
        )
    if not tdoc_ids or newly_parsed + re_parsed == 0:
        raise typer.Exit(code=1)


def _any_filter_set(filter_args: dict[str, object]) -> bool:
    """Return ``True`` when any of the named filter arguments is non-empty.

    Used to enforce "specify at least one filter" before any DB call.
    A filter is considered "set" when its value is not ``None`` and
    not the empty string (Typer treats ``--flag ""`` as the same as
    no flag at all).
    """
    return any(
        value is not None and value != ""
        for value in filter_args.values()
    )


# ---------------------------------------------------------------------------
# Direct-mode helpers (tdoc parse --from-path / --from-url)
# ---------------------------------------------------------------------------


# Output formats accepted in direct mode. The literal is wider than
# ``settings.schema.OutputFormat`` because direct mode adds ``raw``;
# CSV / ``table`` is the default per the plan's D7 decision.
DIRECT_FORMATS: tuple[str, ...] = ("table", "json", "markdown", "raw")


_DIRECT_PARSE_FIELDS: tuple[str, ...] = (
    "tdoc_id",
    "spec",
    "cr_num",
    "rev",
    "version",
    "title",
    "source",
    "tsg",
    "related_wis",
    "date",
    "cr_cat",
    "release",
    "reason_for_change",
    "consequences_if_not_approved",
    "clauses_affected",
    "other_comments",
    "revision_history",
    "extracted_tdoc_id",
    "ftp_url",
)


def _validate_source_mode_flags(
    from_url: str | None,
    from_path: str | None,
) -> None:
    """Enforce mutual exclusivity of ``--from-url`` and ``--from-path``.

    Raises:
        typer.BadParameter: more than one source flag is non-``None``.
    """
    sources = [
        ("--from-url", from_url),
        ("--from-path", from_path),
    ]
    set_sources = [name for name, value in sources if value is not None]
    if len(set_sources) > 1:
        names = ", ".join(set_sources)
        raise typer.BadParameter(
            f"{names} are mutually exclusive; specify exactly one source."
        )


def _warn_on_ignored_filter_flags(
    *,
    tdoc: str | None,
    meeting_id: int | None,
    meeting: str | None,
    status: str | None,
    cr_cat: str | None,
    spec: str | None,
    wi: str | None,
    revision_of: str | None,
    revised_to: str | None,
    title: str | None,
    ftp_url: str | None,
    release: str | None,
    version: str | None,
    cr_num: str | None,
    cr_pack: str | None,
    source: str | None,
    tdoc_type: str | None,
    uploaded_date: str | None,
    force: bool,
    yes: bool,
    from_path: str | None,
    from_path_is_file: bool,
) -> None:
    """Print a stderr warning when filter flags are set together with a direct/local-batch flag.

    Filter flags are silently ignored in direct/local-batch mode (no
    error, just a warning) so existing scripts that pass them continue
    to parse. ``--yes`` is rejected in both modes because there is no
    DB batch to confirm. ``--force`` is rejected in single-file
    direct mode; in local-batch mode it means "overwrite existing
    output files" and is therefore allowed.
    """
    if from_path is not None and from_path_is_file and force:
        raise typer.BadParameter(
            "--force is not applicable when --from-path points to a single file; "
            "remove --force or point --from-path to a directory."
        )
    if yes:
        raise typer.BadParameter(
            "--yes is not applicable in --from-url / --from-path mode; "
            "remove --yes or use the filter path."
        )

    ignored: list[str] = []
    for name, value in (
        ("--tdoc", tdoc),
        ("--meeting-id", meeting_id),
        ("--meeting", meeting),
        ("--status", status),
        ("--cr-cat", cr_cat),
        ("--spec", spec),
        ("--wi", wi),
        ("--revision-of", revision_of),
        ("--revised-to", revised_to),
        ("--title", title),
        ("--ftp-url", ftp_url),
        ("--release", release),
        ("--version", version),
        ("--cr-num", cr_num),
        ("--cr-pack", cr_pack),
        ("--source", source),
        ("--type", tdoc_type),
        ("--uploaded-date", uploaded_date),
    ):
        if value is not None and value != "":
            ignored.append(name)
    if ignored:
        if from_path is None:
            mode_label = "direct-parse mode"
        elif from_path_is_file:
            mode_label = "direct-parse mode"
        else:
            mode_label = "local-batch mode"
        typer.echo(
            f"warning: ignoring filter flag(s) in {mode_label}: {', '.join(ignored)}",
            err=True,
        )


def _resolve_direct_format(fmt: str | None) -> str:
    """Resolve ``--format`` for direct mode, defaulting to ``"table"``."""
    if fmt is None or fmt == "":
        return "table"
    normalised = fmt.strip().lower()
    if normalised not in DIRECT_FORMATS:
        valid = ", ".join(DIRECT_FORMATS)
        raise typer.BadParameter(
            f"Unknown --format {fmt!r} for direct mode. Choose from: {valid}."
        )
    return normalised


def _tdoc_parse_direct(
    *,
    from_path: str | None,
    from_url: str | None,
    fmt: str | None,
    output: str | None,
    full: bool,
) -> None:
    """Dispatch a single ``--from-path`` (file) or ``--from-url`` call.

    Resolves the format, runs the appropriate service method, prints
    the FK-miss warning when applicable, and emits the result in the
    chosen format. Exit code is 0 on success (per the plan's D9
    decision), 1 on every other failure (file missing, bad URL,
    network error, parser error).
    """
    from doc3gpp.parsers.cr_parser import CRHeaderMissingError
    from doc3gpp.parsers.direct_extractor import (
        build_missing_tdoc_id_warning_message,
        build_no_pattern_warning_message,
    )
    from doc3gpp.scraping.tdoc_zip_source import TDocZipDownloadError

    resolved_format = _resolve_direct_format(fmt)
    service = build_tdoc_cr_service()
    raw = from_path if from_path is not None else from_url
    assert raw is not None

    try:
        if from_path is not None:
            payload = Path(from_path).read_bytes()
            result = service.extract_from_bytes(
                payload, from_path, force=False, full=full,
            )
        else:
            result = service.extract_from_url(
                raw, force=False, full=full,
            )
    except FileNotFoundError as exc:
        typer.echo(f"FAILED - FileNotFoundError: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except IsADirectoryError as exc:
        typer.echo(f"FAILED - IsADirectoryError: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except PermissionError as exc:
        typer.echo(f"FAILED - PermissionError: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except TDocZipDownloadError as exc:
        typer.echo(f"FAILED - TDocZipDownloadError: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except CRHeaderMissingError as exc:
        typer.echo(f"FAILED - CRHeaderMissingError: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except ValueError as exc:
        typer.echo(f"FAILED - ValueError: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"FAILED - {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if (
        result.source_kind == "url-3gpp"
        and result.tdoc_id is not None
        and not result.tdoc_id_in_tdocs
    ):
        typer.echo(
            build_missing_tdoc_id_warning_message(result.tdoc_id, raw),
            err=True,
        )
    elif result.source_kind == "url-3gpp" and result.tdoc_id is None:
        typer.echo(build_no_pattern_warning_message(raw), err=True)

    if resolved_format == "raw":
        _emit_record_raw(result.markdown, output)
    elif result.details is None:
        typer.echo(
            f"FAILED - ValueError: --format {resolved_format!r} requires parsed fields; "
            "the parser did not run for this source.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    else:
        _emit_record(result.details, resolved_format, output)


def _emit_record(
    record: TDocCRDetails,
    fmt: str,
    output: str | None,
) -> None:
    """Dispatch to the table / markdown / json emitter for a single parsed record."""
    if fmt == "table":
        _emit_record_table(record, output)
    elif fmt == "markdown":
        _emit_record_markdown(record, output)
    elif fmt == "json":
        _emit_record_json(record, output)
    else:
        raise typer.BadParameter(f"Unsupported direct-parse format: {fmt!r}")


def _emit_record_table(record: TDocCRDetails, output: str | None) -> None:
    """Emit a single record as a tab-separated header + data row."""
    stream, close_after = _open_output(output)
    try:
        stream.write("\t".join(_DIRECT_PARSE_FIELDS))
        stream.write("\n")
        stream.write("\t".join(_serialise_cell(record, name) for name in _DIRECT_PARSE_FIELDS))
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_markdown(record: TDocCRDetails, output: str | None) -> None:
    """Emit a single record as a one-row GFM table."""
    stream, close_after = _open_output(output)
    try:
        stream.write("| " + " | ".join(_md_cell(h) for h in _DIRECT_PARSE_FIELDS) + " |\n")
        stream.write("|" + "|".join(["---"] * len(_DIRECT_PARSE_FIELDS)) + "|\n")
        cells = [_md_cell(_serialise_cell(record, name)) for name in _DIRECT_PARSE_FIELDS]
        stream.write("| " + " | ".join(cells) + " |\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_json(record: TDocCRDetails, output: str | None) -> None:
    """Emit a single record as a JSON object via ``dataclasses.asdict``."""
    payload = dataclasses.asdict(record)
    payload["date"] = record.date.isoformat() if record.date is not None else None
    stream, close_after = _open_output(output)
    try:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_raw(markdown: str, output: str | None) -> None:
    """Write the converted markdown bytes verbatim, no wrapping."""
    stream, close_after = _open_output(output)
    try:
        stream.write(markdown)
        if not markdown.endswith("\n"):
            stream.write("\n")
    finally:
        if close_after:
            stream.close()


# ---------------------------------------------------------------------------
# tdoc show --format renderers
# ---------------------------------------------------------------------------


def _serialise_show_value(value: object) -> object:
    """Normalise ``date`` / ``datetime`` / ``None`` for JSON / Markdown output.

    ``date`` and ``datetime`` are not natively JSON-serialisable, and
    naive ``str(value)`` formats ``datetime`` with a space separator
    while ``isoformat()`` produces strict ISO-8601. Markdown rendering
    only needs the formatted string.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # ``datetime`` is a subclass of ``date`` so the order matters here.
    if isinstance(value, date):
        return value.isoformat()
    return value


@dataclass(slots=True, frozen=True)
class TDocShowRecord:
    """Bundled output of ``tdoc show`` for the JSON / markdown / table renderers.

    Carries the parent :class:`TDoc`, the optional cover-page row keyed
    by the stored ``tdoc.ftp_url`` (the slim ``TDocCRDetails`` shape),
    the optional TTCN sidecar (only populated for TTCN CRs), the
    extract ``extracted_at`` timestamp derived from ``tdoc_extracts``,
    and every auxiliary ``tdoc_files`` row whose ``tdoc_id`` matches.
    Keys are omitted (not null) in renderers when the corresponding
    value is absent.

    Attributes:
        tdoc: The resolved parent TDoc row from the ``tdocs`` table.
        cover: Slim cover-page fields keyed by ``tdoc.ftp_url``;
            ``None`` when no extract row exists for that URL.
        ttcn: TTCN sidecar keyed by ``tdoc.ftp_url``; ``None`` when no
            sidecar row exists for that URL or when the TDoc is not
            a TTCN CR.
        extracted_at: Cache-extract timestamp for ``tdoc.ftp_url``;
            ``None`` when no ``tdoc_extracts`` row exists for that URL.
        files: Auxiliary ``tdoc_files`` rows matching ``tdoc_id``;
            empty tuple when the parent TDoc has no auxiliary files.
            The JSON renderer omits the top-level ``files`` key when
            this is empty; the markdown and table renderers always
            render their auxiliary-files section (markdown) or
            placeholder (table) — see the renderer docstrings.
    """

    tdoc: TDoc
    cover: TDocCRDetails | None = None
    ttcn: TDocCRTTCNDetails | None = None
    extracted_at: datetime | None = None
    files: tuple[TDocFile, ...] = ()


@dataclass(slots=True, frozen=True)
class TDocShowRecordByUrl:
    """Bundled output of ``tdoc show --ftp-url`` for the JSON / markdown / table renderers.

    Mirrors :class:`TDocShowRecord` but anchors on the URL rather than
    on a parent ``TDoc``. The 1:1 invariant between ``ftp_url`` and
    ``tdoc_id`` (enforced by the upload pipeline) means the parent
    ``TDoc`` is optional from the caller's perspective — a URL may
    surface a cover row, TTCN sidecar, extract meta, or auxiliary
    files without a matching ``tdocs`` row.

    ``ftp_url`` is always emitted (it's the selector). Optional keys
    are omitted (not null) in renderers when the corresponding value
    is absent.

    Attributes:
        ftp_url: The normalised URL the user supplied.
        tdoc: The unique TDoc whose ``ftp_url`` matches (1:1 invariant);
            ``None`` when no ``tdocs`` row exists for the URL.
        cover: Slim cover-page fields keyed by ``ftp_url``;
            ``None`` when no extract row exists.
        ttcn: TTCN sidecar keyed by ``ftp_url``; ``None`` when no
            sidecar row exists.
        extracted_at: Cache-extract timestamp for ``ftp_url``;
            ``None`` when no ``tdoc_extracts`` row exists.
        files: Auxiliary ``tdoc_files`` rows matching ``ftp_url``;
            empty tuple when no auxiliary file is attached.
    """

    ftp_url: str
    tdoc: TDoc | None = None
    cover: TDocCRDetails | None = None
    ttcn: TDocCRTTCNDetails | None = None
    extracted_at: datetime | None = None
    files: tuple[TDocFile, ...] = ()


def _render_tdoc_show_json(
    record: TDocShowRecord,
    output: str | None,
) -> None:
    """Emit ``tdoc show --format json``.

    The payload is a single object with the following top-level keys.
    Each optional key is **omitted** (not emitted as ``null``) when no
    corresponding row exists, so the JSON stays dense for hits and
    sparse for misses:

    - ``tdoc``: every :class:`TDoc` field, normalised via
      :func:`_serialise_show_value` so ``date`` / ``datetime`` come out
      as ISO-8601 strings.
    - ``cover``: slim cover-page fields keyed by ``tdoc.ftp_url``;
      every dataclass field of :class:`TDocCRDetails` is serialised.
    - ``ttcn``: TTCN sidecar keyed by ``tdoc.ftp_url``; every
      dataclass field of :class:`TDocCRTTCNDetails` is serialised. The
      ``required_changes`` ``list[dict]`` falls through to the default
      branch and serialises as a JSON array.
    - ``extracted_at``: ISO-8601 string for the cache-extract
      timestamp sourced from the ``tdoc_extracts`` row at
      ``tdoc.ftp_url``. Lives at the top level rather than nested
      under ``cover`` / ``ttcn`` because both detail rows no longer
      carry their own timestamps after the slimming.
    - ``files``: array of every :class:`TDocFile` row matching
      ``tdoc_id`` (auxiliary revisions / reviews / support files).
      Every dataclass field of :class:`TDocFile` is serialised. The
      key is **omitted** when the TDoc has no auxiliary files, so the
      JSON stays dense for hits and sparse for misses.
    """
    payload: dict[str, object] = {
        "tdoc": {
            f.name: _serialise_show_value(getattr(record.tdoc, f.name))
            for f in dataclass_fields(record.tdoc)
        },
    }
    if record.cover is not None:
        payload["cover"] = {
            f.name: _serialise_show_value(getattr(record.cover, f.name))
            for f in dataclass_fields(record.cover)
        }
    if record.ttcn is not None:
        payload["ttcn"] = {
            f.name: _serialise_show_value(getattr(record.ttcn, f.name))
            for f in dataclass_fields(record.ttcn)
        }
    if record.extracted_at is not None:
        payload["extracted_at"] = _serialise_show_value(record.extracted_at)
    if record.files:
        payload["files"] = [
            {
                f.name: _serialise_show_value(getattr(file, f.name))
                for f in dataclass_fields(file)
            }
            for file in record.files
        ]
    stream, close_after = _open_output(output)
    try:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_markdown(
    record: TDocShowRecord,
    output: str | None,
) -> None:
    """Emit ``tdoc show --format markdown``.

    The TDoc row becomes a bullet list under a ``Metadata`` heading.
    When a slim cover-page row exists for ``tdoc.ftp_url`` it renders
    under ``## Extracted Cover Details``; when a TTCN sidecar exists it
    renders under ``## TTCN Details``. When neither is present and no
    ``extracted_at`` is known, a "_no extracted details_" placeholder
    is emitted. Every ``tdoc_files`` row matching ``tdoc_id`` renders
    under ``## Auxiliary Files`` (or a placeholder when none exist);
    the section is always emitted so the document skeleton is stable.

    ``required_changes`` on the TTCN sidecar renders as a JSON fenced
    block (matching the legacy ``details``-field rendering convention)
    so the structured content round-trips through any Markdown viewer.
    Long free-text fields are **not** truncated in this mode — callers
    using Markdown for archival want the full text. ``extracted_at``
    is sourced from the ``tdoc_extracts`` row and lives under the
    ``Extracted Cover Details`` section (or as its own bullet when
    only the timestamp is known).
    """
    stream, close_after = _open_output(output)
    try:
        stream.write(f"# TDoc `{record.tdoc.tdoc_id}`\n\n")
        stream.write("## Metadata\n\n")
        for f in dataclass_fields(record.tdoc):
            value = _serialise_show_value(getattr(record.tdoc, f.name))
            if value is None:
                rendered = "—"
            else:
                rendered = str(value)
            stream.write(f"- **{f.name}**: {rendered}\n")

        if (
            record.cover is None
            and record.ttcn is None
            and record.extracted_at is None
        ):
            stream.write("\n## Extracted Details\n\n")
            stream.write(
                "_No extracted details; run "
                "`doc3gpp tdoc parse --tdoc <id>` first._\n"
            )
        else:
            if record.cover is not None:
                stream.write("\n## Extracted Cover Details\n\n")
                for f in dataclass_fields(record.cover):
                    # Defensive: the slim cover dataclass no longer carries
                    # ``details`` or ``parser_version``, but skip defensively in
                    # case a stale code path slips through.
                    if f.name in {"details", "parser_version"}:
                        continue
                    value = getattr(record.cover, f.name)
                    formatted = _serialise_show_value(value)
                    rendered = "—" if formatted is None else str(formatted)
                    stream.write(f"- **{f.name}**: {rendered}\n")
                if record.extracted_at is not None:
                    stream.write(f"- **extracted_at**: {_fmt_dt(record.extracted_at)}\n")

            if record.ttcn is not None:
                stream.write("\n## TTCN Details\n\n")
                for f in dataclass_fields(record.ttcn):
                    value = getattr(record.ttcn, f.name)
                    if f.name == "required_changes" and isinstance(value, list):
                        stream.write(f"- **{f.name}**:\n\n```json\n")
                        stream.write(
                            json.dumps(value, ensure_ascii=False, indent=2)
                        )
                        stream.write("\n```\n")
                        continue
                    formatted = _serialise_show_value(value)
                    rendered = "—" if formatted is None else str(formatted)
                    stream.write(f"- **{f.name}**: {rendered}\n")

        stream.write("\n## Auxiliary Files\n\n")
        if not record.files:
            stream.write(
                "_No auxiliary files; run "
                "`doc3gpp tdoc sync` first if you haven't synced "
                "this meeting yet._\n"
            )
        else:
            for file in record.files:
                stream.write(f"- **type**: {file.type}\n")
                stream.write(f"  - **file**: {file.file}\n")
                stream.write(f"  - **ftp_url**: {file.ftp_url}\n")
                uploaded = (
                    file.uploaded_date.isoformat()
                    if file.uploaded_date is not None
                    else "—"
                )
                stream.write(f"  - **uploaded_date**: {uploaded}\n")
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_table(
    record: TDocShowRecord,
    output: str | None,
) -> None:
    """Emit ``tdoc show --format table`` (the default).

    Preserves the historical line-oriented output so operator muscle
    memory and existing shell scripts keep working. The slim
    ``TDocCRDetails`` dataclass no longer carries ``parser_version``
    or ``details`` — those lines are dropped. ``extracted_at`` is
    sourced from the ``tdoc_extracts`` row at ``tdoc.ftp_url`` and is
    emitted on the same line-position as before; when no extract row
    exists, ``extracted_at: -`` is rendered.

    When the TTCN sidecar is also present the renderer emits an
    extra ``[TTCN Details]`` block with the six overview fields and a
    ``required_changes: <count> item(s)`` summary line, matching the
    markdown renderer's convention. Every ``tdoc_files`` row matching
    ``tdoc_id`` renders under a ``[Auxiliary Files]`` block with the
    four informative fields (``type``, ``file``, ``ftp_url``,
    ``uploaded_date``); the autoincrement ``id`` and the ``tdoc_id``
    match key are dropped because the parent ``[TDoc]`` block already
    shows the match key. When the TDoc has no auxiliary files, the
    header is omitted and a placeholder line points the reader at
    ``tdoc sync`` (the flow that populates ``tdoc_files``).
    """
    stream, close_after = _open_output(output)
    try:
        stream.write("[TDoc]\n")
        for f in dataclass_fields(record.tdoc):
            value = getattr(record.tdoc, f.name)
            if value is None:
                value = "-"
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            else:
                value = str(value)
            stream.write(f"{f.name}: {value}\n")

        if record.cover is None:
            # No cover row exists for ``tdoc.ftp_url`` — skip the
            # ``[Extracted Details]`` section header so the placeholder
            # message is the only thing rendered under the ``[TDoc]``
            # block. The header re-appears once a real cover row
            # surfaces (see the ``record.cover is not None`` branch
            # below).
            stream.write(
                "No extracted details; run `doc3gpp tdoc parse --tdoc <id>` first.\n"
            )
            if record.extracted_at is not None:
                stream.write(
                    f"extracted_at: {_fmt_dt(record.extracted_at)}\n"
                )
            else:
                stream.write("extracted_at: -\n")

        if record.cover is not None:
            details = record.cover
            stream.write("[Extracted Details]\n")
            if details.ftp_url:
                stream.write(f"ftp_url: {details.ftp_url}\n")
            stream.write(f"spec: {details.spec or '-'}\n")
            stream.write(f"cr_num: {details.cr_num or '-'}\n")
            stream.write(f"rev: {details.rev or '-'}\n")
            stream.write(f"version: {details.version or '-'}\n")
            stream.write(f"title: {details.title or '-'}\n")
            stream.write(f"source: {details.source or '-'}\n")
            stream.write(f"tsg: {details.tsg or '-'}\n")
            stream.write(f"related_wis: {details.related_wis or '-'}\n")
            stream.write(f"date: {details.date or '-'}\n")
            stream.write(f"cr_cat: {details.cr_cat or '-'}\n")
            stream.write(f"release: {details.release or '-'}\n")
            stream.write(
                "reason_for_change: "
                f"{_truncate_for_display(details.reason_for_change)}\n"
            )
            stream.write(
                "consequences_if_not_approved: "
                f"{_truncate_for_display(details.consequences_if_not_approved)}\n"
            )
            stream.write(
                f"clauses_affected: {details.clauses_affected or '-'}\n"
            )
            if record.extracted_at is not None:
                stream.write(f"extracted_at: {_fmt_dt(record.extracted_at)}\n")
            else:
                stream.write("extracted_at: -\n")

        if record.ttcn is not None:
            ttcn = record.ttcn
            stream.write("[TTCN Details]\n")
            if ttcn.ftp_url:
                stream.write(f"ftp_url: {ttcn.ftp_url}\n")
            stream.write(f"testcase: {ttcn.testcase or '-'}\n")
            stream.write(f"ue: {ttcn.ue or '-'}\n")
            stream.write(f"ss: {ttcn.ss or '-'}\n")
            stream.write(f"ats_version: {ttcn.ats_version or '-'}\n")
            stream.write(f"ttcn_release: {ttcn.ttcn_release or '-'}\n")
            stream.write(f"test_suite: {ttcn.test_suite or '-'}\n")
            count = len(ttcn.required_changes)
            stream.write(f"required_changes: {count} item(s)\n")

        if record.files:
            stream.write("[Auxiliary Files]\n")
            for file in record.files:
                # Drop ``id`` (autoincrement PK) and ``tdoc_id``
                # (match key, already in the ``[TDoc]`` block) —
                # both are noise in this output.
                stream.write(f"type: {file.type}\n")
                stream.write(f"file: {file.file}\n")
                stream.write(f"ftp_url: {file.ftp_url}\n")
                uploaded = (
                    file.uploaded_date.isoformat()
                    if file.uploaded_date is not None
                    else "-"
                )
                stream.write(f"uploaded_date: {uploaded}\n")
        else:
            # No header on the empty case — placeholder line alone.
            # Hint points to ``tdoc sync`` (not ``tdoc parse``)
            # because the file table is populated by the sync flow.
            stream.write(
                "No auxiliary files; run `doc3gpp tdoc sync` first "
                "if you haven't synced this meeting yet.\n"
            )
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_raw(tdoc_id: str, output: str | None) -> None:
    """Emit ``tdoc show --format raw``.

    Delegates to :class:`TDocCrService.extract`, which short-circuits
    on a DB cache hit (no network / no conversion) and otherwise
    downloads the zip, renders the markdown and persists the row. The
    rendered markdown is then read back from the cache path the
    service populated. Service-level exceptions are translated into
    friendly CLI errors so the operator never sees a raw traceback.
    """
    try:
        service = build_tdoc_cr_service()
        result = service.extract(tdoc_id)
    except TDocNotFoundError:
        raise typer.BadParameter(
            f"Unknown TDoc '{tdoc_id}'. Run 'doc3gpp tdoc list' to see "
            f"stored TDocs, or 'doc3gpp tdoc sync' to ingest a "
            f"meeting's TDocs first."
        ) from None
    except TDocTypeUnsupportedError as exc:
        raise typer.BadParameter(
            f"TDoc {tdoc_id!r} has type {exc.observed_type!r}; "
            f"--format raw is only available for CR-type TDocs"
        ) from None
    except TDocZipDownloadError as exc:
        raise typer.BadParameter(
            f"Failed to download TDoc '{tdoc_id}' for raw rendering: {exc}"
        ) from None
    except PythonDocxNotInstalledError as exc:
        raise typer.BadParameter(str(exc)) from None

    cache = _build_cache()
    markdown = _read_cached_markdown_path(
        result.extract_meta.cache_file, cache.root,
    )
    if not markdown:
        raise typer.BadParameter(
            f"Markdown cache for TDoc '{tdoc_id}' is empty or unreadable "
            f"(cache_file: {result.extract_meta.cache_file}, "
            f"cache_dir: {cache.root})"
        )
    _emit_record_raw(markdown, output)


# ---------------------------------------------------------------------------
# tdoc show --ftp-url <url> dispatch + renderers
# ---------------------------------------------------------------------------


def _tdoc_show_by_ftp_url(
    raw_url: str,
    fmt: str,
    output: str | None,
) -> None:
    """Dispatch ``tdoc show --ftp-url`` to the right renderer.

    Normalises the URL via :func:`_normalise_cli_ftp_url`, fans out
    to four URL-keyed reads (``tdocs``, ``tdoc_cr_details``,
    ``tdoc_cr_ttcn_details``, ``tdoc_files`` — the
    ``tdoc_extracts`` timestamp is sourced via
    ``TDocCrDetailRepository.get_extract_meta_by_url``), and
    raises :class:`typer.BadParameter` when the URL matches no row
    in any of them.

    Does NOT trigger :func:`trigger_auto_sync` — the URL is the row
    identity, so no parent-meeting sync is meaningful for an
    arbitrary URL. Raw format takes the cache-direct path below
    without going through ``TDocCrService.extract``.
    """
    url = _normalise_cli_ftp_url(raw_url)

    if fmt == "raw":
        _render_tdoc_show_raw_by_url(url, output)
        return

    tdoc_repo = build_tdoc_repository()
    cr_repo = build_tdoc_cr_repository()
    cr_ttcn_repo = build_tdoc_cr_ttcn_repository()
    file_repo = build_tdoc_file_repository()

    tdoc = tdoc_repo.get_by_ftp_url(url)
    cover = cr_repo.get_by_url(url)
    meta = cr_repo.get_extract_meta_by_url(url)
    extracted_at = meta.extracted_at if meta is not None else None
    # TTCN sidecar can only exist when the URL has a cover row
    # (the cover parser is what produces it), so gate the lookup.
    ttcn = cr_ttcn_repo.get_by_url(url) if cover is not None else None
    files = tuple(file_repo.get_by_ftp_url(url))

    if (
        tdoc is None
        and cover is None
        and meta is None
        and ttcn is None
        and not files
    ):
        raise typer.BadParameter(
            f"No row in tdocs, tdoc_cr_details, tdoc_cr_ttcn_details, "
            f"or tdoc_files matches ftp_url {url!r}."
        )

    record = TDocShowRecordByUrl(
        ftp_url=url,
        tdoc=tdoc,
        cover=cover,
        ttcn=ttcn,
        extracted_at=extracted_at,
        files=files,
    )

    if fmt == "json":
        _render_tdoc_show_by_url_json(record, output)
    elif fmt == "markdown":
        _render_tdoc_show_by_url_markdown(record, output)
    else:
        _render_tdoc_show_by_url_table(record, output)


def _render_tdoc_show_raw_by_url(url: str, output: str | None) -> None:
    """Emit ``tdoc show --ftp-url --format raw``.

    The URL is the row identity — the cache file is derived
    directly from the URL via :func:`derive_cache_file`, so no
    TDoc resolution or ``TDocCrService.extract`` call is needed.
    On a cache miss the operator is pointed at the explicit-parse
    paths that would populate the cache.
    """
    cache_file = derive_cache_file(url)
    cache = _build_cache()
    markdown = _read_cached_markdown_path(cache_file, cache.root)
    if not markdown:
        raise typer.BadParameter(
            f"No cached markdown for {url!r} (key {cache_file!r}). "
            "Run `doc3gpp tdoc parse --from-url <url>` or "
            "`doc3gpp tdoc parse --tdoc <id>` first."
        )
    _emit_record_raw(markdown, output)


def _render_tdoc_show_by_url_json(
    record: TDocShowRecordByUrl,
    output: str | None,
) -> None:
    """Emit ``tdoc show --ftp-url --format json``.

    Payload shape mirrors :func:`_render_tdoc_show_json` but
    anchored on the URL. ``ftp_url`` is always emitted; ``tdoc``
    is omitted when no matching ``TDoc`` row exists. Optional keys
    (``cover`` / ``ttcn`` / ``extracted_at`` / ``files``) follow
    the same omit-when-null convention as the existing renderer.
    """
    payload: dict[str, object] = {
        "ftp_url": record.ftp_url,
    }
    if record.tdoc is not None:
        payload["tdoc"] = {
            f.name: _serialise_show_value(getattr(record.tdoc, f.name))
            for f in dataclass_fields(record.tdoc)
        }
    if record.cover is not None:
        payload["cover"] = {
            f.name: _serialise_show_value(getattr(record.cover, f.name))
            for f in dataclass_fields(record.cover)
        }
    if record.ttcn is not None:
        payload["ttcn"] = {
            f.name: _serialise_show_value(getattr(record.ttcn, f.name))
            for f in dataclass_fields(record.ttcn)
        }
    if record.extracted_at is not None:
        payload["extracted_at"] = _serialise_show_value(record.extracted_at)
    if record.files:
        payload["files"] = [
            {
                f.name: _serialise_show_value(getattr(file, f.name))
                for f in dataclass_fields(file)
            }
            for file in record.files
        ]
    stream, close_after = _open_output(output)
    try:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_by_url_markdown(
    record: TDocShowRecordByUrl,
    output: str | None,
) -> None:
    """Emit ``tdoc show --ftp-url --format markdown``.

    The URL is the document anchor (``# FTP URL ...``); each
    populated table gets its own ``## ...`` section. Optional
    sections (``## TDoc``, ``## Extracted Cover Details``,
    ``## TTCN Details``, ``## Auxiliary Files``) are emitted only
    when populated. ``tdoc_files`` rows mirror the per-row layout
    used by :func:`_render_tdoc_show_markdown`.
    """
    stream, close_after = _open_output(output)
    try:
        stream.write(f"# FTP URL `{record.ftp_url}`\n\n")

        if record.tdoc is not None:
            stream.write("## TDoc\n\n")
            for f in dataclass_fields(record.tdoc):
                value = _serialise_show_value(getattr(record.tdoc, f.name))
                rendered = "—" if value is None else str(value)
                stream.write(f"- **{f.name}**: {rendered}\n")
        else:
            stream.write(
                "_No `tdocs` row matches this URL. The URL still "
                "surfaces in `tdoc_cr_details` / `tdoc_cr_ttcn_details` "
                "/ `tdoc_files` because the upstream document appeared "
                "in a sync but no parent TDoc row was stored._\n\n"
            )

        if record.cover is not None:
            stream.write("\n## Extracted Cover Details\n\n")
            for f in dataclass_fields(record.cover):
                if f.name in {"details", "parser_version"}:
                    continue
                value = getattr(record.cover, f.name)
                formatted = _serialise_show_value(value)
                rendered = "—" if formatted is None else str(formatted)
                stream.write(f"- **{f.name}**: {rendered}\n")
            if record.extracted_at is not None:
                stream.write(
                    f"- **extracted_at**: {_fmt_dt(record.extracted_at)}\n"
                )
        elif record.extracted_at is not None:
            stream.write("\n## Extracted Details\n\n")
            stream.write(
                f"- **extracted_at**: {_fmt_dt(record.extracted_at)}\n"
            )

        if record.ttcn is not None:
            stream.write("\n## TTCN Details\n\n")
            for f in dataclass_fields(record.ttcn):
                value = getattr(record.ttcn, f.name)
                if f.name == "required_changes" and isinstance(value, list):
                    stream.write(f"- **{f.name}**:\n\n```json\n")
                    stream.write(
                        json.dumps(value, ensure_ascii=False, indent=2)
                    )
                    stream.write("\n```\n")
                    continue
                formatted = _serialise_show_value(value)
                rendered = "—" if formatted is None else str(formatted)
                stream.write(f"- **{f.name}**: {rendered}\n")

        if record.files:
            stream.write("\n## Auxiliary Files\n\n")
            for file in record.files:
                stream.write(f"- **type**: {file.type}\n")
                stream.write(f"  - **file**: {file.file}\n")
                stream.write(f"  - **ftp_url**: {file.ftp_url}\n")
                uploaded = (
                    file.uploaded_date.isoformat()
                    if file.uploaded_date is not None
                    else "—"
                )
                stream.write(f"  - **uploaded_date**: {uploaded}\n")
        else:
            stream.write(
                "\n_No auxiliary files match this URL._\n"
            )
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_by_url_table(
    record: TDocShowRecordByUrl,
    output: str | None,
) -> None:
    """Emit ``tdoc show --ftp-url --format table`` (the default for URL mode).

    Mirrors :func:`_render_tdoc_show_table` but anchored on the URL:
    ``[FTP URL]`` precedes ``[TDoc]`` / ``[Extracted Details]`` /
    ``[TTCN Details]`` / ``[Auxiliary Files]``. Optional blocks are
    omitted when their source row is absent (the ``[TDoc]`` block
    drops entirely when no ``TDoc`` row matches).
    """
    stream, close_after = _open_output(output)
    try:
        stream.write("[FTP URL]\n")
        stream.write(f"ftp_url: {record.ftp_url}\n")

        if record.tdoc is not None:
            stream.write("[TDoc]\n")
            for f in dataclass_fields(record.tdoc):
                value = getattr(record.tdoc, f.name)
                if value is None:
                    value = "-"
                elif hasattr(value, "isoformat"):
                    value = value.isoformat()
                else:
                    value = str(value)
                stream.write(f"{f.name}: {value}\n")
        else:
            stream.write(
                "No tdocs row matches this URL.\n"
            )

        if record.cover is None:
            stream.write(
                "No extracted details; run `doc3gpp tdoc parse "
                "--from-url <url>` first.\n"
            )
            if record.extracted_at is not None:
                stream.write(
                    f"extracted_at: {_fmt_dt(record.extracted_at)}\n"
                )
            else:
                stream.write("extracted_at: -\n")
        else:
            details = record.cover
            stream.write("[Extracted Details]\n")
            if details.ftp_url:
                stream.write(f"ftp_url: {details.ftp_url}\n")
            stream.write(f"spec: {details.spec or '-'}\n")
            stream.write(f"cr_num: {details.cr_num or '-'}\n")
            stream.write(f"rev: {details.rev or '-'}\n")
            stream.write(f"version: {details.version or '-'}\n")
            stream.write(f"title: {details.title or '-'}\n")
            stream.write(f"source: {details.source or '-'}\n")
            stream.write(f"tsg: {details.tsg or '-'}\n")
            stream.write(f"related_wis: {details.related_wis or '-'}\n")
            stream.write(f"date: {details.date or '-'}\n")
            stream.write(f"cr_cat: {details.cr_cat or '-'}\n")
            stream.write(f"release: {details.release or '-'}\n")
            stream.write(
                "reason_for_change: "
                f"{_truncate_for_display(details.reason_for_change)}\n"
            )
            stream.write(
                "consequences_if_not_approved: "
                f"{_truncate_for_display(details.consequences_if_not_approved)}\n"
            )
            stream.write(
                f"clauses_affected: {details.clauses_affected or '-'}\n"
            )
            if record.extracted_at is not None:
                stream.write(
                    f"extracted_at: {_fmt_dt(record.extracted_at)}\n"
                )
            else:
                stream.write("extracted_at: -\n")

        if record.ttcn is not None:
            ttcn = record.ttcn
            stream.write("[TTCN Details]\n")
            if ttcn.ftp_url:
                stream.write(f"ftp_url: {ttcn.ftp_url}\n")
            stream.write(f"testcase: {ttcn.testcase or '-'}\n")
            stream.write(f"ue: {ttcn.ue or '-'}\n")
            stream.write(f"ss: {ttcn.ss or '-'}\n")
            stream.write(f"ats_version: {ttcn.ats_version or '-'}\n")
            stream.write(f"ttcn_release: {ttcn.ttcn_release or '-'}\n")
            stream.write(f"test_suite: {ttcn.test_suite or '-'}\n")
            count = len(ttcn.required_changes)
            stream.write(f"required_changes: {count} item(s)\n")

        if record.files:
            stream.write("[Auxiliary Files]\n")
            for file in record.files:
                stream.write(f"type: {file.type}\n")
                stream.write(f"file: {file.file}\n")
                stream.write(f"ftp_url: {file.ftp_url}\n")
                uploaded = (
                    file.uploaded_date.isoformat()
                    if file.uploaded_date is not None
                    else "-"
                )
                stream.write(f"uploaded_date: {uploaded}\n")
        else:
            stream.write(
                "No auxiliary files; run `doc3gpp tdoc sync` first "
                "if you haven't synced this meeting yet.\n"
            )
    finally:
        if close_after:
            stream.close()


_DIRECT_FORMAT_EXTENSIONS: dict[str, str] = {
    "table": ".tsv",
    "markdown": ".md",
    "json": ".json",
    "raw": ".md",
}


def _resolve_batch_output_path(
    input_path: Path,
    output_dir: Path,
    fmt: str,
) -> Path:
    """Compute the output file path for a local batch parse input.

    The filename stem is preserved from ``input_path`` and the suffix
    is replaced with the extension that matches ``fmt``. When the input
    lives inside a sub-tree scanned with ``--recursive``, the relative
    parent directories are recreated under ``output_dir``.
    """
    extension = _DIRECT_FORMAT_EXTENSIONS[fmt]
    relative = input_path.parent
    target_dir = output_dir / relative
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / (input_path.stem + extension)


def _collect_local_parse_targets(from_path: Path, recursive: bool) -> list[Path]:
    """Return every legitimate ``.docx`` / ``.zip`` under ``from_path`` in sorted order.

    A file is legitimate when:

    - its extension is ``.docx`` or ``.zip`` (case-insensitive), and
    - its filename contains a 3GPP TDoc id pattern.

    When ``recursive`` is ``False`` only immediate children are considered.
    """
    iterator = from_path.rglob("*") if recursive else from_path.iterdir()
    targets = [
        p
        for p in iterator
        if p.is_file()
        and p.suffix.lower() in (".docx", ".zip")
        and extract_tdoc_id_from_filename(p.name) is not None
    ]
    return sorted(targets)


def _tdoc_parse_local_batch(
    *,
    from_path: str,
    output: str,
    fmt: str | None,
    recursive: bool,
    force: bool,
    full: bool,
) -> None:
    """Parse every ``.docx`` / ``.zip`` under ``from_path`` and write one output file each.

    No cache or DB writes occur. Failures per file are logged and
    counted; the run continues so one bad file does not abort the
    whole batch. A summary is printed to stdout after all files are
    processed.
    """
    from doc3gpp.parsers.cr_parser import CRHeaderMissingError
    from doc3gpp.scraping.tdoc_zip_source import TDocZipDownloadError

    resolved_format = _resolve_direct_format(fmt)
    input_dir = Path(from_path)
    if not input_dir.exists():
        typer.echo(f"FAILED - FileNotFoundError: {input_dir}", err=True)
        raise typer.Exit(code=1) from None
    if not input_dir.is_dir():
        raise typer.BadParameter(f"--from-path must be a directory: {from_path}")

    output_dir = Path(output)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not output_dir.is_dir():
        raise typer.BadParameter(f"--output must be a directory in batch mode: {output}")

    targets = _collect_local_parse_targets(input_dir, recursive)
    if not targets:
        typer.echo(
            "No legitimate .docx/.zip files found under the input path. "
            "Each file must have a .docx or .zip extension and its name "
            "must contain a 3GPP TDoc id pattern."
        )
        raise typer.Exit(code=0)

    service = build_tdoc_cr_service()
    skipped = 0
    re_parsed = 0
    newly_parsed = 0
    failures = 0

    for input_path in targets:
        rel = input_path.relative_to(input_dir)
        out_path = _resolve_batch_output_path(rel, output_dir, resolved_format)

        if out_path.exists() and not force:
            skipped += 1
            logger.debug("Skipping %s because output already exists: %s", input_path, out_path)
            continue

        try:
            payload = input_path.read_bytes()
            result = service.extract_from_bytes(
                payload, str(input_path), force=False, full=full,
            )
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            logger.warning("Failed to read %s: %s", input_path, exc)
            failures += 1
            continue
        except (TDocZipDownloadError, CRHeaderMissingError, ValueError) as exc:
            logger.warning("Failed to parse %s: %s", input_path, exc)
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to parse %s: %s: %s", input_path, type(exc).__name__, exc
            )
            failures += 1
            continue

        try:
            if resolved_format == "raw":
                _emit_record_raw(result.markdown, str(out_path))
            else:
                if result.details is None:
                    logger.warning(
                        "No parsed details for %s; skipping output.", input_path
                    )
                    failures += 1
                    continue
                _emit_record(result.details, resolved_format, str(out_path))
        except OSError as exc:
            logger.warning("Failed to write %s: %s", out_path, exc)
            failures += 1
            continue

        if out_path.exists() and force:
            re_parsed += 1
        else:
            newly_parsed += 1

    typer.echo("---")
    typer.echo(f"Skipped (output already exists): {skipped}")
    typer.echo(f"Re-parsed (with --force):        {re_parsed}")
    typer.echo(f"Newly parsed:                    {newly_parsed}")
    typer.echo(f"Failures:                        {failures}")
    if failures > 0 and newly_parsed + re_parsed == 0:
        raise typer.Exit(code=1)


def _resolve_url_batch_output_path(
    file_url: str,
    output_dir: Path,
    fmt: str,
) -> Path:
    """Compute the mirrored output path for a URL-folder batch result."""
    relative = normalize_ftp_path(file_url)
    input_path = Path(relative)
    extension = _DIRECT_FORMAT_EXTENSIONS[fmt]
    target_dir = output_dir / input_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir / (input_path.stem + extension)


def _tdoc_parse_url_batch(
    *,
    from_url: str,
    output: str | None,
    fmt: str | None,
    max_depth: int,
    force: bool,
    full: bool,
) -> None:
    """Batch-parse every matching file under a 3GPP FTP folder URL.

    DB/cache writes happen inside the service for FK hits. Per-file
    results are written to ``--output`` when it is provided and mirrored
    under the FTP folder structure; otherwise only a summary is printed.
    """
    resolved_format = _resolve_direct_format(fmt)
    service = build_tdoc_cr_service()
    batch = service.extract_from_url_batch(
        from_url,
        max_depth=max_depth,
        force=force,
        full=full,
    )
    if not batch.results and not batch.failures and max_depth == 0:
        typer.echo(
            "No matching files found at the root level. "
            "Use --recursive to scan subfolders.",
            err=True,
        )
    _emit_url_batch_results(
        batch=batch,
        root_url=from_url,
        output=output,
        fmt=resolved_format,
    )


def _emit_url_batch_results(
    *,
    batch: "DirectParseBatchResult",
    root_url: str,
    output: str | None,
    fmt: str,
) -> None:
    """Emit a URL batch result to disk and/or summary."""
    from doc3gpp.parsers.direct_extractor import (
        build_missing_tdoc_id_warning_message,
        build_no_pattern_warning_message,
    )

    output_dir = Path(output) if output is not None else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        if not output_dir.is_dir():
            raise typer.BadParameter(
                f"--output must be a directory in URL batch mode: {output}"
            )

    skipped = 0
    cache_hits = 0
    newly_parsed = 0
    failures = len(batch.failures)

    for result in batch.results:
        if (
            result.source_kind == "url-3gpp"
            and result.tdoc_id is not None
            and not result.tdoc_id_in_tdocs
        ):
            file_url = result.source_url or root_url
            typer.echo(
                build_missing_tdoc_id_warning_message(result.tdoc_id, file_url),
                err=True,
            )
        elif result.source_kind == "url-3gpp" and result.tdoc_id is None:
            file_url = result.source_url or root_url
            typer.echo(build_no_pattern_warning_message(file_url), err=True)

        if output_dir is not None:
            assert result.tdoc_id is not None
            file_url = result.source_url or root_url
            out_path = _resolve_url_batch_output_path(
                file_url=file_url,
                output_dir=output_dir,
                fmt=fmt,
            )
            if out_path.exists() and not result.persisted and not result.from_cache:
                skipped += 1
                logger.debug("Skipping %s because output already exists: %s", result.tdoc_id, out_path)
                continue

            try:
                if fmt == "raw":
                    _emit_record_raw(result.markdown, str(out_path))
                else:
                    if result.details is None:
                        logger.warning(
                            "No parsed details for %s; skipping output.", result.tdoc_id
                        )
                        failures += 1
                        continue
                    _emit_record(result.details, fmt, str(out_path))
            except OSError as exc:
                logger.warning("Failed to write %s: %s", out_path, exc)
                failures += 1
                continue

        if result.from_cache:
            cache_hits += 1
        else:
            newly_parsed += 1

    typer.echo("---")
    typer.echo(f"Scanned:                         {len(batch.results) + failures}")
    if output_dir is not None:
        typer.echo(f"Skipped (output already exists): {skipped}")
    typer.echo(f"Newly parsed:                    {newly_parsed}")
    typer.echo(f"Cache hits:                      {cache_hits}")
    typer.echo(f"Failures:                        {failures}")
    if failures > 0 and newly_parsed + cache_hits == 0:
        raise typer.Exit(code=1)

def _serialise_cell(record: TDocCRDetails, field_name: str) -> str:
    """Render a single :class:`TDocCRDetails` field for the table emitters.

    ``date`` becomes ``isoformat()`` (or empty string for ``None``);
    ``details`` is JSON-encoded with ``ensure_ascii=False`` so the
    cell stays a single tab-delimited token; everything else uses the
    field's ``str()`` form, with ``None`` rendered as empty string.
    """
    value = getattr(record, field_name)
    if value is None:
        return ""
    if field_name == "date":
        return value.isoformat()
    if field_name == "details":
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_BASE_PARSE_COLUMNS: tuple[str, ...] = ("tdoc_id", "title", "type", "cr_cat", "status")
_FILTER_TO_PARSE_COLUMN: dict[str, str] = {
    "spec": "spec",
    "wi": "related_wis",
    "revision_of": "is_revision_of",
    "revised_to": "revised_to",
    "ftp_url": "ftp_url",
    "release": "release",
    "version": "version",
    "cr_num": "cr_num",
    "cr_pack": "cr_pack",
    "source": "source",
    "uploaded_date": "uploaded_date",
    "meeting_id": "meeting_name",
    "meeting": "meeting_name",
}


def _active_extra_columns(filter_args: dict[str, object]) -> tuple[str, ...]:
    """Compose the rendered column list for the parse confirmation prompt.

    Starts from the fixed base (``tdoc_id``, ``title``, ``type``,
    ``cr_cat``, ``status``) and appends one column per active filter
    that maps to a meaningful display field. The mapping is
    deliberately separate from the base columns so a filter like
    ``--status`` does not double-print. Order is preserved for
    readability; duplicates are silently dropped.
    """
    seen: set[str] = set(_BASE_PARSE_COLUMNS)
    extras: list[str] = []
    for flag_name, active in filter_args.items():
        if active is None or active == "":
            continue
        column = _FILTER_TO_PARSE_COLUMN.get(flag_name)
        if column is None or column in seen:
            continue
        extras.append(column)
        seen.add(column)
    return tuple(extras)


def _format_parse_cell(value: object) -> str:
    """Render a single field value for the parse confirmation table.

    ``None`` renders as ``-``; ``date`` / ``datetime`` render as ISO;
    everything else becomes its ``str()`` form. Truncated at 32
    characters with an ellipsis so the prompt stays on one line.
    """
    if value is None:
        return "-"
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return str(value.isoformat())
    text = str(value)
    if len(text) > 32:
        return text[:31] + "…"
    return text


def _print_parse_group(
    label: str,
    rows: list[TDocWithMeeting],
    columns: tuple[str, ...],
) -> None:
    """Print one of the two parse confirmation groups.

    Renders each row's selected fields using
    :func:`_format_parse_cell`. Group is truncated to the first 20
    rows with an explicit ``... and N more`` suffix when longer, so
    the prompt stays readable for big batches. An empty group prints
    ``(none)`` so the operator never wonders which side is missing.
    """
    typer.echo(f"{label} [count={len(rows)}]:")
    if not rows:
        typer.echo("  (none)")
        return
    preview = rows[:20]
    header = "  " + "  ".join(f"{col:<32}" for col in columns)
    typer.echo(header)
    for row in preview:
        cells = [_format_parse_cell(_tdoc_field(row, col)) for col in columns]
        typer.echo("  " + "  ".join(f"{cell:<32}" for cell in cells))
    if len(rows) > len(preview):
        typer.echo(f"  ... and {len(rows) - len(preview)} more")


@tdoc_app.command("show")
def tdoc_show(
    tdoc: str | None = typer.Option(
        None,
        "--tdoc",
        help=(
            "TDoc ID to show (canonical form, e.g. R5s260009). "
            "Case-insensitive for CR-shape IDs. Mutually exclusive "
            "with --ftp-url."
        ),
    ),
    ftp_url: str | None = typer.Option(
        None,
        "--ftp-url",
        help=(
            "3GPP FTP URL (full URL or relative path) to show. "
            "Surfaces every row in tdocs, tdoc_cr_details, "
            "tdoc_cr_ttcn_details, and tdoc_files whose ftp_url "
            "matches. Mutually exclusive with --tdoc."
        ),
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help=(
            "Output format: table (default), json, markdown, or raw "
            "(the converted .docx markdown for CR-type TDocs)."
        ),
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write output to PATH instead of stdout. Pass '-' for stdout."
        ),
    ),
) -> None:
    """Show a stored TDoc (or URL) and any extracted CR cover-page details.

    Two selectors are supported, mutually exclusive:

    - ``--tdoc <id>`` anchors on the parent TDoc row. Auto-sync
      fires when ``Settings.sync.auto_sync`` is enabled; the parent
      TDoc's ``ftp_url`` is then used to look up the cover-page,
      TTCN sidecar, extract meta, and any ``tdoc_files`` rows
      matching the parent ``tdoc_id``.
    - ``--ftp-url <url>`` anchors on the URL. Accepts both full
      URLs (``https://www.3gpp.org/ftp/TSG_RAN/...``) and bare
      relative paths; the value is normalised via
      :func:`normalize_ftp_path` before lookup. Surfaces every
      matching row across the four tables (``tdocs``,
      ``tdoc_cr_details``, ``tdoc_cr_ttcn_details``,
      ``tdoc_files``); auto-sync does NOT fire because no parent
      meeting sync is meaningful for an arbitrary URL.

    ``--format`` controls the output representation; ``raw`` emits
    the converted .docx markdown instead of the DB-row render and
    triggers a fresh extract (TDoc mode) or reads the cache file
    directly (URL mode). ``--output`` / ``-o`` writes the result
    to a file instead of stdout.
    """
    settings = get_settings()
    fmt = _resolve_tdoc_show_format(fmt, default=settings.output.format)

    if (tdoc is None) == (ftp_url is None):
        raise typer.BadParameter(
            "Provide exactly one of --tdoc <id> or --ftp-url <url>."
        )

    if ftp_url is not None:
        _tdoc_show_by_ftp_url(ftp_url, fmt, output)
        return

    trigger_auto_sync(
        auto_sync_enabled=settings.sync.auto_sync,
        meeting_service=build_meeting_service(),
        tdoc_sync_coordinator=build_tdoc_sync_coordinator(),
        tdoc=tdoc,
    )
    repo = build_tdoc_repository()
    record = repo.get_by_id(_normalise_cli_tdoc_id(tdoc))
    if record is None:
        raise typer.BadParameter(
            f"Unknown TDoc '{tdoc}'. Run 'doc3gpp tdoc list' to see stored TDocs, "
            f"or 'doc3gpp tdoc sync' to ingest a meeting's TDocs first."
        )

    # Raw format takes a separate path: it doesn't render the DB rows,
    # it pulls the converted markdown from the cache (populating it via
    # a fresh extract when the cache is cold).
    if fmt == "raw":
        _render_tdoc_show_raw(record.tdoc_id, output)
        return

    cr_repo = build_tdoc_cr_repository()
    cr_ttcn_repo = build_tdoc_cr_ttcn_repository()
    file_repo = build_tdoc_file_repository()

    cover: TDocCRDetails | None = None
    extracted_at: datetime | None = None
    ttcn: TDocCRTTCNDetails | None = None
    if record.ftp_url:
        cover = cr_repo.get_by_url(record.ftp_url)
        meta = cr_repo.get_extract_meta_by_url(record.ftp_url)
        if meta is not None:
            extracted_at = meta.extracted_at
        if is_ttcn_tdoc(record.tdoc_id):
            ttcn = cr_ttcn_repo.get_by_url(record.ftp_url)

    files = tuple(file_repo.get_for_tdoc_id(record.tdoc_id))

    show_record = TDocShowRecord(
        tdoc=record,
        cover=cover,
        ttcn=ttcn,
        extracted_at=extracted_at,
        files=files,
    )

    if fmt == "json":
        _render_tdoc_show_json(show_record, output)
    elif fmt == "markdown":
        _render_tdoc_show_markdown(show_record, output)
    else:
        _render_tdoc_show_table(show_record, output)


@tsg_app.command("list")
def tsg_list(
    fields: str | None = typer.Option(
        None,
        help="Comma-separated list of fields to include (or 'all' for all fields).",
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: table (default, tab-separated), json, or markdown.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write results to FILE instead of stdout. Pass '-' for stdout.",
    ),
) -> None:
    """List TSG reference records from the database.

    The command supports field selection:
    - ``--fields``: comma-separated list of fields to include in output, or ``all``.

    By default, the output includes ``tsg_name``, ``short_name``, and
    ``description`` to keep the listing compact. Use ``--fields all`` to
    include ``url`` as well.

    Output routing:
    - `-o, --output PATH`: write results to PATH instead of stdout.
    - `--format`: ``table`` (legacy tab-separated, default), ``json`` (array of
      objects), or ``markdown`` (GitHub-flavored table).
    """
    allowed_fields = [f.name for f in dataclass_fields(Tsg)]
    settings = get_settings()
    default_fields = settings.output.fields.tsg

    out_fields = _parse_field_selection(fields, allowed_fields, default_fields)
    fmt = _resolve_format(fmt, default=settings.output.format)

    logger.info("Listing TSG reference records (fields=%s)", out_fields)
    service = build_tsg_service()
    records = service.list_all()

    rows: list[list[str]] = []
    for item in records:
        assert isinstance(item, Tsg)
        rows.append([str(getattr(item, f) or "-") for f in out_fields])

    _emit_records(
        rows=rows,
        fields=out_fields,
        fmt=fmt,
        output=output,
        no_records_msg="No TSG records found. Run 'doc3gpp db init' to seed defaults.",
    )


@tsg_app.command("show")
def tsg_show(
    tsg: str = typer.Option(
        ...,
        "--tsg",
        help="TSG short name (e.g. R5) or full tsg_name (e.g. 'RAN WG5').",
    ),
) -> None:
    """Show a single TSG record by short name or full tsg_name."""
    service = build_tsg_service()

    record = service.get_by_short_name(tsg) or service.get_by_tsg_name(tsg)
    if record is None:
        known = service.known_short_names()
        known_list = ", ".join(known) if known else "(no TSGs registered)"
        raise typer.BadParameter(
            f"Unknown TSG '{tsg}'. Known short names: {known_list}."
        )

    typer.echo(f"tsg_name:    {record.tsg_name}")
    typer.echo(f"short_name:  {record.short_name}")
    typer.echo(f"description: {record.description}")
    typer.echo(f"url:         {record.url or '-'}")


@tsg_app.command("seed")
def tsg_seed() -> None:
    """Insert or refresh the canonical 3GPP TSG reference list.

    Safe to run repeatedly: existing rows are updated in place rather than
    duplicated. Run this if a fresh database is missing TSG reference data
    or if the canonical descriptions/URLs need refreshing.
    """
    create_schema()
    service = build_tsg_service()
    seeded = service.seed_defaults()
    typer.echo(f"Seeded {seeded} TSG reference records")


@wi_app.command("sync")
def wi_sync(
    tsg: str = typer.Option(
        DEFAULT_TSG,
        "--tsg",
        help="TSG short name (e.g. R5) for the WI DynaReport page to sync.",
    ),
) -> None:
    """Fetch and store active WIs for a TSG from 3gpp.org.

    Valid --tsg value are:                                                                                                                                                          
    `R1`, `R2`, `R3`, `R4`, `R5`, `RT`,                                                                                                                                             
    `C1`, `C3`, `C4`, `C6`,                                                                                                                                                         
    `S1`, `S2`, `S3`, `S4`, `S5`, `S6`
    """
    logger.info("Starting WI sync for TSG %s", tsg)
    create_schema()
    tsg_service = _ensure_tsg_ready(build_tsg_service())
    canonical_tsg = _validate_tsg_short_name(tsg, tsg_service)
    service = build_wi_service()
    count = service.sync(canonical_tsg)
    typer.echo(f"WI sync complete: {count} WI rows stored for {canonical_tsg}")


@wi_app.command("list")
def wi_list(
    limit: int = typer.Option(20, min=1, max=500),
    tsg: str | None = typer.Option(
        None,
        "--tsg",
        help="Only list WIs belonging to the given TSG short name.",
    ),
    name: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter WI name (supports % and _).",
    ),
    acronym: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter WI acronym (supports % and _).",
    ),
    release: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter WI release marker (supports % and _).",
    ),
    fmt: str | None = typer.Option(
        None,
        "--format",
        help="Output format: table (default, tab-separated), json, or markdown.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write results to FILE instead of stdout. Pass '-' for stdout.",
    ),
) -> None:
    """List stored WIs matching optional filters.

    Filters ``--name``, ``--acronym``, and ``--release`` accept SQL
    ``LIKE`` patterns. Output columns default to wi_id, acronym,
    release, and name; use ``--format`` and ``--output`` to control
    formatting and destination.
    """
    logger.info(
        "Listing %s recent WIs with filters tsg=%s name=%s acronym=%s release=%s",
        limit,
        tsg,
        name,
        acronym,
        release,
    )
    service = build_wi_service()
    records = service.list_recent(
        limit=limit,
        tsg=tsg,
        name_like=name,
        acronym_like=acronym,
        release_like=release,
    )

    settings = get_settings()
    default_fields = settings.output.fields.wi
    fmt = _resolve_format(fmt, default=settings.output.format)

    rows: list[list[str]] = []
    for item in records:
        assert isinstance(item, Wi)
        rows.append([str(getattr(item, f) or "-") for f in default_fields])

    _emit_records(
        rows=rows,
        fields=default_fields,
        fmt=fmt,
        output=output,
        no_records_msg="No WIs found",
    )


@config_app.command("init")
def config_init(
    target: str = typer.Option(
        "auto",
        "--target",
        help="Where to write the config file: 'project' (./doc3gpp.toml) "
        "or 'user' (~/.config/doc3gpp/config.toml). 'auto' (default) picks "
        "project when run from a project root.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite an existing file at the bootstrap target.",
    ),
) -> None:
    """Bootstrap a fresh config file with the full default settings.

    Writes the packaged default template to the chosen target via an
    atomic ``tempfile`` + :func:`os.replace` dance so a crashed write
    cannot leave a partial file behind. After the write completes the
    settings cache is cleared so subsequent commands see the new file.

    Refuses to run when :envvar:`DOC3GPP_CONFIG` is set — the env pin
    would mask the bootstrapped file, so unsetting it is mandatory.
    Use ``--target`` to override the auto-detected location and
    ``--force`` / ``-f`` to overwrite a file that already exists at the
    target.
    """
    if os.environ.get("DOC3GPP_CONFIG"):
        raise typer.BadParameter(
            "config init refuses when DOC3GPP_CONFIG is set; unset it to bootstrap a config file."
        )

    try:
        target_path = resolve_init_target(target)
    except (ValueError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc))

    if target_path.exists() and not force:
        raise typer.BadParameter(
            f"file exists at {target_path}; pass --force to overwrite"
        )

    template = load_default_template()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target_path.parent,
        delete=False,
    )
    tmp_path = Path(tmp_file.name)
    try:
        tmp_file.write(template)
        tmp_file.close()
        os.replace(tmp_path, target_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    get_settings.cache_clear()

    typer.echo(f"Initialized config at {target_path} (full default settings).")
    typer.echo(
        "  Run 'doc3gpp config set <key> <value>' to edit; "
        "'doc3gpp config show' to verify."
    )


@config_app.command("path")
def config_path() -> None:
    """Print the config file in use, or "(no config file found)".

    Useful for diagnosing "why isn't my TOML being picked up?" — the
    command prints the absolute path returned by
    :func:`doc3gpp.settings.config_source.find_config_file`, or an empty
    marker when no candidate exists.
    """
    path = find_config_file()
    typer.echo(str(path) if path is not None else "(no config file found)")


@config_app.command("show")
def config_show() -> None:
    """Print the resolved configuration as JSON.

    Shows every field on :class:`doc3gpp.settings.schema.Settings` after
    merging the active config file with environment variables and the
    built-in defaults. The output is JSON for easy diffing; copy values
    into a TOML file when you want to pin them. The header line records
    which config file (if any) produced the file-derived portion of the
    result, so unexpected overrides are easy to spot.
    """
    path, _data = load_config_data()
    settings = get_settings()
    typer.echo(f"# config source: {path if path is not None else '(no config file)'}")
    typer.echo(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True))


def _env_var_for_key(key: str) -> str | None:
    """Render the pydantic-settings env-var name for ``key``.

    Single-segment keys map to ``DOC3GPP_<UPPER>``; dotted keys map to
    ``DOC3GPP_<UPPER_HEAD>__<UPPER_TAIL>`` where ``TAIL`` joins the
    remaining segments with underscores (matching pydantic-settings'
    ``env_nested_delimiter="__"`` convention).

    Returns ``None`` when the rendered name is **not** on the closed
    :data:`doc3gpp.settings.schema.ALLOWED_ENV_VARS` allowlist, so
    callers can detect TOML-only keys and skip the env-override hint.
    """
    return env_var_for_dotted_key(key)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted key, e.g. 'sync.auto_sync' or 'database_url'."),
    value: str = typer.Argument(..., help="Value as a string; pydantic coerces to the schema field type."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate + echo without writing."),
) -> None:
    """Set a single key in the active config file.

    Edits the TOML config file currently in use — it must already exist
    (use ``doc3gpp config init`` to bootstrap one). Writes ``key = value``
    into the file and clears the settings cache so the new value is
    visible to subsequent commands in this process. ``value`` is always
    passed as a string and coerced by pydantic against
    :class:`Settings`, so ``24h`` is accepted for ``timedelta`` fields
    and ``true``/``false`` for booleans. ``--dry-run`` validates and
    prints what *would* be written without touching disk.
    """
    found = find_config_file()
    if found is None:
        raise typer.BadParameter(
            "no config file in use; run 'doc3gpp config init' to create one. "
            "Run 'doc3gpp config path' to see what's checked."
        )
    target = found

    known = walk_known_dotted_keys(Settings)
    if key not in known:
        raise typer.BadParameter(
            f"Unknown config key: {key}. Run 'doc3gpp config show' to see valid keys."
        )

    data: dict[str, Any]
    try:
        data = read_toml(target)
    except tomllib.TOMLDecodeError as exc:
        raise typer.BadParameter(f"config file at {target} is malformed: {exc}")

    data = patch_dotted(data, key, value)
    data = prune_empty_tables(data, key)

    try:
        settings = validate_against_settings(data)
    except ConfigValidationError as exc:
        raise typer.BadParameter(str(exc))

    if dry_run:
        typer.echo(f"# dry-run: would write {target}")
        typer.echo(
            json.dumps(
                resolve_echo_subtree(settings, key),
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return

    write_toml(target, data)
    get_settings.cache_clear()

    typer.echo(
        f"Set {key} = "
        f"{json.dumps(resolve_echo_subtree(settings, key), indent=2, sort_keys=True, default=str)}"
        f" (written to {target})."
    )
    env_var = _env_var_for_key(key)
    if env_var is None:
        typer.echo(
            "  Note: this setting is TOML-only"
        )
    else:
        typer.echo(
            f"  Note: if {env_var} is set in the environment, "
            f"it overrides this at runtime."
        )
    typer.echo("  Run 'doc3gpp config show' to verify the active value.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()