"""Direct unit tests for :class:`TDocService.sync_tdoc_list`.

Covers the test-gap entry: the service's portal sync path is exercised
in isolation by stubbing the network call and the repository so the
orchestration in ``sync_tdoc_list`` is verified — including the
``meeting_id`` / ``url_template`` forwarding and the ``upsert_many``
single-transaction guarantee.
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


DEFAULT_TEMPLATE = "https://portal.3gpp.org/ngppapp/GenerateDocumentList.aspx?meetingId={meeting_id}"


def test_sync_tdoc_list_calls_upsert_many_once(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    fetched = [
        TDoc(tdoc_id="R5s260001", title="Doc 1"),
        TDoc(tdoc_id="R5s260002", title="Doc 2"),
    ]

    def fake_fetch(meeting_id, url_template):
        return fetched

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_portal",
        fake_fetch,
    )

    count = service.sync_tdoc_list(
        meeting_id=42, url_template=DEFAULT_TEMPLATE
    )

    assert count == 2
    assert len(repo.upsert_calls) == 1
    assert [t.tdoc_id for t in repo.upsert_calls[0]] == ["R5s260001", "R5s260002"]


def test_sync_tdoc_list_forwards_meeting_id_and_url_template(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    observed: dict = {}

    def fake_fetch(meeting_id, url_template):
        observed["meeting_id"] = meeting_id
        observed["url_template"] = url_template
        return [TDoc(tdoc_id="R5s260010", title="Doc")]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_portal",
        fake_fetch,
    )

    service.sync_tdoc_list(meeting_id=99, url_template="http://example/{meeting_id}")

    assert observed == {"meeting_id": 99, "url_template": "http://example/{meeting_id}"}


def test_sync_tdoc_list_empty_result_is_noop(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_portal",
        lambda **_kwargs: [],
    )

    count = service.sync_tdoc_list(meeting_id=1, url_template=DEFAULT_TEMPLATE)

    assert count == 0
    assert repo.upsert_calls == []


def test_sync_tdoc_list_propagates_fetch_exception(monkeypatch) -> None:
    repo = _FakeTDocRepository()
    service = TDocService(repo)  # type: ignore[arg-type]

    def fake_fetch(meeting_id, url_template):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.fetch_tdocs_from_portal",
        fake_fetch,
    )

    with pytest.raises(RuntimeError, match="network down"):
        service.sync_tdoc_list(meeting_id=1, url_template=DEFAULT_TEMPLATE)

    assert repo.upsert_calls == []
