"""Unit tests for ``TDocFileService.sync_from_meeting_ftp``."""

from __future__ import annotations

import pytest

from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.services.tdoc_file_service import TDocFileService


class _FakeTDocFileRepository:
    """In-memory TDocFileRepository double that records upsert_many calls."""

    def __init__(self) -> None:
        self.upsert_calls: list[list[TDocFile]] = []

    def upsert_many(self, files: list[TDocFile]) -> int:
        if not files:
            return 0
        self.upsert_calls.append(list(files))
        return len(files)

    def list(self, limit=20, tdoc_id=None, file_type=None, file_type_in=None):  # pragma: no cover
        return []


def test_sync_forwards_files_to_upsert_many(monkeypatch) -> None:
    repo = _FakeTDocFileRepository()
    service = TDocFileService(repo)  # type: ignore[arg-type]

    fetched = [
        TDocFile(
            tdoc_id="R5s260001", type="revision", file="R5s260001r1.zip",
            ftp_url="x/r1.zip",
        ),
        TDocFile(
            tdoc_id="R5s260001", type="review", file="R5s260001_MCC160Comments.zip",
            ftp_url="x/review.zip",
        ),
    ]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_file_service.fetch_tdoc_files_from_meeting_ftp",
        lambda ftp_url, tdoc_ids: fetched,
    )

    count = service.sync_from_meeting_ftp(
        ftp_url="tsg_ran/WG5/", tdoc_ids=["R5s260001"]
    )

    assert count == 2
    assert len(repo.upsert_calls) == 1
    assert [f.file for f in repo.upsert_calls[0]] == [
        "R5s260001r1.zip",
        "R5s260001_MCC160Comments.zip",
    ]


def test_sync_with_empty_tdoc_ids_is_noop(monkeypatch) -> None:
    repo = _FakeTDocFileRepository()
    service = TDocFileService(repo)  # type: ignore[arg-type]

    called = {"value": False}

    def fake_fetch(ftp_url, tdoc_ids):
        called["value"] = True
        return []

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_file_service.fetch_tdoc_files_from_meeting_ftp",
        fake_fetch,
    )

    count = service.sync_from_meeting_ftp(ftp_url="tsg_ran/WG5/", tdoc_ids=[])

    assert count == 0
    assert called["value"] is False
    assert repo.upsert_calls == []


def test_sync_with_no_tdocs_found_is_noop(monkeypatch) -> None:
    repo = _FakeTDocFileRepository()
    service = TDocFileService(repo)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_file_service.fetch_tdoc_files_from_meeting_ftp",
        lambda ftp_url, tdoc_ids: [],
    )

    count = service.sync_from_meeting_ftp(
        ftp_url="tsg_ran/WG5/", tdoc_ids=["R5s260001"]
    )

    assert count == 0
    assert repo.upsert_calls == []


def test_sync_propagates_fetch_exception(monkeypatch) -> None:
    repo = _FakeTDocFileRepository()
    service = TDocFileService(repo)  # type: ignore[arg-type]

    def fake_fetch(ftp_url, tdoc_ids):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_file_service.fetch_tdoc_files_from_meeting_ftp",
        fake_fetch,
    )

    with pytest.raises(RuntimeError, match="network down"):
        service.sync_from_meeting_ftp(
            ftp_url="tsg_ran/WG5/", tdoc_ids=["R5s260001"]
        )
    assert repo.upsert_calls == []
