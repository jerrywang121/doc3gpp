"""Unit tests for :class:`TDocSyncCoordinator`.

Covers:
- #10: cross-service orchestration lives in the coordinator, not the CLI
- #11: coordinator depends on Protocol-typed repositories
- coordinator converts missing meetings / missing FTP URLs into typed errors
- the coordinator chains the TDoc and TDocFile syncs so that
  ``tdoc sync`` populates both tables in a single call
"""

from __future__ import annotations

from datetime import date

import pytest

from doc3gpp.models.meeting import Meeting
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


class _FakeTDocRepository:
    """In-memory TDocRepository double that records upsert calls."""

    def __init__(self) -> None:
        self.upsert_calls: list[tuple[str, int | None]] = []
        self._tdoc_ids_by_meeting: dict[int, list[str]] = {}

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


def _make_coordinator(
    meeting_repo: _FakeMeetingRepository,
    tdoc_repo: _FakeTDocRepository,
    tdoc_file_repo: _FakeTDocFileRepository | None = None,
) -> TDocSyncCoordinator:
    return TDocSyncCoordinator(
        meeting_repo,  # type: ignore[arg-type]
        tdoc_repo,  # type: ignore[arg-type]
        tdoc_file_repo or _FakeTDocFileRepository(),  # type: ignore[arg-type]
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


def test_sync_for_meeting_id_dispatches_ftp_sync(monkeypatch) -> None:
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

    def fake_sync(self, ftp_url, meeting_id=None):
        captured["ftp_url"] = ftp_url
        captured["meeting_id"] = meeting_id
        # Simulate that the TDoc sync wrote two rows.
        self._repository.upsert_many(
            [
                TDoc(tdoc_id="R5-260001", meeting_id=meeting_id),
                TDoc(tdoc_id="R5-260002", meeting_id=meeting_id),
            ]
        )
        return 2

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(TDocService, "sync_from_meeting_ftp", fake_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    summary = coord.sync_for_meeting_id(42)

    assert captured == {"ftp_url": "tsg_ran/WG5_111/", "meeting_id": 42}
    assert "2 TDoc row(s)" in summary
    # File sync still runs even with no matching files.
    assert "0 auxiliary TDoc file(s)" in summary


def test_sync_for_meeting_name_dispatches_ftp_sync(monkeypatch) -> None:
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

    def fake_sync(self, ftp_url, meeting_id=None):
        captured["ftp_url"] = ftp_url
        captured["meeting_id"] = meeting_id
        return 3

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(TDocService, "sync_from_meeting_ftp", fake_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    summary = coord.sync_for_meeting_name("SA2#150")

    assert captured == {"ftp_url": "tsg_sa/WG2_150/", "meeting_id": 99}
    assert summary.startswith("TDoc sync complete:")
    assert "3 TDoc row(s)" in summary


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

    def fake_tdoc_sync(self, ftp_url, meeting_id=None):
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

    monkeypatch.setattr(TDocService, "sync_from_meeting_ftp", fake_tdoc_sync)
    monkeypatch.setattr(TDocFileService, "sync_from_meeting_ftp", fake_file_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo, tdoc_file_repo)
    summary = coord.sync_for_meeting_id(1)

    assert captured["ftp_url"] == meeting.ftp_url
    # The file sync should see the TDoc IDs the TDoc sync just upserted.
    assert sorted(captured["tdoc_ids"]) == ["R5w260200", "R5w260201"]
    assert "2 TDoc row(s)" in summary
    assert "4 auxiliary TDoc file(s)" in summary


def test_file_sync_receives_no_tdoc_ids_when_tdoc_sync_writes_none(
    monkeypatch,
) -> None:
    meeting = Meeting(
        meeting_id=1,
        name="empty",
        title="empty",
        location="Online",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 2),
        ftp_url="tsg_ran/WG5/",
    )
    meeting_repo = _FakeMeetingRepository({1: meeting})
    tdoc_repo = _FakeTDocRepository()
    tdoc_file_repo = _FakeTDocFileRepository()

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.sync_from_meeting_ftp",
        lambda self, ftp_url, meeting_id=None: 0,
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
    summary = coord.sync_for_meeting_id(1)

    assert captured["tdoc_ids"] == []
    assert "0 TDoc row(s)" in summary
    assert "0 auxiliary TDoc file(s)" in summary


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
