from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from doc3gpp.models.index import (
    IndexRebuildResult,
    IndexRequest,
    IndexStatus,
)
from doc3gpp.models.search import (
    RebuildProgress,
    SearchIndexStatus,
    SearchUnavailableError,
)
from doc3gpp.models.semantic_search import (
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.services.spec_doc_index_service import SpecDocIndexService
from doc3gpp.services.tdoc_index_service import TDocIndexService


@dataclass
class _RecordingFts5:
    events: list[str]
    rebuild_calls: list[dict[str, object]]
    component: str = "fts5"

    def status(self) -> SearchIndexStatus:
        self.events.append(f"status:{self.component}")
        return SearchIndexStatus(
            enabled=True,
            row_count=3,
            last_rebuild_at=None,
            last_indexed_uploaded_date=None,
            latest_tdocs_uploaded_date=None,
            is_stale=False,
        )

    def rebuild(
        self,
        *,
        batch_size: int,
        resume: bool,
        stale_only: bool,
        quiet: bool,
    ):
        self.rebuild_calls.append(
            {
                "batch_size": batch_size,
                "resume": resume,
                "stale_only": stale_only,
                "quiet": quiet,
            }
        )
        self.events.append(f"rebuild:{self.component}")
        yield RebuildProgress(1, 2, "first")
        yield RebuildProgress(2, 2, "second")


@dataclass
class _RecordingVector:
    events: list[str]
    rebuild_calls: list[dict[str, object]]
    component: str = "vector"

    def status(self) -> SearchIndexStatus:
        self.events.append(f"status:{self.component}")
        return SearchIndexStatus(
            enabled=True,
            row_count=5,
            last_rebuild_at=None,
            last_indexed_uploaded_date=None,
            latest_tdocs_uploaded_date=None,
            is_stale=False,
        )

    def rebuild_embeddings(
        self,
        *,
        batch_size: int,
        resume: bool,
        stale_only: bool,
        quiet: bool,
    ):
        self.rebuild_calls.append(
            {
                "batch_size": batch_size,
                "resume": resume,
                "stale_only": stale_only,
                "quiet": quiet,
            }
        )
        self.events.append(f"rebuild:{self.component}")
        yield RebuildProgress(1, 2, "first")
        yield RebuildProgress(2, 2, "second")


class _UnavailableVector:
    def rebuild_embeddings(self, **_kwargs):
        raise VectorIndexUnavailableError("vector missing")

        yield  # pragma: no cover


def _settings(batch_size: int = 17) -> SimpleNamespace:
    return SimpleNamespace(
        search=SimpleNamespace(rebuild_batch_size=batch_size),
    )


def _tdoc_service(
    events: list[str],
    *,
    fts5: _RecordingFts5 | None = None,
    vector: _RecordingVector | None = None,
) -> TDocIndexService:
    return TDocIndexService(
        fts5_service=fts5,
        semantic_service=vector,
        settings=_settings(),
    )


def _spec_doc_service(
    events: list[str],
    *,
    fts5: _RecordingFts5 | None = None,
    vector: _RecordingVector | None = None,
) -> SpecDocIndexService:
    return SpecDocIndexService(
        fts5_service=fts5,
        semantic_service=vector,
        settings=_settings(),
    )


def test_tdoc_no_action_returns_status_without_rebuild() -> None:
    events: list[str] = []
    fts5 = _RecordingFts5(events, [])
    vector = _RecordingVector(events, [])

    result = _tdoc_service(events, fts5=fts5, vector=vector).rebuild(
        IndexRequest()
    )

    assert isinstance(result, IndexStatus)
    assert result.fts5.available is True
    assert result.vector.available is True
    assert fts5.rebuild_calls == []
    assert vector.rebuild_calls == []


def test_tdoc_rebuilds_fts5_with_configured_defaults_and_progress_prefix() -> None:
    events: list[str] = []
    fts5 = _RecordingFts5(events, [])
    vector = _RecordingVector(events, [])
    progress: list[str] = []

    result = _tdoc_service(events, fts5=fts5, vector=vector).rebuild(
        IndexRequest(rebuild=True, resume=True, stale_only=True),
        quiet=True,
        on_progress=progress.append,
    )

    assert result == IndexRebuildResult(fts5_processed=2, vector_processed=0)
    assert fts5.rebuild_calls == [
        {
            "batch_size": 17,
            "resume": True,
            "stale_only": True,
            "quiet": True,
        }
    ]
    assert vector.rebuild_calls == []
    assert progress == [
        "tdoc fts5: 1/2 first",
        "tdoc fts5: 2/2 second",
    ]


def test_tdoc_rebuilds_vector_with_explicit_batch() -> None:
    events: list[str] = []
    fts5 = _RecordingFts5(events, [])
    vector = _RecordingVector(events, [])

    result = _tdoc_service(events, fts5=fts5, vector=vector).rebuild(
        IndexRequest(rebuild_embeddings=True, batch=4)
    )

    assert result == IndexRebuildResult(fts5_processed=0, vector_processed=2)
    assert fts5.rebuild_calls == []
    assert vector.rebuild_calls == [
        {
            "batch_size": 4,
            "resume": False,
            "stale_only": False,
            "quiet": False,
        }
    ]


def test_tdoc_rebuild_all_runs_fts5_before_vector() -> None:
    events: list[str] = []
    fts5 = _RecordingFts5(events, [])
    vector = _RecordingVector(events, [])

    result = _tdoc_service(events, fts5=fts5, vector=vector).rebuild(
        IndexRequest(rebuild_all=True)
    )

    assert result == IndexRebuildResult(fts5_processed=2, vector_processed=2)
    assert events == ["rebuild:fts5", "rebuild:vector"]


@pytest.mark.parametrize(
    "index_request",
    [
        IndexRequest(rebuild=True, rebuild_all=True),
        IndexRequest(rebuild_embeddings=True, rebuild_all=True),
    ],
)
def test_tdoc_rejects_contradictory_actions(index_request: IndexRequest) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _tdoc_service([], fts5=_RecordingFts5([], []), vector=_RecordingVector([], [])).rebuild(
            index_request
        )


def test_tdoc_status_marks_missing_optional_components_and_rebuild_raises() -> None:
    service = _tdoc_service([], fts5=None, vector=_RecordingVector([], []))

    status = service.status()

    assert status.fts5.available is False
    assert status.fts5.status is None
    assert status.vector.available is True
    with pytest.raises(SearchUnavailableError):
        service.rebuild(IndexRequest(rebuild=True))


def test_tdoc_vector_action_raises_when_vector_service_is_unavailable() -> None:
    service = _tdoc_service([], fts5=_RecordingFts5([], []), vector=None)

    with pytest.raises(SemanticSearchUnavailableError):
        service.rebuild(IndexRequest(rebuild_embeddings=True))


def test_tdoc_vector_generator_errors_are_mapped_to_semantic_unavailable() -> None:
    service = _tdoc_service(
        [],
        fts5=_RecordingFts5([], []),
        vector=_UnavailableVector(),  # type: ignore[arg-type]
    )

    with pytest.raises(SemanticSearchUnavailableError, match="vector missing"):
        service.rebuild(IndexRequest(rebuild_embeddings=True))


def test_spec_doc_rebuild_all_uses_spec_doc_progress_prefix() -> None:
    events: list[str] = []
    fts5 = _RecordingFts5(events, [])
    vector = _RecordingVector(events, [])
    progress: list[str] = []

    result = _spec_doc_service(events, fts5=fts5, vector=vector).rebuild(
        IndexRequest(rebuild_all=True),
        on_progress=progress.append,
    )

    assert result == IndexRebuildResult(fts5_processed=2, vector_processed=2)
    assert events == ["rebuild:fts5", "rebuild:vector"]
    assert progress == [
        "spec doc fts5: 1/2 first",
        "spec doc fts5: 2/2 second",
        "spec doc vector: 1/2 first",
        "spec doc vector: 2/2 second",
    ]


def test_spec_doc_status_explicitly_reports_unavailable_components() -> None:
    service = _spec_doc_service([], fts5=_RecordingFts5([], []), vector=None)

    status = service.status()

    assert status.fts5.available is True
    assert status.vector.available is False
    assert status.vector.status is None
    with pytest.raises(SemanticSearchUnavailableError):
        service.rebuild(IndexRequest(rebuild_embeddings=True))
