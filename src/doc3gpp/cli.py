from __future__ import annotations

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
) -> None:
    """List recent meetings from database."""

    logger.info("Listing %s recent meetings for tsg=%s", limit, tsg)
    service = MeetingService(SQLAlchemyMeetingRepository())
    records = service.list_recent(limit=limit, tsg=tsg)
    if not records:
        typer.echo("No meetings found")
        return

    for item in records:
        assert isinstance(item, Meeting)
        typer.echo(
            "\t".join(
                [
                    str(item.meeting_id),
                    item.name,
                    item.start_date.isoformat(),
                    item.end_date.isoformat(),
                    item.ftp_url or "-",
                    _fmt_dt(item.updated_at),
                ]
            )
        )


@tdoc_app.command("sync")
def tdoc_sync(
    meeting_id: int = typer.Option(
        ..., help="Meeting ID from the meetings database to resolve the FTP URL"
    ),
    meeting: str | None = typer.Option(None, help="Optional meeting identifier to associate with imported TDocs"),
) -> None:
    """Fetch TDocs from a stored meeting record and store them."""

    logger.info("Starting TDoc sync for meeting ID %s", meeting_id)
    create_schema()
    service = TDocService(SQLAlchemyTDocRepository())
    meeting_service = MeetingService(SQLAlchemyMeetingRepository())
    meeting_record = meeting_service.get_by_id(meeting_id)
    if meeting_record is None:
        logger.error("Meeting not found for ID %s", meeting_id)
        raise typer.BadParameter(f"Meeting not found with id {meeting_id}")
    if not meeting_record.ftp_url:
        logger.error("Meeting %s does not have an FTP URL stored", meeting_id)
        raise typer.BadParameter(
            f"Meeting {meeting_id} does not have an FTP URL stored"
        )

    count = service.sync_from_meeting_ftp(
        ftp_url=meeting_record.ftp_url,
        meeting=meeting or meeting_record.name,
    )
    typer.echo(f"TDoc sync complete: {count} records stored")


@tdoc_app.command("add")
def tdoc_add(
    tdoc_id: str = typer.Option(..., help="TDoc ID"),
    title: str = typer.Option(..., help="TDoc title"),
    meeting: str | None = typer.Option(None, help="Meeting identifier"),
    url: str | None = typer.Option(None, help="Document URL"),
) -> None:
    """Insert or update one TDoc."""

    logger.info("Saving TDoc %s for meeting %s", tdoc_id, meeting)
    service = TDocService(SQLAlchemyTDocRepository())
    service.save(TDoc(tdoc_id=tdoc_id, title=title, meeting=meeting, url=url))
    typer.echo(f"Saved {tdoc_id}")


@tdoc_app.command("list")
def tdoc_list(limit: int = typer.Option(20, min=1, max=500)) -> None:
    """List recent stored TDocs."""

    logger.info("Listing %s recent TDocs", limit)
    service = TDocService(SQLAlchemyTDocRepository())
    records = service.list_recent(limit=limit)
    if not records:
        typer.echo("No TDocs found")
        return

    for item in records:
        typer.echo(f"{item.tdoc_id}\t{item.title}\t{item.meeting or '-'}\t{item.url or '-'}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
