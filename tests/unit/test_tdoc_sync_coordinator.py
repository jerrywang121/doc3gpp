"""Unit tests for :class:`TDocSyncCoordinator`.

Covers:
- #10: cross-service orchestration lives in the coordinator, not the CLI
- #11: coordinator depends on Protocol-typed repositories
- coordinator converts missing meetings / missing FTP URLs into typed errors
- the coordinator chains the TDoc and TDocFile syncs so that
  ``tdoc sync`` populates both tables in a single call
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.sync import BulkSyncFailure
from doc3gpp.models.sync import BulkSyncOutcome
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
    TDocSyncCoordinator,
)


class _FakeMeetingRepository:
    """In-memory MeetingRepository double that satisfies the Protocol."""

    def __init__(self, meetings: dict[int, Meeting] | None = None) -> None:
        self._meetings = meetings or {}
        self._last_sync_calls: list[tuple[int, object]] = []

    def upsert_many(self, meetings):  # pragma: no cover - not exercised here
        return 0

    def list(self, limit=50):  # pragma: no cover - not exercised here
        return list(self._meetings.values())

    def get_by_id(self, meeting_id: int):
        return self._meetings.get(meeting_id)

    def get_by_name(self, meeting_name: str):
        for m in self._meetings.values():
            if m.name == meeting_name:
                return m
        return None

    def update_tdoc_list_last_sync(self, meeting_id: int, synced_at) -> bool:
        self._last_sync_calls.append((meeting_id, synced_at))
        return meeting_id in self._meetings


class _FakeTDocRepository:
    """In-memory TDocRepository double that records upsert calls."""

    def __init__(self, distinct_meeting_ids: list[int] | None = None) -> None:
        self.upsert_calls: list[tuple[str, int | None]] = []
        self._tdoc_ids_by_meeting: dict[int, list[str]] = {}
        self._distinct_meeting_ids = distinct_meeting_ids or []

    def upsert(self, tdoc):  # pragma: no cover - delegated below
        self.upsert_many([tdoc])

    def upsert_many(self, tdocs: list[TDoc]) -> int:
        for tdoc in tdocs:
            self.upsert_calls.append((tdoc.tdoc_id, tdoc.meeting_id))
            if tdoc.meeting_id is not None:
                self._tdoc_ids_by_meeting.setdefault(tdoc.meeting_id, []).append(
                    tdoc.tdoc_id
                )
        return len(tdocs)

    def list(self, limit=20, **kwargs):  # pragma: no cover
        return []

    def list_tdoc_ids_for_meeting(self, meeting_id: int) -> list[str]:
        return list(self._tdoc_ids_by_meeting.get(meeting_id, []))

    def list_distinct_meeting_ids(self) -> list[int]:
        return list(self._distinct_meeting_ids)


class _FakeTDocFileRepository:
    """In-memory TDocFileRepository double that records upsert calls."""

    def __init__(self) -> None:
        self.upsert_calls: list[list[TDocFile]] = []
        self.sync_calls: list[tuple[str, list[str]]] = []

    def upsert(self, file):  # pragma: no cover - not exercised
        self.upsert_many([file])

    def upsert_many(self, files: list[TDocFile]) -> int:
        self.upsert_calls.append(list(files))
        return len(files)

    def list(self, limit=20, **kwargs):  # pragma: no cover
        return []

    def delete_for_tdoc_ids(self, tdoc_ids):  # pragma: no cover
        return 0


DEFAULT_TEMPLATE = "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}"


def _make_coordinator(
    meeting_repo: _FakeMeetingRepository,
    tdoc_repo: _FakeTDocRepository,
    tdoc_file_repo: _FakeTDocFileRepository | None = None,
    *,
    tdoc_list_sync_interval=timedelta(minutes=30),
    tdoc_list_closed_window=timedelta(days=90),
    tdoc_list_url_template: str = DEFAULT_TEMPLATE,
) -> TDocSyncCoordinator:
    return TDocSyncCoordinator(
        meeting_repo,  # type: ignore[arg-type]
        tdoc_repo,  # type: ignore[arg-type]
        tdoc_file_repo or _FakeTDocFileRepository(),  # type: ignore[arg-type]
        tdoc_list_sync_interval=tdoc_list_sync_interval,
        tdoc_list_closed_window=tdoc_list_closed_window,
        tdoc_list_url_template=tdoc_list_url_template,
    )


# ---------------------------------------------------------------------------
# #11 coordinator accepts Protocol-typed repositories
# ---------------------------------------------------------------------------


def test_coordinator_accepts_protocol_typed_repos() -> None:
    coord = _make_coordinator(
        _FakeMeetingRepository(), _FakeTDocRepository(), _FakeTDocFileRepository()
    )
    assert coord is not None


# ---------------------------------------------------------------------------
# Happy path: TDoc sync only
# ---------------------------------------------------------------------------


def test_sync_for_meeting_id_dispatches_tdoc_list_sync(monkeypatch) -> None:
    meeting = Meeting(
        meeting_id=42,
        name="RAN5#111",
        title="RAN5 111",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url="tsg_ran/WG5_111/",
    )
    meeting_repo = _FakeMeetingRepository({42: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    captured: dict = {}

    def fake_sync(self, meeting_id, url_template):
        captured["meeting_id"] = meeting_id
        captured["url_template"] = url_template
        # Simulate that the TDoc sync wrote two rows.
        self._repository.upsert_many(
            [
                TDoc(tdoc_id="R5-260001", meeting_id=meeting_id),
                TDoc(tdoc_id="R5-260002", meeting_id=meeting_id),
            ]
        )
        return 2

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(TDocService, "sync_tdoc_list", fake_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    outcome = coord.sync_for_meeting_id(42)

    assert captured == {"meeting_id": 42, "url_template": DEFAULT_TEMPLATE}
    assert outcome.status == "synced"
    assert "2 TDoc row(s)" in outcome.reason
    # File sync still runs even with no matching files.
    assert "0 auxiliary TDoc file(s)" in outcome.reason


def test_sync_for_meeting_name_dispatches_tdoc_list_sync(monkeypatch) -> None:
    meeting = Meeting(
        meeting_id=99,
        name="SA2#150",
        title="SA2 150",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url="tsg_sa/WG2_150/",
    )
    meeting_repo = _FakeMeetingRepository({99: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    captured: dict = {}

    def fake_sync(self, meeting_id, url_template):
        captured["meeting_id"] = meeting_id
        captured["url_template"] = url_template
        return 3

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(TDocService, "sync_tdoc_list", fake_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    outcome = coord.sync_for_meeting_name("SA2#150")

    assert captured == {"meeting_id": 99, "url_template": DEFAULT_TEMPLATE}
    assert outcome.status == "synced"
    assert outcome.reason.startswith("TDoc sync complete:")
    assert "3 TDoc row(s)" in outcome.reason


# ---------------------------------------------------------------------------
# Happy path: TDoc sync chains into TDoc file sync
# ---------------------------------------------------------------------------


def test_sync_chains_into_tdoc_file_service(monkeypatch) -> None:
    meeting = Meeting(
        meeting_id=1,
        name="R5--TTCN Workshop#74",
        title="TTCN Workshop",
        location="Online",
        start_date=date(2026, 7, 2),
        end_date=date(2026, 7, 2),
        ftp_url="tsg_ran/WG5_Test_ex-T1/Workshop/TSGR5_Workshop_2026/docs/",
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    def fake_tdoc_sync(self, meeting_id, url_template):
        tdoc_repo.upsert_many(
            [
                TDoc(tdoc_id="R5w260200", meeting_id=meeting_id),
                TDoc(tdoc_id="R5w260201", meeting_id=meeting_id),
            ]
        )
        return 2

    captured: dict = {}

    def fake_file_sync(self, ftp_url, tdoc_ids):
        captured["ftp_url"] = ftp_url
        captured["tdoc_ids"] = list(tdoc_ids)
        return 4

    from doc3gpp.services.tdoc_service import TDocService
    from doc3gpp.services.tdoc_file_service import TDocFileService

    monkeypatch.setattr(TDocService, "sync_tdoc_list", fake_tdoc_sync)
    monkeypatch.setattr(TDocFileService, "sync_from_meeting_ftp", fake_file_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    outcome = coord.sync_for_meeting_id(1)

    assert captured["ftp_url"] == meeting.ftp_url
    # The file sync should see the TDoc IDs the TDoc sync just upserted.
    assert sorted(captured["tdoc_ids"]) == ["R5w260200", "R5w260201"]
    assert outcome.status == "synced"
    assert "2 TDoc row(s)" in outcome.reason
    assert "4 auxiliary TDoc file(s)" in outcome.reason


def test_file_sync_receives_no_tdoc_ids_when_tdoc_sync_writes_none(
    monkeypatch,
) -> None:
    meeting = Meeting(
        meeting_id=1,
        name="empty",
        title="empty",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
        ftp_url="tsg_ran/WG5/",
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.sync_tdoc_list",
        lambda self, meeting_id, url_template: 0,
    )

    captured: dict = {}

    def fake_file_sync(self, ftp_url, tdoc_ids):
        captured["tdoc_ids"] = list(tdoc_ids)
        return 0

    from doc3gpp.services.tdoc_file_service import TDocFileService

    monkeypatch.setattr(
        TDocFileService, "sync_from_meeting_ftp", fake_file_sync
    )

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    outcome = coord.sync_for_meeting_id(1)

    assert captured["tdoc_ids"] == []
    assert outcome.status == "synced"
    assert "0 TDoc row(s)" in outcome.reason
    assert "0 auxiliary TDoc file(s)" in outcome.reason


# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------


def test_skip_when_never_synced_runs(monkeypatch) -> None:
    meeting = Meeting(
        meeting_id=1,
        name="R5#1",
        title="R5 1",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url="tsg_ran/WG5_1/",
        tdoc_list_last_sync=None,
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.sync_tdoc_list",
        lambda self, meeting_id, url_template: 0,
    )

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    outcome = coord.sync_for_meeting_id(1)

    assert outcome.status == "synced"


def test_skip_when_closed_window() -> None:
    meeting = Meeting(
        meeting_id=1,
        name="R5#1",
        title="R5 1",
        location="Online",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 2),
        ftp_url="tsg_ran/WG5_1/",
        tdoc_list_last_sync=None,
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    coord = _make_coordinator(
        meeting_repo,
        tdoc_repo,
        tdoc_file_repo,
        tdoc_list_closed_window=timedelta(days=90),
    )
    outcome = coord.sync_for_meeting_id(1)

    assert outcome.status == "skipped"
    assert "closed window" in outcome.reason


def test_skip_when_within_auto_sync_interval() -> None:
    meeting = Meeting(
        meeting_id=1,
        name="R5#1",
        title="R5 1",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url="tsg_ran/WG5_1/",
        tdoc_list_last_sync=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    coord = _make_coordinator(
        meeting_repo,
        tdoc_repo,
        tdoc_file_repo,
        tdoc_list_sync_interval=timedelta(minutes=30),
    )
    outcome = coord.sync_for_meeting_id(1)

    assert outcome.status == "skipped"
    assert "last sync" in outcome.reason


def test_force_bypasses_all_skip_rules(monkeypatch) -> None:
    last_sync = datetime.now(timezone.utc) - timedelta(minutes=5)
    meeting = Meeting(
        meeting_id=1,
        name="R5#1",
        title="R5 1",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url="tsg_ran/WG5_1/",
        tdoc_list_last_sync=last_sync,
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(
        TDocService, "sync_tdoc_list", lambda self, meeting_id, url_template: 1
    )

    coord = _make_coordinator(
        meeting_repo,
        tdoc_repo,
        tdoc_file_repo,
        tdoc_list_sync_interval=timedelta(minutes=30),
    )
    outcome = coord.sync_for_meeting_id(1, force=True)

    assert outcome.status == "synced"


# ---------------------------------------------------------------------------
# #10 typed errors for CLI conversion
# ---------------------------------------------------------------------------


def test_sync_for_meeting_id_raises_when_meeting_missing() -> None:
    coord = _make_coordinator(
        _FakeMeetingRepository(), _FakeTDocRepository(), _FakeTDocFileRepository()
    )
    with pytest.raises(MeetingNotFoundError, match="id 999"):
        coord.sync_for_meeting_id(999)


def test_sync_for_meeting_name_raises_when_meeting_missing() -> None:
    coord = _make_coordinator(
        _FakeMeetingRepository(), _FakeTDocRepository(), _FakeTDocFileRepository()
    )
    with pytest.raises(MeetingNotFoundError, match="name nope"):
        coord.sync_for_meeting_name("nope")


def test_sync_raises_when_meeting_has_no_ftp_url() -> None:
    meeting = Meeting(
        meeting_id=10,
        name="R5-100",
        title="R5 100",
        location="Online",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 2),
        ftp_url=None,
    )
    coord = _make_coordinator(
        _FakeMeetingRepository({10: meeting}), _FakeTDocRepository(),
        _FakeTDocFileRepository(),
    )
    with pytest.raises(MeetingMissingFtpUrlError, match="10"):
        coord.sync_for_meeting_id(10)


# ---------------------------------------------------------------------------
# Bulk sync: sync_all_tracked_meetings
# ---------------------------------------------------------------------------


def test_sync_all_tracked_meetings_returns_empty_when_no_tdocs() -> None:
    coord = _make_coordinator(
        _FakeMeetingRepository(),
        _FakeTDocRepository(distinct_meeting_ids=[]),
        _FakeTDocFileRepository(),
    )
    outcome = coord.sync_all_tracked_meetings()

    assert outcome == BulkSyncOutcome()
    assert outcome.total == 0


def test_sync_all_tracked_meetings_iterates_each_id(monkeypatch) -> None:
    meetings = {
        1: Meeting(
            meeting_id=1,
            name="R5#1",
            title="R5 1",
            location="Online",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            ftp_url="tsg_ran/WG5_1/",
        ),
        2: Meeting(
            meeting_id=2,
            name="R5#2",
            title="R5 2",
            location="Online",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            ftp_url="tsg_ran/WG5_2/",
        ),
    }
    tdoc_repo = _FakeTDocRepository(distinct_meeting_ids=[1, 2])

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.sync_tdoc_list",
        lambda self, meeting_id, url_template: 1,
    )

    coord = _make_coordinator(
        _FakeMeetingRepository(meetings), tdoc_repo, _FakeTDocFileRepository()
    )
    outcome = coord.sync_all_tracked_meetings()

    assert outcome.total == 2
    assert outcome.synced_count == 2
    assert outcome.failed_count == 0


def test_sync_all_tracked_meetings_collects_missing_meeting_failure() -> None:
    tdoc_repo = _FakeTDocRepository(distinct_meeting_ids=[99])
    coord = _make_coordinator(
        _FakeMeetingRepository(), tdoc_repo, _FakeTDocFileRepository()
    )
    outcome = coord.sync_all_tracked_meetings()

    assert outcome.total == 1
    assert outcome.synced_count == 0
    assert outcome.failed_count == 1
    assert outcome.failures == (
        BulkSyncFailure(
            meeting_id=99,
            error="MeetingNotFoundError",
            reason="Meeting not found with id 99",
        ),
    )


def test_sync_all_tracked_meetings_collects_missing_ftp_url_failure() -> None:
    meeting = Meeting(
        meeting_id=5,
        name="R5#5",
        title="R5 5",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url=None,
    )
    tdoc_repo = _FakeTDocRepository(distinct_meeting_ids=[5])
    coord = _make_coordinator(
        _FakeMeetingRepository({5: meeting}),
        tdoc_repo,
        _FakeTDocFileRepository(),
    )
    outcome = coord.sync_all_tracked_meetings()

    assert outcome.total == 1
    assert outcome.failed_count == 1
    assert outcome.failures[0].error == "MeetingMissingFtpUrlError"
    assert "5" in outcome.failures[0].reason


def test_sync_all_tracked_meetings_force_flag_is_forwarded(monkeypatch) -> None:
    meeting = Meeting(
        meeting_id=1,
        name="R5#1",
        title="R5 1",
        location="Online",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        ftp_url="tsg_ran/WG5_1/",
    )
    tdoc_repo = _FakeTDocRepository(distinct_meeting_ids=[1])
    captured: list[bool] = []

    def fake_sync_for_meeting(self, meeting, force):
        captured.append(force)
        return SyncOutcome(status="synced", reason="ok")

    monkeypatch.setattr(
        TDocSyncCoordinator, "_sync_for_meeting", fake_sync_for_meeting
    )

    coord = _make_coordinator(
        _FakeMeetingRepository({1: meeting}), tdoc_repo, _FakeTDocFileRepository()
    )
    coord.sync_all_tracked_meetings(force=True)

    assert captured == [True]


def test_sync_all_tracked_meetings_counts_synced_and_skipped(monkeypatch) -> None:
    meetings = {
        1: Meeting(
            meeting_id=1,
            name="R5#1",
            title="R5 1",
            location="Online",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            ftp_url="tsg_ran/WG5_1/",
        ),
        2: Meeting(
            meeting_id=2,
            name="R5#2",
            title="R5 2",
            location="Online",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            ftp_url="tsg_ran/WG5_2/",
        ),
    }
    tdoc_repo = _FakeTDocRepository(distinct_meeting_ids=[1, 2])

    def fake_sync_for_meeting(self, meeting, force):
        if meeting.meeting_id == 1:
            return SyncOutcome(status="synced", reason="synced 1")
        return SyncOutcome(status="skipped", reason="skipped 2")

    monkeypatch.setattr(
        TDocSyncCoordinator, "_sync_for_meeting", fake_sync_for_meeting
    )

    coord = _make_coordinator(
        _FakeMeetingRepository(meetings), tdoc_repo, _FakeTDocFileRepository()
    )
    outcome = coord.sync_all_tracked_meetings()

    assert outcome.total == 2
    assert outcome.synced_count == 1
    assert outcome.skipped_count == 1
    assert outcome.failed_count == 0
