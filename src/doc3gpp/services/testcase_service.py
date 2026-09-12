"""Service layer for RAN5 conformance testcases."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from doc3gpp.models.sync import SyncOutcome
from doc3gpp.models.testcase import (
    TestCaseDetail,
    TestCaseSource,
    TestCaseStatus,
    TestCaseWithStatuses,
)
from doc3gpp.parsers.testcase_parser import extract_workbook, parse_testcase_workbook
from doc3gpp.repository.protocols import TestCaseRepository
from doc3gpp.scraping.testcase_source import (
    fetch_testcase_zip,
    list_history_files,
    parse_status_filename,
    select_latest,
)

logger = logging.getLogger(__name__)

TestCaseProgressFn = Callable[[str, dict], None]
"""Signature of the optional progress callback for :meth:`TestCaseService.sync`.

Events:
    ``"listing"`` — fired after the History folder listing is fetched.
        ``data`` is ``{}``.
    ``"downloaded"`` — fired after the status zip bytes are fetched.
        ``data`` is ``{"filename": <str>}``.
    ``"parsed"`` — fired after the workbook is parsed and upserted.
        ``data`` is ``{"testcases": <int>, "statuses": <int>}``.
"""


class TestCaseService:
    """Sync and query RAN5 testcase status snapshots."""

    def __init__(self, repository: TestCaseRepository) -> None:
        self._repository = repository

    def sync(
        self,
        *,
        force: bool = False,
        on_progress: TestCaseProgressFn | None = None,
    ) -> SyncOutcome:
        """Fetch latest History zip -> parse workbook -> upsert.

        Selects the latest status file, skips when its source row is
        already parsed (unless ``force``), otherwise downloads, records
        the download BEFORE parsing, extracts the workbook, parses it,
        upserts headers, replaces per-testcase statuses, and records
        the parsed stamp.
        """
        filenames = list_history_files()
        if on_progress is not None:
            on_progress("listing", {})
        filename = select_latest(filenames)

        existing = self._repository.get_source(filename)
        if existing is not None and existing.parsed_at is not None and not force:
            logger.info("Skipping testcases %s: already parsed", filename)
            return SyncOutcome(
                status="skipped",
                reason=(
                    f"Testcase sync skipped: {filename} already parsed; "
                    "use --force to override."
                ),
                synced_count=None,
            )

        zip_bytes = fetch_testcase_zip(filename)
        if on_progress is not None:
            on_progress("downloaded", {"filename": filename})

        parsed = parse_status_filename(filename)
        year, week, revision = parsed if parsed is not None else (0, 0, 0)
        self._repository.record_download(
            TestCaseSource(
                filename=filename,
                year=year,
                week=week,
                revision=revision,
                downloaded_at=datetime.now(timezone.utc),
            )
        )

        xlsx_bytes = extract_workbook(zip_bytes)
        cases, statuses, _notes = parse_testcase_workbook(xlsx_bytes)

        tc = self._repository.upsert_many(cases)
        grouped: dict[tuple[str, str], list[TestCaseStatus]] = {}
        for status in statuses:
            grouped.setdefault(
                (status.testcase_id, status.group), []
            ).append(status)
        for case in cases:
            self._repository.replace_statuses(
                case.testcase_id,
                case.group,
                grouped.get((case.testcase_id, case.group), []),
            )
        st = len(statuses)

        self._repository.record_parsed(
            filename, datetime.now(timezone.utc), tc, st
        )
        if on_progress is not None:
            on_progress("parsed", {"testcases": tc, "statuses": st})

        return SyncOutcome(
            status="synced",
            reason=(
                f"Testcase sync complete: {tc} testcases, "
                f"{st} statuses from {filename}"
            ),
            synced_count=tc,
        )

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        testcase_id: str | None = None,
        title: str | None = None,
        ats: str | None = None,
        feature: str | None = None,
        release: str | None = None,
        wis: str | None = None,
        spec: str | None = None,
        group: str | None = None,
        status: str | None = None,
        gcf_status: str | None = None,
    ) -> list[TestCaseWithStatuses]:
        """List testcase headers with their ``{path: ttcn_status}`` map."""
        canonical_group = group.upper() if group is not None else None
        cases = self._repository.list(
            limit=limit,
            offset=offset,
            testcase_id=testcase_id,
            title=title,
            ats=ats,
            feature=feature,
            release=release,
            wis=wis,
            spec=spec,
            group=canonical_group,
            status=status,
            gcf_status=gcf_status,
        )
        out: list[TestCaseWithStatuses] = []
        for case in cases:
            rows = self._repository.list_statuses(case.testcase_id, case.group)
            out.append(
                TestCaseWithStatuses(
                    testcase=case,
                    statuses={row.path: row.ttcn_status for row in rows},
                )
            )
        return out

    def get(
        self, testcase_id: str, group: str | None = None
    ) -> TestCaseDetail | None:
        """Return the header plus statuses for ``(testcase_id, group)`` or ``None``."""
        header = self._repository.get(testcase_id, group)
        if header is None:
            return None
        rows = self._repository.list_statuses(testcase_id, header.group)
        return TestCaseDetail(testcase=header, statuses=rows)

    def get_all(self, testcase_id: str) -> list[TestCaseDetail]:
        """Return every ``(testcase_id, group)`` row for ``testcase_id``."""
        cases = self._repository.list(testcase_id=testcase_id, limit=500)
        cases = [c for c in cases if c.testcase_id == testcase_id]
        out: list[TestCaseDetail] = []
        for case in cases:
            rows = self._repository.list_statuses(case.testcase_id, case.group)
            out.append(TestCaseDetail(testcase=case, statuses=rows))
        return out
