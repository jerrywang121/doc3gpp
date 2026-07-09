from __future__ import annotations

import json
import logging
import sys
from dataclasses import fields as dataclass_fields
from datetime import datetime
from pathlib import Path
from typing import TextIO

import typer
from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from doc3gpp.config import get_settings
from doc3gpp.cli_filters import validate_date_filter
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi
from doc3gpp.parsers.docx_converter import PythonDocxNotInstalledError
from doc3gpp.scraping.cache import CacheStatus, TDocCache
from doc3gpp.scraping.tdoc_zip_source import canonicalise_tdoc_id
from doc3gpp.services.factory import (
    build_meeting_service,
    build_tdoc_cr_repository,
    build_tdoc_cr_service,
    build_tdoc_repository,
    build_tdoc_service,
    build_tdoc_sync_coordinator,
    build_tsg_service,
    build_wi_service,
)
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
)
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.settings.config_source import find_config_file, load_config_data
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
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
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


def _build_meeting_url(tsg: str, ext: str = "htm") -> str:
    """Compose the 3GPP DynaReport meeting-calendar URL for ``tsg``.

    The default ``ext="htm"`` matches the canonical 3GPP filename. Pass
    ``"html"`` if the upstream ever switches the suffix (the ``tsg_service``
    page already serves ``.html`` for some links, so callers can request the
    alternate suffix without patching this helper).
    """
    if ext not in ("htm", "html"):
        raise ValueError(f"Unsupported meeting URL extension: {ext!r}")
    return f"https://www.3gpp.org/dynareport?code=Meetings-{tsg.upper()}.{ext}"


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

# Upper bound for ``tdoc parse --meeting-id`` batches. ``TDocRepository.list``
# has no "no limit" variant; the typer-enforced ``max=500`` only applies to
# the ``tdoc list`` CLI. 3GPP meetings rarely exceed a few hundred TDocs, so
# this cap is well above the realistic ceiling without bloating the protocol.
_TDOC_BATCH_LIMIT = 10_000


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
    """Print cache file count, total bytes, limit, and per-subdir breakdown.

    Walks both the ``zips/`` and ``markdown/`` subtrees under the
    configured cache directory and reports their combined size and file
    counts. The output is a fixed plain-text table; no ``--format``
    flag is exposed in this initial cut because the table is short and
    machine-friendly enough to grep / awk.

    The status command is a pure read — it does **not** trigger FIFO
    eviction, even if the cache is currently over the configured size
    limit. Use ``doc3gpp cache purge`` (or the next ``tdoc parse``
    call) to evict.
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
) -> None:
    """Delete every cached zip and markdown file.

    The ``zips/`` and ``markdown/`` subtrees are wiped and recreated
    empty so the cache remains usable for subsequent ``tdoc parse``
    calls. On-disk artefacts referenced from the
    ``tdoc_extracts.markdown_path`` and ``tdoc_extracts.zip_path``
    columns become stale — the next extract will repopulate them.

    By default the command prompts for confirmation; pass ``--yes`` to
    skip the prompt. The default is also overridable via the TOML
    config file or the ``DOC3GPP_CACHE__PURGE_CONFIRM=false`` env var
    — set to ``false`` to skip the prompt globally (CI / scripted
    use).
    """
    settings = get_settings()
    if settings.cache.purge_confirm and not yes:
        typer.confirm("Delete all cached files?", abort=True)
    cache = _build_cache()
    deleted = cache.purge()
    typer.echo(f"Deleted {deleted} files from cache.")


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
    """Delete the SQLite database file and recreate the schema from scratch.

    Intended for recovering from schema drift after an ORM change — Alembic is
    not wired up in this project, so manual migrations are the norm and a
    mismatched schema can leave the DB unusable. This command is destructive:
    every row in every table is wiped.

    Only file-based SQLite URLs are supported:

    - ``sqlite:///...`` / ``sqlite+pysqlite:///...`` — the on-disk file is
      deleted and recreated.
    - ``sqlite:///:memory:`` — there is nothing to delete; the schema is
      re-applied to the (transient) in-memory database.

    MySQL and PostgreSQL URLs are rejected with an explicit error — use the
    backend-native ``DROP DATABASE`` / ``CREATE DATABASE`` workflow instead.

    By default the command prompts for confirmation; pass ``--yes`` to skip
    the prompt. After deletion the SQLAlchemy engine cache is cleared so the
    subsequent ``create_schema()`` opens a fresh connection to the (now
    empty) file. The ``tsgs`` reference table is then re-seeded.
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
    tsg: str = typer.Option(DEFAULT_TSG, help="TSG short name for 3GPP meeting report"),
    closed_years: int | None = typer.Option(
        None,
        min=0,
        max=20,
        help=(
            "Years of closed meetings to keep. "
            "Default: [meeting_sync].closed_years from config file or 2."
        ),
    ),
    future_years: int | None = typer.Option(
        None,
        min=0,
        max=10,
        help=(
            "Years of future meetings to keep. "
            "Default: [meeting_sync].future_years from config file or 1."
        ),
    ),
) -> None:
    """Fetch and store meetings from 3GPP site.

    The ``--tsg`` value is validated against the ``tsgs`` reference table
    (see ``doc3gpp tsg list``). On a fresh database the reference table is
    auto-seeded with the canonical 3GPP TSG list, so this command is safe to
    run without an explicit ``db init`` first.

    The year window defined by ``--closed-years`` and ``--future-years`` is
    *additive*: after upserting the freshly scraped meetings, the sync
    deletes any previously stored meeting whose ``end_date`` falls strictly
    before today minus ``--closed-years`` years. Re-running with a narrower
    ``--closed-years`` therefore trims older rows; widening it does not
    resurrect already-deleted rows (run a fresh sync from the source instead).

    When neither ``--closed-years`` nor ``--future-years`` is passed, the
    values come from the ``[meeting_sync]`` section of the config file
    (``closed_years`` / ``future_years``), or the built-in defaults
    (``2`` / ``1``) when no config file is present.
    """
    settings = get_settings()
    effective_closed = (
        closed_years if closed_years is not None else settings.meeting_sync.closed_years
    )
    effective_future = (
        future_years if future_years is not None else settings.meeting_sync.future_years
    )
    logger.info(
        "Starting meeting sync for TSG %s (closed_years=%s future_years=%s)",
        tsg, effective_closed, effective_future,
    )
    create_schema()
    tsg_service = _ensure_tsg_ready(build_tsg_service())
    tsg = _validate_tsg_short_name(tsg, tsg_service)
    service = build_meeting_service()
    meeting_url = _build_meeting_url(tsg)
    count = service.sync(
        meeting_url,
        max_year_closed=effective_closed,
        max_year_future=effective_future,
        tsg=tsg,
    )
    typer.echo(f"Meeting sync complete: {count} meeting rows stored")


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
    tsg: str | None = typer.Option(None, help="Only list meetings for the given TSG short name"),
    name: str | None = typer.Option(None, help="SQL LIKE pattern to filter meeting name (supports % and _)") ,
    location: str | None = typer.Option(None, help="SQL LIKE pattern to filter meeting location (supports % and _)") ,
    year: int | None = typer.Option(None, help="Filter meetings by end_date year"),
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

    The command supports filtering, pagination, and field selection:
    - `--tsg`: optional TSG short name to restrict results (validated against
      the ``tsgs`` reference table; matches the ``meetings.tsg`` FK exactly).
    - `--name`: SQL LIKE pattern to filter `name` (supports `%` and `_`).
    - `--location`: SQL LIKE pattern to filter `location` (supports `%` and `_`).
    - `--year`: filter by the end_date year.
    - `--limit` / `--offset`: pagination. ``--offset`` is applied first, then
      ``--limit`` caps the returned rows. Use ``--offset`` to page past
      earlier rows without re-running the filters.
    - `--fields`: comma-separated list of fields to include, or `all`.

    By default, the output includes the most useful columns for planning
    further commands (``meeting_id``, ``name``, ``location``, ``start_date``,
    ``end_date``, ``ftp_url``, ``start_doc``, ``end_doc``); pass
    ``--fields all`` for the full schema including ``title`` and ``tsg``.

    Available fields for selection:
    meeting_id, name, title, location, start_date, end_date, ftp_url,
    start_doc, end_doc, tsg

    Output routing:
    - `-o, --output PATH`: write results to PATH instead of stdout.
    - `--format`: ``table`` (legacy tab-separated, default), ``json`` (array of
      objects), or ``markdown`` (GitHub-flavored table).
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

    # Validate --tsg against the reference table (matches `meeting sync`).
    # The table is auto-seeded on a fresh DB so this is safe without `db init`.
    if tsg is not None:
        tsg_service = _ensure_tsg_ready(build_tsg_service())
        tsg = _validate_tsg_short_name(tsg, tsg_service)

    logger.info(
        "Listing meetings limit=%s offset=%s tsg=%s name=%s location=%s year=%s",
        limit, offset, tsg, name, location, year,
    )
    service = build_meeting_service()
    records = service.list_recent(
        limit=limit, offset=offset, tsg=tsg, name_like=name, location_like=location, year=year,
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


@tdoc_app.command("sync")
def tdoc_sync(
    meeting_id: int | None = typer.Option(
        None, help="Meeting ID from the meeting database (see `doc3gpp meeting sync`) to resolve the FTP URL"
    ),
    meeting: str | None = typer.Option(
        None,
        help="Meeting name from the meeting database (see `doc3gpp meeting sync`) to resolve the FTP URL",
    ),
) -> None:
    """Fetch TDocs from a stored meeting record and store them."""

    if (meeting_id is None) == (meeting is None):
        raise typer.BadParameter("Specify exactly one of --meeting-id or --meeting.")

    coordinator = build_tdoc_sync_coordinator()
    try:
        if meeting_id is not None:
            logger.info("Starting TDoc sync for meeting ID %s", meeting_id)
            summary = coordinator.sync_for_meeting_id(meeting_id)
        else:
            logger.info("Starting TDoc sync for meeting name %s", meeting)
            summary = coordinator.sync_for_meeting_name(meeting)
    except MeetingNotFoundError as exc:
        logger.error("Meeting not found: %s", exc)
        raise typer.BadParameter(str(exc)) from None
    except MeetingMissingFtpUrlError as exc:
        logger.error("Meeting has no FTP URL stored: %s", exc)
        raise typer.BadParameter(str(exc)) from None

    typer.echo(summary)


@tdoc_app.command("list")
def tdoc_list(
    limit: int = typer.Option(20, min=1, max=500),
    tsg: str | None = typer.Option(None, help="TSG prefix to filter TDoc IDs (e.g. R5)."),
    year: int | None = typer.Option(None, help="Two-digit year code within the TDoc identifier."),
    meeting: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter meeting name; supports % and _."
    ),
    meeting_id: int | None = typer.Option(
        None,
        help="Exact meeting ID to filter TDocs (see `doc3gpp meeting list`).",
    ),
    source: str | None = typer.Option(
        None,
        help=(
            "Filter TDoc source / contributor (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL source rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    spec: str | None = typer.Option(
        None,
        help=(
            "Filter by technical specification (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL spec rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    wi: str | None = typer.Option(
        None,
        help=(
            "Filter by related work items (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL related_wis rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    title: str | None = typer.Option(
        None,
        help=(
            "Filter by TDoc title (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL title rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    cat: str | None = typer.Option(
        None,
        help=(
            "Filter by CR category / `cr_cat` (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL cr_cat rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    status: str | None = typer.Option(
        None,
        help=(
            "Filter by TDoc status (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL status rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    type: str | None = typer.Option(
        None,
        help=(
            "Filter by TDoc type (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL type rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    revision_of: str | None = typer.Option(
        None,
        "--revision-of",
        help=(
            "Filter by `is_revision_of` (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    revised_to: str | None = typer.Option(
        None,
        "--revised-to",
        help=(
            "Filter by `revised_to` (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    ftp_url: str | None = typer.Option(
        None,
        "--ftp-url",
        help=(
            "Filter by `ftp_url` (SQL LIKE pattern; supports % and _). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL ftp_url rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    uploaded_date: str | None = typer.Option(
        None,
        "--uploaded-date",
        help=(
            "Filter by `uploaded_date`. Accepts:\n\n"
            "- 'null' / 'not-null' to match NULL / NOT NULL rows;\n"
            "- an SQL comparison like \">= '2026-02-31'\", "
            "\"< '2026-01-01'\", \"= '2026-03-15'\", etc. — the operator "
            "(=, !=, <, <=, >, >=) is bound as a parameter so injection "
            "is impossible."
        ),
    ),
    fields: str | None = typer.Option(
        None,
        help="Comma-separated list of fields to include in output, or 'all' for all fields.",
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
    """List recent stored TDocs.

    The command supports filtering and field selection:
    - `--tsg`: filter by TSG prefix (e.g. R5, S2)
    - `--year`: filter by the two-digit year code in the TDoc ID (e.g. 26)
    - `--meeting`: substring filter on the meeting name; auto-wrapped with
      wildcards when no ``%`` / ``_`` is present, so `--meeting RAN5#111`
      matches anything containing that string.
    - `--meeting-id`: exact match on the meeting ID. Combinable with
      `--meeting`; rows must satisfy both predicates.

    The text-column filters (``--source``, ``--spec``, ``--wi``, ``--title``,
    ``--cat``, ``--status``, ``--type``, ``--revision-of``, ``--revised-to``,
    ``--ftp-url``) each accept a SQL ``LIKE`` pattern (with ``%`` / ``_``
    wildcards) and additionally the literal tokens ``null`` / ``not-null``
    to match the column's nullability. A leading ``!`` flips the
    comparison to ``NOT LIKE`` — the ``!`` is consumed and the
    remainder is bound as the pattern (e.g. ``--title "!%Sidelink%"``
    excludes rows whose title contains ``Sidelink``). ``--uploaded-date``
    accepts the same ``null`` / ``not-null`` tokens plus an SQL date
    comparison of the form ``"<op> 'YYYY-MM-DD'"`` with ``<op>`` in
    ``=`` / ``!=`` / ``<`` / ``<=`` / ``>`` / ``>=``. Invalid date inputs
    are rejected at the CLI boundary with a clear error before the
    database is touched. The operator and date literal are bound as
    parameters — injection is impossible.

    Field selection:
    - `--fields`: comma-separated list of fields to include, or `all`.

    By default, the output includes: tdoc_id, meeting_name, title, source, type,
    status, cr_cat, spec, version, related_wis.

    Available fields for selection:
    tdoc_id, title, meeting_id, meeting_name, ftp_url, source, type, status,
    reservation_date, uploaded_date, cr_cat, is_revision_of, revised_to,
    release, spec, version, related_wis, cr_num, cr_pack

    Output routing:
    - `-o, --output PATH`: write results to PATH instead of stdout.
    - `--format`: ``table`` (legacy tab-separated, default), ``json`` (array of
      objects), or ``markdown`` (GitHub-flavored table).
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
        "Listing %s recent TDocs with filters tsg=%s year=%s meeting=%s meeting_id=%s "
        "source=%s spec=%s wi=%s title=%s cat=%s status=%s type=%s "
        "revision_of=%s revised_to=%s ftp_url=%s uploaded_date=%s",
        limit,
        tsg,
        year,
        meeting,
        meeting_id,
        source,
        spec,
        wi,
        title,
        cat,
        status,
        type,
        revision_of,
        revised_to,
        ftp_url,
        uploaded_date,
    )

    service = build_tdoc_service()
    records = service.list_recent_with_meeting(
        limit=limit,
        tsg=tsg,
        meeting_like=_auto_wrap_like(meeting) if meeting else None,
        meeting_id=meeting_id,
        year=year,
        # Rich-filter surface — supports `null` / `not-null` / LIKE.
        source=source,
        spec=spec,
        wi=wi,
        title=title,
        cr_cat=cat,
        status=status,
        tdoc_type=type,
        revision_of=revision_of,
        revised_to=revised_to,
        ftp_url=ftp_url,
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


@tdoc_app.command("parse")
def tdoc_parse(
    tdoc: list[str] = typer.Option(
        None,
        "--tdoc",
        help=(
            "TDoc ID to parse (repeatable for batch). "
            "Case-insensitive for CR-shape IDs (e.g. 'r5s260213' resolves "
            "to 'R5s260213' as stored in the database)."
        ),
    ),
    tdoc_id: list[int] = typer.Option(
        None,
        "--tdoc-id",
        help="Integer TDoc ID to resolve via the tdocs table (repeatable).",
    ),
meeting_id: int | None = typer.Option(
        None,
        "--meeting-id",
        help=(
            "Batch parse all CR-type TDocs under the given meeting ID "
            "(see `doc3gpp meeting list`). Without --force, only TDocs "
            "that have not yet been parsed yet are processed; pass --force "
            "to re-parse every CR-type TDoc under the meeting. "
            "Mutually exclusive with --tdoc and --tdoc-id. Combinable with "
            "the field filters below (--status, --cat, --spec, --wi, "
            "--revision-of, --revised-to, --title, --ftp-url, --source, "
            "--type, --uploaded-date) to narrow the batch before extraction."
        ),
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Filter meeting TDocs by status (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL status rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    cat: str | None = typer.Option(
        None,
        "--cat",
        help=(
            "Filter meeting TDocs by CR category / `cr_cat` (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL cr_cat rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    spec: str | None = typer.Option(
        None,
        "--spec",
        help=(
            "Filter meeting TDocs by technical specification (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL spec rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    wi: str | None = typer.Option(
        None,
        "--wi",
        help=(
            "Filter meeting TDocs by `related_wis` (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL related_wis rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    revision_of: str | None = typer.Option(
        None,
        "--revision-of",
        help=(
            "Filter meeting TDocs by `is_revision_of` (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    revised_to: str | None = typer.Option(
        None,
        "--revised-to",
        help=(
            "Filter meeting TDocs by `revised_to` (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    title_filter: str | None = typer.Option(
        None,
        "--title",
        help=(
            "Filter meeting TDocs by title (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL title rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    ftp_url: str | None = typer.Option(
        None,
        "--ftp-url",
        help=(
            "Filter meeting TDocs by `ftp_url` (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL ftp_url rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    source: str | None = typer.Option(
        None,
        "--source",
        help=(
            "Filter meeting TDocs by source / contributor (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL source rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    tdoc_type: str | None = typer.Option(
        None,
        "--type",
        help=(
            "Filter meeting TDocs by document type (LIKE pattern). "
            "Pass 'null' or 'not-null' to match NULL / NOT NULL type rows; prefix with '!' (e.g. '!%foo%') to negate as NOT LIKE."
        ),
    ),
    uploaded_date: str | None = typer.Option(
        None,
        "--uploaded-date",
        help=(
            "Filter meeting TDocs by `uploaded_date`. Accepts:\n\n"
            "- 'null' / 'not-null' to match NULL / NOT NULL rows;\n"
            "- an SQL comparison like \">= '2026-02-31'\", "
            "\"< '2026-01-01'\", \"= '2026-03-15'\", etc. — the operator "
            "(=, !=, <, <=, >, >=) is bound as a parameter so injection "
            "is impossible."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip both the on-disk zip/markdown cache and the persisted tdoc_cr_details row.",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help=(
            "Reserved for forward-compatibility with the parser's "
            "`full=True` mode (pulls in before_change / after_change "
            "per correction). The current service does not yet wire "
            "this through; accepted silently so existing scripts "
            "keep parsing."
        ),
    ),
) -> None:
    """Download and extract structured CR cover-page fields for one or more TDocs.

    Each ``--tdoc`` value is a canonical TDoc identifier (e.g.
    ``R5s260009``). ``--tdoc-id N`` resolves the integer form against
    the ``tdocs`` table (one DB lookup per integer) and substitutes the
    resolved ``tdoc_id`` string into the batch. An unknown integer
    prints a warning and is skipped — the rest of the batch still runs.

    ``--meeting-id N`` is a batch selector that resolves the meeting
    row, fetches every CR-type TDoc stored under it, and dispatches the
    same batch through :meth:`TDocCrService.extract_many`. Without
    ``--force`` only TDocs that have not yet been parsed (no row in
    ``tdoc_cr_details``) are processed; ``--force`` re-parses every
    CR-type TDoc under the meeting. The selector is mutually exclusive
    with ``--tdoc`` and ``--tdoc-id``.

    Combinable with ``--meeting-id`` are the field filters ``--status``,
    ``--cat``, ``--spec``, ``--wi``, ``--revision-of``, ``--revised-to``,
    ``--title``, ``--ftp-url``, ``--source``, ``--type`` and
    ``--uploaded-date``. The first ten treat their value as a SQL
    ``LIKE`` pattern (with ``%`` / ``_`` wildcards), and additionally
    accept the literal tokens ``null`` / ``not-null`` to match the
    column's nullability. A leading ``!`` flips the comparison to
    ``NOT LIKE`` (e.g. ``--title "!%Sidelink%"`` excludes titles
    containing ``Sidelink``); the ``!`` is consumed before the pattern
    is bound. ``--uploaded-date`` accepts the same ``null`` / ``not-null``
    tokens plus an SQL date comparison of the form ``"<op> 'YYYY-MM-DD'"``
    with ``<op>`` in ``=`` / ``!=`` / ``<`` / ``<=`` / ``>`` / ``>=``.
    The operator and date literal are bound as parameters — the date
    string is never string-interpolated into the SQL, so injection is
    impossible. Invalid date inputs are rejected at the CLI boundary
    with a clear error before the database is touched.

    The service :meth:`TDocCrService.extract_many` catches the
    following per-id exception types internally and skips the broken
    id: ``TDocZipDownloadError``, ``TDocTypeUnsupportedError``,
    ``TDocNotFoundError``, ``CRHeaderMissingError``, plus the
    ``ValueError`` raised by the tdoc_id shape guard. The CLI prints
    one ``FAILED - {ExceptionClassName}: {message}`` line per broken
    id so the operator can tell *which* step failed (download, parse,
    type guard) without tailing the log file. A full traceback is still
    written to the logs for debugging.

    Exit code:

    - ``0`` — at least one TDoc extracted successfully (or, for
      ``--meeting-id`` without ``--force``, every CR-type TDoc was
      already parsed and there was nothing new to do).
    - ``1`` — every TDoc failed, **or** python-docx is missing and the
      batch could not even start, **or** the meeting holds no CR-type
      TDocs, **or** an invalid ``--uploaded-date`` value was supplied.
    """
    if meeting_id is not None and (tdoc or tdoc_id):
        raise typer.BadParameter(
            "--meeting-id is mutually exclusive with --tdoc and --tdoc-id."
        )
    if not tdoc and not tdoc_id and meeting_id is None:
        raise typer.BadParameter(
            "Specify at least one --tdoc, --tdoc-id, or --meeting-id."
        )

    # ``--tdoc`` values are case-normalised to canonical form (R5s######)
    # so a CLI user typing ``r5s260213`` resolves the same DB row as
    # ``R5s260009``. ``--tdoc-id`` is resolved via a single repository
    # lookup per integer; missing ids are skipped.
    tdoc_ids: list[str] = []
    if tdoc:
        tdoc_ids.extend(_normalise_cli_tdoc_id(raw) for raw in tdoc)
    if tdoc_id:
        repo = build_tdoc_repository()
        for raw in tdoc_id:
            resolved = repo.get_by_id(str(raw))
            if resolved is None:
                typer.echo(
                    f"warning: --tdoc-id {raw} not found in tdocs table; skipping.",
                    err=True,
                )
                continue
            tdoc_ids.append(resolved.tdoc_id)

    if meeting_id is not None:
        if uploaded_date is not None:
            try:
                validate_date_filter(uploaded_date)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from None

        # Validate the meeting exists so a typo produces a clear error
        # rather than an empty "no CR-type TDocs" exit.
        meeting = build_meeting_service().get_by_id(meeting_id)
        if meeting is None:
            raise typer.BadParameter(
                f"Unknown meeting_id {meeting_id}. "
                f"Run 'doc3gpp meeting list' to see stored meetings."
            )
        tdoc_repo = build_tdoc_repository()
        cr_tdocs = tdoc_repo.list(
            meeting_id=meeting_id,
            tdoc_type=tdoc_type or "CR",
            limit=_TDOC_BATCH_LIMIT,
            status=status,
            cr_cat=cat,
            spec=spec,
            wi=wi,
            revision_of=revision_of,
            revised_to=revised_to,
            title=title_filter,
            ftp_url=ftp_url,
            source=source,
            uploaded_date=uploaded_date,
        )
        if not cr_tdocs:
            typer.echo(
                f"No CR-type TDocs found for meeting_id {meeting_id}."
            )
            raise typer.Exit(code=1)

        if force:
            tdoc_ids.extend(row.tdoc_id for row in cr_tdocs)
        else:
            cr_repo = build_tdoc_cr_repository()
            new_ids = [
                row.tdoc_id for row in cr_tdocs
                if not cr_repo.get(row.tdoc_id)
            ]
            skipped = len(cr_tdocs) - len(new_ids)
            if not new_ids:
                typer.echo(
                    f"All {len(cr_tdocs)} CR-type TDocs for meeting_id "
                    f"{meeting_id} are already parsed "
                    f"(use --force to re-parse)."
                )
                raise typer.Exit(code=0)
            if skipped:
                logger.info(
                    "Skipped %d already-parsed CR-type TDocs for meeting_id %d",
                    skipped, meeting_id,
                )
            tdoc_ids.extend(new_ids)

    if not tdoc_ids:
        typer.echo("No TDocs to extract (all --tdoc-id values were unknown).")
        raise typer.Exit(code=1)

    logger.info(
        "Starting TDoc parse for %d id(s) (force=%s, full=%s)",
        len(tdoc_ids), force, full,
    )
    service = build_tdoc_cr_service()
    try:
        batch = service.extract_many(tdoc_ids, force=force)
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
            # Defensive: extract_many should always record a success or
            # failure per id; this only fires on a contract regression.
            typer.echo(f"{normalised}: FAILED - extract error (no diagnostic)")
            failures.append(normalised)

    total = len(tdoc_ids)
    successes = total - len(failures)
    typer.echo(f"Extracted {successes}/{total} TDocs ({len(failures)} failures)")
    if successes == 0:
        raise typer.Exit(code=1)


@tdoc_app.command("show")
def tdoc_show(
    tdoc: str = typer.Option(
        ...,
        "--tdoc",
        help=(
            "TDoc ID to show (canonical form, e.g. R5s260009). "
            "Case-insensitive for CR-shape IDs."
        ),
    ),
) -> None:
    """Show TDoc details, including extracted CR fields if available.

    Looks up the TDoc row in the ``tdocs`` table and prints every
    :class:`TDoc` field under a ``[TDoc]`` section. When one or more
    matching ``tdoc_cr_details`` rows exist (i.e. ``tdoc parse`` has
    been run for this id at least once) the parsed cover-page fields
    are printed under one ``[Extracted Details]`` block **per revision**
    (each revision is keyed by the immutable download URL — multiple
    URLs share the same ``tdoc_id`` across revisions). The
    ``corrections`` list is JSON-dumped for full fidelity.

    The ``--tdoc`` argument is case-insensitive for CR-shape IDs (so
    ``r5s260213`` and ``R5s260213`` resolve the same row); the DB still
    stores the canonical form.

    Raises a ``BadParameter`` when the requested TDoc is not stored.
    """
    repo = build_tdoc_repository()
    record = repo.get_by_id(_normalise_cli_tdoc_id(tdoc))
    if record is None:
        raise typer.BadParameter(
            f"Unknown TDoc '{tdoc}'. Run 'doc3gpp tdoc list' to see stored TDocs, "
            f"or 'doc3gpp tdoc sync' to ingest a meeting's TDocs first."
        )

    typer.echo("[TDoc]")
    for f in dataclass_fields(record):
        value = getattr(record, f.name)
        if value is None:
            value = "-"
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        else:
            value = str(value)
        typer.echo(f"{f.name}: {value}")

    cr_repo = build_tdoc_cr_repository()
    details_list = cr_repo.get(tdoc)
    meta_list = cr_repo.get_extract_meta(tdoc)
    if not details_list and not meta_list:
        typer.echo("[Extracted Details]")
        typer.echo("No extracted details; run `doc3gpp tdoc parse --tdoc <id>` first.")
        return

    meta_by_url = {meta.ftp_url: meta for meta in meta_list}
    for details in details_list:
        typer.echo("[Extracted Details]")
        if details.ftp_url:
            typer.echo(f"ftp_url: {details.ftp_url}")
        typer.echo(f"spec: {details.spec or '-'}")
        typer.echo(f"cr_num: {details.cr_num or '-'}")
        typer.echo(f"rev: {details.rev or '-'}")
        typer.echo(f"version: {details.version or '-'}")
        typer.echo(f"title: {details.title or '-'}")
        typer.echo(f"source: {details.source or '-'}")
        typer.echo(f"tsg: {details.tsg or '-'}")
        typer.echo(f"related_wis: {details.related_wis or '-'}")
        typer.echo(f"date: {details.date or '-'}")
        typer.echo(f"cr_cat: {details.cr_cat or '-'}")
        typer.echo(f"release: {details.release or '-'}")
        typer.echo(f"reason_for_change: {_truncate_for_display(details.reason_for_change)}")
        typer.echo(
            "consequences_if_not_approved: "
            f"{_truncate_for_display(details.consequences_if_not_approved)}"
        )
        typer.echo(f"clauses_affected: {details.clauses_affected or '-'}")
        typer.echo(f"ats_version: {details.ats_version or '-'}")
        typer.echo(f"ttcn_release: {details.ttcn_release or '-'}")
        typer.echo(f"test_case: {details.test_case or '-'}")
        typer.echo(f"test_suite: {details.test_suite or '-'}")
        typer.echo(f"ue: {details.ue or '-'}")
        typer.echo(f"ss: {details.ss or '-'}")
        typer.echo(f"year: {details.year if details.year is not None else '-'}")
        typer.echo(f"tech: {details.tech or '-'}")
        typer.echo(f"parser_version: {details.parser_version}")
        typer.echo(f"corrections: {json.dumps(details.corrections, ensure_ascii=False, indent=2)}")
        meta = meta_by_url.get(details.ftp_url or "")
        if meta is not None:
            typer.echo(f"extracted_at: {_fmt_dt(meta.extracted_at)}")


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
    """Fetch and store the active WIs for one TSG from 3gpp.org.

    The ``--tsg`` value is validated against the ``tsgs`` reference table
    (see ``doc3gpp tsg list``). On a fresh database the reference table is
    auto-seeded so this command is safe to run without an explicit
    ``db init`` first. Existing rows for the same ``(wi_id, tsg_short)``
    pair are updated in place, so re-running this command refreshes the
    acronym, release and name fields without duplication.
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
    """List stored WIs matching the filters (default output: wi_id, acronym, release, name).

    The command exposes four optional SQL ``LIKE`` filters:

    - ``--tsg``: restrict results to a single TSG short name (case-insensitive).
    - ``--name``: SQL ``LIKE`` pattern to filter the WI title.
    - ``--acronym``: SQL ``LIKE`` pattern to filter the WI acronym.
    - ``--release``: SQL ``LIKE`` pattern to filter the release marker
      (e.g. ``Rel-19``).

    By default the output prints four columns: ``wi_id``, ``acronym``,
    ``release`` and ``name``. Each value is rendered as a plain string,
    ``-`` when the underlying field is missing.

    Output routing:
    - `-o, --output PATH`: write results to PATH instead of stdout.
    - `--format`: ``table`` (legacy tab-separated, default), ``json`` (array of
      objects), or ``markdown`` (GitHub-flavored table).
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


def main() -> None:
    app()


if __name__ == "__main__":
    main()