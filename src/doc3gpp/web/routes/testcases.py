"""HTTP routes for the testcase list + detail pages."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from doc3gpp.services.testcase_service import TestCaseService
from doc3gpp.web.deps import get_pending_jobs, get_testcase_service
from doc3gpp.web.errors import InvalidFilterError, TestcaseNotFoundError
from doc3gpp.web.filters import is_htmx_request, parse_int_query, parse_text_query
from doc3gpp.web.render import testcase_rows, testcase_status_rows
from doc3gpp.web.templates_setup import templates


router = APIRouter(prefix="/testcases", tags=["testcases"])


_LIMIT_CAP = 200

# Mirrors ``settings.output.fields.testcase`` — what
# ``doc3gpp testcase list --format json`` emits by default.
_TESTCASE_DEFAULT_FIELDS = [
    "testcase_id",
    "title",
    "spec",
    "group",
    "release",
    "statuses",
]
_TESTCASE_SHOW_FIELDS = [
    "testcase_id",
    "title",
    "ats",
    "feature",
    "release",
    "wis",
    "spec",
    "group",
]
_TESTCASE_STATUS_FIELDS = ["group", "path", "gcf_ptcrb", "ttcn_status"]

_VALID_GROUPS = ("5G", "LTE", "IMS", "UTRA", "POS", "MCX")


def _validate_group(group: str | None) -> str | None:
    """Return the canonical group or raise :class:`InvalidFilterError`."""
    if group is None:
        return None
    canonical = group.upper()
    if canonical not in _VALID_GROUPS:
        valid = ", ".join(_VALID_GROUPS)
        raise InvalidFilterError(
            f"Unknown testcase group '{group}'. Valid groups: {valid}."
        )
    return canonical


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def list_testcases(
    request: Request,
    testcase: str | None = Query(default=None),
    title: str | None = Query(default=None),
    ats: str | None = Query(default=None),
    feature: str | None = Query(default=None),
    release: str | None = Query(default=None),
    wis: str | None = Query(default=None),
    spec: str | None = Query(default=None),
    group: str | None = Query(default=None),
    status: str | None = Query(default=None),
    gcf_status: str | None = Query(default=None),
    limit: str | None = Query(default="50"),
    offset: str | None = Query(default="0"),
    format: str | None = Query(default=None, alias="format"),
    service: TestCaseService = Depends(get_testcase_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``testcase_list.html`` or a JSON list of testcases.

    ``?format=json`` returns the same payload as
    ``doc3gpp testcase list --format json``: a bare array of
    field-selected rows (``settings.output.fields.testcase`` by default)
    with ``statuses`` preserved as a dict via
    :func:`doc3gpp.web.render.testcase_rows`.

    The numeric query params (``limit``, ``offset``) are declared as
    ``str`` so an empty form value (``limit=``) doesn't trigger a 422 —
    :func:`parse_int_query` treats ``""`` as ``None`` and the route
    fills in the default. The CLI's typed path accepts ints only; the
    HTTP path is a best-effort form-binding layer.
    """
    parsed_limit = parse_int_query(limit, min=1, max=_LIMIT_CAP) or 50
    parsed_offset = parse_int_query(offset, min=0) or 0
    rows = service.list_recent(
        limit=parsed_limit,
        offset=parsed_offset,
        testcase_id=parse_text_query(testcase),
        title=parse_text_query(title),
        ats=parse_text_query(ats),
        feature=parse_text_query(feature),
        release=parse_text_query(release),
        wis=parse_text_query(wis),
        spec=parse_text_query(spec),
        group=_validate_group(parse_text_query(group)),
        status=parse_text_query(status),
        gcf_status=parse_text_query(gcf_status),
    )

    if format == "json":
        return JSONResponse(content=testcase_rows(rows, _TESTCASE_DEFAULT_FIELDS))

    next_offset = (
        parsed_offset + len(rows) if len(rows) == parsed_limit else None
    )
    template_name = (
        "partials/testcase_results.html"
        if is_htmx_request(request)
        else "testcase_list.html"
    )
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "active_nav": "testcases",
            "testcases": rows,
            "total": len(rows),
            "limit": parsed_limit,
            "offset": parsed_offset,
            "next_offset": next_offset,
            "pending_jobs": pending_jobs,
            "filters": {
                "testcase": testcase or "",
                "title": title or "",
                "ats": ats or "",
                "feature": feature or "",
                "release": release or "",
                "wis": wis or "",
                "spec": spec or "",
                "group": group or "",
                "status": status or "",
                "gcf_status": gcf_status or "",
                "limit": parsed_limit,
            },
        },
    )


@router.get("/{testcase_id}", include_in_schema=False)
async def show_testcase(
    request: Request,
    testcase_id: str,
    group: str | None = Query(default=None),
    format: str | None = Query(default=None, alias="format"),
    service: TestCaseService = Depends(get_testcase_service),
    pending_jobs: int = Depends(get_pending_jobs),
) -> Any:
    """Render ``testcase_show.html`` or a JSON payload with header + statuses.

    Without ``?group=`` every stored group for the id is returned: JSON
    emits an array of ``{"testcase", "statuses"}`` objects (one element
    when a single group matches) and HTML renders one section per group.
    """
    canonical_group = _validate_group(parse_text_query(group))
    if canonical_group is not None:
        detail = service.get(testcase_id, canonical_group)
        if detail is None:
            raise TestcaseNotFoundError(f"Testcase {testcase_id!r} not found")
        details = [detail]
    else:
        details = service.get_all(testcase_id)
        if not details:
            raise TestcaseNotFoundError(f"Testcase {testcase_id!r} not found")

    if format == "json":
        return JSONResponse(
            content=[
                {
                    "testcase": {
                        f: getattr(item.testcase, f, None)
                        for f in _TESTCASE_SHOW_FIELDS
                    },
                    "statuses": testcase_status_rows(
                        item.statuses, _TESTCASE_STATUS_FIELDS
                    ),
                }
                for item in details
            ]
        )

    return templates.TemplateResponse(
        request=request,
        name="testcase_show.html",
        context={
            "active_nav": "testcases",
            "details": details,
            "testcase": details[0].testcase,
            "statuses": details[0].statuses,
            "total": sum(len(item.statuses) for item in details),
            "pending_jobs": pending_jobs,
        },
    )


__all__ = ["router"]
