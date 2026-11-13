"""SQLite integration tests for LISTSERV vector search, hybrid fusion, and embedding indexing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.util import find_spec

import numpy as np
import pytest
from sqlalchemy import text

from doc3gpp.models.listserv import (
    ArchivePage,
    ListservAttachment,
    ListservDetail,
    ListservFilters,
    ListservMessage,
    ListservRecord,
    parse_date_set,
)
from doc3gpp.models.search import SearchUnavailableError
from doc3gpp.models.semantic_search import (
    EmbedderUnavailableError,
    SemanticSearchUnavailableError,
    VectorIndexUnavailableError,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.scraping.listserv_source import ListservSource
from doc3gpp.settings.schema import SemanticSearchSettings, Settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import ListservListORM
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.repositories.listserv_search_sql import (
    SQLAlchemyListservSearchRepository,
)
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository

DIM = 384


def _require_task8_modules():
    assert find_spec("doc3gpp.storage.repositories.listserv_vector_sql") is not None
    assert find_spec("doc3gpp.services.listserv_semantic") is not None
    from doc3gpp.services import factory
    from doc3gpp.services.listserv_search import ListservSearchService
    from doc3gpp.services.listserv_semantic import (
        ListservSemanticService,
        build_listserv_semantic_service,
    )
    from doc3gpp.storage.repositories.listserv_vector_sql import (
        SQLAlchemyListservVectorRepository,
    )

    assert hasattr(factory, "build_listserv_semantic_service")
    return (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        build_listserv_semantic_service,
        ListservSearchService,
        factory,
    )


class _DeterministicEmbedder:
    """Deterministic embedder mapping named token markers onto orthogonal axes."""

    dim = DIM
    model_name = "deterministic-listserv-embedder"

    def __init__(self, axis_tokens: dict[str, int] | None = None) -> None:
        self.inputs: list[str] = []
        self.axis_tokens = axis_tokens or {
            "beamforming": 0,
            "mimo": 1,
            "handover": 2,
            "sidelink": 3,
            "ntn": 4,
            "positioning": 5,
            "redcap": 6,
            "xr": 7,
            "chunk_far": 8,
            "chunk_near": 9,
            "crowded_noise": 10,
            "target_signal": 11,
        }

    def encode(self, texts: list[str]) -> np.ndarray:
        self.inputs.extend(texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for idx, raw in enumerate(texts):
            lowered = raw.lower()
            matched = False
            if "blend_near" in lowered:
                out[idx, 0] = 0.8
                out[idx, 1] = 0.6
                matched = True
            elif "blend_mid" in lowered:
                out[idx, 0] = 0.6
                out[idx, 1] = 0.8
                matched = True
            for token, col in self.axis_tokens.items():
                if token.lower() in lowered:
                    out[idx, col] += 1.0
                    matched = True
            if not matched:
                # Fallback deterministic non-zero unit vector on last axis
                out[idx, self.dim - 1] = 1.0
        return out


def _make_page(
    list_id: str = "3GPP_TSG_RAN_WG1",
    url: str = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=3GPP_TSG_RAN_WG1",
    period: str = "2026-10-W1",
) -> ArchivePage:
    return ArchivePage(list_id=list_id, url=url, period=period)


def _make_message(
    url: str = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=1",
    *,
    list_id: str = "3GPP_TSG_RAN_WG1",
    subject: str = "Topic R1-2610793",
    sender: str = "Alice <[log in to unmask]>",
    dt: datetime = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    date_precision: str = "datetime",
    week: str = "2026-10-W1",
    subject_tdocs: tuple[str, ...] = ("R1-2610793",),
) -> ListservMessage:
    return ListservMessage(
        url=url,
        list_id=list_id,
        subject=subject,
        sender=sender,
        date=dt,
        date_precision=date_precision,  # type: ignore[arg-type]
        week=week,
        subject_tdocs=subject_tdocs,
    )


@pytest.fixture()
def repository(sqlite_env) -> ListservRepository:
    create_schema("main")
    return SQLAlchemyListservRepository()


class _LazyIndexedEmbedder:
    @property
    def inputs(self) -> list[str]:
        _require_task8_modules()
        return []


@pytest.fixture()
def indexed_embedder(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> _DeterministicEmbedder | _LazyIndexedEmbedder:
    if (
        find_spec("doc3gpp.storage.repositories.listserv_vector_sql") is None
        or find_spec("doc3gpp.services.listserv_semantic") is None
    ):
        return _LazyIndexedEmbedder()
    (
        _VectorRepo,
        _SemanticService,
        build_listserv_semantic_service,
        _SearchService,
        _factory,
    ) = _require_task8_modules()

    def _forbid_network(*_args, **_kwargs):
        raise AssertionError("index_record must never fetch upstream network content")

    monkeypatch.setattr(ListservSource, "fetch", _forbid_network)

    page = _make_page()
    msg = _make_message(
        "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=3GPP_TSG_RAN_WG1&P=99",
        list_id="3GPP_TSG_RAN_WG1",
        subject="Discussion on [slides](https://portal.etsi.org/secret-subj-url/slide.pptx)",
        sender="AliceHarbor <https://example.com/sender-secret>",
        dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        week="2026-10-W1",
        subject_tdocs=(),
    )
    repository.commit_page(page, [msg], parse_date_set(["= 2026-10-01"]), ())
    att = ListservAttachment(
        name="contribution.docx",
        url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=3GPP_TSG_RAN_WG1&N=R1-2610793.zip&att_secret=1",
    )
    # Stored content is already capped at storage time; truncated tail "truncated_off_tail_token" is not stored
    stored_body = (
        "Stored capped body discussing beamforming and [spec link](https://3gpp.org/body-secret-url/doc.pdf) "
        "plus bare https://list.etsi.org/bare-secret-path."
    )
    repository.save_detail(
        ListservDetail(
            url=msg.url,
            attachments=(att,),
            tdocs=("R1-2610793",),
            content=stored_body,
            content_truncated=True,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    record = repository.get(msg.url)
    assert record is not None

    embedder = _DeterministicEmbedder()
    sem_service = build_listserv_semantic_service(embedder=embedder)
    assert sem_service is not None
    replaced = sem_service.index_record(record)
    assert replaced >= 1
    return embedder


def test_embedding_input_excludes_urls(indexed_embedder: _DeterministicEmbedder) -> None:
    text = " ".join(indexed_embedder.inputs)
    assert "https://" not in text and "R1-2610793" in text
    assert "contribution.docx" in text
    assert "http://" not in text
    assert "wa.exe" not in text
    assert "secret-subj-url" not in text
    assert "sender-secret" not in text
    assert "att_secret" not in text
    assert "body-secret-url" not in text
    assert "bare-secret-path" not in text
    assert "3GPP_TSG_RAN_WG1" in text
    assert "AliceHarbor" in text
    assert "2026-10-01" in text
    assert "2026-10-W1" in text
    assert "Stored capped body discussing beamforming" in text


def test_index_record_metadata_only_empty_body_and_transactional_replace(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    def _forbid_network(*_args, **_kwargs):
        raise AssertionError("index_record must never read upstream source")

    monkeypatch.setattr(ListservSource, "fetch", _forbid_network)

    page = _make_page()
    m_meta = _make_message(
        "https://list.etsi.org/msg/meta-only",
        subject="Metadata only beamforming R1-2610793",
        subject_tdocs=("R1-2610793",),
        dt=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
    )
    m_empty = _make_message(
        "https://list.etsi.org/msg/empty-body",
        subject="Empty body sidelink",
        subject_tdocs=(),
        dt=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
    )
    repository.commit_page(page, [m_meta, m_empty], parse_date_set(["= 2026-10-01"]), ())
    repository.save_detail(
        ListservDetail(
            url=m_empty.url,
            attachments=(),
            tdocs=(),
            content="",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=6,
            chunk_overlap=0,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        source_repository=repository,
        settings=settings,
    )
    search_service = ListservSearchService(
        repository=repository,
        semantic_service=sem_service,
        settings=settings,
    )

    rec_meta = repository.get(m_meta.url)
    rec_empty = repository.get(m_empty.url)
    assert rec_meta is not None and rec_empty is not None

    count_meta_1 = sem_service.index_record(rec_meta)
    count_empty = sem_service.index_record(rec_empty)
    assert count_meta_1 >= 2  # small chunk_size=6 produces multiple chunks
    assert count_empty >= 1

    # Both metadata-only and empty-body parsed records are discoverable via semantic search
    hits_meta = search_service.search(semantic="beamforming")
    assert hits_meta[0].record.message.url == m_meta.url
    assert hits_meta[0].record.detail is None
    assert hits_meta[0].content_preview is None
    assert hits_meta[0].preview_truncated is None
    assert hits_meta[0].matched_source == "content"
    assert hits_meta[0].snippet is not None

    hits_empty = search_service.search(semantic="sidelink")
    assert hits_empty[0].record.message.url == m_empty.url
    assert hits_empty[0].record.detail is not None
    assert hits_empty[0].content_preview == ""
    assert hits_empty[0].preview_truncated is False

    # Re-indexing with larger chunk_size replaces old chunks (no orphan tail chunks left behind)
    settings_single = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=100,
            chunk_overlap=0,
        )
    )
    vec_repo_single = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings_single.semantic_search,
    )
    sem_service_single = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo_single,
        source_repository=repository,
        settings=settings_single,
    )
    replaced_single = sem_service_single.index_record(rec_meta)
    assert replaced_single == 1
    with get_engine().begin() as conn:
        vec_rows = conn.execute(
            text("SELECT count(*) FROM vec_listserv_embeddings WHERE url = :u"),
            {"u": m_meta.url},
        ).scalar()
        txt_rows = conn.execute(
            text("SELECT count(*) FROM vec_listserv_chunk_text WHERE url = :u"),
            {"u": m_meta.url},
        ).scalar()
    assert vec_rows == 1
    assert txt_rows == 1

    # Transactional rollback if chunk_text insert fails: old chunks survive intact
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "CREATE TRIGGER fail_listserv_chunk_text BEFORE INSERT ON vec_listserv_chunk_text "
                "BEGIN SELECT RAISE(ABORT, 'chunk text failure'); END"
            )
        )
    try:
        with pytest.raises(Exception, match="chunk text failure"):
            sem_service.index_record(rec_meta)
    finally:
        with get_engine().begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS fail_listserv_chunk_text"))

    with get_engine().begin() as conn:
        vec_after = conn.execute(
            text("SELECT count(*) FROM vec_listserv_embeddings WHERE url = :u"),
            {"u": m_meta.url},
        ).scalar()
        txt_after = conn.execute(
            text("SELECT count(*) FROM vec_listserv_chunk_text WHERE url = :u"),
            {"u": m_meta.url},
        ).scalar()
    assert vec_after == 1
    assert txt_after == 1


def test_semantic_only_distance_order_multi_chunk_dedup_and_same_snapshot_chunk_text(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    page = _make_page()
    m_multi = _make_message(
        "https://list.etsi.org/msg/multi",
        subject="Multi chunk subject",
        sender="Alice",
        dt=datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m_mid = _make_message(
        "https://list.etsi.org/msg/mid",
        subject="Mid distance subject",
        sender="Bob",
        dt=datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m_far = _make_message(
        "https://list.etsi.org/msg/far",
        subject="Far distance subject",
        sender="Carol",
        dt=datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    repository.commit_page(page, [m_multi, m_mid, m_far], parse_date_set(["= 2026-10-01"]), ())

    # With chunk_size=8, overlap=0:
    # m_multi has chunk 0 (far) and chunk 1 (exact match on beamforming: distance 0.0)
    repository.save_detail(
        ListservDetail(
            url=m_multi.url,
            attachments=(),
            tdocs=(),
            content="chunk_far filler one two three four five six chunk_near beamforming exact match",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_mid.url,
            attachments=(),
            tdocs=(),
            content="blend_near moderate match for query",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_far.url,
            attachments=(),
            tdocs=(),
            content="blend_mid weaker match for query",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=8,
            chunk_overlap=0,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        source_repository=repository,
        settings=settings,
    )
    for url in (m_multi.url, m_mid.url, m_far.url):
        rec = repository.get(url)
        assert rec is not None
        sem_service.index_record(rec)

    search_service = ListservSearchService(
        repository=repository,
        semantic_service=sem_service,
        settings=settings,
    )

    hits = search_service.search(semantic="beamforming")
    # 3 emails, deduplicated even though m_multi has multiple chunks
    assert [h.record.message.url for h in hits] == [m_multi.url, m_mid.url, m_far.url]
    # Positive cosine distances sorted ascending (lower is better)
    scores = [h.score for h in hits]
    assert all(s is not None and s >= 0.0 for s in scores)
    assert scores[0] == pytest.approx(0.0, abs=1e-6)
    assert scores[1] == pytest.approx(0.2, abs=1e-5)  # 1 - 0.8
    assert scores[2] == pytest.approx(0.4, abs=1e-5)  # 1 - 0.6
    assert scores[0] < scores[1] < scores[2]

    # Best chunk of m_multi is the chunk containing "beamforming exact match" (not "chunk_far")
    assert hits[0].matched_source == "content"
    assert hits[0].snippet is not None
    assert "beamforming exact match" in hits[0].snippet
    assert "chunk_far" not in hits[0].snippet

    # Nonzero offset pagination after multi-chunk deduplication
    page_1 = search_service.search(
        semantic="beamforming",
        filters=ListservFilters(limit=1, offset=1),
    )
    assert [h.record.message.url for h in page_1] == [m_mid.url]

    # Same-snapshot verification: if another writer mutates chunk text after knn returns,
    # the result retains the chunk text captured in the knn read snapshot.
    original_knn = vec_repo.knn
    mutated = False

    def _knn_then_mutate(*args, **kwargs):
        nonlocal mutated
        rows = original_knn(*args, **kwargs)
        if not mutated:
            mutated = True
            with get_engine().begin() as writer:
                writer.execute(
                    text("UPDATE vec_listserv_chunk_text SET text = 'mutated_after_knn_snapshot' WHERE url = :u"),
                    {"u": m_multi.url},
                )
        return rows

    monkeypatch.setattr(vec_repo, "knn", _knn_then_mutate)
    snapshot_hits = search_service.search(semantic="beamforming")
    assert snapshot_hits[0].snippet is not None
    assert "beamforming exact match" in snapshot_hits[0].snippet
    assert "mutated_after_knn_snapshot" not in snapshot_hits[0].snippet


def test_hybrid_rrf_formula_provenance_tiebreak_and_dedup_pagination(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    page = _make_page()
    # Four emails:
    # m_both: FTS rank 0 (has "sharedterm sharedterm"), Vec rank 1 (blend_near -> dist 0.2)
    # m_vec_top: FTS rank 1 (has "sharedterm"), Vec rank 0 (beamforming -> dist 0.0)
    # m_fts_only: FTS rank 2 (has "sharedterm"), not in top vec for beamforming? Wait: all indexed emails are in vec unless absent from vec index.
    # Let's leave m_fts_only unindexed in vector store so it only appears on FTS side,
    # and m_vec_only without "sharedterm" so it only appears on vector side!
    m_both = _make_message(
        "https://list.etsi.org/msg/both",
        subject="Both match",
        dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m_vec_top = _make_message(
        "https://list.etsi.org/msg/vec-top",
        subject="Vector top match",
        dt=datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m_fts_only = _make_message(
        "https://list.etsi.org/msg/fts-only",
        subject="FTS only match sharedterm",
        dt=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m_vec_only = _make_message(
        "https://list.etsi.org/msg/vec-only",
        subject="Vector only match",
        dt=datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    repository.commit_page(
        page,
        [m_both, m_vec_top, m_fts_only, m_vec_only],
        parse_date_set(["= 2026-10-01"]),
        (),
    )
    repository.save_detail(
        ListservDetail(
            url=m_both.url,
            attachments=(),
            tdocs=(),
            content="sharedterm sharedterm sharedterm blend_near body",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_vec_top.url,
            attachments=(),
            tdocs=(),
            content="sharedterm beamforming exact vector hit",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_fts_only.url,
            attachments=(),
            tdocs=(),
            content="sharedterm in fts only email",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_vec_only.url,
            attachments=(),
            tdocs=(),
            content="blend_mid vector only chunk text",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()

    # Use rrf_k=60, fts5_weight=0.25 -> vector_weight W = 0.75
    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            rrf_k=60,
            fts5_weight=0.25,
            chunk_size=64,
            chunk_overlap=0,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        fts_repository=fts_repo,
        source_repository=repository,
        settings=settings,
    )
    # Index m_vec_top, m_both, m_vec_only in vector repo (m_fts_only omitted from vector repo)
    for url in (m_vec_top.url, m_both.url, m_vec_only.url):
        rec = repository.get(url)
        assert rec is not None
        sem_service.index_record(rec)

    search_service = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=sem_service,
        settings=settings,
    )

    # Check individual ranks:
    # FTS order for "sharedterm":
    fts_only_hits = search_service.search(text="sharedterm")
    fts_urls = [h.record.message.url for h in fts_only_hits]
    rank_fts = {u: idx for idx, u in enumerate(fts_urls)}
    assert set(fts_urls) == {m_both.url, m_vec_top.url, m_fts_only.url}

    # Vector order for "beamforming":
    # m_vec_top (dist 0.0, rank 0), m_both (dist 0.2, rank 1), m_vec_only (dist 0.4, rank 2)
    vec_only_hits = search_service.search(semantic="beamforming")
    vec_urls = [h.record.message.url for h in vec_only_hits]
    assert vec_urls == [m_vec_top.url, m_both.url, m_vec_only.url]
    rank_vec = {u: idx for idx, u in enumerate(vec_urls)}

    hybrid_hits = search_service.search(text="sharedterm", semantic="beamforming")
    assert len(hybrid_hits) == 4

    w = 1.0 - 0.25  # 0.75
    k = 60
    expected_scores: dict[str, float] = {}
    for u in (m_both.url, m_vec_top.url, m_fts_only.url, m_vec_only.url):
        term_fts = (1.0 / (k + rank_fts[u])) * (1.0 - w) if u in rank_fts else 0.0
        term_vec = (1.0 / (k + rank_vec[u])) * w if u in rank_vec else 0.0
        expected_scores[u] = term_fts + term_vec

    for hit in hybrid_hits:
        u = hit.record.message.url
        assert hit.score is not None and hit.score > 0.0
        assert hit.score == pytest.approx(expected_scores[u], rel=1e-6)

    # Scores are sorted descending (higher RRF score is better)
    hybrid_scores = [h.score for h in hybrid_hits]
    assert hybrid_scores == sorted(hybrid_scores, reverse=True)

    # Provenance:
    # Hits present in FTS retain FTS matched_source and FTS <<...>> snippet
    by_url = {h.record.message.url: h for h in hybrid_hits}
    assert by_url[m_both.url].matched_source == "content"
    assert by_url[m_both.url].snippet is not None and "<<sharedterm>>" in by_url[m_both.url].snippet
    assert by_url[m_fts_only.url].matched_source == "subject"
    assert by_url[m_fts_only.url].snippet is not None and "<<sharedterm>>" in by_url[m_fts_only.url].snippet

    # Vector-only hit in hybrid uses best vector chunk text and matched_source="content"
    assert by_url[m_vec_only.url].matched_source == "content"
    assert by_url[m_vec_only.url].snippet == (
        "3GPP_TSG_RAN_WG1 Vector only match Alice <[log in to unmask]> "
        "2026-10-01 2026-10-W1 blend_mid vector only chunk text"
    )

    # Pagination applies once after fusion
    p0 = search_service.search(
        text="sharedterm",
        semantic="beamforming",
        filters=ListservFilters(limit=2, offset=0),
    )
    p1 = search_service.search(
        text="sharedterm",
        semantic="beamforming",
        filters=ListservFilters(limit=2, offset=2),
    )
    assert [h.record.message.url for h in (*p0, *p1)] == [
        h.record.message.url for h in hybrid_hits
    ]


    # Hybrid with multiple vector chunks per email assigns rank_vec by distinct email
    # (sibling chunks of m_vec_top do not push m_both or m_vec_only down in rank_vec)
    m_vec_top_rec = repository.get(m_vec_top.url)
    assert m_vec_top_rec is not None
    small_chunk_settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            rrf_k=60,
            fts5_weight=0.25,
            chunk_size=5,
            chunk_overlap=0,
        )
    )
    vec_repo_multi = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=small_chunk_settings.semantic_search,
    )
    sem_multi = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo_multi,
        fts_repository=fts_repo,
        source_repository=repository,
        settings=small_chunk_settings,
    )
    assert sem_multi.index_record(m_vec_top_rec) >= 2
    for u in (m_both.url, m_vec_only.url):
        rec = repository.get(u)
        assert rec is not None
        sem_multi.index_record(rec)
    search_multi = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=sem_multi,
        settings=small_chunk_settings,
    )
    hybrid_multi = search_multi.search(text="sharedterm", semantic="beamforming")
    for hit in hybrid_multi:
        u = hit.record.message.url
        assert hit.score == pytest.approx(expected_scores[u], rel=1e-6)
    # Nonzero offset pagination after multi-chunk deduplication in hybrid
    hyb_offset_1 = search_multi.search(
        text="sharedterm",
        semantic="beamforming",
        filters=ListservFilters(limit=1, offset=1),
    )
    assert [h.record.message.url for h in hyb_offset_1] == [hybrid_multi[1].record.message.url]



def test_hybrid_fts_candidates_cover_deep_offsets(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    page = _make_page()
    messages = [
        _make_message(
            f"https://list.etsi.org/msg/fts-only-{idx:03d}",
            subject=f"FTS-only paginationmarker record {idx:03d}",
            dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            subject_tdocs=(),
        )
        for idx in range(205)
    ]
    repository.commit_page(page, messages, parse_date_set(["= 2026-10-01"]), ())

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=64,
            chunk_overlap=0,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    semantic_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        fts_repository=fts_repo,
        source_repository=repository,
        settings=settings,
    )
    search_service = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=semantic_service,
        settings=settings,
    )

    filters = ListservFilters(limit=7, offset=170)
    expected = fts_repo.search("paginationmarker", filters)
    actual = search_service.search(
        text="paginationmarker", semantic="beamforming", filters=filters
    )

    assert len(expected) == 7
    assert [hit.record.message.url for hit in actual] == [
        hit.record.message.url for hit in expected
    ]


def test_equal_score_tie_breaking_in_semantic_and_hybrid(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    page = _make_page()
    tz_minus_5 = timezone(timedelta(hours=-5))
    tz_plus_2 = timezone(timedelta(hours=2))

    # Instant 1 (newest): 2026-10-01T18:00:00-05:00 == 23:00Z
    m1 = _make_message(
        "https://list.etsi.org/msg/tie-1",
        subject="Tie subject",
        dt=datetime(2026, 10, 1, 18, 0, 0, tzinfo=tz_minus_5),
        subject_tdocs=(),
    )
    # Instant 2: 2026-10-01T22:00:00+02:00 == 20:00Z (same timestamp for URL tie-break a vs b)
    m2_b = _make_message(
        "https://list.etsi.org/msg/tie-2-b",
        subject="Tie subject",
        dt=datetime(2026, 10, 1, 22, 0, 0, tzinfo=tz_plus_2),
        subject_tdocs=(),
    )
    m2_a = _make_message(
        "https://list.etsi.org/msg/tie-2-a",
        subject="Tie subject",
        dt=datetime(2026, 10, 1, 22, 0, 0, tzinfo=tz_plus_2),
        subject_tdocs=(),
    )
    repository.commit_page(page, [m2_b, m1, m2_a], parse_date_set(["= 2026-10-01"]), ())
    for m in (m2_b, m1, m2_a):
        repository.save_detail(
            ListservDetail(
                url=m.url,
                attachments=(),
                tdocs=(),
                content="beamforming identical content",
                content_truncated=False,
                parser_version="1.0.0",
                parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
            )
        )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            fts5_weight=0.5,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        fts_repository=fts_repo,
        source_repository=repository,
        settings=settings,
    )
    for m in (m2_b, m1, m2_a):
        rec = repository.get(m.url)
        assert rec is not None
        sem_service.index_record(rec)

    search_service = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=sem_service,
        settings=settings,
    )

    expected_order = [m1.url, m2_a.url, m2_b.url]
    sem_hits = search_service.search(semantic="beamforming")
    assert [h.record.message.url for h in sem_hits] == expected_order

    # For hybrid equal-score tie-break: give m2_a (FTS rank 0, Vec rank 1) and m2_b (FTS rank 1, Vec rank 0)
    # with fts5_weight = 0.5 so their RRF scores are identical!
    m_sym_early = _make_message(
        "https://list.etsi.org/msg/sym-early",
        subject="Sym",
        dt=datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    m_sym_late = _make_message(
        "https://list.etsi.org/msg/sym-late",
        subject="Sym",
        dt=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    repository.commit_page(page, [m_sym_early, m_sym_late], parse_date_set(["= 2026-10-02"]), ())
    # m_sym_early has more FTS term frequency (rank 0 in FTS, rank 1 in Vec)
    # m_sym_late has closer Vec distance (rank 1 in FTS, rank 0 in Vec)
    repository.save_detail(
        ListservDetail(
            url=m_sym_early.url,
            attachments=(),
            tdocs=(),
            content="symword symword symword blend_near",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_sym_late.url,
            attachments=(),
            tdocs=(),
            content="symword beamforming",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc),
        )
    )
    fts_repo.upsert_many([m_sym_early.url, m_sym_late.url])
    for u in (m_sym_early.url, m_sym_late.url):
        rec = repository.get(u)
        assert rec is not None
        sem_service.index_record(rec)

    sym_hits = search_service.search(
        text="symword",
        semantic="beamforming",
        filters=ListservFilters(dates=parse_date_set(["= 2026-10-02"])),
    )
    assert len(sym_hits) == 2
    assert sym_hits[0].score == pytest.approx(sym_hits[1].score)
    # Equal RRF score breaks ties by newest timestamp first
    assert [h.record.message.url for h in sym_hits] == [m_sym_late.url, m_sym_early.url]


def test_shared_filters_applied_before_paging_and_knn_widens_for_chunk_crowding(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    page_ran = _make_page("3GPP_TSG_RAN_WG1", "https://list.etsi.org/p-ran", "2026-10-W1")
    page_sa = _make_page("3GPP_TSG_SA_WG2", "https://list.etsi.org/p-sa", "2026-10-W1")

    # m_crowded has 12 chunks matching "target_signal" very closely, but is on 3GPP_TSG_RAN_WG1 / 2026-10-03
    m_crowded = _make_message(
        "https://list.etsi.org/msg/crowded",
        list_id="3GPP_TSG_RAN_WG1",
        subject="Crowded noise message R1-111111",
        sender="NoiseSender",
        dt=datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=("R1-111111",),
    )
    # m_match_1 and m_match_2 have weaker vector similarity (blend_near / blend_mid on axis 0,
    # so we query "beamforming") while m_crowded has 12 chunks of "beamforming"
    m_match_1 = _make_message(
        "https://list.etsi.org/msg/match-1",
        list_id="3GPP_TSG_SA_WG2",
        subject="Target SA2 topic R2-222222",
        sender="TargetAlice",
        dt=datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=("R2-222222",),
    )
    m_match_2 = _make_message(
        "https://list.etsi.org/msg/match-2",
        list_id="3GPP_TSG_SA_WG2",
        subject="Target SA2 second",
        sender="TargetAlice",
        dt=datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    repository.commit_page(page_ran, [m_crowded], parse_date_set(["= 2026-10-03"]), ())
    repository.commit_page(
        page_sa,
        [m_match_1, m_match_2],
        parse_date_set([">= 2026-10-01", "<= 2026-10-05"]),
        (),
    )

    # Give m_crowded 12 chunks of "beamforming" (chunk_size=4, overlap=0 -> 48 words)
    crowded_body = " ".join(f"beamforming word{i}a word{i}b word{i}c" for i in range(12))
    repository.save_detail(
        ListservDetail(
            url=m_crowded.url,
            attachments=(),
            tdocs=(),
            content=crowded_body,
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_match_1.url,
            attachments=(),
            tdocs=(),
            content="blend_near sharedalpha",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=m_match_2.url,
            attachments=(),
            tdocs=("R5s260009",),
            content="blend_mid sharedalpha",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc),
        )
    )

    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()

    # Set fanout_multiplier=2 so initial k for limit=1 is small (e.g. 2 or 4),
    # forcing KNN widening past m_crowded's 12 chunks!
    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=4,
            chunk_overlap=0,
            fanout_multiplier=2,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        fts_repository=fts_repo,
        source_repository=repository,
        settings=settings,
    )
    for u in (m_crowded.url, m_match_1.url, m_match_2.url):
        rec = repository.get(u)
        assert rec is not None
        sem_service.index_record(rec)

    search_service = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=sem_service,
        settings=settings,
    )

    # Unfiltered limit=2 must widen KNN past m_crowded's 12 chunks to find m_match_1 as second email
    unfiltered_top2 = search_service.search(
        semantic="beamforming",
        filters=ListservFilters(limit=2, offset=0),
    )
    assert [h.record.message.url for h in unfiltered_top2] == [m_crowded.url, m_match_1.url]

    # Filtered by list_id, sender, subject, tdocs, and date hole before paging in both semantic and hybrid
    hole_dates = parse_date_set([">= 2026-10-01", "!= 2026-10-03", "<= 2026-10-05"])
    combined_filters = ListservFilters(
        list_id="3gpp_tsg_sa%",
        sender="Target%",
        subject="!%Crowded%",
        tdocs="R5s260009",
        dates=hole_dates,
        limit=1,
        offset=0,
    )
    sem_filtered = search_service.search(semantic="beamforming", filters=combined_filters)
    assert [h.record.message.url for h in sem_filtered] == [m_match_2.url]

    hyb_filtered = search_service.search(
        text="sharedalpha",
        semantic="beamforming",
        filters=combined_filters,
    )
    assert [h.record.message.url for h in hyb_filtered] == [m_match_2.url]


def test_mode_dispatch_whitespace_normalization_and_availability_errors(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        build_listserv_semantic_service,
        ListservSearchService,
        factory,
    ) = _require_task8_modules()

    page = _make_page()
    m1 = _make_message(
        "https://list.etsi.org/msg/mode-1",
        subject="Mode dispatch beamforming",
        dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
    )
    repository.commit_page(page, [m1], parse_date_set(["= 2026-10-01"]), ())
    repository.save_detail(
        ListservDetail(
            url=m1.url,
            attachments=(),
            tdocs=(),
            content="Mode dispatch beamforming content",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    embedder = _DeterministicEmbedder()
    fts_repo = SQLAlchemyListservSearchRepository()
    fts_repo.rebuild()
    sem_service = build_listserv_semantic_service(embedder=embedder)
    assert sem_service is not None
    rec = repository.get(m1.url)
    assert rec is not None
    sem_service.index_record(rec)

    service = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=sem_service,
    )

    # Whitespace-only text and semantic normalize to absent (filter-only mode)
    filter_hits = service.search(text="   ", semantic=" \t\n ")
    assert [h.record.message.url for h in filter_hits] == [m1.url]
    assert filter_hits[0].score is None
    assert filter_hits[0].matched_source is None

    # Whitespace-only text + non-empty semantic -> semantic-only mode (even when FTS is None!)
    sem_only_no_fts = ListservSearchService(
        repository=repository,
        fts_repository=None,
        semantic_service=sem_service,
    )
    sem_hits = sem_only_no_fts.search(text="   ", semantic="  beamforming  ")
    assert [h.record.message.url for h in sem_hits] == [m1.url]
    assert sem_hits[0].score is not None and sem_hits[0].score >= 0.0
    assert sem_hits[0].matched_source == "content"

    # Drop listserv_search table to prove semantic-only works when FTS table is absent
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS listserv_search"))
    sem_hits_dropped_fts = sem_only_no_fts.search(semantic="beamforming")
    assert [h.record.message.url for h in sem_hits_dropped_fts] == [m1.url]

    # Hybrid requires BOTH FTS and semantic and never falls back
    with pytest.raises(SearchUnavailableError):
        sem_only_no_fts.search(text="beamforming", semantic="beamforming")

    create_schema("main")
    fts_repo.rebuild()
    fts_only_no_sem = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=None,
    )
    # Whitespace-only semantic + non-empty text -> FTS-only mode (works without semantic_service)
    fts_hits = fts_only_no_sem.search(text="beamforming", semantic="   ")
    assert [h.record.message.url for h in fts_hits] == [m1.url]
    assert fts_hits[0].score is not None and fts_hits[0].score <= 0.0  # BM25 score unchanged

    # Hybrid without semantic_service raises SemanticSearchUnavailableError
    with pytest.raises(SemanticSearchUnavailableError):
        fts_only_no_sem.search(text="beamforming", semantic="beamforming")

    # Filter-only requires neither FTS nor semantic
    filter_only_service = ListservSearchService(
        repository=repository,
        fts_repository=None,
        semantic_service=None,
    )
    assert [h.record.message.url for h in filter_only_service.search()] == [m1.url]

    # build_listserv_semantic_service returns None when semantic_search.enabled=False or url unset without embedder
    disabled_sem = Settings(
        semantic_search=SemanticSearchSettings(enabled=False),
    )
    assert build_listserv_semantic_service(disabled_sem, embedder=embedder) is None
    unconfigured_sem = Settings(
        semantic_search=SemanticSearchSettings(enabled=True, embedding_base_url=None),
    )
    assert build_listserv_semantic_service(unconfigured_sem) is None

    # build_listserv_search_service wires lazy semantic service so unconfigured semantic raises SemanticSearchUnavailableError
    built_search = factory.build_listserv_search_service(settings=unconfigured_sem)
    assert [h.record.message.url for h in built_search.search()] == [m1.url]
    with pytest.raises(SemanticSearchUnavailableError):
        built_search.search(semantic="beamforming")

    # Vector dim mismatch raises actionable VectorIndexUnavailableError
    with pytest.raises(VectorIndexUnavailableError):
        bad_vec_repo = SQLAlchemyListservVectorRepository(expected_dim=128)
        bad_vec_repo.verify_compatible(128, "other-model")

    # Query / upsert with wrong vector dim raises VectorIndexUnavailableError
    good_vec_repo = SQLAlchemyListservVectorRepository(expected_dim=DIM)
    with pytest.raises(VectorIndexUnavailableError):
        good_vec_repo.knn(np.zeros(128, dtype=np.float32), limit=5)

    # Model mismatch raises VectorIndexUnavailableError
    with pytest.raises(VectorIndexUnavailableError):
        good_vec_repo.verify_compatible(DIM, "wrong-model-name")

    # Dropping vector tables causes SQLAlchemyListservVectorRepository to raise VectorIndexUnavailableError
    with get_engine().begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS vec_listserv_embeddings"))
    with pytest.raises(VectorIndexUnavailableError):
        SQLAlchemyListservVectorRepository()
    # And build_listserv_semantic_service returns None gracefully when vector schema is missing
    assert build_listserv_semantic_service(embedder=embedder) is None

    # Embedder raising EmbedderUnavailableError during search propagates explicitly
    create_schema("main")
    valid_sem = build_listserv_semantic_service(embedder=embedder)
    assert valid_sem is not None

    class _FailingEmbedder:
        dim = DIM
        model_name = "failing-embedder"

        def encode(self, _texts: list[str]) -> np.ndarray:
            raise EmbedderUnavailableError("embedding endpoint unreachable")

    failing_sem = ListservSemanticService(
        embedder=_FailingEmbedder(),
        vector_repo=SQLAlchemyListservVectorRepository(),
        source_repository=repository,
    )
    failing_search = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=failing_sem,
    )
    with pytest.raises(EmbedderUnavailableError, match="embedding endpoint unreachable"):
        failing_search.search(semantic="beamforming")
    with pytest.raises(EmbedderUnavailableError, match="embedding endpoint unreachable"):
        failing_search.search(text="beamforming", semantic="beamforming")


def test_stale_source_revision_and_mismatched_fingerprint_do_not_attach_stale_chunk_text(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    page = _make_page()
    msg = _make_message(
        "https://list.etsi.org/msg/rev-guard",
        subject="Revision guard topic",
        dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    repository.commit_page(page, [msg], parse_date_set(["= 2026-10-01"]), ())
    repository.save_detail(
        ListservDetail(
            url=msg.url,
            attachments=(),
            tdocs=(),
            content="beamforming old_generation_text",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=16,
            chunk_overlap=0,
        )
    )
    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        source_repository=repository,
        settings=settings,
    )
    search_service = ListservSearchService(
        repository=repository,
        semantic_service=sem_service,
        settings=settings,
    )

    rec_n = repository.get(msg.url)
    assert rec_n is not None
    rev_n = rec_n.source_revision
    sem_service.index_record(rec_n)
    chunk_id = f"{msg.url}#0"
    assert vec_repo.matched_chunk_texts([chunk_id])[chunk_id].text.endswith(
        "beamforming old_generation_text"
    )

    # 1. Advance stored detail so source_revision becomes N+1 without re-indexing vectors
    repository.save_detail(
        ListservDetail(
            url=msg.url,
            attachments=(),
            tdocs=(),
            content="beamforming new_generation_text",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc),
        )
    )
    rec_n_plus_1 = repository.get(msg.url)
    assert rec_n_plus_1 is not None
    assert rec_n_plus_1.source_revision == rev_n + 1

    # Stale vector/text rows from revision N must not be returned by matched_chunk_texts or attached as snippet
    assert vec_repo.matched_chunk_texts([chunk_id]) == {}
    stale_hits = search_service.search(semantic="beamforming")
    assert len(stale_hits) == 1
    assert stale_hits[0].record.source_revision == rev_n + 1
    assert stale_hits[0].content_preview == "beamforming new_generation_text"
    assert stale_hits[0].snippet is None

    # 2. Re-index at revision N+1, then corrupt vec_listserv_chunk_text.source_revision so v != s
    sem_service.index_record(rec_n_plus_1)
    assert "new_generation_text" in (vec_repo.matched_chunk_texts([chunk_id])[chunk_id].text)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE vec_listserv_chunk_text SET source_revision = :bad WHERE chunk_id = :cid"),
            {"bad": rev_n, "cid": chunk_id},
        )
    assert vec_repo.matched_chunk_texts([chunk_id]) == {}
    mismatch_rev_hits = search_service.search(semantic="beamforming")
    assert mismatch_rev_hits[0].snippet is None

    # 3. Restore revision equality via re-index, then test mismatched chunking fingerprint
    sem_service.index_record(rec_n_plus_1)
    changed_fp_settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=32,
            chunk_overlap=0,
        )
    )
    vec_repo_diff_fp = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=changed_fp_settings.semantic_search,
    )
    sem_diff_fp = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo_diff_fp,
        source_repository=repository,
        settings=changed_fp_settings,
    )
    assert vec_repo_diff_fp.matched_chunk_texts([chunk_id]) == {}
    fp_hits = sem_diff_fp.search("beamforming")
    assert len(fp_hits) == 1
    assert fp_hits[0].snippet is None


def test_semantic_hydration_uses_injected_repository_and_get_many_batches(
    repository: ListservRepository,
) -> None:
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_sem,
        _ListservSearchService,
        _factory,
    ) = _require_task8_modules()

    # 1. SQLAlchemyListservRepository.get_many batches >250 URLs and returns complete records
    assert hasattr(repository, "get_many")
    page = _make_page()
    total_msgs = 310
    messages = [
        _make_message(
            f"https://list.etsi.org/msg/batch-{i:04d}",
            subject=f"Batch message {i} R1-2610793",
            dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            subject_tdocs=("R1-2610793",),
        )
        for i in range(total_msgs)
    ]
    repository.commit_page(page, messages, parse_date_set(["= 2026-10-01"]), ())
    repository.save_detail(
        ListservDetail(
            url=messages[0].url,
            attachments=(
                ListservAttachment(
                    name="batch0.docx",
                    url="https://list.etsi.org/att/batch0.docx",
                ),
            ),
            tdocs=("RP-2601234",),
            content="Batch zero parsed detail",
            content_truncated=False,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )
    repository.save_detail(
        ListservDetail(
            url=messages[-1].url,
            attachments=(),
            tdocs=(),
            content="Batch last parsed detail",
            content_truncated=True,
            parser_version="1.0.0",
            parsed_at=datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc),
        )
    )

    requested_urls = [m.url for m in messages] + ["https://list.etsi.org/msg/nonexistent"]
    loaded_map = repository.get_many(requested_urls)
    assert len(loaded_map) == total_msgs
    assert loaded_map[messages[0].url].detail is not None
    assert loaded_map[messages[0].url].detail.content == "Batch zero parsed detail"
    assert loaded_map[messages[0].url].tdocs == ("R1-2610793", "RP-2601234")
    assert loaded_map[messages[150].url].detail is None
    assert loaded_map[messages[-1].url].detail is not None
    assert loaded_map[messages[-1].url].detail.content == "Batch last parsed detail"
    assert loaded_map[messages[-1].url].detail.content_truncated is True

    # 2. ListservSemanticService hydrates candidates via injected source_repository.get_many
    target_url = messages[0].url
    real_record = loaded_map[target_url]
    custom_record = ListservRecord(
        message=_make_message(
            target_url,
            subject="Injected repository subject override",
            dt=real_record.message.date,
        ),
        detail=real_record.detail,
        tdocs=real_record.tdocs,
        source_revision=real_record.source_revision,
    )

    class _RecordingSourceRepo:
        def __init__(self) -> None:
            self.get_many_calls: list[list[str]] = []

        def get_many(self, urls):
            url_list = list(urls)
            self.get_many_calls.append(url_list)
            return {u: custom_record for u in url_list if u == target_url}

    embedder = _DeterministicEmbedder()
    vec_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
    )
    recording_repo = _RecordingSourceRepo()
    sem_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vec_repo,
        source_repository=recording_repo,  # type: ignore[arg-type]
    )
    sem_service.index_record(real_record)

    hits = sem_service.search("beamforming")
    assert len(recording_repo.get_many_calls) == 1
    assert target_url in recording_repo.get_many_calls[0]
    assert len(hits) == 1
    assert hits[0].record.message.subject == "Injected repository subject override"



def _large_vector_test_stack(repository, *, fanout_multiplier: int = 1):
    (
        SQLAlchemyListservVectorRepository,
        ListservSemanticService,
        _build_semantic_service,
        ListservSearchService,
        _factory,
    ) = _require_task8_modules()
    from doc3gpp.settings.schema import (
        SearchSettings,
        SemanticSearchSettings,
        Settings,
    )

    settings = Settings(
        search=SearchSettings(enabled=True),
        semantic_search=SemanticSearchSettings(
            enabled=True,
            chunk_size=4,
            chunk_overlap=0,
            fanout_multiplier=fanout_multiplier,
            rrf_k=60,
            fts5_weight=0.5,
        ),
    )
    embedder = _DeterministicEmbedder()
    vector_repo = SQLAlchemyListservVectorRepository(
        expected_model=embedder.model_name,
        expected_dim=embedder.dim,
        chunk_settings=settings.semantic_search,
    )
    fts_repo = SQLAlchemyListservSearchRepository(
        source_repository=repository,
        settings=settings,
    )
    semantic_service = ListservSemanticService(
        embedder=embedder,
        vector_repo=vector_repo,
        fts_repository=fts_repo,
        source_repository=repository,
        settings=settings,
    )
    search_service = ListservSearchService(
        repository=repository,
        fts_repository=fts_repo,
        semantic_service=semantic_service,
        settings=settings,
    )
    return settings, embedder, vector_repo, fts_repo, semantic_service, search_service


def _commit_large_vector_test_messages(repository, messages) -> None:
    # These bulk-search fixtures bypass sync discovery, so create their FK
    # parents explicitly before writing child message rows.
    grouped_messages: dict[str, list[ListservMessage]] = {}
    for message in messages:
        grouped_messages.setdefault(message.list_id, []).append(message)

    with get_session_factory()() as session:
        for list_id in sorted(grouped_messages):
            if session.get(ListservListORM, list_id) is None:
                session.add(
                    ListservListORM(
                        list_id=list_id, tsg=None, description=None
                    )
                )
        session.commit()

    coverage = parse_date_set([">= 2026-10-01", "<= 2026-10-02"])
    for list_id, list_messages in sorted(grouped_messages.items()):
        page = _make_page(
            list_id=list_id,
            url=f"https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L={list_id}",
        )
        for start in range(0, len(list_messages), 500):
            repository.commit_page(
                page, list_messages[start : start + 500], coverage, ()
            )


def _seed_single_chunk_vectors(repository, vector_repo, messages, vectors, texts) -> None:
    from doc3gpp.models.semantic_search import EmbeddedChunk

    records = repository.get_many([message.url for message in messages])
    items = []
    for message in messages:
        record = records[message.url]
        items.append(
            (
                message.url,
                [
                    EmbeddedChunk(
                        chunk_id=f"{message.url}#0",
                        chunk_index=0,
                        embedding=vectors[message.url],
                        text=texts[message.url],
                    )
                ],
                record.source_revision,
            )
        )
    assert vector_repo.upsert_chunks_many(items) == len(items)


def test_deep_semantic_and_hybrid_match_exact_oracle(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from doc3gpp.models.listserv import ListservFilters

    _, embedder, vector_repo, fts_repo, _semantic_service, search_service = (
        _large_vector_test_stack(repository)
    )
    query_vector = embedder.encode(["beamforming"])[0]
    deep_messages = [
        _make_message(
            f"https://list.etsi.org/msg/deep-{index:05d}",
            subject="matchmarker",
            sender="Oracle sender",
            dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            subject_tdocs=(),
        )
        for index in range(4098)
    ]
    fts_only = _make_message(
        "https://list.etsi.org/msg/fts-only",
        subject="ftsmarker",
        sender="Oracle sender",
        dt=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    vector_only = _make_message(
        "https://list.etsi.org/msg/vector-only",
        subject="vectoronly",
        sender="Oracle sender",
        dt=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        subject_tdocs=(),
    )
    _commit_large_vector_test_messages(repository, [*deep_messages, fts_only, vector_only])
    fts_repo.rebuild()

    vector_messages = [*deep_messages, vector_only]
    vectors = {message.url: query_vector for message in vector_messages}
    texts = {
        message.url: f"matched chunk for {message.url.rsplit('-', 1)[-1]}"
        for message in vector_messages
    }
    _seed_single_chunk_vectors(repository, vector_repo, vector_messages, vectors, texts)

    requested_k: list[int] = []
    original_knn = vector_repo.knn

    def record_knn_limit(query_vec, limit):
        requested_k.append(limit)
        return original_knn(query_vec, limit)

    monkeypatch.setattr(vector_repo, "knn", record_knn_limit)
    filters = ListservFilters(limit=2, offset=4096)

    semantic_page = search_service.search(semantic="beamforming", filters=filters)
    vector_distances = {
        url: float(
            1.0
            - np.dot(query_vector, vector)
            / (np.linalg.norm(query_vector) * np.linalg.norm(vector))
        )
        for url, vector in vectors.items()
    }
    exact_vector_order = sorted(
        vectors,
        key=lambda url: (vector_distances[url], f"{url}#0"),
    )
    message_timestamps = {
        message.url: message.date.timestamp() for message in vector_messages
    }
    expected_semantic = sorted(
        exact_vector_order,
        key=lambda url: (vector_distances[url], -message_timestamps[url], url),
    )[4096:4098]
    assert [hit.record.message.url for hit in semantic_page] == expected_semantic

    # Build an independent RRF oracle from the repository's FTS ranks and
    # cosine distances computed from the known seeded vectors.
    fts_urls: list[str] = []
    fts_offset = 0
    while True:
        fts_page = fts_repo.search(
            "matchmarker",
            ListservFilters(limit=150, offset=fts_offset),
        )
        fts_urls.extend(hit.record.message.url for hit in fts_page)
        if len(fts_page) < 150:
            break
        fts_offset += len(fts_page)
    assert len(fts_urls) == len(deep_messages)

    vector_rank = {url: rank for rank, url in enumerate(exact_vector_order)}
    fts_rank = {url: rank for rank, url in enumerate(fts_urls)}
    expected_scores = {
        url: (0.5 / (60 + fts_rank[url]) if url in fts_rank else 0.0)
        + (0.5 / (60 + vector_rank[url]) if url in vector_rank else 0.0)
        for url in dict.fromkeys([*fts_urls, *exact_vector_order])
    }
    exact_hybrid_order = sorted(expected_scores, key=lambda url: (-expected_scores[url], url))
    expected_hybrid = exact_hybrid_order[4096:4098]
    hybrid_page = search_service.search(
        text="matchmarker",
        semantic="beamforming",
        filters=filters,
    )
    assert [hit.record.message.url for hit in hybrid_page] == expected_hybrid
    for hit in hybrid_page:
        assert hit.score == pytest.approx(expected_scores[hit.record.message.url])
        assert hit.matched_source == "subject"
        assert hit.snippet is not None and "<<matchmarker>>" in hit.snippet
    assert all(limit <= 4096 for limit in requested_k)

    # The same run covers an FTS-only and a vector-only result; the deep
    # semantic/hybrid pages above are FTS+vector hits.
    extra_hits = search_service.search(
        text="ftsmarker",
        semantic="beamforming",
        filters=ListservFilters(
            dates=parse_date_set(["= 2026-10-02"]),
            limit=10,
        ),
    )
    by_url = {hit.record.message.url: hit for hit in extra_hits}
    assert set(by_url) == {fts_only.url, vector_only.url}
    assert by_url[fts_only.url].matched_source == "subject"
    assert by_url[fts_only.url].snippet is not None
    assert "<<ftsmarker>>" in by_url[fts_only.url].snippet
    assert by_url[vector_only.url].matched_source == "content"
    assert by_url[vector_only.url].snippet == texts[vector_only.url]


def test_exact_window_applies_filters_before_counting(
    repository: ListservRepository,
) -> None:
    from datetime import datetime, timezone

    from doc3gpp.models.listserv import ListservFilters

    _, embedder, vector_repo, _fts_repo, _semantic_service, search_service = (
        _large_vector_test_stack(repository)
    )
    query_vector = embedder.encode(["beamforming"])[0]
    excluded = [
        _make_message(
            f"https://list.etsi.org/msg/excluded-{index:05d}",
            list_id="OTHER_LIST",
            subject="Noise subject",
            sender="Blocked sender",
            dt=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
            subject_tdocs=("R9-9999999",),
        )
        for index in range(4098)
    ]
    eligible = [
        _make_message(
            f"https://list.etsi.org/msg/eligible-{index}",
            list_id="LIST_TARGET",
            subject="Target contribution",
            sender="Target Alice",
            dt=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
            subject_tdocs=("R1-2610793",),
        )
        for index in range(2)
    ]
    _commit_large_vector_test_messages(repository, [*excluded, *eligible])

    far_vector = np.zeros_like(query_vector)
    far_vector[1] = 1.0
    vector_messages = [*excluded, *eligible]
    excluded_urls = {message.url for message in excluded}
    vectors = {
        message.url: query_vector if message.url in excluded_urls else far_vector
        for message in vector_messages
    }
    texts = {message.url: f"chunk for {message.url}" for message in vector_messages}
    _seed_single_chunk_vectors(repository, vector_repo, vector_messages, vectors, texts)

    filters = ListservFilters(
        list_id="LIST_%",
        sender="!Blocked%",
        subject="Target%",
        tdocs="R1-26%",
        dates=parse_date_set(["= 2026-10-02"]),
        limit=2,
    )
    hits = search_service.search(semantic="beamforming", filters=filters)
    assert [hit.record.message.url for hit in hits] == [message.url for message in eligible]
    assert [hit.score for hit in hits] == pytest.approx([1.0, 1.0])

    # The vector repository uses the same rich-filter grammar for null tokens.
    assert vector_repo.exact_distinct(
        query_vector,
        target_distinct=1,
        filters=ListservFilters(list_id="not-null"),
    )
    assert vector_repo.exact_distinct(
        query_vector,
        target_distinct=1,
        filters=ListservFilters(list_id="null"),
    ) == []


def test_crowded_cap_ties_and_exhaustion(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from doc3gpp.models.listserv import ListservFilters
    from doc3gpp.models.semantic_search import EmbeddedChunk

    _, embedder, vector_repo, _fts_repo, _semantic_service, search_service = (
        _large_vector_test_stack(repository)
    )
    query_vector = embedder.encode(["beamforming"])[0]
    dt = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    crowded = _make_message(
        "https://list.etsi.org/msg/crowded-cap",
        subject="Crowded",
        dt=dt,
        subject_tdocs=(),
    )
    near = _make_message("https://list.etsi.org/msg/near-cap", subject="Near", dt=dt, subject_tdocs=())
    other = _make_message("https://list.etsi.org/msg/other-cap", subject="Other", dt=dt, subject_tdocs=())
    _commit_large_vector_test_messages(repository, [crowded, near, other])
    records = repository.get_many([crowded.url, near.url, other.url])
    far_vector = np.zeros_like(query_vector)
    far_vector[1] = 1.0
    chunks = [
        EmbeddedChunk(
            chunk_id=f"{crowded.url}#{index}",
            chunk_index=index,
            embedding=query_vector,
            text=f"crowded chunk {index}",
        )
        for index in range(4096)
    ]
    items = [
        (crowded.url, chunks, records[crowded.url].source_revision),
        (
            near.url,
            [EmbeddedChunk(f"{near.url}#0", 0, far_vector, "near chunk")],
            records[near.url].source_revision,
        ),
        (
            other.url,
            [EmbeddedChunk(f"{other.url}#0", 0, far_vector, "other chunk")],
            records[other.url].source_revision,
        ),
    ]
    assert vector_repo.upsert_chunks_many(items) == 4098

    requested_k: list[int] = []
    original_knn = vector_repo.knn

    def record_knn_limit(query_vec, limit):
        requested_k.append(limit)
        return original_knn(query_vec, limit)

    monkeypatch.setattr(vector_repo, "knn", record_knn_limit)
    top = search_service.search(
        semantic="beamforming",
        filters=ListservFilters(limit=2),
    )
    assert [hit.record.message.url for hit in top] == [crowded.url, near.url]
    assert top[0].snippet == "crowded chunk 0"

    original_exact = vector_repo.exact_distinct
    exact_targets: list[int] = []

    def record_exact_target(query_vec, *, target_distinct, filters):
        exact_targets.append(target_distinct)
        return original_exact(
            query_vec,
            target_distinct=target_distinct,
            filters=filters,
        )

    monkeypatch.setattr(vector_repo, "exact_distinct", record_exact_target)
    exhausted = search_service.search(
        semantic="beamforming",
        filters=ListservFilters(limit=1, offset=3),
    )
    assert exhausted == []
    missing = search_service.search(
        semantic="beamforming",
        filters=ListservFilters(list_id="MISSING%", limit=1),
    )
    assert missing == []
    assert exact_targets

    prior_exact_calls = len(exact_targets)
    fast_page = search_service.search(
        semantic="beamforming",
        filters=ListservFilters(limit=1),
    )
    assert [hit.record.message.url for hit in fast_page] == [crowded.url]
    assert len(exact_targets) > prior_exact_calls
    assert requested_k and max(requested_k) <= 4096


def test_fast_and_exact_matched_chunk_text_use_read_snapshot(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import text

    from doc3gpp.models.listserv import ListservFilters
    from doc3gpp.models.semantic_search import EmbeddedChunk
    from doc3gpp.storage.db.session import get_engine

    _, embedder, vector_repo, _fts_repo, _semantic_service, _search_service = (
        _large_vector_test_stack(repository)
    )
    query_vector = embedder.encode(["beamforming"])[0]
    dt = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    crowded = _make_message(
        "https://list.etsi.org/msg/snapshot-crowded",
        subject="Snapshot crowded",
        dt=dt,
        subject_tdocs=(),
    )
    next_message = _make_message(
        "https://list.etsi.org/msg/snapshot-next",
        subject="Snapshot next",
        dt=dt,
        subject_tdocs=(),
    )
    _commit_large_vector_test_messages(repository, [crowded, next_message])
    records = repository.get_many([crowded.url, next_message.url])
    far_vector = np.zeros_like(query_vector)
    far_vector[1] = 1.0
    original_texts = {
        f"{crowded.url}#{index}": (
            "original chunk zero" if index == 0 else f"crowded chunk {index}"
        )
        for index in range(4096)
    }
    chunks = [
        EmbeddedChunk(
            chunk_id=f"{crowded.url}#{index}",
            chunk_index=index,
            embedding=query_vector,
            text=original_texts[f"{crowded.url}#{index}"],
        )
        for index in range(4096)
    ]
    assert vector_repo.upsert_chunks_many(
        [
            (crowded.url, chunks, records[crowded.url].source_revision),
            (
                next_message.url,
                [EmbeddedChunk(f"{next_message.url}#0", 0, far_vector, "next chunk")],
                records[next_message.url].source_revision,
            ),
        ]
    ) == 4097

    original_matched = vector_repo._matched_chunk_texts
    mutation = {
        "fired": False,
        "replacement": "changed during exact read",
        "chunk_id": "",
        "original_text": "",
    }

    def mutate_before_text(conn, chunk_ids):
        if not mutation["fired"]:
            assert chunk_ids
            chunk_id = chunk_ids[0]
            mutation["chunk_id"] = chunk_id
            mutation["original_text"] = original_texts[chunk_id]
            with get_engine().begin() as writer:
                writer.execute(
                    text(
                        "UPDATE vec_listserv_chunk_text SET text = :body "
                        "WHERE chunk_id = :chunk_id"
                    ),
                    {"body": mutation["replacement"], "chunk_id": chunk_id},
                )
            mutation["fired"] = True
        return original_matched(conn, chunk_ids)

    monkeypatch.setattr(vector_repo, "_matched_chunk_texts", mutate_before_text)
    exact_rows = vector_repo.exact_distinct(
        query_vector,
        target_distinct=2,
        filters=ListservFilters(),
    )
    assert mutation["fired"]
    assert exact_rows[0][0] == crowded.url
    assert exact_rows[0].matched_chunk is not None
    assert exact_rows[0].matched_chunk.text == "original chunk zero"

    # Reset the text, then trigger the same concurrent write in the KNN fast path.
    with get_engine().begin() as writer:
        writer.execute(
            text(
                "UPDATE vec_listserv_chunk_text SET text = :body "
                "WHERE chunk_id = :chunk_id"
            ),
            {"body": mutation["original_text"], "chunk_id": mutation["chunk_id"]},
        )
    mutation["fired"] = False
    mutation["replacement"] = "changed during KNN read"
    fast_rows = vector_repo.knn(query_vector, limit=1)
    assert mutation["fired"]
    assert fast_rows[0][1] == mutation["chunk_id"]
    assert fast_rows[0].matched_chunk is not None
    assert fast_rows[0].matched_chunk.text == mutation["original_text"]


def test_semantic_window_falls_back_when_knn_cuts_distance_tie(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from doc3gpp.models.listserv import ListservFilters

    (
        _settings,
        embedder,
        vector_repo,
        _fts_repo,
        _semantic_service,
        search_service,
    ) = _large_vector_test_stack(repository, fanout_multiplier=4)
    query_vector = embedder.encode(["beamforming"])[0]
    messages = [
        _make_message(
            f"https://list.etsi.org/msg/tie-cut-{index:02d}",
            subject=f"Tied result {index}",
            dt=datetime(2026, 10, 1, 12, index, tzinfo=timezone.utc),
            subject_tdocs=(),
        )
        for index in range(5)
    ]
    _commit_large_vector_test_messages(repository, messages)
    _seed_single_chunk_vectors(
        repository,
        vector_repo,
        messages,
        {message.url: query_vector for message in messages},
        {message.url: f"matched text {index}" for index, message in enumerate(messages)},
    )

    filters = ListservFilters(limit=1)
    original_exact = vector_repo.exact_distinct
    tied_order = original_exact(
        query_vector, target_distinct=len(messages), filters=filters
    )
    assert len(tied_order) == len(messages)
    assert len({candidate[3] for candidate in tied_order}) == 1

    requested_k: list[int] = []
    exact_targets: list[int] = []

    def bounded_knn(query_vec, limit):
        requested_k.append(limit)
        return tied_order[:limit]

    def record_exact(query_vec, *, target_distinct, filters):
        exact_targets.append(target_distinct)
        return original_exact(
            query_vec, target_distinct=target_distinct, filters=filters
        )

    monkeypatch.setattr(vector_repo, "knn", bounded_knn)
    monkeypatch.setattr(vector_repo, "exact_distinct", record_exact)

    hits = search_service.search(semantic="beamforming", filters=filters)
    expected_url = max(messages, key=lambda message: message.date).url

    assert [hit.record.message.url for hit in hits] == [expected_url]
    assert requested_k == [5]
    assert exact_targets == [1]


def test_semantic_window_without_boundary_tie_keeps_knn_fast_path(
    repository: ListservRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from doc3gpp.models.listserv import ListservFilters

    (
        _settings,
        embedder,
        vector_repo,
        _fts_repo,
        _semantic_service,
        search_service,
    ) = _large_vector_test_stack(repository, fanout_multiplier=4)
    query_vector = embedder.encode(["beamforming"])[0]
    messages = [
        _make_message(
            f"https://list.etsi.org/msg/no-tie-{index:02d}",
            subject=f"Unequal result {index}",
            dt=datetime(2026, 10, 1, 12, index, tzinfo=timezone.utc),
            subject_tdocs=(),
        )
        for index in range(5)
    ]
    _commit_large_vector_test_messages(repository, messages)
    vectors = {}
    for index, message in enumerate(messages):
        cosine = 1.0 - index * 0.1
        vector = np.zeros_like(query_vector)
        vector[0] = cosine
        vector[1] = np.sqrt(1.0 - cosine * cosine)
        vectors[message.url] = vector
    _seed_single_chunk_vectors(
        repository,
        vector_repo,
        messages,
        vectors,
        {message.url: f"matched text {index}" for index, message in enumerate(messages)},
    )

    requested_k: list[int] = []
    exact_targets: list[int] = []
    original_knn = vector_repo.knn
    original_exact = vector_repo.exact_distinct

    def record_knn(query_vec, limit):
        requested_k.append(limit)
        return original_knn(query_vec, limit)

    def record_exact(query_vec, *, target_distinct, filters):
        exact_targets.append(target_distinct)
        return original_exact(
            query_vec, target_distinct=target_distinct, filters=filters
        )

    monkeypatch.setattr(vector_repo, "knn", record_knn)
    monkeypatch.setattr(vector_repo, "exact_distinct", record_exact)

    hits = search_service.search(
        semantic="beamforming", filters=ListservFilters(limit=1)
    )

    assert [hit.record.message.url for hit in hits] == [messages[0].url]
    assert requested_k
    assert max(requested_k) <= 4096
    assert exact_targets == []
