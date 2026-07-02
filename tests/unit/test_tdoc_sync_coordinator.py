"""Unit tests for :class:`TDocSyncCoordinator`.

Covers:
- #10: cross-service orchestration lives in the coordinator, not the CLI
- #11: coordinator depends on Protocol-typed repositories
- coordinator converts missing meetings / missing FTP URLs into typed errors
"""

from __future__ import annotations

from datetime import date

import pytest

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
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

    def upsert(self, tdoc):  # pragma: no cover - delegated below
        self.upsert_many([tdoc])

    def upsert_many(self, tdocs: list[TDoc]) -> int:
        for tdoc in tdocs:
            self.upsert_calls.append((tdoc.tdoc_id, tdoc.meeting_id))
        return len(tdocs)

    def list(self, limit=20, **kwargs):  # pragma: no cover
        return []


def _make_coordinator(meeting_repo, tdoc_repo) -> TDocSyncCoordinator:
    """Construct the coordinator with Protocol-typed fake repos.

    The fakes structurally satisfy the Protocols without needing
    ``@runtime_checkable`` decoration; the static ``# type: ignore`` here
    documents that we deliberately pass the duck-typed fakes.
    """
    return TDocSyncCoordinator(meeting_repo, tdoc_repo)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# #11 coordinator accepts Protocol-typed repositories
# ---------------------------------------------------------------------------


def test_coordinator_accepts_protocol_typed_repos() -> None:
    meeting_repo = _FakeMeetingRepository()
    tdoc_repo = _FakeTDocRepository()
    coord = TDocSyncCoordinator(meeting_repo, tdoc_repo)  # type: ignore[arg-type]
    assert coord is not None


# ---------------------------------------------------------------------------
# Happy path
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

    captured: dict = {}

    def fake_sync(self, ftp_url, meeting_id=None):
        captured["ftp_url"] = ftp_url
        captured["meeting_id"] = meeting_id
        return 7

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(TDocService, "sync_from_meeting_ftp", fake_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo)
    count = coord.sync_for_meeting_id(42)

    assert count == 7
    assert captured == {"ftp_url": "tsg_ran/WG5_111/", "meeting_id": 42}


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

    captured: dict = {}

    def fake_sync(self, ftp_url, meeting_id=None):
        captured["ftp_url"] = ftp_url
        captured["meeting_id"] = meeting_id
        return 3

    from doc3gpp.services.tdoc_service import TDocService

    monkeypatch.setattr(TDocService, "sync_from_meeting_ftp", fake_sync)

    coord = _make_coordinator(meeting_repo, tdoc_repo)
    count = coord.sync_for_meeting_name("SA2#150")

    assert count == 3
    assert captured == {"ftp_url": "tsg_sa/WG2_150/", "meeting_id": 99}


# ---------------------------------------------------------------------------
# #10 typed errors for CLI conversion
# ---------------------------------------------------------------------------


def test_sync_for_meeting_id_raises_when_meeting_missing() -> None:
    coord = _make_coordinator(_FakeMeetingRepository(), _FakeTDocRepository())
    with pytest.raises(MeetingNotFoundError, match="id 999"):
        coord.sync_for_meeting_id(999)


def test_sync_for_meeting_name_raises_when_meeting_missing() -> None:
    coord = _make_coordinator(_FakeMeetingRepository(), _FakeTDocRepository())
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
        _FakeMeetingRepository({10: meeting}), _FakeTDocRepository()
    )
    with pytest.raises(MeetingMissingFtpUrlError, match="10"):
        coord.sync_for_meeting_id(10)