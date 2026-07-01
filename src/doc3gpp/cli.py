from __future__ import annotations

from dataclasses import fields as dataclass_fields
from datetime import datetime

import logging
import typer
from sqlalchemy import text

from doc3gpp.config import get_settings
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_service import TDocService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import TDocORM
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

app = typer.Typer(help="doc3gpp command line tools")
db_app = typer.Typer(help="database commands")
meetings_app = typer.Typer(help="meetings commands")
tdoc_app = typer.Typer(help="tdoc commands")
app.add_typer(db_app, name="db")
app.add_typer(meetings_app, name="meetings")
app.add_typer(tdoc_app, name="tdoc")

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


def _build_meetings_url(tsg: str) -> str:
    return f"https://www.3gpp.org/dynareport?code=Meetings-{tsg.upper()}.htm"


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
    """Create schema for current backend."""

    logger.info("Initializing database schema")
    create_schema()
    typer.echo("Database schema initialized")


@meetings_app.command("sync")
def meetings_sync(
    tsg: str = typer.Option(DEFAULT_TSG, help="TSG short name for 3GPP meetings report"),
    closed_years: int = typer.Option(2, min=0, max=20, help="Years of closed meetings to keep"),
    future_years: int = typer.Option(1, min=0, max=10, help="Years of future meetings to keep"),
) -> None:
    """Fetch and store meetings from 3GPP site."""

    logger.info("Starting meetings sync for TSG %s", tsg)
    create_schema()
    service = MeetingService(SQLAlchemyMeetingRepository())
    meetings_url = _build_meetings_url(tsg)
    count = service.sync(meetings_url, max_year_closed=closed_years, max_year_future=future_years)
    typer.echo(f"Meetings sync complete: {count} meeting rows stored")


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat(sep=" ", timespec="seconds")


@meetings_app.command("list")
def meetings_list(
    limit: int = typer.Option(20, min=1, max=500),
    tsg: str | None = typer.Option(None, help="Only list meetings for the given TSG short name"),
    name: str | None = typer.Option(None, help="SQL LIKE pattern to filter meeting name (supports % and _)") ,
    location: str | None = typer.Option(None, help="SQL LIKE pattern to filter meeting location (supports % and _)") ,
    year: int | None = typer.Option(None, help="Filter meetings by end_date year"),
    fields: str | None = typer.Option(
        None,
        help="Comma-separated list of fields to include (or 'all' for all fields).",
    ),
) -> None:
    """List recent meetings from database.

    The command supports additional filters and field selection:
    - `--tsg`: optional TSG short name to restrict results (matches name prefix)
    - `--name`: SQL LIKE pattern to filter `name` (supports `%` and `_`)
    - `--location`: SQL LIKE pattern to filter `location` (supports `%` and `_`)
    - `--year`: filter by the end_date year
    - `--fields`: comma-separated list of fields to include in output, or `all`

    By default, the output includes all fields except `title`, `updated_at`, and
    `ftp_url` to keep the listing compact.

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

    # Default: all fields except title, updated_at and ftp_url
    default_fields = [f for f in allowed_fields if f not in ("title", "updated_at", "ftp_url")]

    if fields:
        requested = [f.strip() for f in fields.split(",") if f.strip()]
        if "all" in [f.lower() for f in requested]:
            out_fields = allowed_fields
        else:
            invalid = [f for f in requested if f not in allowed_fields]
            if invalid:
                valid_list = ", ".join(allowed_fields)
                raise typer.BadParameter(
                    f"Unknown field(s): {', '.join(invalid)}. Valid fields: {valid_list}"
                )
            out_fields = requested
    else:
        out_fields = default_fields

    logger.info("Listing %s recent meetings for tsg=%s name=%s location=%s year=%s", limit, tsg, name, location, year)
    service = MeetingService(SQLAlchemyMeetingRepository())
    records = service.list_recent(limit=limit, tsg=tsg, name_like=name, location_like=location, year=year)
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
        None, help="Meeting ID from the meetings database to resolve the FTP URL"
    ),
    meeting: str | None = typer.Option(
        None,
        help="Meeting name from the meetings database to resolve the FTP URL",
    ),
) -> None:
    """Fetch TDocs from a stored meeting record and store them."""

    if (meeting_id is None) == (meeting is None):
        raise typer.BadParameter("Specify exactly one of --meeting-id or --meeting.")

    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())
    meeting_service = MeetingService(SQLAlchemyMeetingRepository())

    if meeting_id is not None:
        logger.info("Starting TDoc sync for meeting ID %s", meeting_id)
        meeting_record = meeting_service.get_by_id(meeting_id)
    else:
        logger.info("Starting TDoc sync for meeting name %s", meeting)
        meeting_record = meeting_service.get_by_name(meeting)

    if meeting_record is None:
        if meeting_id is not None:
            logger.error("Meeting not found for ID %s", meeting_id)
            raise typer.BadParameter(f"Meeting not found with id {meeting_id}")
        logger.error("Meeting not found for name %s", meeting)
        raise typer.BadParameter(f"Meeting not found with name {meeting}")

    if not meeting_record.ftp_url:
        logger.error(
            "Meeting %s does not have an FTP URL stored",
            meeting_id if meeting_id is not None else meeting,
        )
        raise typer.BadParameter(
            f"Meeting {meeting_id if meeting_id is not None else meeting} does not have an FTP URL stored"
        )

    count = service.sync_from_meeting_ftp(
        ftp_url=meeting_record.ftp_url,
        meeting_id=meeting_record.meeting_id,
    )
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
    - `--meeting`: SQL LIKE pattern to filter by meeting name
    - `--source`: SQL LIKE pattern to filter by source/contributor
    - `--spec`: SQL LIKE pattern to filter by technical specification
    - `--wi`: SQL LIKE pattern to filter by related work items
    - `--title`: SQL LIKE pattern to filter by TDoc title
    - `--cat`: SQL LIKE pattern to filter by CR category
    - `--status`: SQL LIKE pattern to filter by TDoc status
    - `--type`: SQL LIKE pattern to filter by TDoc type
    - `--fields`: comma-separated list of fields to include, or 'all'

    By default, the output includes: tdoc_id, meeting_name, title, source, type,
    status, cr_cat, spec, version, related_wis.

    Available fields for selection:
    tdoc_id, title, meeting_id, meeting_name, url, source, type, status,
    reservation_date, uploaded_date, cr_cat, is_revision_of, revised_to,
    release, spec, version, related_wis, cr_num, cr_pack
    """

    allowed_fields = [f.name for f in dataclass_fields(TDoc)]
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

    if fields:
        requested = [f.strip() for f in fields.split(",") if f.strip()]
        if "all" in [f.lower() for f in requested]:
            out_fields = allowed_fields
        else:
            invalid = [f for f in requested if f not in allowed_fields]
            if invalid:
                valid_list = ", ".join(allowed_fields)
                raise typer.BadParameter(
                    f"Unknown field(s): {', '.join(invalid)}. Valid fields: {valid_list}"
                )
            out_fields = requested
    else:
        out_fields = default_fields

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

    service = TDocService(SQLAlchemyTDocRepository())
    records = service.list_recent(
        limit=limit,
        tsg=tsg,
        meeting_like=meeting,
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
        vals: list[str] = []
        for f in out_fields:
            v = getattr(item, f, None)
            if v is None:
                vals.append("-")
                continue

            vals.append(str(v))

        typer.echo("\t".join(vals))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
