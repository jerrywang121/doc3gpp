"""Unit tests for LISTSERV date-set parsing, coverage, and settings."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from doc3gpp.models.listserv import (
    ArchivePage,
    DateInterval,
    DateSet,
    ListservAttachment,
    ListservBatchResult,
    ListservDetail,
    ListservDetailMetadata,
    ListservFilters,
    ListservMessage,
    ListservRecord,
    ListservSearchRecord,
    ListservSearchResult,
    coverage_contains,
    date_matches,
    default_sync_dates,
    parse_date_set,
    union_coverage,
)
from doc3gpp.settings.config_writer import load_default_template, tomllib
from doc3gpp.settings.schema import ListservSettings, Settings


def test_date_intersection_retains_hole() -> None:
    dates = parse_date_set([">= 2026-01-01", "!= 2026-01-05", "<= 2026-01-10"])
    assert date_matches(dates, date(2026, 1, 4))
    assert not date_matches(dates, date(2026, 1, 5))
    assert date_matches(dates, date(2026, 1, 6))
    assert dates.intervals == (
        DateInterval(date(2026, 1, 1), date(2026, 1, 4)),
        DateInterval(date(2026, 1, 6), date(2026, 1, 10)),
    )


def test_coverage_contains_hole_and_full_range() -> None:
    with_hole = parse_date_set([">= 2026-01-01", "!= 2026-01-05", "<= 2026-01-10"])
    full_range = parse_date_set([">= 2026-01-01", "<= 2026-01-10"])
    coverage = union_coverage(
        DateSet((DateInterval(date(2026, 1, 1), date(2026, 1, 4)),)),
        DateSet((DateInterval(date(2026, 1, 6), date(2026, 1, 10)),)),
    )
    assert coverage_contains(coverage, with_hole)
    assert not coverage_contains(coverage, full_range)
    assert coverage_contains(coverage, DateSet())


def test_union_coverage_merges_overlapping_and_adjacent() -> None:
    left = DateSet((DateInterval(date(2026, 1, 1), date(2026, 1, 4)),))
    middle = DateSet((DateInterval(date(2026, 1, 5), date(2026, 1, 7)),))
    right = DateSet((DateInterval(date(2026, 1, 6), date(2026, 1, 10)),))
    merged = union_coverage(union_coverage(left, right), middle)
    assert merged == DateSet((DateInterval(date(2026, 1, 1), date(2026, 1, 10)),))


def test_parse_date_set_all_operators_and_quotes() -> None:
    eq = parse_date_set(["= '2026-03-15'"])
    assert eq.intervals == (DateInterval(date(2026, 3, 15), date(2026, 3, 15)),)

    gt = parse_date_set(["> 2026-03-15"])
    assert gt.intervals == (DateInterval(date(2026, 3, 16), None),)
    assert not date_matches(gt, date(2026, 3, 15))
    assert date_matches(gt, date(2026, 3, 16))

    lt = parse_date_set(["< '2026-03-15'"])
    assert lt.intervals == (DateInterval(None, date(2026, 3, 14)),)
    assert date_matches(lt, date(2026, 3, 14))
    assert not date_matches(lt, date(2026, 3, 15))

    neq = parse_date_set(["!= 2026-03-15"])
    assert neq.intervals == (
        DateInterval(None, date(2026, 3, 14)),
        DateInterval(date(2026, 3, 16), None),
    )


def test_unbounded_and_empty_intersections() -> None:
    all_dates = parse_date_set([])
    assert all_dates.intervals == (DateInterval(None, None),)
    assert date_matches(all_dates, date(1999, 1, 1))
    assert date_matches(all_dates, date(2099, 12, 31))

    empty = parse_date_set([">= 2026-02-10", "< 2026-02-10"])
    assert empty.intervals == ()
    assert not date_matches(empty, date(2026, 2, 10))
    assert not coverage_contains(empty, all_dates)

    contradiction = parse_date_set(["= 2026-02-10", "!= 2026-02-10"])
    assert contradiction.intervals == ()


def test_leap_day_and_rolling_year_endpoints() -> None:
    leap_window = default_sync_dates(date(2024, 2, 29))
    assert leap_window.intervals == (
        DateInterval(date(2023, 2, 28), date(2024, 2, 29)),
    )
    assert date_matches(leap_window, date(2023, 2, 28))
    assert not date_matches(leap_window, date(2023, 2, 27))
    assert date_matches(leap_window, date(2024, 2, 29))

    normal_window = default_sync_dates(date(2026, 11, 11))
    assert normal_window.intervals == (
        DateInterval(date(2025, 11, 11), date(2026, 11, 11)),
    )


@pytest.mark.parametrize(
    "bad_expr",
    [
        "",
        "2026-01-01",
        "== 2026-01-01",
        "= 2026-02-29",  # non-leap year impossible date
        "= 2026-13-01",
        "= 2026-00-10",
        "= 2026-01-32",
        ">= 2026/01/01",
        "null",
        "not-null",
        "!= '2026-01-01\"",
    ],
)
def test_parse_date_set_rejects_malformed_or_impossible_dates(bad_expr: str) -> None:
    with pytest.raises(ValueError):
        parse_date_set([bad_expr])


def test_shared_dataclasses_are_frozen_slotted_and_normalized() -> None:
    page = ArchivePage(list_id="3gpp_tsg_ran_wg1", url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1", period="2026-10-W1")
    assert page.list_id == "3GPP_TSG_RAN_WG1"
    assert hasattr(ArchivePage, "__slots__")

    msg = ListservMessage(
        url="https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=1",
        list_id="3gpp_tsg_ran_wg1",
        subject="Subject R1-2610793",
        sender="Alice <[log in to unmask]>",
        date=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-10-W1",
        subject_tdocs=("r1-2610793", "R1-2610793"),
    )
    assert msg.list_id == "3GPP_TSG_RAN_WG1"
    assert msg.subject_tdocs == ("R1-2610793",)

    att = ListservAttachment(name="doc.docx", url="https://list.etsi.org/att/1")
    detail = ListservDetail(
        url=msg.url,
        attachments=(att,),
        tdocs=("rp-2601234",),
        content="Hello",
        content_truncated=False,
        parser_version="1.0.0",
        parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
    )
    assert detail.tdocs == ("RP-2601234",)
    rec = ListservRecord(message=msg, detail=detail, tdocs=("R1-2610793", "RP-2601234"), source_revision=1)
    meta = ListservDetailMetadata(
        attachments=(att,),
        content_truncated=False,
        parser_version="1.0.0",
        parsed_at=detail.parsed_at,
    )
    srec = ListservSearchRecord(message=msg, detail=meta, tdocs=rec.tdocs, source_revision=1)
    sres = ListservSearchResult(
        record=srec,
        score=1.0,
        snippet="Hello",
        matched_source="content",
        content_preview="Hello",
        preview_truncated=False,
    )
    batch = ListservBatchResult(processed=1, skipped=0, failures={})
    filters = ListservFilters()
    for obj in (page, msg, att, detail, rec, meta, srec, sres, batch, filters):
        assert hasattr(type(obj), "__slots__")
        with pytest.raises((AttributeError, TypeError)):
            setattr(obj, " non_existent", 1)


def test_listserv_settings_and_template() -> None:
    s = Settings()
    assert s.listserv.max_content_chars == 10000
    assert ListservSettings().max_content_chars == 10000
    with pytest.raises(ValidationError):
        ListservSettings(max_content_chars=0)
    with pytest.raises(ValidationError):
        ListservSettings(max_content_chars=-5)

    template_text = load_default_template()
    parsed = tomllib.loads(template_text)
    loaded = Settings(**parsed)
    assert loaded.listserv.max_content_chars == 10000
    assert "max_content_chars = 10000" in template_text
