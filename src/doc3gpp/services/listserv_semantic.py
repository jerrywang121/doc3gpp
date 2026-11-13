"""Semantic (embedding + vector) and hybrid RRF search service for ETSI LISTSERV archives."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import OperationalError as SAOperationalError

from doc3gpp.models.listserv import (
    ListservFilters,
    ListservRecord,
    ListservSearchResult,
)
from doc3gpp.models.listserv_payloads import (
    indexable_text,
    record_to_search_result,
)
from doc3gpp.models.search import MatchedChunk, SearchUnavailableError
from doc3gpp.models.semantic_chunking import _chunks, chunking_fingerprint
from doc3gpp.models.semantic_search import (
    EmbeddedChunk,
    EmbedderUnavailableError,
    VectorCandidate,
    VectorIndexUnavailableError,
)
from doc3gpp.repository.listserv import ListservRepository
from doc3gpp.repository.protocols import Embedder
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.repositories.listserv_sql import (
    SQLAlchemyListservRepository,
    _decompose_message_datetime,
)
from doc3gpp.storage.repositories.listserv_vector_sql import (
    SQLAlchemyListservVectorRepository,
)

_KNN_MAX_K = 4096
_DEFAULT_FILTERS = ListservFilters()


@dataclass(slots=True)
class _VecEmailHit:
    """Best KNN evidence for one canonical LISTSERV email URL."""

    url: str
    rank: int
    best_chunk_id: str
    distance: float
    matched_chunk: MatchedChunk | None = None


def _group_vec_candidates(
    candidates: Sequence[VectorCandidate],
) -> dict[str, _VecEmailHit]:
    """Group KNN chunk rows by email URL, assigning 0-based distinct-email ranks."""
    grouped: dict[str, _VecEmailHit] = {}
    for candidate in candidates:
        url, chunk_id, _chunk_idx, dist = candidate
        matched_chunk = getattr(candidate, "matched_chunk", None)
        entry = grouped.get(url)
        if entry is None:
            grouped[url] = _VecEmailHit(
                url=url,
                rank=len(grouped),
                best_chunk_id=chunk_id,
                distance=float(dist),
                matched_chunk=matched_chunk,
            )
        elif float(dist) < entry.distance:
            entry.best_chunk_id = chunk_id
            entry.distance = float(dist)
            entry.matched_chunk = matched_chunk
    return grouped


def _sort_timestamp_for_record(record: ListservRecord) -> datetime:
    """Return the UTC-normalized (or naive wall-clock fallback) sort timestamp for tie-breaking."""
    _local_dt, _local_d, _tz_offset, sort_ts = _decompose_message_datetime(
        record.message.date, record.message.date_precision
    )
    return sort_ts


def _desc_timestamp_key(dt: datetime) -> float:
    """Negated POSIX seconds key so ascending tuple sort orders newest timestamp first."""
    return -dt.replace(tzinfo=timezone.utc).timestamp()


class ListservSemanticService:
    """Semantic and hybrid RRF search and per-email vector indexing for LISTSERV archives."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        vector_repo: SQLAlchemyListservVectorRepository,
        fts_repository: Any | None = None,
        source_repository: ListservRepository | None = None,
        settings: Settings | None = None,
        owns_embedder: bool = False,
    ) -> None:
        self._embedder = embedder
        self._owns_embedder = owns_embedder
        self._closed = False
        self._vec = vector_repo
        self._fts = fts_repository
        self._source_repo = source_repository or SQLAlchemyListservRepository()
        self._settings = settings

    def close(self) -> None:
        """Close the embedder only when this service created it."""
        if self._closed:
            return
        self._closed = True
        if not self._owns_embedder:
            return
        close_fn = getattr(self._embedder, "close", None)
        if callable(close_fn):
            close_fn()

    def _resolve_settings(self) -> Settings:
        return self._settings if self._settings is not None else get_settings()

    @property
    def embedder(self) -> Embedder:
        return self._embedder

    @property
    def vector_repository(self) -> SQLAlchemyListservVectorRepository:
        return self._vec

    def index_record(self, record: ListservRecord, *, stamp: bool = True) -> int:
        """Chunk, embed, and atomically replace vector rows for ``record``; return chunk count."""
        settings = self._resolve_settings().semantic_search
        chunk_size = settings.chunk_size
        chunk_overlap = settings.chunk_overlap
        fingerprint = chunking_fingerprint(chunk_size, chunk_overlap)

        sanitized_text = indexable_text(record)
        chunk_texts = _chunks(sanitized_text, chunk_size, chunk_overlap)
        if not chunk_texts:
            self._vec.remove(record.message.url)
            return 0

        embeddings = self._embedder.encode(chunk_texts)
        url = record.message.url
        embedded_chunks = [
            EmbeddedChunk(
                chunk_id=f"{url}#{idx}",
                chunk_index=idx,
                embedding=embeddings[idx],
                text=chunk_text,
                chunking_fingerprint=fingerprint,
            )
            for idx, chunk_text in enumerate(chunk_texts)
        ]
        return self._vec.upsert_chunks(
            url,
            embedded_chunks,
            source_revision=record.source_revision,
            stamp=stamp,
        )

    def search(
        self,
        query: str,
        filters: ListservFilters = _DEFAULT_FILTERS,
        *,
        fts_query: str | None = None,
        fts_repository: Any | None = None,
    ) -> list[ListservSearchResult]:
        """Run semantic-only or hybrid (FTS5 + vector RRF) search over stored LISTSERV emails."""
        query_clean = query.strip() if query and query.strip() else ""
        fts_clean = fts_query.strip() if fts_query and fts_query.strip() else None

        if filters.limit == 0:
            if fts_clean is not None:
                active_fts = fts_repository if fts_repository is not None else self._fts
                if active_fts is None:
                    raise SearchUnavailableError("FTS5 search is not available")
                active_fts.search(fts_clean, filters)
            return []

        settings = self._resolve_settings()
        sem_settings = settings.semantic_search
        query_vec = self._embedder.encode([query_clean])[0]

        target_distinct = max(filters.offset + filters.limit, 1)
        fanout = max(sem_settings.fanout_multiplier, 1)

        if fts_clean is None:
            vec_candidates = self._distinct_email_rows(
                query_vec,
                target_distinct=target_distinct,
                fanout=fanout,
                filters=filters,
            )
            grouped = _group_vec_candidates(vec_candidates)
            if not grouped:
                return []

            records_by_url = self._load_records_by_url(list(grouped.keys()))
            scored_items: list[tuple[float, float, str, ListservSearchResult]] = []
            for url, entry in grouped.items():
                record = records_by_url.get(url)
                if record is None:
                    continue
                snippet = (
                    entry.matched_chunk.text
                    if entry.matched_chunk is not None
                    else None
                )
                result = record_to_search_result(
                    record,
                    score=float(entry.distance),
                    snippet=snippet,
                    matched_source="content",
                )
                sort_ts = _sort_timestamp_for_record(record)
                scored_items.append(
                    (
                        float(entry.distance),
                        _desc_timestamp_key(sort_ts),
                        url,
                        result,
                    )
                )

            scored_items.sort(key=lambda item: (item[0], item[1], item[2]))
            paged = scored_items[filters.offset : filters.offset + filters.limit]
            return [item[3] for item in paged]

        active_fts = fts_repository if fts_repository is not None else self._fts
        if active_fts is None:
            raise SearchUnavailableError("FTS5 search is not available")

        candidate_window = max(target_distinct * fanout, 1)
        fts_hits_by_url: dict[str, ListservSearchResult] = {}
        fts_offset = 0
        while len(fts_hits_by_url) < candidate_window:
            batch_limit = min(150, candidate_window - len(fts_hits_by_url))
            fts_window_filters = ListservFilters(
                list_id=filters.list_id,
                sender=filters.sender,
                subject=filters.subject,
                tdocs=filters.tdocs,
                dates=filters.dates,
                limit=batch_limit,
                offset=fts_offset,
            )
            fts_page = active_fts.search(fts_clean, fts_window_filters)
            if not fts_page:
                break
            fts_offset += len(fts_page)
            for hit in fts_page:
                fts_hits_by_url.setdefault(hit.record.message.url, hit)
            if len(fts_page) < batch_limit:
                break
        fts_hits = list(fts_hits_by_url.values())
        vec_candidates = self._distinct_email_rows(
            query_vec,
            target_distinct=max(target_distinct * fanout, target_distinct),
            fanout=fanout,
            filters=filters,
        )
        vec_grouped = _group_vec_candidates(vec_candidates)

        fts_rank: dict[str, int] = {
            hit.record.message.url: idx for idx, hit in enumerate(fts_hits)
        }
        fts_by_url: dict[str, ListservSearchResult] = {
            hit.record.message.url: hit for hit in fts_hits
        }

        all_urls = list(dict.fromkeys([*fts_rank.keys(), *vec_grouped.keys()]))
        if not all_urls:
            return []

        records_by_url = self._load_records_by_url(all_urls)
        k = sem_settings.rrf_k
        w = 1.0 - float(sem_settings.fts5_weight)

        hybrid_items: list[tuple[float, float, str, ListservSearchResult]] = []
        for url in all_urls:
            record = records_by_url.get(url)
            if record is None:
                continue
            r_fts = fts_rank.get(url)
            vec_entry = vec_grouped.get(url)
            r_vec = vec_entry.rank if vec_entry is not None else None

            fts_term = (1.0 / (k + r_fts)) * (1.0 - w) if r_fts is not None else 0.0
            vec_term = (1.0 / (k + r_vec)) * w if r_vec is not None else 0.0
            rrf_score = float(fts_term + vec_term)

            fts_hit = fts_by_url.get(url)
            if fts_hit is not None and fts_hit.matched_source is not None:
                matched_source: str | None = fts_hit.matched_source
                snippet: str | None = fts_hit.snippet
            # Vector-only hit in hybrid uses best vector chunk text and matched_source="content"
            elif vec_entry is not None:
                matched_source = "content"
                snippet = (
                    vec_entry.matched_chunk.text
                    if vec_entry.matched_chunk is not None
                    else None
                )
            elif fts_hit is not None:
                matched_source = fts_hit.matched_source
                snippet = fts_hit.snippet
            else:
                matched_source = None
                snippet = None

            result = record_to_search_result(
                record,
                score=rrf_score,
                snippet=snippet,
                matched_source=matched_source,
            )
            sort_ts = _sort_timestamp_for_record(record)
            hybrid_items.append(
                (
                    -rrf_score,
                    _desc_timestamp_key(sort_ts),
                    url,
                    result,
                )
            )

        hybrid_items.sort(key=lambda item: (item[0], item[1], item[2]))
        paged_hybrid = hybrid_items[filters.offset : filters.offset + filters.limit]
        return [item[3] for item in paged_hybrid]

    def _distinct_email_rows(
        self,
        query_vec: Any,
        *,
        target_distinct: int,
        fanout: int,
        filters: ListservFilters,
    ) -> list[VectorCandidate]:
        """Fetch eligible vector rows, resolving KNN cutoff ties exactly."""
        if target_distinct > _KNN_MAX_K:
            return self._vec.exact_distinct(
                query_vec, target_distinct=target_distinct, filters=filters
            )

        fetch = min(max(target_distinct * fanout, target_distinct, 1), _KNN_MAX_K)
        while True:
            probe_limit = min(fetch + 1, _KNN_MAX_K)
            probe_window = self._vec.knn(query_vec, limit=probe_limit)
            if not probe_window:
                return []

            corpus_exhausted = len(probe_window) < probe_limit
            if len(probe_window) > fetch:
                if probe_window[fetch - 1][3] == probe_window[fetch][3]:
                    return self._vec.exact_distinct(
                        query_vec, target_distinct=target_distinct, filters=filters
                    )
                window = probe_window[:fetch]
            else:
                window = probe_window

            # At the vec0 ceiling there is no lookahead row to prove the cutoff
            # is outside a tie, so a full window must be ranked exactly.
            if fetch == _KNN_MAX_K and not corpus_exhausted:
                return self._vec.exact_distinct(
                    query_vec, target_distinct=target_distinct, filters=filters
                )

            candidate_urls = {row[0] for row in window}
            allowed_urls = self._vec.matching_urls(candidate_urls, filters)
            kept = [row for row in window if row[0] in allowed_urls]
            distinct_count = len({row[0] for row in kept})
            if distinct_count >= target_distinct or corpus_exhausted:
                return kept
            fetch = min(fetch * 2, _KNN_MAX_K)

    def _load_records_by_url(
        self, urls: Sequence[str]
    ) -> dict[str, ListservRecord]:
        if not urls:
            return {}
        return self._source_repo.get_many(urls)


def build_listserv_semantic_service(
    settings: Settings | None = None,
    *,
    embedder: Embedder | None = None,
    vector_repo: SQLAlchemyListservVectorRepository | None = None,
    fts_repository: Any | None = None,
    source_repository: ListservRepository | None = None,
) -> ListservSemanticService | None:
    """Build a :class:`ListservSemanticService` or return ``None`` if unavailable."""
    from doc3gpp.services import factory

    if settings is None:
        settings = get_settings()
    if not settings.semantic_search.enabled:
        return None

    owned_embedder: Embedder | None = None

    def close_owned_embedder() -> None:
        if owned_embedder is None:
            return
        close_fn = getattr(owned_embedder, "close", None)
        if callable(close_fn):
            close_fn()

    try:
        if embedder is None:
            embedder = factory.build_embedder(settings)
            if embedder is None:
                return None
            owned_embedder = embedder
        if vector_repo is None:
            try:
                live_dim = getattr(embedder, "dim", None)
            except EmbedderUnavailableError:
                close_owned_embedder()
                return None
            if live_dim is None:
                close_owned_embedder()
                return None
            vector_repo = SQLAlchemyListservVectorRepository(
                expected_model=getattr(embedder, "model_name", None),
                expected_dim=live_dim,
                chunk_settings=settings.semantic_search,
            )
        if source_repository is None:
            source_repository = SQLAlchemyListservRepository()
        return ListservSemanticService(
            embedder=embedder,
            vector_repo=vector_repo,
            fts_repository=fts_repository,
            source_repository=source_repository,
            settings=settings,
            owns_embedder=owned_embedder is not None,
        )
    except (
        VectorIndexUnavailableError,
        EmbedderUnavailableError,
        SAOperationalError,
    ):
        close_owned_embedder()
        return None
    except Exception:
        close_owned_embedder()
        raise


__all__ = [
    "ListservSemanticService",
    "build_listserv_semantic_service",
]
