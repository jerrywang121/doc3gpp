"""Direct unit tests for :class:`TDocService.sync_from_meeting_ftp`.

Covers the test-gap entry: the service's FTP sync path was previously only
exercised through the integration tests. These tests stub the network call
and the repository so the orchestration in ``sync_from_meeting_ftp`` is
verified in isolation — including the ``meeting_id`` forwarding and the
``upsert_many`` single-transaction guarantee.
"""

from __future__ import annotations

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.tdoc_service import TDocService


class _FakeTDocRepository:
    """In-memory TDocRepository double that records upsert_many calls."""

    def __init__(self) -> None:
        self.upsert_calls: list[list[TDoc]] = []

    def upsert(self, tdoc: TDoc) -> None:  # pragma: no cover - not exercised
        self.upsert_many([tdoc])

    def upsert_many(self, tdocs: list[TDoc]) -> int:
        # Mirror SQLAlchemyTDocRepository: empty input is a no-op.
        if not tdocs:
            return 0
        self.upsert_calls.append(list(tdocs))
        return len(tdocs)

    def list(self, limit=20, **kwargs):  # pragma: no cover - not exercised
        return []


def test_sync_from_meeting_ftp_calls_upsert_many_once(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    fetched = [
        TDoc(tdoc_id="R5s260001", title="Doc 1"),
        TDoc(tdoc_id="R5s260002", title="Doc 2"),
    ]

    def fake_fetch(ftp_url, meeting_id=None):
        return fetched

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_meeting_ftp",
        fake_fetch,
    )

    count = service.sync_from_meeting_ftp(ftp_url="tsg_ran/WG5_111/", meeting_id=42)

    assert count == 2
    assert len(repo.upsert_calls) == 1
    assert [t.tdoc_id for t in repo.upsert_calls[0]] == ["R5s260001", "R5s260002"]


def test_sync_from_meeting_ftp_forwards_meeting_id(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    observed: dict = {}

    def fake_fetch(ftp_url, meeting_id=None):
        observed["ftp_url"] = ftp_url
        observed["meeting_id"] = meeting_id
        return [TDoc(tdoc_id="R5s260010", title="Doc")]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_meeting_ftp",
        fake_fetch,
    )

    service.sync_from_meeting_ftp(ftp_url="tsg_ran/WG5_111/", meeting_id=99)

    assert observed == {"ftp_url": "tsg_ran/WG5_111/", "meeting_id": 99}


def test_sync_from_meeting_ftp_meeting_id_defaults_to_none(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    observed: dict = {}

    def fake_fetch(ftp_url, meeting_id=None):
        observed["meeting_id"] = meeting_id
        return []

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_meeting_ftp",
        fake_fetch,
    )

    service.sync_from_meeting_ftp(ftp_url="tsg_ran/WG5_111/")

    assert observed["meeting_id"] is None


def test_sync_from_meeting_ftp_empty_result_is_noop(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_meeting_ftp",
        lambda **_kwargs: [],
    )

    count = service.sync_from_meeting_ftp(ftp_url="tsg_ran/WG5_111/", meeting_id=1)

    assert count == 0
    assert repo.upsert_calls == []


def test_sync_from_meeting_ftp_propagates_fetch_exception(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    def fake_fetch(ftp_url, meeting_id=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_meeting_ftp",
        fake_fetch,
    )

    with pytest.raises(RuntimeError, match="network down"):
        service.sync_from_meeting_ftp(ftp_url="tsg_ran/WG5_111/")

    assert repo.upsert_calls == []
