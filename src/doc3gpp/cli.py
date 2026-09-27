from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
import tempfile
from dataclasses import fields as dataclass_fields
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

from doc3gpp.cli_auto_sync import (
    _build_meeting_url,
    collect_tdoc_candidates_for_url,
    trigger_auto_sync,
)
from doc3gpp.cli_filters import parse_tdoc_id, validate_date_filter
from doc3gpp.cli_server import server_app
from doc3gpp.cli_url_helpers import (
    _looks_like_3gpp_file_url,
    _looks_like_3gpp_folder_url,
    is_3gpp_ftp_url,
)
from doc3gpp.config import get_settings
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.schema_info import SCHEMA_FIELDS, schema_payload
from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.spec_doc import SpecDocSearchFilters
from doc3gpp.models.sync import BulkSyncOutcome, SyncOutcome
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tdoc_cr import (
    DirectParseBatchResult,
    TDocCRDetails,
)
from doc3gpp.models.tdoc_show import TDocShowRecord, TDocShowRecordByUrl, TDocShowRepos
from doc3gpp.models.testcase import TestCaseDetail, TestCaseStatus, TestCaseWithStatuses
from doc3gpp.models.tsg import Tsg
from doc3gpp.models.wi import Wi
from doc3gpp.parsers.direct_extractor import (
    NotAFolderError,
    extract_tdoc_id_from_filename,
)
from doc3gpp.parsers.docx_converter import PythonDocxNotInstalledError
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.scraping.cache import CacheStatus, TDocCache
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.scraping.tdoc_zip_source import canonicalise_tdoc_id
from doc3gpp.services.factory import (
    build_meeting_service,
    build_spec_doc_service,
    build_spec_service,
    build_tdoc_cr_change_details_repository,
    build_tdoc_cr_repository,
    build_tdoc_cr_service,
    build_tdoc_cr_ttcn_repository,
    build_tdoc_file_repository,
    build_tdoc_repository,
    build_tdoc_service,
    build_tdoc_sync_coordinator,
    build_testcase_service,
    build_tsg_service,
    build_wi_service,
)
from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
)
from doc3gpp.services.tdoc_cr_service import (
    TDocNotFoundError,
    TDocTypeUnsupportedError,
    TDocZipDownloadError,
    _read_cached_markdown_path,
)
from doc3gpp.services.tdoc_sync_coordinator import (
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
from doc3gpp.settings.schema import Settings, env_var_for_dotted_key
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import (
    get_engine,
    get_specdata_engine,
    get_testcase_engine,
    resolve_specdata_database_url,
    resolve_testcase_database_url,
)

app = typer.Typer(help="doc3gpp command line tools")
db_app = typer.Typer(help="database commands")
meeting_app = typer.Typer(help="meeting commands")
tdoc_app = typer.Typer(help="tdoc commands")
tsg_app = typer.Typer(help="tsg reference data commands")
wi_app = typer.Typer(help="wi commands")
spec_app = typer.Typer(help="spec commands")
testcase_app = typer.Typer(help="testcase commands")
config_app = typer.Typer(help="inspect the resolved configuration")
cache_app = typer.Typer(help="TDoc extraction cache commands")
app.add_typer(db_app, name="db")
app.add_typer(meeting_app, name="meeting")
app.add_typer(tdoc_app, name="tdoc")
app.add_typer(tsg_app, name="tsg")
app.add_typer(wi_app, name="wi")
app.add_typer(spec_app, name="spec")
app.add_typer(testcase_app, name="testcase")
app.add_typer(config_app, name="config")
app.add_typer(cache_app, name="cache")
app.add_typer(server_app, name="server")

logger = logging.getLogger(__name__)

# One-shot latch for the stale-index hint. Set the first time we
# print the hint; cleared when the CLI process exits. Suppresses
# spam in long batch runs.
_stale_index_hint_emitted: bool = False

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
    # `httpx` (huggingface_hub dependency) logs every cache probe at
    # INFO; `huggingface_hub.utils._http` logs the same HEAD/GET
    # chatter at WARNING when unauthenticated. Silence both so the
    # rebuild progress line is readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
    logger.debug("Logging configured at %s", logging.getLevelName(level))


def _version_callback(value: bool) -> None:
    """Print the doc3gpp version and exit.

    Local-imports ``__version__`` so a future refactor that moves the
    constant cannot take the whole CLI down at import time.
    """
    if value:
        from doc3gpp import __version__

        typer.echo(f"doc3gpp {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the doc3gpp version and exit.",
    ),
) -> None:
    _configure_logging()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


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


def _parse_bool_option(value: str | None, option_name: str) -> bool | None:
    if value is None:
        return None
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise typer.BadParameter(f"{option_name} must be 'true' or 'false'")


def _resolve_compact(compact: bool) -> bool:
    """Resolve ``--compact`` against :attr:`Settings.output.compact`.

    CLI flag wins when ``True``; otherwise the setting decides. This
    keeps the precedence consistent with ``_resolve_format`` (CLI >
    settings) while keeping the Typer ``Option`` a plain ``bool`` —
    no ``--no-compact`` toggle is exposed because absence of the
    flag maps to the default ``False`` unambiguously.
    """
    if compact:
        return True
    return get_settings().output.compact


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


VALID_DB_SCOPES: tuple[str, ...] = ("main", "testcase", "specdata", "all")


def _resolve_db_scope(scope: str) -> str:
    """Resolve ``--scope`` for the ``db`` commands.

    Mirrors :func:`_resolve_cache_purge_scope`: normalises whitespace
    + case and validates against :data:`VALID_DB_SCOPES`. Unknown
    values raise :class:`typer.BadParameter`.
    """
    normalized = scope.strip().lower()
    if normalized not in VALID_DB_SCOPES:
        valid = ", ".join(VALID_DB_SCOPES)
        raise typer.BadParameter(
            f"Unknown --scope {scope!r}. Choose from: {valid}."
        )
    return normalized


def _sqlite_file_for_scope(database_url: str, scope: str) -> Path | None:
    """Return the sqlite file for ``database_url`` or ``None`` for ``:memory:``.

    Raises :class:`typer.BadParameter` naming ``scope`` when the URL is
    not sqlite. Call for every selected scope BEFORE deleting anything
    so a mixed-backend ``reset`` fails without touching any file.
    """
    parsed = make_url(database_url)
    if not parsed.drivername.startswith("sqlite"):
        raise typer.BadParameter(
            f"'db reset' only supports SQLite backends "
            f"(scope {scope!r}: {database_url})."
        )
    if parsed.database and parsed.database != ":memory:":
        return Path(parsed.database)
    return None


def _testcase_url_or_raise() -> str:
    """Resolve the testcase URL, mapping derivation errors to CLI errors."""
    try:
        return resolve_testcase_database_url()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _specdata_url_or_raise() -> str:
    """Resolve the specdata URL, mapping derivation errors to CLI errors."""
    try:
        return resolve_specdata_database_url()
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _delete_sqlite_file(db_file: Path | None, scope: str) -> None:
    """Delete ``db_file`` + WAL sidecars, echoing what happened."""
    if db_file is not None and db_file.exists():
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
        typer.echo(f"No existing SQLite file to delete ({scope}).")


def _open_output(path: str | TextIO | None) -> tuple[TextIO, bool]:
    """Open ``path`` for writing, or return ``(sys.stdout, False)`` for stdout.

    ``None`` and the literal ``"-"`` both resolve to stdout. A
    pre-opened :class:`io.TextIOBase` (e.g. an ``io.StringIO`` in
    tests) is returned as-is and is the caller's responsibility to
    close. The second return value tells the caller whether to close
    the stream afterwards (``False`` for stdout or for caller-owned
    streams).
    """
    if path is None or path == "-":
        return sys.stdout, False
    if hasattr(path, "write"):
        return path, False
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


def _emit_json(
    rows: list[list[str]],
    stream: TextIO,
    fields: list[str],
    *,
    compact: bool = False,
) -> None:
    """Emit ``rows`` as a JSON array.

    When ``compact=True`` the output is a single line with no indent
    and no operator-space (``separators=(",", ":")``) and no trailing
    newline. The default is byte-identical to the legacy pretty-printed
    output (``indent=2`` + trailing newline).
    """
    objs = [dict(zip(fields, row)) for row in rows]
    if compact:
        json.dump(objs, stream, ensure_ascii=False, separators=(",", ":"))
        return
    json.dump(objs, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def _spec_list_json_rows(records: list[Spec], fields: list[str]) -> list[dict[str, object]]:
    return [
        {
            field: getattr(record, field, None)
            if field == "parsed"
            else str(getattr(record, field, None) or "-")
            for field in fields
        }
        for record in records
    ]


def _emit_markdown(
    rows: list[list[str]],
    stream: TextIO,
    fields: list[str],
    *,
    compact: bool = False,
) -> None:
    """Emit ``rows`` as a markdown table or a compact ``key: value`` block.

    Default shape is the legacy GFM table (``| col | col |`` +
    ``|---|---|`` + one row per record). When ``compact=True`` the
    table is replaced with a per-row ``key: value`` block; rows are
    separated by a single blank line, the field name is repeated
    per row (so the output is parseable without an external schema).
    """
    if compact:
        for index, row in enumerate(rows):
            if index:
                stream.write("\n")
            stream.writelines(f"{field}: {cell}\n" for field, cell in zip(fields, row))
        return
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
    compact: bool = False,
) -> None:
    """Emit ``rows`` to ``output`` (or stdout) in the chosen format.

    ``compact=True`` propagates to the JSON and markdown emitters —
    the table emitter ignores it. Empty rows are emitted as ``[]`` /
    header-only in JSON and markdown so downstream consumers always
    see a parseable payload. The friendly "no records" message prints
    only when ``--format table`` is paired with stdout — writing an
    empty table file would just be noise.
    """
    stream, close_after = _open_output(output)
    try:
        if not rows:
            if fmt == "json":
                _emit_json([], stream, fields, compact=compact)
            elif fmt == "markdown":
                _emit_markdown([], stream, fields, compact=compact)
            elif output is None:
                stream.write(no_records_msg + "\n")
            return

        if fmt == "table":
            _emit_table(rows, stream)
        elif fmt == "json":
            _emit_json(rows, stream, fields, compact=compact)
        else:
            _emit_markdown(rows, stream, fields, compact=compact)
    finally:
        if close_after:
            stream.close()


def _validate_search_filter_values(
    *,
    release: str | None = None,
    spec: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> None:
    """Validate search filters before constructing a facade request."""
    from doc3gpp.cli_filters import (
        parse_date_filter,
        parse_release_filter,
        parse_spec_filter,
    )

    try:
        if since:
            parse_date_filter(since)
        if until:
            parse_date_filter(until)
        if release:
            parse_release_filter(release)
        if spec:
            parse_spec_filter(spec)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _display_value(value: object) -> str:
    """Render one unified result value for table/Markdown output."""
    if value is None:
        return "-"
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _render_unified_tdoc_results(
    results: list,
    format: str,
    compact: bool,
) -> None:
    """Render flattened TDoc results without exposing internal hit DTOs."""
    from doc3gpp.models.unified_search import tdoc_search_result_to_dict

    if format == "json":
        payload = [tdoc_search_result_to_dict(result) for result in results]
        kwargs = {"ensure_ascii": False}
        if compact:
            kwargs["separators"] = (",", ":")
        else:
            kwargs["indent"] = 2
        typer.echo(json.dumps(payload, **kwargs))
        return

    fields = [
        "tdoc_id",
        "score",
        "search_mode",
        "title",
        "meeting",
        "tsg",
        "uploaded_date",
        "ftp_url",
        "wis",
        "type",
        "status",
        "best_chunk_id",
    ]
    if any(result.previews is not None for result in results):
        fields.append("previews")
    rows = [
        [_display_value(getattr(result, field)) for field in fields]
        for result in results
    ]
    if "previews" in fields:
        preview_index = fields.index("previews")
        for row, result in zip(rows, results):
            previews = result.previews or {}
            row[preview_index] = "\n".join(
                f"{column}: {snippet}" for column, snippet in previews.items()
            ) or "-"
    if format == "markdown":
        _emit_markdown(rows, sys.stdout, fields, compact=compact)
        return
    typer.echo("\t".join(fields))
    for row in rows:
        _emit_table([row], sys.stdout)


def _render_unified_spec_doc_results(
    results: list,
    format: str,
    compact: bool,
    fields: list[str],
    output: str | None = None,
) -> None:
    """Render flattened spec-document results with field selection."""
    from doc3gpp.models.unified_search import spec_doc_search_result_to_dict

    if format == "json":
        payload = [spec_doc_search_result_to_dict(result) for result in results]
        stream, close_after = _open_output(output)
        try:
            kwargs = {"ensure_ascii": False}
            if compact:
                kwargs["separators"] = (",", ":")
            else:
                kwargs["indent"] = 2
            json.dump(payload, stream, **kwargs)
            if not compact:
                stream.write("\n")
        finally:
            if close_after:
                stream.close()
        return

    display_fields = ["score", "search_mode"]
    display_fields.extend(
        field for field in fields if field not in {"score", "search_mode"}
    )
    if not any(result.previews is not None for result in results):
        display_fields = [field for field in display_fields if field != "previews"]
    rows = [
        [_display_value(getattr(result, field)) for field in display_fields]
        for result in results
    ]
    stream, close_after = _open_output(output)
    try:
        if not rows:
            if output is None:
                stream.write("No spec document chunks found\n")
            return
        if format == "markdown":
            _emit_markdown(rows, stream, display_fields, compact=compact)
            return
        stream.write("\t".join(display_fields) + "\n")
        _emit_table(rows, stream)
    finally:
        if close_after:
            stream.close()


def _render_index_result(result: object, *, resource: str, action: bool) -> None:
    """Render an index status or the processed counts from a rebuild."""
    if hasattr(result, "fts5_processed"):
        typer.echo(f"{resource} index rebuild complete")
        typer.echo(f"FTS5 processed: {result.fts5_processed}")
        typer.echo(f"Vector processed: {result.vector_processed}")
        return

    if action:
        return
    typer.echo(f"{resource.title()} index status")
    for label, component in (
        ("FTS5", getattr(result, "fts5", None)),
        ("Vector", getattr(result, "vector", None)),
    ):
        if component is None or not component.available:
            error = getattr(component, "error", None) or "unavailable"
            typer.echo(f"{label}: unavailable ({error})")
            continue
        status = getattr(component, "status", None)
        if status is None:
            typer.echo(f"{label}: available")
            continue
        typer.echo(f"{label}: available")
        typer.echo(f"{label} rows: {status.row_count:,}")
        if label == "FTS5":
            typer.echo(
                f"Last rebuild: {status.last_rebuild_at or 'never'}"
            )
            typer.echo(
                f"Last indexed: {status.last_indexed_uploaded_date or 'never'}"
            )
            typer.echo(
                f"Latest tdocs: {status.latest_tdocs_uploaded_date or 'none'}"
            )
        if getattr(status, "embedding_model", None) is not None:
            typer.echo(f"{label} model: {status.embedding_model}")
        if getattr(status, "embedding_dim", None) is not None:
            typer.echo(f"{label} dim: {status.embedding_dim}")
        if getattr(status, "is_stale", False):
            typer.echo(f"{label} status: STALE")


def _emit_unified_search_explain(
    text: str,
    snippet_tokens: int,
    settings: object,
) -> None:
    """Print the existing FTS5 explanation without bypassing the facade."""
    from doc3gpp.cli_filters import SearchQueryBuilder

    match = SearchQueryBuilder(text).build()
    weights = getattr(getattr(settings, "search", None), "bm25_weights", ())
    typer.echo("# search config", err=True)
    typer.echo(f"match:           {match}", err=True)
    typer.echo(f"snippet_tokens:  {snippet_tokens}", err=True)
    typer.echo(f"bm25_weights:    {list(weights)}", err=True)


def _emit_unified_stale_hint(
    facade: object,
    *,
    quiet: bool,
    resource: str,
    fts_bearing: bool,
) -> None:
    """Emit a stale hint when an injected facade exposes status metadata."""
    global _stale_index_hint_emitted
    if quiet or _stale_index_hint_emitted or not fts_bearing:
        return
    status_method = getattr(facade, "status", None)
    if not callable(status_method):
        return
    try:
        status = status_method()
    except Exception:  # noqa: BLE001 - stale hint is best-effort only
        return
    if getattr(status, "is_stale", False):
        typer.echo(
            f"search index is stale; run `doc3gpp {resource} index --rebuild` to refresh",
            err=True,
        )
        _stale_index_hint_emitted = True


def _emit_schema(
    resource: str,
    fmt: str,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit the static field registry for ``resource`` in the chosen format.

    JSON goes through :func:`_dump_show_json` so ``nullable`` stays a
    real bool (matching REST ``?format=json`` and the MCP tools
    byte-for-byte). Table/markdown go through :func:`_emit_records`
    with ``nullable`` rendered as ``yes``/``no``. No DB access, no
    filters — the whole descriptor fits in one response.
    """
    payload = schema_payload(resource)
    if fmt == "json":
        _dump_show_json(payload, output, compact=compact)
        return
    rows = [
        [
            str(row["table"]),
            str(row["field"]),
            str(row["type"]),
            "yes" if row["nullable"] else "no",
            str(row["description"]),
            str(row["values"]),
        ]
        for row in payload
    ]
    _emit_records(
        rows=rows,
        fields=SCHEMA_FIELDS,
        fmt=fmt,
        output=output,
        no_records_msg=f"No schema for {resource}",
        compact=compact,
    )


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
def db_check(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Which database to check: 'main', 'testcase', 'specdata', or 'all'.",
    ),
) -> None:
    """Validate database connectivity for configured backend(s)."""
    resolved_scope = _resolve_db_scope(scope)
    logger.info("Checking database connectivity")
    settings = get_settings()
    if resolved_scope in ("main", "all"):
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        typer.echo(f"Database connection OK: {settings.database_url}")
    if resolved_scope in ("testcase", "all"):
        tc_url = _testcase_url_or_raise()
        tc_engine = get_testcase_engine()
        with tc_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        typer.echo(f"Testcase database connection OK: {tc_url}")
    if resolved_scope in ("specdata", "all"):
        sd_url = _specdata_url_or_raise()
        sd_engine = get_specdata_engine()
        with sd_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        typer.echo(f"Specdata database connection OK: {sd_url}")


@db_app.command("init")
def db_init(
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Which database to initialise: 'main', 'testcase', 'specdata', or 'all'.",    ),
) -> None:
    """Create schema for current backend(s) and seed the TSG reference table.

    Re-running this command is safe: the TSG seed is upsert-based, so existing
    rows are refreshed in place rather than duplicated.
    """
    resolved_scope = _resolve_db_scope(scope)
    logger.info("Initializing database schema")
    create_schema(resolved_scope)
    if resolved_scope in ("main", "all"):
        tsg_service = build_tsg_service()
        seeded = tsg_service.seed_defaults()
        logger.info("Seeded %s TSG reference records", seeded)
        typer.echo(f"Database schema initialized; seeded {seeded} TSG records")
    elif resolved_scope == "testcase":
        typer.echo("Testcase database schema initialized")
    else:
        typer.echo("Specdata database schema initialized")


@db_app.command("reset")
def db_reset(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Which database to reset: 'main', 'testcase', 'specdata', or 'all'.",
    ),
) -> None:
    """Delete the SQLite database file(s) and recreate the schema.

    Destructive: all data in the selected scope is wiped. SQLite URLs
    only — every selected scope must be sqlite or the whole reset is
    rejected before anything is deleted. Prompts for confirmation
    unless ``--yes`` is passed. After reset the ``tsgs`` reference
    table is re-seeded when the main scope is selected.
    """
    resolved_scope = _resolve_db_scope(scope)
    settings = get_settings()
    urls: list[tuple[str, str]] = []
    if resolved_scope in ("main", "all"):
        urls.append(("main", settings.database_url))
    if resolved_scope in ("testcase", "all"):
        # NOTE: with a non-sqlite main URL and no explicit testcase URL
        # the derivation itself fails (ValueError → BadParameter naming
        # 'testcase_database_url'), which also aborts before any delete.
        urls.append(("testcase", _testcase_url_or_raise()))
    if resolved_scope in ("specdata", "all"):
        # Same derivation-abort guarantee as the testcase branch above.
        urls.append(("specdata", _specdata_url_or_raise()))
    # Validate every selected scope BEFORE deleting anything: a
    # mixed-backend reset fails without touching any file.
    files: list[tuple[str, Path | None]] = [
        (scope_name, _sqlite_file_for_scope(url, scope_name))
        for scope_name, url in urls
    ]

    targets = [str(f) for _, f in files if f is not None and f.exists()]
    if targets and not yes:
        typer.confirm(
            "Delete SQLite database file(s)?\n" + "\n".join(targets),
            abort=True,
        )
    # Windows will not unlink SQLite files while a cached engine still has
    # pooled connections open. Dispose the selected engines before deleting
    # their files, then clear the caches so schema creation gets fresh engines.
    engines = {
        "main": get_engine,
        "testcase": get_testcase_engine,
        "specdata": get_specdata_engine,
    }
    for scope_name, _ in files:
        engines[scope_name]().dispose()
    get_engine.cache_clear()
    get_testcase_engine.cache_clear()
    get_specdata_engine.cache_clear()

    for scope_name, db_file in files:
        _delete_sqlite_file(db_file, scope_name)

    logger.info("Recreating database schema")
    create_schema(resolved_scope)
    if resolved_scope in ("main", "all"):
        tsg_service = build_tsg_service()
        seeded = tsg_service.seed_defaults()
        logger.info("Seeded %s TSG reference records", seeded)
        typer.echo(f"Database reset complete; seeded {seeded} TSG records")
    elif resolved_scope == "testcase":
        typer.echo("Testcase database reset complete")
    else:
        typer.echo("Specdata database reset complete")


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
    create_schema("all")
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
        help="Rich filter on meeting TSG short name (LIKE, !NOT LIKE, null/not-null)",
    ),
    name: str | None = typer.Option(None, help="Rich filter on meeting name (LIKE, !NOT LIKE, null/not-null)") ,
    location: str | None = typer.Option(None, help="Rich filter on meeting location (LIKE, !NOT LIKE, null/not-null)") ,
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
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
    resolved_compact = _resolve_compact(compact)

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
        compact=resolved_compact,
    )


@meeting_app.command("schema")
def meeting_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the meetings table (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing meeting schema")
    _emit_schema("meeting", fmt, output, compact=resolved_compact)


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
    ls_to: str | None = typer.Option(
        None,
        "--ls-to",
        help="SQL LIKE pattern on ls_to (or null/not-null/!pattern).",
    ),
    ls_cc: str | None = typer.Option(
        None,
        "--ls-cc",
        help="SQL LIKE pattern on ls_cc (or null/not-null/!pattern).",
    ),
    original_ls: str | None = typer.Option(
        None,
        "--original-ls",
        help="SQL LIKE pattern on original_ls (or null/not-null/!pattern).",
    ),
    tdoc_for: str | None = typer.Option(
        None,
        "--for",
        "--tdoc-for",
        help="SQL LIKE pattern on tdoc_for (or null/not-null/!pattern).",
    ),
    abstract: str | None = typer.Option(
        None,
        "--abstract",
        help="SQL LIKE pattern on abstract (or null/not-null/!pattern).",
    ),
    secretary_remarks: str | None = typer.Option(
        None,
        "--secretary-remarks",
        help="SQL LIKE pattern on secretary_remarks (or null/not-null/!pattern).",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
    ),
) -> None:
    """List stored TDocs from the database.

    Filter optional flags are combinable; the query is ANDed together:
      --tdoc, --meeting-id, --meeting, --status, --cr-cat, --spec, --wi,
      --revision-of, --revised-to, --title, --ftp-url, --release, --version,
      --cr-num, --cr-pack, --source, --type, --uploaded-date,
      --ls-to, --ls-cc, --original-ls, --for, --abstract, --secretary-remarks
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
    resolved_compact = _resolve_compact(compact)

    logger.info(
        "Listing %s recent TDocs (offset=%s) with filters tdoc=%s meeting=%s "
        "meeting_id=%s source=%s spec=%s wi=%s title=%s cr_cat=%s status=%s "
        "type=%s revision_of=%s revised_to=%s ftp_url=%s release=%s version=%s "
        "cr_num=%s cr_pack=%s uploaded_date=%s ls_to=%s ls_cc=%s original_ls=%s "
        "tdoc_for=%s abstract=%s secretary_remarks=%s",
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
        ls_to,
        ls_cc,
        original_ls,
        tdoc_for,
        abstract,
        secretary_remarks,
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
        ls_to=ls_to,
        ls_cc=ls_cc,
        original_ls=original_ls,
        tdoc_for=tdoc_for,
        abstract=abstract,
        secretary_remarks=secretary_remarks,
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
        compact=resolved_compact,
    )


@tdoc_app.command("search")
def tdoc_search(
    text: str | None = typer.Option(
        None, "--text", help="FTS5 text or MATCH expression."
    ),
    semantic: str | None = typer.Option(
        None, "--semantic", help="Natural-language semantic query."
    ),
    tsg: str | None = typer.Option(None, "--tsg", help="Filter by meetings.tsg."),
    meeting: str | None = typer.Option(
        None,
        "--meeting",
        help="Rich filter over meeting name or title.",
    ),
    meeting_id: int | None = typer.Option(
        None, "--meeting-id", help="Filter by meetings.meeting_id."
    ),
    tdoc_id: str | None = typer.Option(
        None, "--tdoc-id", help="Filter by tdocs.tdoc_id."
    ),
    release: str | None = typer.Option(
        None, "--release", help="Rich filter over tdocs.release."
    ),
    spec: str | None = typer.Option(
        None, "--spec", help="Rich filter over tdocs.spec."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Uploaded-date lower bound."
    ),
    until: str | None = typer.Option(
        None, "--until", help="Uploaded-date upper bound."
    ),
    limit: int = typer.Option(20, "--limit", min=0, help="Max results."),
    fmt: str | None = typer.Option(
        None, "--format", help="table | json | markdown"
    ),
    compact: bool = typer.Option(False, "--compact", help="Strip JSON / Markdown decorators."),
    snippet_tokens: int = typer.Option(
        8, "--snippet-tokens", min=1, max=64, help="FTS5 snippet length."
    ),
    explain: bool = typer.Option(
        False, "--explain", help="Print the FTS5 search configuration."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress stale-index hints and progress output."
    ),
) -> None:
    """Search TDocs using text, semantic, hybrid, or filter mode."""
    from doc3gpp.models.search import (
        SearchError,
        SearchFilters,
        SearchIndexCorruptError,
        SearchQueryError,
        SearchUnavailableError,
    )
    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError,
        SemanticSearchQueryError,
        SemanticSearchUnavailableError,
        VectorIndexUnavailableError,
    )
    from doc3gpp.services.factory import build_tdoc_search_facade

    _validate_search_filter_values(
        release=release,
        spec=spec,
        since=since,
        until=until,
    )
    settings = get_settings()
    fmt_resolved = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    filters = SearchFilters(
        tsg=tsg,
        meeting=meeting,
        meeting_id=meeting_id,
        tdoc_id=tdoc_id,
        release=release,
        spec=spec,
        since=since,
        until=until,
        limit=limit,
    )
    facade = build_tdoc_search_facade()
    try:
        results = facade.search(
            text=text,
            semantic=semantic,
            filters=filters,
            snippet_tokens=snippet_tokens,
        )
    except SearchQueryError as exc:
        typer.echo(f"bad query: {exc}", err=True)
        raise typer.Exit(code=2)
    except SemanticSearchQueryError as exc:
        typer.echo(f"bad query: {exc}", err=True)
        raise typer.Exit(code=2)
    except SearchIndexCorruptError:
        typer.echo(
            "search index corrupt; run `doc3gpp tdoc index --rebuild`",
            err=True,
        )
        raise typer.Exit(code=3)
    except SearchUnavailableError:
        typer.echo("search disabled in settings", err=True)
        raise typer.Exit(code=0)
    except EmbedderUnavailableError as exc:
        typer.echo(f"embedding model load failed: {exc}", err=True)
        raise typer.Exit(code=1)
    except VectorIndexUnavailableError as exc:
        typer.echo(f"vector index unavailable: {exc}", err=True)
        raise typer.Exit(code=1)
    except SemanticSearchUnavailableError as exc:
        typer.echo(f"search sem unavailable: {exc}", err=True)
        raise typer.Exit(code=1)
    except SearchError:
        typer.echo(
            "search index corrupt; run `doc3gpp tdoc index --rebuild`",
            err=True,
        )
        raise typer.Exit(code=3)

    if explain and text and text.strip():
        _emit_unified_search_explain(text, snippet_tokens, settings)
    _render_unified_tdoc_results(results, fmt_resolved, resolved_compact)
    _emit_unified_stale_hint(
        facade,
        quiet=quiet,
        resource="tdoc",
        fts_bearing=bool(text and text.strip()),
    )


@tdoc_app.command("index")
def tdoc_index(
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild the FTS5 index."),
    rebuild_embeddings: bool = typer.Option(
        False, "--rebuild-embeddings", help="Rebuild the vector index."
    ),
    rebuild_all: bool = typer.Option(
        False, "--rebuild-all", help="Rebuild both indexes."
    ),
    batch: int | None = typer.Option(None, "--batch", min=1, help="Rebuild batch size."),
    resume: bool = typer.Option(False, "--resume", help="Resume from the last cursor."),
    stale_only: bool = typer.Option(
        False, "--stale-only", help="Only rebuild stale rows."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress rebuild progress messages."
    ),
) -> None:
    """Show or maintain TDoc FTS5 and vector indexes."""
    from doc3gpp.models.index import IndexRequest
    from doc3gpp.models.search import SearchUnavailableError
    from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
    from doc3gpp.services.factory import build_tdoc_index_service

    service = build_tdoc_index_service()
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
            request,
            quiet=quiet,
            on_progress=None if quiet else typer.echo,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (SearchUnavailableError, SemanticSearchUnavailableError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    _render_index_result(
        result,
        resource="tdoc",
        action=any((rebuild, rebuild_embeddings, rebuild_all)),
    )


@tdoc_app.command("schema")
def tdoc_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, tdoc_cr_change_details, tdoc_files and tdoc_extracts tables (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing tdoc schema")
    _emit_schema("tdoc", fmt, output, compact=resolved_compact)


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
    settings: Settings,
) -> int:
    """Return the effective recursion depth for a URL-folder batch parse."""
    if max_depth is not None:
        return max_depth
    if recursive:
        return settings.tdoc_parse.max_ftp_depth
    return 0


def _resolve_max_tdoc_size_bytes(
    *,
    override_kb: int | None,
    settings: Settings,
) -> int:
    """Return the effective per-file cap in bytes; ``0`` = unlimited.

    Mirrors :func:`_resolve_url_batch_depth`: when ``override_kb`` is
    ``None`` (the CLI flag was not supplied), the value comes from
    ``settings.tdoc_parse.max_tdoc_size_kb``. The bytes value (KB ×
    1024) is the canonical form the service layer and gate points
    consume; the CLI flag is in KB for human readability.
    """
    if override_kb is not None:
        return override_kb * 1024
    return settings.tdoc_parse.max_tdoc_size_kb * 1024


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
    max_tdoc_size_kb: int | None = typer.Option(
        None,
        "--max-tdoc-size-kb",
        min=0,
        help=(
            "Override tdoc_parse.max_tdoc_size_kb. Source files "
            "(.zip or .docx) larger than this many KB are skipped "
            "(size-limit skip bucket). 0 = unlimited."
        ),
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table`` and ``raw``. Defaults to ``output.compact`` in "
            "settings when the flag is not passed. Applies to direct mode "
            "(--from-path / --from-url); the DB-mode filter branch renders "
            "an inline per-row summary and is unaffected."
        ),
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
    max_tdoc_size_bytes = _resolve_max_tdoc_size_bytes(
        override_kb=max_tdoc_size_kb,
        settings=get_settings(),
    )
    resolved_compact = _resolve_compact(compact)
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
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                    compact=resolved_compact,
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
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                    compact=resolved_compact,
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
            tdoc_service = build_tdoc_cr_service(max_tdoc_size_bytes=max_tdoc_size_bytes)
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
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                    compact=resolved_compact,
                )
            elif _looks_like_3gpp_folder_url(from_url):
                _tdoc_parse_url_batch(
                    from_url=from_url,
                    output=direct_output,
                    fmt=direct_format,
                    max_depth=effective_depth,
                    force=force,
                    full=full,
                    max_tdoc_size_bytes=max_tdoc_size_bytes,
                    compact=resolved_compact,
                )
            else:
                try:
                    batch = tdoc_service.extract_from_url_batch(
                        from_url,
                        max_depth=effective_depth,
                        force=force,
                        full=full,
                        max_tdoc_size_bytes=max_tdoc_size_bytes or None,
                    )
                except NotAFolderError:
                    _tdoc_parse_direct(
                        from_path=None,
                        from_url=from_url,
                        fmt=direct_format,
                        output=direct_output,
                        full=full,
                        max_tdoc_size_bytes=max_tdoc_size_bytes,
                        compact=resolved_compact,
                    )
                else:
                    _emit_url_batch_results(
                        batch=batch,
                        root_url=from_url,
                        output=direct_output,
                        fmt=direct_format,
                        compact=resolved_compact,
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
            f"Already parsed in tdoc_cr_cover_page{suffix}",
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
    service = build_tdoc_cr_service(max_tdoc_size_bytes=max_tdoc_size_bytes)
    try:
        batch = service.extract_many(tdoc_ids, force=force, full=full)
    except PythonDocxNotInstalledError as exc:
        typer.echo(
            "python-docx is not installed; install with `pip install doc3gpp[extract]`.",
            err=True,
        )
        typer.echo(f"hint: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        # Any exception escaping ``extract_many`` is an internal bug
        # (recoverable per-id failures are caught inside the batch
        # loop). Log the full traceback; surface a short message.
        logger.exception("Unexpected error extracting TDoc batch")
        typer.echo(
            f"Unexpected error: {type(exc).__name__}: {exc}",
            err=True,
        )
        raise typer.Exit(code=1) from None

    failures: list[str] = []
    skipped_ftp: list[str] = []
    skipped_size: list[str] = []
    for raw_id in tdoc_ids:
        normalised = raw_id.strip()
        if normalised in batch.successes:
            result = batch.successes[normalised]
            typer.echo(
                f"{normalised}: spec={result.details.spec} "
                f"cr_num={result.details.cr_num} "
                f"title={result.details.title}"
            )
        elif normalised in batch.skipped:
            reason = batch.skipped[normalised]
            typer.echo(f"{normalised}: SKIPPED - {reason}")
            if reason.startswith("TDocTooLargeError:"):
                skipped_size.append(normalised)
            else:
                skipped_ftp.append(normalised)
        elif normalised in batch.failures:
            typer.echo(f"{normalised}: FAILED - {batch.failures[normalised]}")
            failures.append(normalised)
        else:
            typer.echo(f"{normalised}: FAILED - extract error (no diagnostic)")
            failures.append(normalised)

    success_set = set(batch.successes.keys())
    already_parsed = len(parsed_ids - dispatched_set)
    re_parsed = len(parsed_ids & success_set)
    newly_parsed = len(success_set - parsed_ids)
    typer.echo("---")
    typer.echo(
        f"Skipped (exceeds max_tdoc_size_kb):          {len(skipped_size)}"
    )
    typer.echo(f"Skipped (already parsed before this run): {already_parsed}")
    typer.echo(f"Skipped (not yet on FTP):                 {len(skipped_ftp)}")
    typer.echo(f"Re-parsed (with --force):                  {re_parsed}")
    typer.echo(f"Newly parsed:                              {newly_parsed}")
    typer.echo(f"Failures:                                  {len(failures)}")
    if truncated:
        typer.echo(
            f"Remaining (truncated by max_batch={max_batch}): "
            f"at least 1 — re-run the same command (without --force) "
            f"to continue."
        )
    if failures and not batch.successes:
        raise typer.Exit(code=1)
    if not tdoc_ids:
        raise typer.Exit(code=1)
    if not batch.successes and not batch.skipped:
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
    "summary_of_change",
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
        if from_path is None or from_path_is_file:
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
    max_tdoc_size_bytes: int = 0,
    compact: bool = False,
) -> None:
    """Dispatch a single ``--from-path`` (file) or ``--from-url`` call.

    ``max_tdoc_size_bytes`` is forwarded to
    :meth:`TDocCrService.extract_from_bytes` /
    :meth:`TDocCrService.extract_from_url` so the same per-file byte
    cap that gates ``tdoc parse --tdoc`` also applies to direct
    single-file invocations. ``0`` (the default) disables the cap;
    the CLI always resolves the resolved bytes value up-front and
    passes it explicitly.

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
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

    resolved_format = _resolve_direct_format(fmt)
    service = build_tdoc_cr_service(max_tdoc_size_bytes=max_tdoc_size_bytes)
    raw = from_path if from_path is not None else from_url
    assert raw is not None

    try:
        if from_path is not None:
            payload = Path(from_path).read_bytes()
            result = service.extract_from_bytes(
                payload, from_path, force=False, full=full,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
            )
        else:
            result = service.extract_from_url(
                raw, force=False, full=full,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
            )
    except TDocTooLargeError as exc:
        # Size-limit skip — exit 0 with a warning. The operator's
        # --max-tdoc-size-kb is an explicit budget decision, not a
        # bug. Consistent with the non-3GPP URL warning+emit path
        # below.
        typer.echo(
            f"SKIPPED - {type(exc).__name__}: {exc}",
            err=True,
        )
        raise typer.Exit(code=0) from None
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
        _emit_record_raw(result.markdown, output, compact=compact)
    elif result.details is None:
        typer.echo(
            f"FAILED - ValueError: --format {resolved_format!r} requires parsed fields; "
            "the parser did not run for this source.",
            err=True,
        )
        raise typer.Exit(code=1) from None
    else:
        _emit_record(result.details, resolved_format, output, compact=compact)


def _emit_record(
    record: TDocCRDetails,
    fmt: str,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Dispatch to the table / markdown / json emitter for a single parsed record."""
    if fmt == "table":
        _emit_record_table(record, output, compact=compact)
    elif fmt == "markdown":
        _emit_record_markdown(record, output, compact=compact)
    elif fmt == "json":
        _emit_record_json(record, output, compact=compact)
    else:
        raise typer.BadParameter(f"Unsupported direct-parse format: {fmt!r}")


def _emit_record_table(
    record: TDocCRDetails,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
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


def _emit_record_markdown(
    record: TDocCRDetails,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit a single record as a one-row GFM table, or a per-field
    ``key: value`` block when ``compact=True``."""
    stream, close_after = _open_output(output)
    try:
        if compact:
            for name in _DIRECT_PARSE_FIELDS:
                stream.write(f"{name}: {_serialise_cell(record, name)}\n")
            return
        stream.write("| " + " | ".join(_md_cell(h) for h in _DIRECT_PARSE_FIELDS) + " |\n")
        stream.write("|" + "|".join(["---"] * len(_DIRECT_PARSE_FIELDS)) + "|\n")
        cells = [_md_cell(_serialise_cell(record, name)) for name in _DIRECT_PARSE_FIELDS]
        stream.write("| " + " | ".join(cells) + " |\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_json(
    record: TDocCRDetails,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit a single record as a JSON object via ``dataclasses.asdict``."""
    payload = dataclasses.asdict(record)
    payload["date"] = record.date.isoformat() if record.date is not None else None
    stream, close_after = _open_output(output)
    try:
        if compact:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            return
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _emit_record_raw(
    markdown: str,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
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


def _display_show_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value)
    return str(value or "-")


# ``TDocShowRecord`` / ``TDocShowRecordByUrl`` are imported from
# ``doc3gpp.models.tdoc_show`` (re-exported above for backwards
# compatibility with code that imports them from ``doc3gpp.cli``).
# The composition lives in the models layer so the CLI and the
# HTTP web route can share the same record-building path.


def _build_show_payload(
    record: TDocShowRecord | TDocShowRecordByUrl,
    *,
    anchor_key: str | None = None,
    anchor_value: object = None,
    include_tdoc: bool = True,
) -> dict[str, object]:
    """Build the omit-when-null JSON payload for the show renderers.

    Both the by-id and by-url JSON renderers share the same payload
    shape (cover / ttcn / changes / extracted_at / files keys) and the
    same omit-when-null convention. The by-id renderer anchors on a
    required ``tdoc`` key; the by-url renderer anchors on a required
    ``anchor_key`` (``"ftp_url"``) and treats ``tdoc`` as optional.

    ``record.tdoc`` is always read through the same iterator when it is
    non-``None`` so the by-id renderer's always-present ``tdoc`` key
    and the by-url renderer's optional ``tdoc`` key share the same
    serialisation rules.
    """
    payload: dict[str, object] = {}
    if anchor_key is not None:
        payload[anchor_key] = anchor_value
    if include_tdoc and record.tdoc is not None:
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
    if record.changes is not None:
        payload["changes"] = {
            "clauses": list(record.changes.clauses),
            "changes": [
                {"clauses": list(b["clauses"]), "text": b["text"]}
                for b in record.changes.changes
            ],
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
    return payload


def _dump_show_json(
    payload: dict[str, object] | list[object],
    output: str | TextIO | None,
    *,
    compact: bool,
) -> None:
    """Open ``output`` and serialise ``payload`` as JSON, honouring ``compact``.

    The by-id and by-url JSON renderers share the same compact /
    pretty-printed write path; centralising it here keeps the omit-when-null
    payload rules in :func:`_build_show_payload` and the byte-level
    formatting in one place.
    """
    stream, close_after = _open_output(output)
    try:
        if compact:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            return
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_json(
    record: TDocShowRecord,
    output: str | TextIO | None,
    *,
    compact: bool = False,
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

    When ``compact=True`` the output is a single line with no indent,
    no operator-space, and no trailing newline — sized for tight
    log/scan pipelines instead of human reading.
    """
    payload = _build_show_payload(record)
    _dump_show_json(payload, output, compact=compact)


def _render_tdoc_show_markdown_compact(
    stream: TextIO,
    record: TDocShowRecord | TDocShowRecordByUrl,
    *,
    anchor_field: str | None,
    anchor_value: str | None,
    tdoc_missing_note: str | None,
    parse_hint: str,
    files_missing_hint: str,
) -> None:
    """Write the shared ``--compact`` markdown body for the show renderers.

    Both the by-id and by-url compact renderers share the same
    per-block logic; the only differences are the tdoc preamble
    (an optional anchor line + an optional "no tdocs row" note vs
    just the tdoc fields), the "no extracted details" placeholder
    text, and the "no auxiliary files" placeholder text. Those are
    all parameters here.
    """
    if anchor_field is not None and anchor_value is not None:
        stream.write(f"{anchor_field}: {anchor_value}\n")

    if record.tdoc is not None:
        for f in dataclass_fields(record.tdoc):
            value = _serialise_show_value(getattr(record.tdoc, f.name))
            rendered = "-" if value is None else str(value)
            stream.write(f"{f.name}: {rendered}\n")
    elif tdoc_missing_note is not None:
        stream.write(f"\n{tdoc_missing_note}\n")

    if (
        record.cover is None
        and record.ttcn is None
        and record.extracted_at is None
        and record.changes is None
    ):
        stream.write(f"\nnote: No extracted details; run doc3gpp tdoc parse {parse_hint} first.\n")
    else:
        if record.cover is not None:
            stream.write("\n")
            for f in dataclass_fields(record.cover):
                if f.name in {"details", "parser_version"}:
                    continue
                value = getattr(record.cover, f.name)
                formatted = _serialise_show_value(value)
                rendered = "-" if formatted is None else str(formatted)
                stream.write(f"{f.name}: {rendered}\n")
            if record.extracted_at is not None:
                stream.write(
                    f"extracted_at: {_fmt_dt(record.extracted_at)}\n"
                )

        if record.ttcn is not None:
            stream.write("\n")
            for f in dataclass_fields(record.ttcn):
                value = getattr(record.ttcn, f.name)
                if f.name == "required_changes" and isinstance(value, list):
                    inline = json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    )
                    stream.write(f"{f.name}: {inline}\n")
                    continue
                if f.name == "changed_functions" and isinstance(value, list):
                    if not value:
                        stream.write(f"{f.name}: -\n")
                    else:
                        stream.write(f"{f.name}: {', '.join(value)}\n")
                    continue
                formatted = _serialise_show_value(value)
                rendered = "-" if formatted is None else str(formatted)
                stream.write(f"{f.name}: {rendered}\n")

    if record.changes is not None:
        stream.write("\n")
        stream.write(
            f"changes: {len(record.changes.changes)} block(s), "
            f"{len(record.changes.clauses)} clause(s)\n"
        )
        for idx, block in enumerate(record.changes.changes, start=1):
            stream.write(f"* block {idx}:\n")
            if block["clauses"]:
                stream.write(
                    f"  * clauses: {', '.join(block['clauses'])}\n"
                )
            if block["text"]:
                stream.write(f"  * changes: \n{block['text']}\n")
            stream.write("\n")

    stream.write("\n")
    if not record.files:
        stream.write(f"note: {files_missing_hint}\n")
    else:
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


def _render_tdoc_show_markdown_full(
    stream: TextIO,
    record: TDocShowRecord | TDocShowRecordByUrl,
    *,
    header_line: str,
    tdoc_heading: str,
    tdoc_missing_note: str | None,
    parse_hint: str,
    show_extracted_details_fallback: bool,
    files_missing_hint: str,
    omit_files_heading_when_empty: bool = False,
) -> None:
    """Write the shared full-mode markdown body for the show renderers.

    Both renderers share the per-block (cover / ttcn / changes / files)
    logic; the differences are the top-level document header, the
    tdoc section heading + fallback note, the parse-hint string baked
    into the "no extracted details" placeholder, whether to render an
    ``## Extracted Details`` section when only ``extracted_at`` is
    known (by-url yes, by-id no), the "no auxiliary files" placeholder
    text, and whether the ``## Auxiliary Files`` heading is emitted
    when no files are attached (by-id yes, by-url no). Those are all
    parameters here.
    """
    stream.write(f"{header_line}\n\n")

    if record.tdoc is not None:
        stream.write(f"{tdoc_heading}\n\n")
        for f in dataclass_fields(record.tdoc):
            value = _serialise_show_value(getattr(record.tdoc, f.name))
            rendered = "—" if value is None else str(value)
            stream.write(f"- **{f.name}**: {rendered}\n")
    elif tdoc_missing_note is not None:
        stream.write(f"{tdoc_missing_note}\n\n")

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
    elif show_extracted_details_fallback and record.extracted_at is not None:
        # by-url only: when no cover row exists but the extract
        # timestamp does, surface it under its own short section so
        # the document skeleton stays populated.
        stream.write("\n## Extracted Details\n\n")
        stream.write(
            f"- **extracted_at**: {_fmt_dt(record.extracted_at)}\n"
        )
    elif (
        record.cover is None
        and record.ttcn is None
        and record.extracted_at is None
        and record.changes is None
        and not show_extracted_details_fallback
    ):
        # by-id: when nothing is known, emit a single "no extracted
        # details" placeholder pointing at the parse command.
        stream.write("\n## Extracted Details\n\n")
        stream.write(
            f"_No extracted details; run "
            f"`doc3gpp tdoc parse {parse_hint}` first._\n"
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
            if f.name == "changed_functions" and isinstance(value, list):
                if not value:
                    stream.write(f"- **{f.name}**: —\n")
                else:
                    stream.write(f"- **{f.name}**:\n")
                    stream.writelines(f"  * {entry}\n" for entry in value)
                continue
            formatted = _serialise_show_value(value)
            rendered = "—" if formatted is None else str(formatted)
            stream.write(f"- **{f.name}**: {rendered}\n")

    if record.changes is not None:
        stream.write("\n## Change Details\n\n")
        stream.write(f"- **clauses**: {', '.join(record.changes.clauses) or '—'}\n")
        stream.write(f"- **changes**: {len(record.changes.changes)} change block(s)\n")
        for idx, block in enumerate(record.changes.changes, start=1):
            stream.write(f"\n* block {idx}:\n")
            if block["clauses"]:
                stream.write(
                    f"  * clauses: {', '.join(block['clauses'])}\n"
                )
            if block["text"]:
                stream.write("  * Changes:\n")
                stream.writelines(f">{ln}\n" for ln in block["text"].split("\n"))
                stream.write("\n")

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
    elif not omit_files_heading_when_empty:
        # by-id: always emit the heading and put the placeholder
        # underneath so the document skeleton stays stable.
        stream.write("\n## Auxiliary Files\n\n")
        stream.write(f"_{files_missing_hint}_\n")
    else:
        # by-url: drop the heading when no files match and emit the
        # placeholder as a single italic line preceded by a blank.
        stream.write(f"\n_{files_missing_hint}_\n")


def _render_tdoc_show_markdown(
    record: TDocShowRecord,
    output: str | TextIO | None,
    *,
    compact: bool = False,
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

    When ``compact=True`` every CommonMark decorator (``**bold**``,
    ``*italic*``, ``## headings``, ``- `` bullets, ```` ```json ````
    fences) is dropped; fields become ``key: value`` plain lines and
    sections are separated by a single blank line. ``None`` values
    render as ``-`` and ``—`` is normalised to ``-``.
    ``required_changes`` becomes a single-line JSON literal;
    ``changed_functions`` becomes a comma-joined line. Placeholder
    text becomes a single ``note: <plain>`` line.
    """
    stream, close_after = _open_output(output)
    try:
        if compact:
            _render_tdoc_show_markdown_compact(
                stream, record,
                anchor_field=None,
                anchor_value=None,
                tdoc_missing_note=None,
                parse_hint="--tdoc <id>",
                files_missing_hint=(
                    "No auxiliary files; run doc3gpp tdoc sync "
                    "first if you haven't synced this meeting yet."
                ),
            )
            return
        _render_tdoc_show_markdown_full(
            stream, record,
            header_line=f"# TDoc `{record.tdoc.tdoc_id}`",
            tdoc_heading="## Metadata",
            tdoc_missing_note=None,
            parse_hint="--tdoc <id>",
            show_extracted_details_fallback=False,
            files_missing_hint=(
                "No auxiliary files; run "
                "`doc3gpp tdoc sync` first if you haven't synced "
                "this meeting yet."
            ),
        )
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_table_body(
    stream: TextIO,
    record: TDocShowRecord | TDocShowRecordByUrl,
    *,
    parse_hint: str,
    tdoc_missing_msg: str | None = None,
) -> None:
    """Write the shared table body for the show renderers.

    The by-id and by-url table renderers share the same per-block
    (tdoc / cover / ttcn / changes / files) line-oriented layout;
    the only differences are the parse-hint text baked into the
    "no extracted details" placeholder and whether the tdoc block
    has a fallback "no tdocs row" message (by-url) or always
    renders the parent row (by-id, ``tdoc_missing_msg=None``).

    The optional ``[FTP URL]`` header is the by-url renderer's
    responsibility — see :func:`_render_tdoc_show_by_url_table`.
    """
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
    elif tdoc_missing_msg is not None:
        stream.write(f"{tdoc_missing_msg}\n")

    if record.cover is None:
        # No cover row exists for the URL — skip the
        # ``[Extracted Details]`` section header so the placeholder
        # message is the only thing rendered under the ``[TDoc]``
        # block (by-id) or after the [FTP URL] anchor (by-url). The
        # header re-appears once a real cover row surfaces (see the
        # ``record.cover is not None`` branch below).
        stream.write(
            f"No extracted details; run `doc3gpp tdoc parse {parse_hint}` first.\n"
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
        if details.summary_of_change is not None:
            stream.write(
                "summary_of_change: "
                f"{_truncate_for_display(details.summary_of_change)}\n"
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
        if ttcn.changed_functions is None:
            stream.write("changed_functions: -\n")
        elif not ttcn.changed_functions:
            stream.write("changed_functions: 0 item(s)\n")
        else:
            count = len(ttcn.changed_functions)
            stream.write(f"changed_functions: {count} item(s)\n")
            stream.writelines(f"  - {entry}\n" for entry in ttcn.changed_functions)

    if record.changes is not None:
        stream.write("\n[Change Details]\n")
        stream.write(
            f"clauses: {len(record.changes.clauses)} clause(s)\n"
        )
        stream.write(
            f"changes: {len(record.changes.changes)} change block(s)\n"
        )

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


def _render_tdoc_show_table(
    record: TDocShowRecord,
    output: str | TextIO | None,
    *,
    compact: bool = False,
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

    ``compact`` is accepted for symmetry with the other renderers but
    ignored — the table format is already line-oriented and maximally
    compact by construction.
    """
    stream, close_after = _open_output(output)
    try:
        _render_tdoc_show_table_body(stream, record, parse_hint="--tdoc <id>")
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_raw(
    tdoc_id: str,
    output: str | TextIO | None,
    *,
    compact: bool = False,
) -> None:
    """Emit ``tdoc show --format raw``.

    Delegates to :class:`TDocCrService.extract`, which short-circuits
    on a DB cache hit (no network / no conversion) and otherwise
    downloads the zip, renders the markdown and persists the row. The
    rendered markdown is then read back from the cache path the
    service populated. Service-level exceptions are translated into
    friendly CLI errors so the operator never sees a raw traceback.

    ``compact`` is accepted for symmetry with the other renderers but
    ignored — raw emits the converted markdown verbatim, which is
    already maximally compact by construction.
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
    *,
    compact: bool = False,
) -> None:
    """Dispatch ``tdoc show --ftp-url`` to the right renderer.

    Normalises the URL via :func:`_normalise_cli_ftp_url`, fans out
    to four URL-keyed reads (``tdocs``, ``tdoc_cr_cover_page``,
    ``tdoc_cr_ttcn_details``, ``tdoc_files`` — the
    ``tdoc_extracts`` timestamp is sourced via
    ``TDocCrDetailRepository.get_extract_meta_by_url``), and
    raises :class:`typer.BadParameter` when the URL matches no row
    in any of them.

    Does NOT trigger :func:`trigger_auto_sync` — the URL is the row
    identity, so no parent-meeting sync is meaningful for an
    arbitrary URL. Raw format takes the cache-direct path below
    without going through ``TDocCrService.extract``.

    ``compact`` is resolved against :attr:`Settings.output.compact` via
    :func:`_resolve_compact` and forwarded to the by-url renderers. The
    CLI flag (``--compact`` on the parent ``tdoc show`` command) wins
    over the setting; the dispatcher mirrors the by-id path so both
    selectors produce byte-identical output.
    """
    url = _normalise_cli_ftp_url(raw_url)
    resolved_compact = _resolve_compact(compact)

    if fmt == "raw":
        _render_tdoc_show_raw_by_url(url, output, compact=resolved_compact)
        return

    show_repos = TDocShowRepos(
        tdoc=build_tdoc_repository(),
        cr=build_tdoc_cr_repository(),
        cr_ttcn=build_tdoc_cr_ttcn_repository(),
        cr_change_details=build_tdoc_cr_change_details_repository(),
        file=build_tdoc_file_repository(),
    )
    record = TDocShowRecordByUrl.from_ftp_url(url, show_repos)

    if (
        record.tdoc is None
        and record.cover is None
        and record.extracted_at is None
        and record.ttcn is None
        and record.changes is None
        and not record.files
    ):
        raise typer.BadParameter(
            f"No row in tdocs, tdoc_cr_cover_page, tdoc_cr_ttcn_details, "
            f"tdoc_cr_change_details, or tdoc_files matches ftp_url {url!r}."
        )

    if fmt == "json":
        _render_tdoc_show_by_url_json(record, output, compact=resolved_compact)
    elif fmt == "markdown":
        _render_tdoc_show_by_url_markdown(record, output, compact=resolved_compact)
    else:
        _render_tdoc_show_by_url_table(record, output, compact=resolved_compact)


def _render_tdoc_show_raw_by_url(
    url: str,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit ``tdoc show --ftp-url --format raw``.

    The URL is the row identity — the cache file is derived
    directly from the URL via :func:`derive_cache_file`, so no
    TDoc resolution or ``TDocCrService.extract`` call is needed.
    On a cache miss the operator is pointed at the explicit-parse
    paths that would populate the cache.

    ``compact`` is accepted for symmetry with the other renderers but
    ignored — raw emits the converted markdown verbatim, which is
    already maximally compact by construction.
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
    *,
    compact: bool = False,
) -> None:
    """Emit ``tdoc show --ftp-url --format json``.

    Payload shape mirrors :func:`_render_tdoc_show_json` but
    anchored on the URL. ``ftp_url`` is always emitted; ``tdoc``
    is omitted when no matching ``TDoc`` row exists. Optional keys
    (``cover`` / ``ttcn`` / ``extracted_at`` / ``files``) follow
    the same omit-when-null convention as the existing renderer.

    When ``compact=True`` the output is a single line with no indent,
    no operator-space, and no trailing newline — sized for tight
    log/scan pipelines instead of human reading.
    """
    payload = _build_show_payload(
        record, anchor_key="ftp_url", anchor_value=record.ftp_url,
    )
    _dump_show_json(payload, output, compact=compact)


def _render_tdoc_show_by_url_markdown(
    record: TDocShowRecordByUrl,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit ``tdoc show --ftp-url --format markdown``.

    The URL is the document anchor (``# FTP URL ...``); each
    populated table gets its own ``## ...`` section. Optional
    sections (``## TDoc``, ``## Extracted Cover Details``,
    ``## TTCN Details``, ``## Auxiliary Files``) are emitted only
    when populated. ``tdoc_files`` rows mirror the per-row layout
    used by :func:`_render_tdoc_show_markdown`.

    When ``compact=True`` every CommonMark decorator (``**bold**``,
    ``*italic*``, ``## headings``, ``- `` bullets, ```` ```json ````
    fences) is dropped; fields become ``key: value`` plain lines and
    sections are separated by a single blank line. ``None`` values
    render as ``-`` and ``—`` is normalised to ``-``.
    ``required_changes`` becomes a single-line JSON literal;
    ``changed_functions`` becomes a comma-joined line. Placeholder
    text becomes a single ``note: <plain>`` line. ``ftp_url`` is
    just another ``key: value`` line (the first field in the compact
    form) — the URL is the document anchor in the non-compact form
    only.
    """
    stream, close_after = _open_output(output)
    try:
        if compact:
            _render_tdoc_show_markdown_compact(
                stream, record,
                anchor_field="ftp_url",
                anchor_value=record.ftp_url,
                tdoc_missing_note=(
                    "note: No tdocs row matches this URL; the URL "
                    "still surfaces in tdoc_cr_cover_page / "
                    "tdoc_cr_ttcn_details / tdoc_files because the "
                    "upstream document appeared in a sync but no "
                    "parent TDoc row was stored."
                ),
                parse_hint="--from-url <url>",
                files_missing_hint="No auxiliary files match this URL.",
            )
            return
        _render_tdoc_show_markdown_full(
            stream, record,
            header_line=f"# FTP URL `{record.ftp_url}`",
            tdoc_heading="## TDoc",
            tdoc_missing_note=(
                "_No `tdocs` row matches this URL. The URL still "
                "surfaces in `tdoc_cr_cover_page` / `tdoc_cr_ttcn_details` "
                "/ `tdoc_files` because the upstream document appeared "
                "in a sync but no parent TDoc row was stored._"
            ),
            parse_hint="--from-url <url>",
            show_extracted_details_fallback=True,
            files_missing_hint="No auxiliary files match this URL.",
            omit_files_heading_when_empty=True,
        )
    finally:
        if close_after:
            stream.close()


def _render_tdoc_show_by_url_table(
    record: TDocShowRecordByUrl,
    output: str | None,
    *,
    compact: bool = False,
) -> None:
    """Emit ``tdoc show --ftp-url --format table`` (the default for URL mode).

    Mirrors :func:`_render_tdoc_show_table` but anchored on the URL:
    ``[FTP URL]`` precedes ``[TDoc]`` / ``[Extracted Details]`` /
    ``[TTCN Details]`` / ``[Auxiliary Files]``. Optional blocks are
    omitted when their source row is absent (the ``[TDoc]`` block
    drops entirely when no ``TDoc`` row matches).

    ``compact`` is accepted for symmetry with the other renderers but
    ignored — the table format is already line-oriented and maximally
    compact by construction.
    """
    stream, close_after = _open_output(output)
    try:
        stream.write("[FTP URL]\n")
        stream.write(f"ftp_url: {record.ftp_url}\n")
        _render_tdoc_show_table_body(
            stream,
            record,
            parse_hint="--from-url <url>",
            tdoc_missing_msg="No tdocs row matches this URL.",
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
    max_tdoc_size_bytes: int = 0,
    compact: bool = False,
) -> None:
    """Parse every ``.docx`` / ``.zip`` under ``from_path`` and write one output file each.

    No cache or DB writes occur. Failures per file are logged and
    counted; the run continues so one bad file does not abort the
    whole batch. A summary is printed to stdout after all files are
    processed.
    """
    from doc3gpp.parsers.cr_parser import CRHeaderMissingError
    from doc3gpp.scraping.tdoc_zip_source import TDocZipDownloadError
    from doc3gpp.services.tdoc_cr_service import TDocTooLargeError

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

    service = build_tdoc_cr_service(max_tdoc_size_bytes=max_tdoc_size_bytes)
    skipped = 0
    size_skipped = 0
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

        if max_tdoc_size_bytes > 0:
            try:
                file_size = input_path.stat().st_size
            except OSError as exc:
                logger.warning("Failed to stat %s: %s", input_path, exc)
                failures += 1
                continue
            if file_size > max_tdoc_size_bytes:
                logger.warning(
                    "Skipping %s: %d bytes exceeds max_tdoc_size_kb limit (%d bytes)",
                    input_path, file_size, max_tdoc_size_bytes,
                )
                size_skipped += 1
                continue

        try:
            payload = input_path.read_bytes()
            result = service.extract_from_bytes(
                payload, str(input_path), force=False, full=full,
                max_tdoc_size_bytes=max_tdoc_size_bytes,
            )
        except TDocTooLargeError as exc:
            logger.warning("Skipping %s: %s", input_path, exc)
            size_skipped += 1
            continue
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
                _emit_record_raw(result.markdown, str(out_path), compact=compact)
            else:
                if result.details is None:
                    logger.warning(
                        "No parsed details for %s; skipping output.", input_path
                    )
                    failures += 1
                    continue
                _emit_record(result.details, resolved_format, str(out_path), compact=compact)
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
    typer.echo(f"Skipped (exceeds max_tdoc_size_kb): {size_skipped}")
    typer.echo(f"Re-parsed (with --force):        {re_parsed}")
    typer.echo(f"Newly parsed:                    {newly_parsed}")
    typer.echo(f"Failures:                        {failures}")
    if failures > 0 and newly_parsed + re_parsed == 0 and size_skipped == 0:
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
    max_tdoc_size_bytes: int = 0,
    compact: bool = False,
) -> None:
    """Batch-parse every matching file under a 3GPP FTP folder URL.

    DB/cache writes happen inside the service for FK hits. Per-file
    results are written to ``--output`` when it is provided and mirrored
    under the FTP folder structure; otherwise only a summary is printed.
    """
    resolved_format = _resolve_direct_format(fmt)
    service = build_tdoc_cr_service(max_tdoc_size_bytes=max_tdoc_size_bytes)
    batch = service.extract_from_url_batch(
        from_url,
        max_depth=max_depth,
        force=force,
        full=full,
        max_tdoc_size_bytes=max_tdoc_size_bytes or None,
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
        compact=compact,
    )


def _emit_url_batch_results(
    *,
    batch: DirectParseBatchResult,
    root_url: str,
    output: str | None,
    fmt: str,
    compact: bool = False,
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
                    _emit_record_raw(result.markdown, str(out_path), compact=compact)
                else:
                    if result.details is None:
                        logger.warning(
                            "No parsed details for %s; skipping output.", result.tdoc_id
                        )
                        failures += 1
                        continue
                    _emit_record(result.details, fmt, str(out_path), compact=compact)
            except OSError as exc:
                logger.warning("Failed to write %s: %s", out_path, exc)
                failures += 1
                continue

        if result.from_cache:
            cache_hits += 1
        else:
            newly_parsed += 1

    typer.echo("---")
    typer.echo(f"Scanned:                         {len(batch.results) + failures + len(batch.skipped)}")
    if output_dir is not None:
        typer.echo(f"Skipped (output already exists): {skipped}")
    typer.echo(f"Skipped (exceeds max_tdoc_size_kb): {len(batch.skipped)}")
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
            "Surfaces every row in tdocs, tdoc_cr_cover_page, "
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table`` and ``raw``. Defaults to ``output.compact`` in "
            "settings when the flag is not passed."
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
      ``tdoc_cr_cover_page``, ``tdoc_cr_ttcn_details``,
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
    resolved_compact = _resolve_compact(compact)

    if (tdoc is None) == (ftp_url is None):
        raise typer.BadParameter(
            "Provide exactly one of --tdoc <id> or --ftp-url <url>."
        )

    if ftp_url is not None:
        _tdoc_show_by_ftp_url(ftp_url, fmt, output, compact=resolved_compact)
        return

    trigger_auto_sync(
        auto_sync_enabled=settings.sync.auto_sync,
        meeting_service=build_meeting_service(),
        tdoc_sync_coordinator=build_tdoc_sync_coordinator(),
        tdoc=tdoc,
    )
    normalised_tdoc_id = _normalise_cli_tdoc_id(tdoc)

    # Raw format takes a separate path: it doesn't render the DB rows,
    # it pulls the converted markdown from the cache (populating it via
    # a fresh extract when the cache is cold). Resolved via the repo so
    # the cache lookup has the canonical tdoc_id.
    if fmt == "raw":
        record_for_raw = build_tdoc_repository().get_by_id(normalised_tdoc_id)
        if record_for_raw is None:
            raise typer.BadParameter(
                f"Unknown TDoc '{tdoc}'. Run 'doc3gpp tdoc list' to see stored "
                "TDocs, or 'doc3gpp tdoc sync' to ingest a meeting's TDocs first."
            )
        _render_tdoc_show_raw(record_for_raw.tdoc_id, output, compact=resolved_compact)
        return

    show_repos = TDocShowRepos(
        tdoc=build_tdoc_repository(),
        cr=build_tdoc_cr_repository(),
        cr_ttcn=build_tdoc_cr_ttcn_repository(),
        cr_change_details=build_tdoc_cr_change_details_repository(),
        file=build_tdoc_file_repository(),
    )
    try:
        show_record = TDocShowRecord.from_tdoc_id(normalised_tdoc_id, show_repos)
    except TDocNotFoundError:
        raise typer.BadParameter(
            f"Unknown TDoc '{tdoc}'. Run 'doc3gpp tdoc list' to see stored "
            "TDocs, or 'doc3gpp tdoc sync' to ingest a meeting's TDocs first."
        ) from None
    if fmt == "json":
        _render_tdoc_show_json(show_record, output, compact=resolved_compact)
    elif fmt == "markdown":
        _render_tdoc_show_markdown(show_record, output, compact=resolved_compact)
    else:
        _render_tdoc_show_table(show_record, output, compact=resolved_compact)


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


@tsg_app.command("schema")
def tsg_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the tsgs table (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing tsg schema")
    _emit_schema("tsg", fmt, output, compact=resolved_compact)


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
    create_schema("all")
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
    create_schema("all")
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
        help="Rich filter on WI name (LIKE, !NOT LIKE, null/not-null).",
    ),
    acronym: str | None = typer.Option(
        None,
        help="Rich filter on WI acronym (LIKE, !NOT LIKE, null/not-null).",
    ),
    release: str | None = typer.Option(
        None,
        help="Rich filter on WI release marker (LIKE, !NOT LIKE, null/not-null).",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
    ),
) -> None:
    """List stored WIs matching optional filters.

    Filters ``--name``, ``--acronym``, and ``--release`` accept the rich
    filter grammar (``null`` / ``not-null`` / ``!pattern`` / plain LIKE
    with ``%`` and ``_`` wildcards). Output columns default to wi_id,
    acronym, release, and name; use ``--format`` and ``--output`` to
    control formatting and destination.
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
    resolved_compact = _resolve_compact(compact)

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
        compact=resolved_compact,
    )


@wi_app.command("schema")
def wi_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the wis table (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing wi schema")
    _emit_schema("wi", fmt, output, compact=resolved_compact)


@spec_app.command("sync")
def spec_sync(
    tsg: str | None = typer.Option(
        None,
        "--tsg",
        help=(
            "TSG short name (e.g. R5) for the spec list page to sync. "
            "Mutually exclusive with --spec-id. When neither --tsg nor "
            "--spec-id is given, every distinct TSG found in the local "
            "specs table is synced."
        ),
    ),
    spec_id: str | None = typer.Option(
        None,
        "--spec-id",
        help=(
            "Dotted spec id (e.g. 36.579-5) to sync a single stored spec. "
            "Mutually exclusive with --tsg. When neither --tsg nor "
            "--spec-id is given, every distinct TSG found in the local "
            "specs table is synced."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass the spec sync interval skip rule.",
    ),
    per_version_details: bool = typer.Option(
        False,
        "--per-version-details",
        "-d",
        help=(
            "Also fetch per-version follow-ups (ETSI PDF link + CR list). "
            "Default off so the sync stays cheap; existing stored "
            "pdf_url and crs values are preserved either way."
        ),
    ),
) -> None:
    """Fetch and store specs (and their versions) from 3gpp.org.

    Valid --tsg values are:
    `R1`, `R2`, `R3`, `R4`, `R5`, `RT`, `RP`,
    `C1`, `C3`, `C4`, `C6`, `CP`,
    `S1`, `S2`, `S3`, `S4`, `S5`, `S6`, `SP`

    When no ``--tsg`` and no ``--spec-id`` is given, every distinct TSG
    found in the local specs table is synced.

    When a TSG was synced within ``sync.spec_sync_interval``
    the sync is skipped unless ``--force`` is passed.

    By default, the per-version follow-ups (ETSI PDF link and CR list)
    are not re-fetched; pass ``--per-version-details`` to also pull
    them. Existing stored ``pdf_url`` and ``crs`` values are preserved
    either way.
    """
    create_schema("all")
    tsg_service = _ensure_tsg_ready(build_tsg_service())
    service = build_spec_service()

    if tsg is not None and spec_id is not None:
        raise typer.BadParameter(
            "--tsg and --spec-id are mutually exclusive; pass exactly one."
        )

    if spec_id is not None:
        from tqdm import tqdm

        bar = tqdm(total=1, desc=f"spec {spec_id}", unit="spec", dynamic_ncols=True)

        def _on_progress(event: str, data: dict) -> None:
            if event == "spec_done":
                bar.update(1)

        try:
            outcome = service.sync_spec(
                spec_id,
                force=force,
                per_version_details=per_version_details,
                on_progress=_on_progress,
            )
        except SpecUnknownOnUpstreamError as exc:
            bar.close()
            raise typer.BadParameter(str(exc)) from exc
        bar.close()
        typer.echo(outcome.reason)
        return

    if tsg is None:
        tsgs = service.list_distinct_tsgs()
        if not tsgs:
            logger.info("No stored specs with a TSG found; nothing to sync")
            typer.echo("No stored specs with a TSG found; nothing to sync.")
            return
        logger.info(
            "Starting spec sync for %s stored TSG(s): %s", len(tsgs), ", ".join(tsgs)
        )
    else:
        tsgs = [_validate_tsg_short_name(tsg, tsg_service)]
        logger.info("Starting spec sync for TSG %s", tsgs[0])

    for tsg_short in tsgs:
        if not tsg_service.is_known_short_name(tsg_short):
            logger.warning("Skipping unknown TSG '%s' found in specs table", tsg_short)
            typer.echo(f"Skipping unknown TSG '{tsg_short}' found in specs table.")
            continue

        from tqdm import tqdm

        bar: tqdm | None = None

        def _on_progress(
            event: str,
            data: dict,
            tsg_name: str = tsg_short,
        ) -> None:
            nonlocal bar
            if event == "list_parsed":
                bar = tqdm(
                    total=data["total"],
                    desc=f"spec {tsg_name}",
                    unit="spec",
                    dynamic_ncols=True,
                )
            elif event == "spec_done" and bar is not None:
                bar.update(1)

        outcome: SyncOutcome = service.sync(
            tsg_short,
            force=force,
            per_version_details=per_version_details,
            on_progress=_on_progress,
        )
        if bar is not None:
            bar.close()
        typer.echo(outcome.reason)


@spec_app.command("list")
def spec_list(
    limit: int = typer.Option(50, min=1, max=500),
    offset: int = typer.Option(
        0, min=0, help="Number of rows to skip before applying --limit (pagination)."
    ),
    tsg: str | None = typer.Option(None, "--tsg", help="Only list specs for the given TSG."),
    type: str | None = typer.Option(None, "--type", help="Rich filter on spec type (TS|TR)."),
    spec_id: str | None = typer.Option(
        None, "--spec-id", help="Rich filter on spec id (e.g. 36.579-5)."
    ),
    title: str | None = typer.Option(None, "--title", help="Rich filter on spec title."),
    status: str | None = typer.Option(None, "--status", help="Rich filter on spec status."),
    radio_tech: str | None = typer.Option(
        None, "--radio-tech", help="Rich filter on radio technologies."
    ),
    initial_release: str | None = typer.Option(
        None, "--initial-release", help="Rich filter on initial release."
    ),
    wis: str | None = typer.Option(
        None, "--wis", help="Rich filter on related WIs (comma-joined)."
    ),
    rapporteurs: str | None = typer.Option(
        None, "--rapporteurs", help="Rich filter on rapporteurs (comma-joined company names)."
    ),
    parsed: str | None = typer.Option(
        None,
        "--parsed",
        help="Filter by whether any stored spec document is parsed: true or false.",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
    ),
) -> None:
    """List stored specs matching optional filters.

    All filter flags accept the rich filter grammar (``null`` /
    ``not-null`` / ``!pattern`` / plain LIKE with ``%`` and ``_``
    wildcards) where applicable. Output columns default to ``spec_id``,
    ``type``, ``title``, ``status``, ``radio_tech``, ``initial_release``,
    ``tsg``, ``wis``, and ``rapporteurs`` from
    ``settings.output.fields.spec``.
    """
    logger.info(
        "Listing %s specs (offset=%s) tsg=%s type=%s spec_id=%s",
        limit,
        offset,
        tsg,
        type,
        spec_id,
    )
    parsed_filter = _parse_bool_option(parsed, "--parsed")
    service = build_spec_service()
    records = service.list_recent(
        limit=limit,
        offset=offset,
        tsg=tsg,
        type=type,
        spec_id=spec_id,
        title=title,
        status=status,
        radio_tech=radio_tech,
        initial_release=initial_release,
        wis=wis,
        rapporteurs=rapporteurs,
        parsed=parsed_filter,
    )

    settings = get_settings()
    default_fields = settings.output.fields.spec
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)

    if fmt == "json":
        _dump_show_json(
            _spec_list_json_rows(records, default_fields),
            output,
            compact=resolved_compact,
        )
        return

    rows: list[list[str]] = []
    for item in records:
        assert isinstance(item, Spec)
        rows.append([str(getattr(item, f) or "-") for f in default_fields])

    _emit_records(
        rows=rows,
        fields=default_fields,
        fmt=fmt,
        output=output,
        no_records_msg="No specs found",
        compact=resolved_compact,
    )


@spec_app.command("schema")
def spec_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the specs and spec_versions tables (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing spec schema")
    _emit_schema("spec", fmt, output, compact=resolved_compact)


SPEC_DOC_LIST_FIELDS: list[str] = [
    "spec_id",
    "version",
    "release",
    "sections",
    "tables",
    "chunk_index",
    "text",
]

SPEC_DOC_TOC_FIELDS: list[str] = [
    "section_no",
    "title",
    "level",
    "source_file",
]

SPEC_DOC_SEARCH_FIELDS: list[str] = [
    "chunk_id",
    "score",
    "search_mode",
    "previews",
    "spec_id",
    "version",
    "release",
    "sections",
    "tables",
    "chunk_index",
    "text",
]

spec_doc_app = typer.Typer(help="spec document corpus: parse, TOC, search")
spec_app.add_typer(spec_doc_app, name="doc")
spec_doc_toc_app = typer.Typer(help="spec document TOC commands")
spec_doc_app.add_typer(spec_doc_toc_app, name="toc")


@spec_doc_app.command("search")
def spec_doc_search(
    text: str | None = typer.Option(
        None, "--text", help="FTS5 text or MATCH expression."
    ),
    semantic: str | None = typer.Option(
        None, "--semantic", help="Natural-language semantic query."
    ),
    spec: str | None = typer.Option(None, "--spec", help="Rich filter over spec id."),
    release: str | None = typer.Option(None, "--release", help="Rich filter over release."),
    version: str | None = typer.Option(None, "--version", help="Rich filter over version."),
    sections: str | None = typer.Option(
        None, "--sections", help="Rich filter over combined section metadata."
    ),
    tables: str | None = typer.Option(
        None, "--tables", help="Rich filter over combined table metadata."
    ),
    limit: int = typer.Option(20, "--limit", min=0, help="Max results."),
    offset: int = typer.Option(
        0, "--offset", min=0, help="Number of rows to skip before applying --limit."
    ),
    fields: str | None = typer.Option(
        None, "--fields", help="Comma-separated fields, or 'all'."
    ),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Write results to PATH instead of stdout."
    ),
    fmt: str | None = typer.Option(
        None, "--format", help="table | json | markdown"
    ),
    compact: bool = typer.Option(False, "--compact", help="Strip JSON / Markdown decorators."),
) -> None:
    """Search spec-document chunks using text, semantic, hybrid, or filter mode."""
    from doc3gpp.models.search import (
        SearchError,
        SearchIndexCorruptError,
        SearchQueryError,
        SearchUnavailableError,
    )
    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError,
        SemanticSearchQueryError,
        SemanticSearchUnavailableError,
        VectorIndexUnavailableError,
    )
    from doc3gpp.services.factory import build_spec_doc_search_facade

    _validate_search_filter_values(release=release, spec=spec)
    settings = get_settings()
    out_fields = _parse_field_selection(
        fields,
        SPEC_DOC_SEARCH_FIELDS,
        settings.output.fields.spec_doc,
    )
    fmt_resolved = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    filters = SpecDocSearchFilters(
        spec_id=spec,
        release=release,
        version=version,
        sections=sections,
        tables=tables,
        limit=limit,
        offset=offset,
    )
    facade = build_spec_doc_search_facade()
    try:
        results = facade.search(
            text=text,
            semantic=semantic,
            filters=filters,
        )
    except SearchQueryError as exc:
        typer.echo(f"bad query: {exc}", err=True)
        raise typer.Exit(code=2)
    except SemanticSearchQueryError as exc:
        typer.echo(f"bad query: {exc}", err=True)
        raise typer.Exit(code=2)
    except SearchIndexCorruptError:
        typer.echo(
            "search index corrupt; run `doc3gpp spec doc index --rebuild`",
            err=True,
        )
        raise typer.Exit(code=3)
    except SearchUnavailableError:
        typer.echo("search disabled in settings", err=True)
        raise typer.Exit(code=0)
    except EmbedderUnavailableError as exc:
        typer.echo(f"embedding model load failed: {exc}", err=True)
        raise typer.Exit(code=1)
    except VectorIndexUnavailableError as exc:
        typer.echo(f"vector index unavailable: {exc}", err=True)
        raise typer.Exit(code=1)
    except SemanticSearchUnavailableError as exc:
        typer.echo(f"search sem unavailable: {exc}", err=True)
        raise typer.Exit(code=1)
    except SearchError as exc:
        typer.echo(f"search index corrupt; run `doc3gpp spec doc index --rebuild`: {exc}", err=True)
        raise typer.Exit(code=3)
    _render_unified_spec_doc_results(
        results, fmt_resolved, resolved_compact, out_fields, output
    )


@spec_doc_app.command("index")
def spec_doc_index(
    rebuild: bool = typer.Option(False, "--rebuild", help="Rebuild the FTS5 index."),
    rebuild_embeddings: bool = typer.Option(
        False, "--rebuild-embeddings", help="Rebuild the vector index."
    ),
    rebuild_all: bool = typer.Option(
        False, "--rebuild-all", help="Rebuild both indexes."
    ),
    batch: int | None = typer.Option(None, "--batch", min=1, help="Rebuild batch size."),
    resume: bool = typer.Option(False, "--resume", help="Resume from the last cursor."),
    stale_only: bool = typer.Option(
        False, "--stale-only", help="Only rebuild stale rows."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress rebuild progress messages."
    ),
) -> None:
    """Show or maintain spec-document FTS5 and vector indexes."""
    from doc3gpp.models.index import IndexRequest
    from doc3gpp.models.search import SearchUnavailableError
    from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
    from doc3gpp.services.factory import build_spec_doc_index_service

    service = build_spec_doc_index_service()
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
            request,
            quiet=quiet,
            on_progress=None if quiet else typer.echo,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (SearchUnavailableError, SemanticSearchUnavailableError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    _render_index_result(result, resource="spec doc", action=any((rebuild, rebuild_embeddings, rebuild_all)))


def _spec_doc_source_to_row(src: object) -> dict[str, object]:
    """Serialise a :class:`SpecDocSource` for the fetch/parse JSON payload."""
    return {
        "spec_id": src.spec_id,
        "version": src.version,
        "release": _serialise_show_value(getattr(src, "release", None)),
        "ftp_url": getattr(src, "ftp_url", ""),
        "downloaded_at": _serialise_show_value(getattr(src, "downloaded_at", None)),
        "parsed_at": _serialise_show_value(getattr(src, "parsed_at", None)),
        "chunk_count": getattr(src, "chunk_count", 0),
        "docx_count": getattr(src, "docx_count", 0),
    }


def _spec_doc_chunk_to_row(hit: object, fields: list[str]) -> list[str]:
    """Render one :class:`SpecDocHit` as a table/markdown row over ``fields``."""
    return [str(getattr(hit, f, None) if getattr(hit, f, None) is not None else "-") for f in fields]


def _spec_doc_toc_entry_to_row(entry: object, fields: list[str]) -> list[str]:
    """Render one :class:`SpecDocTocEntry` as a table/markdown row over ``fields``."""
    return [str(getattr(entry, f, None) if getattr(entry, f, None) is not None else "-") for f in fields]


@spec_doc_app.command("parse")
def spec_doc_parse(
    spec: list[str] | None = typer.Option(  # noqa: B008 - Typer option declaration
        None,
        "--spec",
        help="Spec id to parse; repeat per spec (at least one required).",
    ),
    release: str | None = typer.Option(
        None, "--release", help="Release marker, e.g. Rel-18.",
    ),
    version: str | None = typer.Option(
        None, "--version", help="Exact version, e.g. 18.5.0.",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="Re-download and re-parse already-parsed pairs.",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Batch form: --spec may repeat; success/skip/failure buckets printed."""
    create_schema("all")
    if not spec:
        raise typer.BadParameter("pass at least one --spec <id>")
    svc = build_spec_doc_service()
    result = svc.parse_many(spec, release=release, version=version, force=force)
    for sid in result.successes:
        typer.echo(f"ok {sid}")
    for sid, why in result.skipped.items():
        typer.echo(f"skipped {sid}: {why}")
    for sid, why in result.failures.items():
        typer.echo(f"failed {sid}: {why}", err=True)


@spec_doc_toc_app.command("show")
def spec_doc_toc_show(
    spec: str = typer.Option(
        ...,
        "--spec",
        help="Dotted spec id (e.g. 38.331).",
    ),
    version: str = typer.Option(
        ...,
        "--version",
        help="Exact version, e.g. 18.5.0.",
    ),
    release: str | None = typer.Option(
        None, "--release", help="Release marker, e.g. Rel-18.",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Render the parsed TOC for one ``(spec_id, version)`` pair."""
    from doc3gpp.models.spec_doc import SpecDocUnknownVersionError

    create_schema("all")
    svc = build_spec_doc_service()
    try:
        toc = svc.get_toc(spec, version, release=release)
    except SpecDocUnknownVersionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    settings = get_settings()
    default_fields = settings.output.fields.spec_doc_toc
    out_fields = _parse_field_selection(fields, SPEC_DOC_TOC_FIELDS, default_fields)
    fmt_resolved = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    if fmt_resolved == "json":
        payload = {
            "spec_id": toc.spec_id,
            "version": toc.version,
            "release": _serialise_show_value(toc.release),
            "docx_count": toc.docx_count,
            "entries": [
                {f: _serialise_show_value(getattr(e, f)) for f in out_fields}
                for e in toc.entries
            ],
            "files": [
                {
                    "source_file": f.source_file,
                    "file_order": f.file_order,
                    "first_section": _serialise_show_value(f.first_section),
                }
                for f in toc.files
            ],
        }
        _dump_show_json(payload, output, compact=resolved_compact)
        return
    rows = [_spec_doc_toc_entry_to_row(e, out_fields) for e in toc.entries]
    _emit_records(
        rows=rows,
        fields=out_fields,
        fmt=fmt_resolved,
        output=output,
        no_records_msg=f"No TOC entries stored for {spec}@{version}",
        compact=resolved_compact,
    )


@spec_doc_app.command("schema")
def spec_doc_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the spec_doc_sources, spec_doc_tocs and spec_doc_chunks tables (separate specdata sqlite file) (meaning, format, possible values)."""
    settings = get_settings()
    fmt_resolved = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing spec_doc schema")
    _emit_schema("spec_doc", fmt_resolved, output, compact=resolved_compact)


@spec_app.command("show")
def spec_show(
    spec_id: str = typer.Argument(..., help="Dotted spec id (e.g. 36.579-5)."),
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
    ),
    limit: int = typer.Option(10, min=1, max=500, help="Max versions to return."),
    offset: int = typer.Option(0, min=0, help="Number of versions to skip before applying --limit (pagination)."),
    version: str | None = typer.Option(
        None, "--version", help="Rich filter pattern on the version (e.g. 19.%)."
    ),
    no_wis_crs: bool = typer.Option(
        False, "--no-wis-crs",
        help="Drop the 'wis' header field and the per-version 'crs' field from output.",
    ),
) -> None:
    """Render one spec with its versions and per-version metadata.

    Looks up the spec by its dotted id; emits the header row first,
    a blank separator line, then one row per stored version. The
    JSON payload nests the header under a ``"spec"`` key and the
    version rows under ``"versions"`` so downstream consumers can
    parse the shape without consulting multiple tables.
    """
    service = build_spec_service()
    spec = service.get(spec_id)
    if spec is None:
        raise typer.BadParameter(
            f"Unknown spec id '{spec_id}'. "
            f"Run 'doc3gpp spec sync --spec-id {spec_id}' to fetch it directly, "
            f"or '--tsg <tsg>' to sync the whole TSG."
        )
    versions = service.list_versions(spec_id, limit=limit, offset=offset, version=version)

    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)

    header_fields = [
        "spec_id", "type", "title", "status", "radio_tech",
        "initial_release", "tsg", "wis", "rapporteurs",
    ]
    version_fields = [
        "version", "parsed", "release", "ftp_url", "meeting_id", "meeting_name",
        "upload_date", "pdf_url", "crs",
    ]

    if no_wis_crs:
        header_fields = [f for f in header_fields if f != "wis"]
        version_fields = [f for f in version_fields if f != "crs"]

    if fmt == "json":
        payload = {
            "spec": {
                f: _serialise_show_value(getattr(spec, f)) for f in header_fields
            },
            "versions": [
                {
                    f: _serialise_show_value(getattr(v, f)) for f in version_fields
                }
                for v in versions
            ],
        }
        _dump_show_json(payload, output, compact=resolved_compact)
        return

    header_row = [[_display_show_value(getattr(spec, f)) for f in header_fields]]
    version_rows: list[list[str]] = []
    for v in versions:
        assert isinstance(v, SpecVersion)
        version_rows.append([_display_show_value(getattr(v, f)) for f in version_fields])

    _emit_records(
        rows=header_row,
        fields=header_fields,
        fmt=fmt,
        output=output,
        no_records_msg=f"No spec {spec_id}",
        compact=resolved_compact,
    )
    typer.echo("")
    _emit_records(
        rows=version_rows,
        fields=version_fields,
        fmt=fmt,
        output=output,
        no_records_msg=f"No versions stored for {spec_id}",
        compact=resolved_compact,
    )


VALID_TESTCASE_GROUPS: tuple[str, ...] = ("5G", "LTE", "IMS", "UTRA", "POS", "MCX")

TESTCASE_LIST_FIELDS: list[str] = [
    "testcase_id",
    "title",
    "ats",
    "feature",
    "release",
    "wis",
    "spec",
    "group",
    "statuses",
]

TESTCASE_SHOW_HEADER_FIELDS: list[str] = [
    "testcase_id",
    "title",
    "ats",
    "feature",
    "release",
    "wis",
    "spec",
    "group",
]

TESTCASE_SHOW_STATUS_FIELDS: list[str] = ["path", "gcf_ptcrb", "ttcn_status"]


def _validate_testcase_group(group: str) -> str:
    """Return the canonical group name or raise typer.BadParameter."""
    canonical = group.upper()
    if canonical not in VALID_TESTCASE_GROUPS:
        valid = ", ".join(VALID_TESTCASE_GROUPS)
        raise typer.BadParameter(
            f"Unknown testcase group '{group}'. Valid groups: {valid}."
        )
    return canonical


def _format_testcase_statuses(statuses: list[TestCaseStatus]) -> str:
    """Render status rows as compact ``path=gcf/ttcn;…`` pairs.

    Rows arrive pre-sorted by ``PATH_RANK`` from the repository;
    ``None`` values render as ``-`` so table/markdown cells stay
    non-empty.
    """
    return ";".join(
        f"{s.path}={s.gcf_ptcrb or '-'}/{s.ttcn_status or '-'}"
        for s in statuses
    )


@testcase_app.command("sync")
def testcase_sync(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Re-download and re-parse the latest status file even when already recorded.",
    ),
) -> None:
    """Fetch the latest RAN5 TTCN status file and store its testcases.

    Resolves the single latest ``TTCN CR Agreement Status`` zip in the
    upstream ``History/`` folder, downloads it once, and upserts the
    parsed testcase headers and status rows. Re-running is a no-op
    until a new file appears unless ``--force`` is passed.
    """
    create_schema("all")
    service = build_testcase_service()

    from tqdm import tqdm

    bar: tqdm | None = None

    def _on_progress(event: str, data: dict) -> None:
        nonlocal bar
        if event == "listing":
            bar = tqdm(total=3, desc="testcase", unit="step", dynamic_ncols=True)
            bar.update(1)
        elif event in ("downloaded", "parsed") and bar is not None:
            bar.update(1)

    outcome = service.sync(force=force, on_progress=_on_progress)
    if bar is not None:
        bar.close()
    typer.echo(outcome.reason)


@testcase_app.command("list")
def testcase_list(
    limit: int = typer.Option(50, min=1, max=500),
    offset: int = typer.Option(
        0, min=0, help="Number of rows to skip before applying --limit (pagination)."
    ),
    testcase: str | None = typer.Option(
        None, "--testcase", help="Rich filter on testcase id."
    ),
    title: str | None = typer.Option(None, "--title", help="Rich filter on title."),
    ats: str | None = typer.Option(None, "--ats", help="Rich filter on ATS."),
    feature: str | None = typer.Option(
        None, "--feature", help="Rich filter on feature."
    ),
    release: str | None = typer.Option(
        None, "--release", help="Rich filter on release (e.g. Rel-17)."
    ),
    wis: str | None = typer.Option(
        None, "--wis", help="Rich filter on related WIs (comma-joined)."
    ),
    spec: str | None = typer.Option(
        None, "--spec", help="Rich filter on spec (e.g. 38.523-1)."
    ),
    group: str | None = typer.Option(
        None,
        "--group",
        help="Exact group match: 5G, LTE, IMS, UTRA, POS, or MCX.",
    ),
    status: str | None = typer.Option(
        None,
        "--status",
        help="Rich filter on ttcn_status (matches any path).",
    ),
    gcf_status: str | None = typer.Option(
        None,
        "--gcf-status",
        help="Rich filter on gcf_ptcrb (matches any path).",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
    ),
) -> None:
    """List stored RAN5 testcases matching optional filters.

    All filter flags accept the rich filter grammar (``null`` /
    ``not-null`` / ``!pattern`` / plain LIKE with ``%`` and ``_``
    wildcards). ``--status`` matches ``ttcn_status`` on any path and
    ``--gcf-status`` matches ``gcf_ptcrb`` on any path. Each row
    carries a nested ``statuses`` list of ``{path, gcf_ptcrb, ttcn_status}`` objects.
    Output columns default to ``testcase_id``, ``title``, ``spec``,
    ``group``, ``release``, and ``statuses`` from
    ``settings.output.fields.testcase``.
    """
    if group is not None:
        group = _validate_testcase_group(group)
    logger.info(
        "Listing %s testcases (offset=%s) testcase=%s group=%s spec=%s",
        limit,
        offset,
        testcase,
        group,
        spec,
    )
    service = build_testcase_service()
    records = service.list_recent(
        limit=limit,
        offset=offset,
        testcase_id=testcase,
        title=title,
        ats=ats,
        feature=feature,
        release=release,
        wis=wis,
        spec=spec,
        group=group,
        status=status,
        gcf_status=gcf_status,
    )

    settings = get_settings()
    default_fields = settings.output.fields.testcase
    out_fields = _parse_field_selection(fields, TESTCASE_LIST_FIELDS, default_fields)
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)

    if fmt == "json":
        payload = [
            {
                **{
                    f: str(getattr(item.testcase, f, None) or "-")
                    for f in out_fields
                    if f != "statuses"
                },
                **(
                    {
                        "statuses": [
                            {
                                f: _serialise_show_value(getattr(s, f))
                                for f in ("path", "gcf_ptcrb", "ttcn_status")
                            }
                            for s in item.statuses
                        ]
                    }
                    if "statuses" in out_fields
                    else {}
                ),
            }
            for item in records
        ]
        stream, close_after = _open_output(output)
        try:
            if resolved_compact:
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
        finally:
            if close_after:
                stream.close()
        return

    rows: list[list[str]] = []
    for item in records:
        assert isinstance(item, TestCaseWithStatuses)
        cells: list[str] = []
        for f in out_fields:
            if f == "statuses":
                cells.append(_format_testcase_statuses(item.statuses))
            else:
                cells.append(str(getattr(item.testcase, f, None) or "-"))
        rows.append(cells)

    _emit_records(
        rows=rows,
        fields=out_fields,
        fmt=fmt,
        output=output,
        no_records_msg="No testcases found",
        compact=resolved_compact,
    )


@testcase_app.command("schema")
def testcase_schema(
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Strip output formatting: JSON drops indent and operator-space. No-op for ``table``.",
    ),
) -> None:
    """Describe every column of the testcases, testcase_status and testcase_sources tables (separate testcase sqlite file) (meaning, format, possible values)."""
    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)
    logger.info("Describing testcase schema")
    _emit_schema("testcase", fmt, output, compact=resolved_compact)


@testcase_app.command("show")
def testcase_show(
    testcase: str = typer.Option(
        ...,
        "--testcase",
        help="Testcase id to render (e.g. TC_1).",
    ),
    group: str | None = typer.Option(
        None,
        "--group",
        help="Exact group match: 5G, LTE, IMS, UTRA, POS, or MCX. "
        "When omitted and the id exists in several groups, every "
        "matching group is rendered.",
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
    compact: bool = typer.Option(
        False,
        "--compact",
        help=(
            "Strip output formatting: JSON drops indent and operator-space; "
            "Markdown drops GFM tables, bullets, and bold. No-op for "
            "``table``. Defaults to ``output.compact`` in settings when "
            "the flag is not passed."
        ),
    ),
) -> None:
    """Render one testcase with every stored status row.

    Emits the header row first, a blank separator line, then one row
    per stored ``(group, path, gcf_ptcrb, ttcn_status)`` status triple.
    Without ``--group`` and several stored groups, every matching group
    is rendered: JSON emits an array with one flat object per
    ``(testcase_id, group)`` (a single-element array when exactly one
    group matches); table/markdown emit one header block per group
    separated by blank lines, then that group's status rows
    (``path, gcf_ptcrb, ttcn_status``).
    """
    service = build_testcase_service()
    if group is not None:
        group = _validate_testcase_group(group)
        detail = service.get(testcase, group)
        if detail is None:
            raise typer.BadParameter(f"Testcase {testcase!r} not found")
        details = [detail]
    else:
        details = service.get_all(testcase)
        if not details:
            raise typer.BadParameter(f"Testcase {testcase!r} not found")
    assert all(isinstance(detail, TestCaseDetail) for detail in details)

    settings = get_settings()
    fmt = _resolve_format(fmt, default=settings.output.format)
    resolved_compact = _resolve_compact(compact)

    def _detail_payload(detail: TestCaseDetail) -> dict:
        return {
            **{
                f: _serialise_show_value(getattr(detail.testcase, f))
                for f in TESTCASE_SHOW_HEADER_FIELDS
            },
            "statuses": [
                {
                    f: _serialise_show_value(getattr(status_row, f))
                    for f in TESTCASE_SHOW_STATUS_FIELDS
                }
                for status_row in detail.statuses
            ],
        }

    if fmt == "json":
        _dump_show_json(
            [_detail_payload(detail) for detail in details],
            output,
            compact=resolved_compact,
        )
        return

    for index, detail in enumerate(details):
        if index:
            typer.echo("")
        header_row = [
            [str(getattr(detail.testcase, f) or "-") for f in TESTCASE_SHOW_HEADER_FIELDS]
        ]
        status_rows: list[list[str]] = []
        for status_row in detail.statuses:
            assert isinstance(status_row, TestCaseStatus)
            status_rows.append(
                [str(getattr(status_row, f) or "-") for f in TESTCASE_SHOW_STATUS_FIELDS]
            )

        _emit_records(
            rows=header_row,
            fields=TESTCASE_SHOW_HEADER_FIELDS,
            fmt=fmt,
            output=output,
            no_records_msg=f"No testcase {testcase}",
            compact=resolved_compact,
        )
        typer.echo("")
        _emit_records(
            rows=status_rows,
            fields=TESTCASE_SHOW_STATUS_FIELDS,
            fmt=fmt,
            output=output,
            no_records_msg=f"No statuses stored for {testcase}",
            compact=resolved_compact,
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

    tmp_path: Path
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_path.parent,
            delete=False,
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(template)
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
    dumped = settings.model_dump(mode="json")
    sem = dumped.get("semantic_search")
    if isinstance(sem, dict) and sem.get("embedding_api_key"):
        sem["embedding_api_key"] = "***"
    typer.echo(json.dumps(dumped, indent=2, sort_keys=True))


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
