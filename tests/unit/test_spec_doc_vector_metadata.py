from types import SimpleNamespace

from doc3gpp.services.spec_doc_semantic_service import SpecDocSemanticService


class _Embedder:
    def encode(self, texts):
        return [[0.0] for _ in texts]


class _VectorRepository:
    def __init__(self):
        self.cursor = "38.331@18.5.0"
        self.calls = []

    def get_resume_cursor(self):
        return self.cursor

    def clear_resume_cursor(self):
        raise AssertionError("resume=True must preserve the vector cursor")

    def count_versions_to_index(self, *, stale_only, after_id):
        assert stale_only is False
        assert after_id == self.cursor
        return 2

    def rebuild_batch(self, *, batch_size, after_id, stale_only):
        assert batch_size == 10
        assert after_id == self.cursor
        assert stale_only is False
        yield [("38.331", "19.0.0"), ("38.331", "20.0.0")]

    def set_resume_cursor(self, cursor):
        self.cursor = cursor


class _RetryableVectorRepository:
    def __init__(self):
        self.pairs = [("38.331", "18.0.0"), ("38.331", "19.0.0")]
        self.cursor = None
        self.failed = set()
        self.watermark = "2026-01-02T00:00:00+00:00"
        self.touched = False

    def get_resume_cursor(self):
        return self.cursor

    def clear_resume_cursor(self):
        self.cursor = None

    def count_versions_to_index(self, *, stale_only, after_id):
        return len(self._selected(stale_only, after_id))

    def rebuild_batch(self, *, batch_size, after_id, stale_only):
        selected = self._selected(stale_only, after_id)
        if selected:
            yield selected[:batch_size]

    def set_resume_cursor(self, cursor):
        self.cursor = cursor

    def record_rebuild_failure(self, spec_id, version):
        self.failed.add((spec_id, version))

    def clear_rebuild_failure(self, spec_id, version):
        self.failed.discard((spec_id, version))

    def touch_rebuild_at(self):
        self.touched = True

    def _selected(self, stale_only, after_id):
        selected = [
            pair
            for pair in self.pairs
            if (not after_id or f"{pair[0]}@{pair[1]}" > after_id)
            or pair in self.failed
        ]
        if stale_only:
            selected = [pair for pair in selected if pair in self.failed]
        return selected


class _GreaterThanCursorVectorRepository:
    failed_pair = ("38.331", "20.0.0")
    later_pair = ("38.331", "21.0.0")

    def __init__(self):
        self.cursor = "38.331@19.0.0"
        self.failed = {self.failed_pair}
        self.clear_calls = []

    def get_resume_cursor(self):
        return self.cursor

    def count_versions_to_index(self, *, stale_only, after_id):
        assert stale_only is False
        assert after_id == self.cursor
        return 2

    def rebuild_batch(self, *, batch_size, after_id, stale_only):
        assert batch_size == 10
        assert after_id == self.cursor
        assert stale_only is False
        yield [self.failed_pair]
        if self.failed_pair in self.failed:
            yield [self.later_pair]
        else:
            yield [self.failed_pair, self.later_pair]

    def set_resume_cursor(self, cursor):
        self.cursor = cursor

    def clear_rebuild_failure(self, spec_id, version):
        pair = (spec_id, version)
        if pair in self.failed:
            self.clear_calls.append(pair)
            self.failed.discard(pair)

    def touch_rebuild_at(self):
        pass


def test_rebuild_embeddings_resume_indexes_only_pairs_after_cursor():
    vector = _VectorRepository()
    service = SpecDocSemanticService(
        fts5_service=SimpleNamespace(),
        embedder=_Embedder(),
        vector_repo=vector,
        settings=SimpleNamespace(
            semantic_search=SimpleNamespace(fanout_multiplier=2, rrf_k=60)
        ),
    )
    indexed = []
    service.index_for_version = lambda spec_id, version: indexed.append(
        (spec_id, version)
    )

    list(
        service.rebuild_embeddings(
            batch_size=10,
            stale_only=False,
            quiet=True,
            resume=True,
        )
    )

    assert indexed == [("38.331", "19.0.0"), ("38.331", "20.0.0")]
    assert vector.cursor == "38.331@20.0.0"


def test_failed_pair_remains_retryable_after_later_pair_succeeds():
    vector = _RetryableVectorRepository()
    service = SpecDocSemanticService(
        fts5_service=SimpleNamespace(),
        embedder=_Embedder(),
        vector_repo=vector,
        settings=SimpleNamespace(
            semantic_search=SimpleNamespace(fanout_multiplier=2, rrf_k=60)
        ),
    )
    attempts = []

    def index_for_version(spec_id, version):
        attempts.append((spec_id, version))
        if (spec_id, version) == vector.pairs[0] and vector.pairs[0] not in vector.failed:
            raise RuntimeError("transient embedding failure")

    service.index_for_version = index_for_version
    list(
        service.rebuild_embeddings(
            batch_size=10, stale_only=False, quiet=True, resume=False
        )
    )

    assert attempts == vector.pairs
    assert vector.cursor == "38.331@19.0.0"
    assert vector.failed == {vector.pairs[0]}

    attempts.clear()
    list(
        service.rebuild_embeddings(
            batch_size=10, stale_only=True, quiet=True, resume=True
        )
    )

    assert attempts == [vector.pairs[0]]
    assert vector.failed == set()
    assert vector.cursor == "38.331@19.0.0"
    assert vector.touched


def test_retry_pair_greater_than_cursor_is_processed_once():
    vector = _GreaterThanCursorVectorRepository()
    service = SpecDocSemanticService(
        fts5_service=SimpleNamespace(),
        embedder=_Embedder(),
        vector_repo=vector,
        settings=SimpleNamespace(
            semantic_search=SimpleNamespace(fanout_multiplier=2, rrf_k=60)
        ),
    )
    attempts = []
    service.index_for_version = lambda spec_id, version: attempts.append(
        (spec_id, version)
    )

    progress = list(
        service.rebuild_embeddings(
            batch_size=10, stale_only=False, quiet=True, resume=True
        )
    )

    assert attempts == [vector.failed_pair, vector.later_pair]
    assert max(item.processed for item in progress) <= progress[0].total
    assert vector.failed == set()
    assert vector.clear_calls == [vector.failed_pair]
