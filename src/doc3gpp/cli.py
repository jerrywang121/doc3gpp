from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import datetime

import logging
import typer
from sqlalchemy import text

from doc3gpp.config import get_settings
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi
from doc3gpp.services.factory import (
    build_meeting_service,
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
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine

app = typer.Typer(help="doc3gpp command line tools")
db_app = typer.Typer(help="database commands")
meeting_app = typer.Typer(help="meeting commands")
tdoc_app = typer.Typer(help="tdoc commands")
tsg_app = typer.Typer(help="tsg reference data commands")
wi_app = typer.Typer(help="wi commands")
app.add_typer(db_app, name="db")
app.add_typer(meeting_app, name="meeting")
app.add_typer(tdoc_app, name="tdoc")
app.add_typer(tsg_app, name="tsg")
app.add_typer(wi_app, name="wi")

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


@meeting_app.command("sync")
def meeting_sync(
    tsg: str = typer.Option(DEFAULT_TSG, help="TSG short name for 3GPP meeting report"),
    closed_years: int = typer.Option(2, min=0, max=20, help="Years of closed meetings to keep"),
    future_years: int = typer.Option(1, min=0, max=10, help="Years of future meetings to keep"),
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
    """
    logger.info("Starting meeting sync for TSG %s", tsg)
    create_schema()
    tsg_service = _ensure_tsg_ready(build_tsg_service())
    tsg = _validate_tsg_short_name(tsg, tsg_service)
    service = build_meeting_service()
    meeting_url = _build_meeting_url(tsg)
    count = service.sync(meeting_url, max_year_closed=closed_years, max_year_future=future_years)
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
) -> None:
    """List meetings from database with optional filtering and pagination.

    The command supports filtering, pagination, and field selection:
    - `--tsg`: optional TSG short name to restrict results (validated against
      the ``tsgs`` reference table; matches the meeting name prefix).
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
    ``--fields all`` for the full schema including ``title`` and ``updated_at``.

    Available fields for selection:
    meeting_id, name, title, location, start_date, end_date, ftp_url,
    start_doc, end_doc, updated_at
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
        "updated_at",
    ]

    # Default: all fields except title and updated_at (kept compact); ftp_url
    # is retained because it drives `tdoc sync` planning.
    default_fields = [f for f in allowed_fields if f not in ("title", "updated_at")]

    out_fields = _parse_field_selection(fields, allowed_fields, default_fields)

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
    if not records:
        typer.echo("No meetings found")
        return

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
            elif f == "updated_at":
                vals.append(_fmt_dt(v))
            else:
                vals.append(str(v))

        typer.echo("\t".join(vals))


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
            count = coordinator.sync_for_meeting_id(meeting_id)
        else:
            logger.info("Starting TDoc sync for meeting name %s", meeting)
            count = coordinator.sync_for_meeting_name(meeting)
    except MeetingNotFoundError as exc:
        logger.error("Meeting not found: %s", exc)
        raise typer.BadParameter(str(exc)) from None
    except MeetingMissingFtpUrlError as exc:
        logger.error("Meeting has no FTP URL stored: %s", exc)
        raise typer.BadParameter(str(exc)) from None

    typer.echo(f"TDoc sync complete: {count} records stored")


@tdoc_app.command("list")
def tdoc_list(
    limit: int = typer.Option(20, min=1, max=500),
    tsg: str | None = typer.Option(None, help="TSG prefix to filter TDoc IDs (e.g. R5)."),
    year: int | None = typer.Option(None, help="Two-digit year code within the TDoc identifier."),
    meeting: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter meeting name; supports % and _."
    ),
    source: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter TDoc source (e.g. 'Qualcomm%', '%Huawei%')."
    ),
    spec: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter by technical specification (e.g. '38.331%')."
    ),
    wi: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter by related work items."
    ),
    title: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter by TDoc title."
    ),
    cat: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter by CR category."
    ),
    status: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter by TDoc status."
    ),
    type: str | None = typer.Option(
        None,
        help="SQL LIKE pattern to filter by TDoc type."
    ),
    fields: str | None = typer.Option(
        None,
        help="Comma-separated list of fields to include in output, or 'all' for all fields.",
    ),
) -> None:
    """List recent stored TDocs.

    The command supports filtering and field selection:
    - `--tsg`: filter by TSG prefix (e.g. R5, S2)
    - `--year`: filter by the two-digit year code in the TDoc ID (e.g. 26)
    - `--meeting`: substring filter on the meeting name; auto-wrapped with
      wildcards when no ``%`` / ``_`` is present, so `--meeting RAN5#111`
      matches anything containing that string.
    - `--source`: SQL LIKE pattern to filter by source/contributor
    - `--spec`: SQL LIKE pattern to filter by technical specification
    - `--wi`: SQL LIKE pattern to filter by related work items
    - `--title`: SQL LIKE pattern to filter by TDoc title
    - `--cat`: SQL LIKE pattern to filter by CR category
    - `--status`: SQL LIKE pattern to filter by TDoc status
    - `--type`: SQL LIKE pattern to filter by TDoc type
    - `--fields`: comma-separated list of fields to include, or `all`

    By default, the output includes: tdoc_id, meeting_name, title, source, type,
    status, cr_cat, spec, version, related_wis.

    Available fields for selection:
    tdoc_id, title, meeting_id, meeting_name, url, source, type, status,
    reservation_date, uploaded_date, cr_cat, is_revision_of, revised_to,
    release, spec, version, related_wis, cr_num, cr_pack, updated_at
    """

    # ``meeting_name`` is a top-level attribute on ``TDocWithMeeting``; the
    # rest live on ``TDocWithMeeting.tdoc``.
    # ``meeting_name`` is a top-level attribute on ``TDocWithMeeting``; the
    # rest live on ``TDocWithMeeting.tdoc``.
    allowed_fields = [f.name for f in dataclass_fields(TDoc)] + ["meeting_name"]
    default_fields = [
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

    out_fields = _parse_field_selection(fields, allowed_fields, default_fields)

    logger.info(
        "Listing %s recent TDocs with filters tsg=%s year=%s meeting=%s source=%s spec=%s wi=%s title=%s cat=%s status=%s type=%s",
        limit,
        tsg,
        year,
        meeting,
        source,
        spec,
        wi,
        title,
        cat,
        status,
        type,
    )

    service = build_tdoc_service()
    records = service.list_recent_with_meeting(
        limit=limit,
        tsg=tsg,
        meeting_like=_auto_wrap_like(meeting) if meeting else None,
        year=year,
        source_like=source,
        spec_like=spec,
        wi_like=wi,
        title_like=title,
        cat_like=cat,
        status_like=status,
        type_like=type,
    )
    if not records:
        typer.echo("No TDocs found")
        return

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
            elif f == "updated_at":
                vals.append(_fmt_dt(v))
            else:
                vals.append(str(v))

        typer.echo("\t".join(vals))


@tsg_app.command("list")
def tsg_list(
    fields: str | None = typer.Option(
        None,
        help="Comma-separated list of fields to include (or 'all' for all fields).",
    ),
) -> None:
    """List TSG reference records from the database.

    The command supports field selection:
    - ``--fields``: comma-separated list of fields to include in output, or ``all``.

    By default, the output includes ``tsg_name``, ``short_name``, and
    ``description`` to keep the listing compact. Use ``--fields all`` to
    include ``url`` as well.
    """
    allowed_fields = [f.name for f in dataclass_fields(Tsg)]
    default_fields = ["tsg_name", "short_name", "description"]

    out_fields = _parse_field_selection(fields, allowed_fields, default_fields)

    logger.info("Listing TSG reference records (fields=%s)", out_fields)
    service = build_tsg_service()
    records = service.list_all()
    if not records:
        typer.echo("No TSG records found. Run 'doc3gpp db init' to seed defaults.")
        return

    for item in records:
        assert isinstance(item, Tsg)
        vals = [str(getattr(item, f) or "-") for f in out_fields]
        typer.echo("\t".join(vals))


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
    acronym, release, name and ``updated_at`` fields without duplication.
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
    if not records:
        typer.echo("No WIs found")
        return

    default_fields = ["wi_id", "acronym", "release", "name"]
    for item in records:
        assert isinstance(item, Wi)
        vals = [str(getattr(item, f) or "-") for f in default_fields]
        typer.echo("\t".join(vals))


def main() -> None:
    app()


if __name__ == "__main__":
    main()