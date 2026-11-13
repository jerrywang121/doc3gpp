"""Typer adapter for the ``doc3gpp listserv`` command group."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import typer

from doc3gpp.models.listserv_failures import is_index_failure as _is_index_failure

listserv_app = typer.Typer(help="ETSI LISTSERV archive commands")

_FORMATS = ("table", "json")


def _check_format(fmt: str) -> str:
    value = fmt.strip().lower()
    if value not in _FORMATS:
        raise typer.BadParameter(f"--format must be one of: {', '.join(_FORMATS)}")
    return value


def _validate_exact_inputs(list_id: str, dates: list[str]) -> None:
    """Validate the exact list ID and date expressions before any DB or source work."""
    from doc3gpp.models.listserv import normalize_list_id, parse_date_set
    from doc3gpp.scraping.listserv_source import archive_index_url

    try:
        archive_index_url(normalize_list_id(list_id))
        parse_date_set(dates)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _report_batch(verb: str, result: Any) -> None:
    source_failures = {
        k: v for k, v in result.failures.items() if not _is_index_failure(v)
    }
    index_failures = {
        k: v for k, v in result.failures.items() if _is_index_failure(v)
    }
    typer.echo(
        f"LISTSERV {verb} complete: processed={result.processed} "
        f"skipped={result.skipped} failed={len(source_failures)} "
        f"index_failed={len(index_failures)}"
    )
    for url, reason in source_failures.items():
        typer.echo(f"FAILED {url}: {reason}", err=True)
    if index_failures:
        typer.echo(
            "Derived index updates failed; source data was committed "
            "(run `doc3gpp listserv index --rebuild` to repair):",
            err=True,
        )
        for url, reason in index_failures.items():
            typer.echo(f"INDEX FAILED {url}: {reason}", err=True)
    if result.failures:
        raise typer.Exit(code=1)


def _run_batch(
    verb: str,
    builder_name: str,
    list_id: str,
    dates: list[str] | None,
    force: bool,
) -> None:
    date_list = list(dates or [])
    _validate_exact_inputs(list_id, date_list)
    from doc3gpp.services import factory
    from doc3gpp.storage.db.migrate import create_schema

    create_schema("main")
    service = getattr(factory, builder_name)()
    try:
        result = getattr(service, verb)(
            list_id,
            dates=date_list,
            force=force,
            on_progress=lambda m: typer.echo(m, err=True),
        )
    finally:
        close: Callable[[], None] | None = getattr(service, "close", None)
        if close is not None:
            close()
    _report_batch(verb, result)


_DATE_HELP = (
    "Date comparison: <op> YYYY-MM-DD or <op> 'YYYY-MM-DD' "
    "(e.g. >= 2026-10-15); operators =, !=, <, <=, >, >=. "
    "Repeatable; expressions intersect."
)
_RICH_FILTER_HELP = (
    "Rich LIKE: % matches any number of characters; _ matches one character; "
    "prefix ! to negate; 'null' matches SQL NULL; 'not-null' matches SQL NOT NULL; "
    "plain values match exactly."
)
_TDOCS_FILTER_HELP = (
    "For tdocs, null means no IDs (both subject and extracted fields NULL or empty); "
    "not-null means at least one ID. Stored TDoc IDs are comma-delimited; use a "
    "contains pattern such as %R1-2610793% to match an ID within a multi-ID value."
)


@listserv_app.command("sync")
def listserv_sync(
    list_id: str = typer.Argument(..., help="Exact LISTSERV list ID (e.g. 3GPP_TSG_RAN_WG5)."),
    date: list[str] | None = typer.Option(None, "--date", help=_DATE_HELP),  # noqa: B008
    force: bool = typer.Option(False, "--force", "-f", help="Re-fetch covered archive pages."),
) -> None:
    """Sync email items (metadata only) for one LISTSERV list."""
    _run_batch("sync", "build_listserv_sync_service", list_id, date, force)


@listserv_app.command("parse")
def listserv_parse(
    list_id: str = typer.Argument(..., help="Exact LISTSERV list ID (e.g. 3GPP_TSG_RAN_WG5)."),
    date: list[str] | None = typer.Option(None, "--date", help=_DATE_HELP),  # noqa: B008
    force: bool = typer.Option(False, "--force", "-f", help="Re-parse selected email items even if already parsed."),
) -> None:
    """Parse stored email items for one LISTSERV list."""
    _run_batch("parse", "build_listserv_parse_service", list_id, date, force)


@listserv_app.command("list")
def listserv_list() -> None:
    """List LISTSERV catalog entries and their TSG and description."""
    from doc3gpp.services.factory import build_listserv_service
    from doc3gpp.storage.db.migrate import create_schema

    create_schema("main")
    typer.echo("list_id\ttsg\tdescription")
    for item in build_listserv_service().list_lists():
        typer.echo(
            "\t".join(
                value if value is not None else "-"
                for value in (item.list_id, item.tsg, item.description)
            )
        )


@listserv_app.command("search")

def listserv_search(
    date: list[str] | None = typer.Option(None, "--date", help=_DATE_HELP),  # noqa: B008
    list_id: str | None = typer.Option(None, "--list-id", help=f"Rich LIKE filter on the stored list ID. {_RICH_FILTER_HELP}"),
    sender: str | None = typer.Option(None, "--from", help=f"Rich LIKE filter on sender. {_RICH_FILTER_HELP}"),
    subject: str | None = typer.Option(None, "--subject", help=f"Rich LIKE filter on subject. {_RICH_FILTER_HELP}"),
    tdocs: str | None = typer.Option(
        None,
        "--tdocs",
        help=f"Rich LIKE filter on TDoc IDs. {_RICH_FILTER_HELP} {_TDOCS_FILTER_HELP}",
    ),
    text: str | None = typer.Option(None, "--text", help='FTS5 MATCH query (e.g. "handover OR mobility").'),
    semantic: str | None = typer.Option(None, "--semantic", help="Natural-language embedding query."),
    limit: int = typer.Option(20, "--limit", help="Maximum results (0-150)."),
    offset: int = typer.Option(0, "--offset", help="Non-negative number of results to skip."),
    fmt: str = typer.Option("table", "--format", help="Output format: table or json."),
) -> None:
    """Search stored LISTSERV email items (SQL/FTS5/semantic; never downloads)."""
    from doc3gpp.models.listserv import ListservFilters, parse_date_set
    from doc3gpp.models.listserv_payloads import listserv_search_payload
    from doc3gpp.models.search import SearchError, SearchQueryError
    from doc3gpp.services.factory import build_listserv_search_service
    from doc3gpp.storage.db.migrate import create_schema

    fmt = _check_format(fmt)
    try:
        filters = ListservFilters(
            list_id=list_id,
            sender=sender,
            subject=subject,
            tdocs=tdocs,
            dates=parse_date_set(date) if date else None,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    create_schema("main")
    service = build_listserv_search_service()
    try:
        try:
            results = service.search(text=text, semantic=semantic, filters=filters)
        except SearchQueryError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        except SearchError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from None
        payloads = [listserv_search_payload(r) for r in results]
        if fmt == "json":
            typer.echo(json.dumps(payloads, ensure_ascii=False, indent=2))
            return
        for p in payloads:
            typer.echo(
                "\t".join(
                    str(p[k] if p[k] is not None else "-")
                    for k in ("date", "list_id", "sender", "subject", "url")
                )
            )
    finally:
        service.close()


@listserv_app.command("show")
def listserv_show(
    url: str = typer.Argument(..., help="Exact URL returned by listserv_search for a stored email item; pass it unchanged."),
    fmt: str = typer.Option("table", "--format", help="Output format: table or json."),
) -> None:
    """Show one stored email item with its stored content."""
    from doc3gpp.models.listserv_payloads import listserv_record_payload
    from doc3gpp.services.factory import build_listserv_service
    from doc3gpp.services.listserv_service import ListservMessageNotFoundError
    from doc3gpp.storage.db.migrate import create_schema

    fmt = _check_format(fmt)
    create_schema("main")
    try:
        record = build_listserv_service().show(url)
    except ListservMessageNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    payload = listserv_record_payload(record)
    if fmt == "json":
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for key, value in payload.items():
        if key == "content":
            continue
        typer.echo(f"{key}: {value if value is not None else '-'}")
    typer.echo("")
    typer.echo(payload["content"] if payload["content"] is not None else "(not parsed)")


@listserv_app.command("index")

def listserv_index(
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild the LISTSERV FTS5 index."),
    rebuild_embeddings: bool = typer.Option(
        False, "--rebuild-embeddings", help="Rebuild the LISTSERV vector index."
    ),
    rebuild_all: bool = typer.Option(False, "--rebuild-all", help="Rebuild both indexes; mutually exclusive with --rebuild and --rebuild-embeddings."),
    batch: int | None = typer.Option(None, "--batch", min=1, help="Batch size (at least 1)."),
    resume: bool = typer.Option(False, "--resume", help="Resume from the last cursor."),
    stale_only: bool = typer.Option(False, "--stale-only", help="Only rebuild stale rows."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress rebuild progress messages."),
) -> None:
    """Show or maintain LISTSERV FTS5 and vector indexes."""
    from doc3gpp.cli import _render_index_result
    from doc3gpp.models.index import IndexRequest
    from doc3gpp.models.search import SearchError
    from doc3gpp.services.factory import build_listserv_index_service
    from doc3gpp.storage.db.migrate import create_schema

    create_schema("main")
    service = build_listserv_index_service()
    try:
        request = IndexRequest(
            rebuild=rebuild,
            rebuild_embeddings=rebuild_embeddings,
            rebuild_all=rebuild_all,
            batch=batch,
            resume=resume,
            stale_only=stale_only,
        )
        try:
            result = service.rebuild(
                request, quiet=quiet, on_progress=None if quiet else typer.echo
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        except SearchError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None
        _render_index_result(
            result,
            resource="listserv",
            action=any((rebuild, rebuild_embeddings, rebuild_all)),
        )
    finally:
        service.close()
