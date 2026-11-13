"""SQLite integration tests for LISTSERV index lifecycle, incremental updates, and recovery."""

from __future__ import annotations

from datetime import date, datetime, timezone
from importlib.util import find_spec
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import text

from doc3gpp.models.index import IndexRebuildResult, IndexRequest
from doc3gpp.models.listserv import (
    ArchivePage,
    ListservDetail,
    ListservMessage,
    parse_date_set,
)
from doc3gpp.scraping.listserv_source import FetchedPage, ListservSource
from doc3gpp.services.listserv_search import ListservSearchService
from doc3gpp.services.listserv_semantic import ListservSemanticService
from doc3gpp.settings.schema import (
    ListservSettings,
    SemanticSearchSettings,
    Settings,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.listserv_search_sql import (
    SQLAlchemyListservSearchRepository,
)
from doc3gpp.storage.repositories.listserv_sql import SQLAlchemyListservRepository
from doc3gpp.storage.repositories.listserv_vector_sql import (
    SQLAlchemyListservVectorRepository,
)

DIM = 384
LIST_ID = "EXAMPLE"
PAGE = ArchivePage(
    list_id=LIST_ID,
    url="https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE",
    period="2026-10-W1",
)


def _index_module():
    assert find_spec("doc3gpp.services.listserv_index") is not None
    from doc3gpp.services import listserv_index

    return listserv_index


class _Embedder:
    dim = DIM
    model_name = "index-test-model"

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.fail = False

    def encode(self, texts: list[str]) -> np.ndarray:
        if self.fail:
            raise RuntimeError("embedding backend exploded: secret-body-text")
        self.inputs.extend(texts)
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        out[:, 0] = 1.0
        return out


class _FailingFts:
    """FTS repository wrapper that fails writes on demand."""

    def __init__(self, inner: SQLAlchemyListservSearchRepository) -> None:
        self._inner = inner
        self.fail = True

    def upsert_many(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("fts exploded: secret-body-text")
        return self._inner.upsert_many(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _msg(n: int, subject: str | None = None) -> ListservMessage:
    return ListservMessage(
        url=f"https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P={n}",
        list_id=LIST_ID,
        subject=subject or f"Subject number {n}",
        sender="Alice <[log in to unmask]>",
        date=datetime(2026, 10, 1, 12, n % 60, tzinfo=timezone.utc),
        date_precision="datetime",
        week="2026-10-W1",
        subject_tdocs=(),
    )


def _detail(msg: ListservMessage, content: str) -> ListservDetail:
    return ListservDetail(
        url=msg.url,
        attachments=(),
        tdocs=(),
        content=content,
        content_truncated=False,
        parser_version="1.0.0",
        parsed_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
    )


class Env:
    def __init__(self, settings: Settings | None = None) -> None:
        create_schema("main")
        self.settings = settings or Settings(
            semantic_search=SemanticSearchSettings(enabled=True)
        )
        self.repo = SQLAlchemyListservRepository()
        self.embedder = _Embedder()
        self.fts = SQLAlchemyListservSearchRepository(
            source_repository=self.repo, settings=self.settings
        )
        self.vec = SQLAlchemyListservVectorRepository(
            expected_model=self.embedder.model_name,
            expected_dim=self.embedder.dim,
            chunk_settings=self.settings.semantic_search,
        )
        self.semantic = ListservSemanticService(
            embedder=self.embedder,
            vector_repo=self.vec,
            source_repository=self.repo,
            settings=self.settings,
        )

    def add(self, *messages: ListservMessage) -> None:
        self.repo.commit_page(
            PAGE, list(messages), parse_date_set(["= 2026-10-01"]), ()
        )

    def index(self, *, fts=True, semantic=True):
        mod = _index_module()
        return mod.ListservIndexService(
            source_repository=self.repo,
            fts_repository=self.fts if fts is True else (fts or None),
            semantic_service=self.semantic if semantic else None,
            settings=self.settings,
        )

    def records(self):
        return list(self.repo.get_many(self.urls()).values())

    def urls(self) -> list[str]:
        with get_engine().connect() as conn:
            return [
                r[0]
                for r in conn.execute(
                    text("SELECT url FROM listserv_messages ORDER BY url")
                )
            ]

    def meta(self, table: str) -> dict[str, str]:
        with get_engine().connect() as conn:
            return dict(conn.execute(text(f"SELECT key, value FROM {table}")).all())


@pytest.fixture()
def env(sqlite_env, monkeypatch) -> Env:
    def _forbid(*_a, **_k):
        raise AssertionError("indexing must never make outbound ETSI requests")

    monkeypatch.setattr(ListservSource, "fetch", _forbid)
    return Env()


def test_index_module_is_present(sqlite_env) -> None:
    _index_module()


def test_hooks_gated_independently_and_stale_when_disabled(sqlite_env) -> None:
    settings = Settings(
        semantic_search=SemanticSearchSettings(enabled=True),
        listserv=ListservSettings(auto_index_on_sync=False),
    )
    env = Env(settings)
    env.add(_msg(1))
    svc = env.index()
    assert svc.update_records(env.records(), operation="sync") == {}
    status = svc.status()
    assert status.fts5.status.is_stale
    assert not status.vector.status.is_stale
    assert svc.update_records(env.records(), operation="parse") == {}
    assert not svc.status().fts5.status.is_stale


def test_embed_flag_disabled_leaves_vector_stale(sqlite_env) -> None:
    settings = Settings(
        semantic_search=SemanticSearchSettings(enabled=True),
        listserv=ListservSettings(auto_embed_on_sync=False),
    )
    env = Env(settings)
    env.add(_msg(1))
    svc = env.index()
    svc.update_records(env.records(), operation="sync")
    status = svc.status()
    assert not status.fts5.status.is_stale
    assert status.vector.status.is_stale
    assert env.embedder.inputs == []


def test_failures_are_sanitized_and_component_independent(env: Env) -> None:
    env.add(_msg(1))
    flaky = _FailingFts(env.fts)
    svc = env.index(fts=flaky)
    env.embedder.fail = True
    failures = svc.update_records(env.records(), operation="sync")
    url = env.urls()[0]
    assert set(failures) == {url}
    assert "secret-body-text" not in failures[url]
    assert "FTS5" in failures[url] and "vector" in failures[url]
    status = svc.status()
    assert status.fts5.status.is_stale and status.vector.status.is_stale
    assert env.repo.get(url) is not None

    env.embedder.fail = False
    failures = svc.update_records(env.records(), operation="sync")
    assert set(failures) == {url} and "vector" not in failures[url]
    assert not svc.status().vector.status.is_stale
    assert svc.status().fts5.status.is_stale


def test_noop_source_revision_does_not_invalidate_or_reembed(env: Env) -> None:
    msg = _msg(1)
    env.add(msg)
    svc = env.index()
    svc.update_records(env.records(), operation="sync")
    seen = len(env.embedder.inputs)
    revision = env.records()[0].source_revision

    env.add(msg)
    assert env.records()[0].source_revision == revision
    assert not svc.status().fts5.status.is_stale
    assert not svc.status().vector.status.is_stale
    svc.update_records(env.records(), operation="sync")
    assert len(env.embedder.inputs) == seen

    env.add(_msg(1, subject="Changed subject"))
    assert env.records()[0].source_revision == revision + 1
    assert svc.status().fts5.status.is_stale
    assert svc.status().vector.status.is_stale
    svc.update_records(env.records(), operation="sync")
    assert len(env.embedder.inputs) > seen
    assert not svc.status().vector.status.is_stale


def test_rebuild_repairs_failed_incremental_update(env: Env) -> None:
    msg = _msg(1)
    env.add(msg)
    env.repo.save_detail(_detail(msg, "repairtoken body"))
    flaky = _FailingFts(env.fts)
    svc = env.index(fts=flaky, semantic=False)
    assert svc.update_records(env.records(), operation="parse")
    assert svc.status().fts5.status.is_stale
    flaky.fail = False
    search = ListservSearchService(
        repository=env.repo, fts_repository=env.fts, settings=env.settings
    )
    assert not search.search(text="repairtoken")
    result = svc.rebuild(IndexRequest(rebuild=True))
    assert isinstance(result, IndexRebuildResult)
    assert result.fts5_processed == 1 and result.vector_processed == 0
    assert not svc.status().fts5.status.is_stale
    assert search.search(text="repairtoken")


def test_rebuild_modes_batches_progress_and_sql_only(env: Env) -> None:
    messages = [_msg(n) for n in range(1, 6)]
    env.add(*messages)
    env.repo.save_detail(_detail(messages[0], ""))
    svc = env.index()
    lines: list[str] = []

    result = svc.rebuild(
        IndexRequest(rebuild=True, batch=2), on_progress=lines.append
    )
    assert result == IndexRebuildResult(fts5_processed=5, vector_processed=0)
    assert len(lines) == 3 and not env.embedder.inputs
    assert svc.status().vector.status.is_stale
    assert not svc.status().fts5.status.is_stale
    assert "last_rebuild_at" in env.meta("listserv_search_meta")

    result = svc.rebuild(IndexRequest(rebuild_embeddings=True, batch=3))
    assert result == IndexRebuildResult(fts5_processed=0, vector_processed=5)
    assert not svc.status().vector.status.is_stale

    result = svc.rebuild(IndexRequest(rebuild_all=True))
    assert result == IndexRebuildResult(fts5_processed=5, vector_processed=5)

    assert not isinstance(svc.rebuild(IndexRequest()), IndexRebuildResult)


def test_stale_only_and_resume_process_only_pending(env: Env) -> None:
    env.add(*[_msg(n) for n in range(1, 4)])
    svc = env.index()
    svc.rebuild(IndexRequest(rebuild_all=True))
    seen = len(env.embedder.inputs)
    result = svc.rebuild(IndexRequest(rebuild_all=True, stale_only=True))
    assert result == IndexRebuildResult(0, 0)
    assert len(env.embedder.inputs) == seen

    env.add(_msg(2, subject="Changed two"))
    result = svc.rebuild(IndexRequest(rebuild_all=True, resume=True))
    assert result == IndexRebuildResult(1, 1)
    assert not svc.status().fts5.status.is_stale
    assert not svc.status().vector.status.is_stale


def test_cancellation_leaves_rebuild_unmarked_and_resume_completes(env: Env) -> None:
    mod = _index_module()
    env.add(*[_msg(n) for n in range(1, 6)])
    svc = env.index()
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(mod.IndexRebuildCancelled):
        svc.rebuild(IndexRequest(rebuild_all=True, batch=2), cancelled=cancelled)
    assert "last_rebuild_at" not in env.meta("listserv_search_meta")
    assert svc.status().fts5.status.is_stale

    result = svc.rebuild(IndexRequest(rebuild_all=True, resume=True))
    assert result.fts5_processed >= 1 and result.vector_processed == 5
    assert not svc.status().fts5.status.is_stale
    assert not svc.status().vector.status.is_stale
    assert "last_rebuild_at" in env.meta("vec_listserv_meta")


def test_vector_failure_preserves_source_and_does_not_stamp(env: Env) -> None:
    env.add(_msg(1), _msg(2))
    svc = env.index()
    env.embedder.fail = True
    with pytest.raises(RuntimeError):
        svc.rebuild(IndexRequest(rebuild_embeddings=True))
    assert len(env.urls()) == 2
    meta = env.meta("vec_listserv_meta")
    assert "last_rebuild_at" not in meta and "last_indexed_at" not in meta
    assert svc.status().vector.status.is_stale


def test_reset_for_rebuild_clears_completion_timestamps(env: Env) -> None:
    env.add(_msg(1))
    svc = env.index()
    svc.rebuild(IndexRequest(rebuild_embeddings=True))
    meta = env.meta("vec_listserv_meta")
    assert "last_rebuild_at" in meta and "last_indexed_at" in meta
    env.vec.reset_for_rebuild(DIM, env.embedder.model_name)
    meta = env.meta("vec_listserv_meta")
    assert "last_rebuild_at" not in meta and "last_indexed_at" not in meta


def test_model_and_chunk_setting_changes_mark_vector_stale(env: Env) -> None:
    msg = _msg(1)
    env.add(msg)
    env.repo.save_detail(_detail(msg, "word " * 400))
    svc = env.index()
    svc.rebuild(IndexRequest(rebuild_embeddings=True))
    assert not svc.status().vector.status.is_stale

    small = env.settings.model_copy(
        update={
            "semantic_search": env.settings.semantic_search.model_copy(
                update={"chunk_size": 200, "chunk_overlap": 0}
            )
        }
    )
    svc_small = _index_module().ListservIndexService(
        source_repository=env.repo,
        fts_repository=env.fts,
        semantic_service=env.semantic,
        settings=small,
    )
    assert svc_small.status().vector.status.is_stale

    other = _Embedder()
    other.model_name = "other-model"
    vec2 = SQLAlchemyListservVectorRepository(
        expected_model=other.model_name,
        expected_dim=other.dim,
        chunk_settings=env.settings.semantic_search,
    )
    sem2 = ListservSemanticService(
        embedder=other,
        vector_repo=vec2,
        source_repository=env.repo,
        settings=env.settings,
    )
    svc2 = _index_module().ListservIndexService(
        source_repository=env.repo,
        fts_repository=env.fts,
        semantic_service=sem2,
        settings=env.settings,
    )
    assert svc2.status().vector.status.is_stale
    svc2.rebuild(IndexRequest(rebuild_embeddings=True))
    assert not svc2.status().vector.status.is_stale
    assert env.meta("vec_listserv_meta")["embedding_model"] == "other-model"


def test_empty_database_rebuild_is_clean(env: Env) -> None:
    svc = env.index()
    assert svc.rebuild(IndexRequest(rebuild_all=True)) == IndexRebuildResult(0, 0)
    assert not svc.status().fts5.status.is_stale
    assert not svc.status().vector.status.is_stale



def test_rebuild_uses_progress_callback_even_when_quiet(env: Env) -> None:
    from doc3gpp.models.index import IndexRequest

    env.add(_msg(1), _msg(2))
    service = env.index(semantic=False)
    progress: list[str] = []

    result = service.rebuild(
        IndexRequest(rebuild=True, batch=1),
        quiet=True,
        on_progress=progress.append,
    )

    assert result.fts5_processed == 2
    assert progress == ["listserv fts5: 1/2", "listserv fts5: 2/2"]


def test_unavailable_vector_component_is_reported(env: Env) -> None:
    svc = env.index(semantic=False)
    status = svc.status()
    assert status.vector.available is False
    assert status.fts5.available is True


class _StubIndex:
    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.failures = failures or {}

    def update_records(self, records, *, operation):
        self.calls.append((operation, sorted(r.message.url for r in records)))
        return dict(self.failures)


def _run_sync(env: Env, index, messages, *, commit_fails=False):
    from doc3gpp.services import listserv_sync as mod

    repo = env.repo
    if commit_fails:
        real = env.repo

        class _Repo:
            def __getattr__(self, name):
                return getattr(real, name)

            def commit_page(self, *a, **k):
                raise RuntimeError("commit exploded")

        repo = _Repo()

    class _Source:
        def fetch(self, url):
            return FetchedPage(text="", content_type="text/html", final_url=url)

    service = mod.ListservSyncService(
        repository=repo,
        source=_Source(),
        max_workers=1,
        today_fn=lambda: date(2026, 10, 1),
        index_service=index,
    )
    with (
        patch.object(mod, "parse_archive_index", return_value=[PAGE]),
        patch.object(
            mod.ListservSyncService,
            "_fetch_and_parse_page",
            staticmethod(lambda source, page: (messages, [])),
        ),
    ):
        return service.sync(LIST_ID, dates=["= 2026-10-01"], force=True)


def test_sync_calls_update_records_only_after_committed_source(env: Env) -> None:
    stub = _StubIndex()
    messages = [_msg(1), _msg(2)]
    result = _run_sync(env, stub, messages)
    assert result.processed == 1 and not result.failures
    assert stub.calls == [("sync", sorted(m.url for m in messages))]

    stub = _StubIndex()
    result = _run_sync(env, stub, messages, commit_fails=True)
    assert result.failures and stub.calls == []


def test_sync_reports_index_failures_without_failing_source_commit(env: Env) -> None:
    msg = _msg(1)
    stub = _StubIndex({msg.url: "FTS5 index update failed"})
    result = _run_sync(env, stub, [msg])
    assert result.processed == 1
    assert result.failures == {msg.url: "FTS5 index update failed"}
    assert env.repo.get(msg.url) is not None


def test_parse_calls_update_records_after_detail_commit(env: Env) -> None:
    from doc3gpp.services import listserv_parse as mod

    msg = _msg(1)
    env.add(msg)
    stub = _StubIndex({msg.url: "vector index update failed"})

    class _Source:
        def fetch(self, url):
            return FetchedPage(text="x", content_type="text/html", final_url=url)

    service = mod.ListservParseService(
        repository=env.repo,
        source=_Source(),
        max_workers=1,
        settings=env.settings,
        index_service=stub,
    )
    with patch.object(
        mod, "parse_detail_page", return_value=(None, (), "parsed body")
    ):
        result = service.parse(LIST_ID)
    assert result.processed == 1
    assert stub.calls == [("parse", [msg.url])]
    assert result.failures == {msg.url: "vector index update failed"}
    record = env.repo.get(msg.url)
    assert record is not None and record.detail is not None


class _ProbeEmbedder:
    """Embedder whose live-dimension and encode paths must not run for status."""

    model_name = "index-test-model"

    def __init__(self) -> None:
        self.dim_reads = 0
        self.encode_calls = 0

    @property
    def dim(self) -> int:
        self.dim_reads += 1
        raise AssertionError("status must not resolve the live embedding dimension")

    def encode(self, texts):
        self.encode_calls += 1
        raise AssertionError("status must not call the embedding endpoint")


def test_status_metadata_factory_does_not_initialize_dimension_or_probe_embedder(sqlite_env) -> None:
    from doc3gpp.services.factory import build_listserv_index_service

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )
    seeded = Env(settings)
    seeded.add(_msg(1))
    seeded.index().rebuild(IndexRequest(rebuild_embeddings=True))
    seeded.add(_msg(1, subject="Changed after indexing"))

    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM vec_listserv_meta WHERE key = 'embedding_dim'")
        )
    before_meta = seeded.meta("vec_listserv_meta")
    assert "embedding_dim" not in before_meta

    probe = _ProbeEmbedder()
    svc = build_listserv_index_service(settings, embedder=probe)
    status = svc.status()
    second_status = svc.status()

    assert seeded.meta("vec_listserv_meta") == before_meta
    assert (probe.dim_reads, probe.encode_calls) == (0, 0)
    for result in (status, second_status):
        assert result.vector.available is True
        assert result.vector.status.is_stale is True
        assert result.vector.status.embedding_model == "index-test-model"
        assert result.vector.status.embedding_dim == DIM

    rebuild_service = build_listserv_index_service(settings, embedder=seeded.embedder)
    result = rebuild_service.rebuild(IndexRequest(rebuild_embeddings=True))
    assert result.vector_processed == 1
    assert seeded.meta("vec_listserv_meta")["embedding_dim"] == str(DIM)


def test_status_metadata_factory_preserves_existing_dimension_and_model(sqlite_env) -> None:
    from doc3gpp.services.factory import build_listserv_index_service

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )
    seeded = Env(settings)
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vec_listserv_meta (key, value) "
                "VALUES ('embedding_dim', '768') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        )
        conn.execute(
            text(
                "INSERT INTO vec_listserv_meta (key, value) "
                "VALUES ('embedding_model', 'preserved-model') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        )
    before_meta = seeded.meta("vec_listserv_meta")

    svc = build_listserv_index_service(settings, embedder=_ProbeEmbedder())
    status = svc.status()

    assert seeded.meta("vec_listserv_meta") == before_meta
    assert status.vector.status.embedding_dim == 768
    assert status.vector.status.embedding_model == "preserved-model"


def test_status_metadata_propagates_unexpected_vector_repository_error(sqlite_env, monkeypatch) -> None:
    from doc3gpp.services.factory import build_listserv_index_service

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )
    Env(settings)

    def fail_indexed_revisions(_self):
        raise RuntimeError("unexpected vector status failure")

    monkeypatch.setattr(
        SQLAlchemyListservVectorRepository, "indexed_revisions", fail_indexed_revisions
    )
    svc = build_listserv_index_service(settings, embedder=_ProbeEmbedder())

    with pytest.raises(RuntimeError, match="unexpected vector status failure"):
        svc.status()



def test_semantic_builder_closes_owned_embedder_once_and_preserves_borrowed(sqlite_env, monkeypatch) -> None:
    from doc3gpp.services import factory, listserv_semantic
    from doc3gpp.services.listserv_semantic import build_listserv_semantic_service

    class ProbeEmbedder:
        model_name = "close-probe"
        dim = DIM

        def __init__(self) -> None:
            self.close_calls = 0

        def encode(self, texts):
            return [np.zeros(DIM, dtype=np.float32) for _ in texts]

        def close(self) -> None:
            self.close_calls += 1

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )
    owned = ProbeEmbedder()
    monkeypatch.setattr(factory, "build_embedder", lambda _settings: owned)
    monkeypatch.setattr(
        listserv_semantic,
        "SQLAlchemyListservVectorRepository",
        lambda **_kwargs: object(),
    )

    service = build_listserv_semantic_service(settings)
    assert service is not None
    close = getattr(service, "close", None)
    assert callable(close), "semantic service must expose deterministic cleanup"
    close()
    close()
    assert owned.close_calls == 1

    borrowed = ProbeEmbedder()
    borrowed_service = build_listserv_semantic_service(
        settings, embedder=borrowed, vector_repo=object()
    )
    assert borrowed_service is not None
    borrowed_close = getattr(borrowed_service, "close", None)
    assert callable(borrowed_close)
    borrowed_close()
    borrowed_close()
    assert borrowed.close_calls == 0


@pytest.mark.parametrize("failure", ["dimension", "vector", "unexpected"])
def test_semantic_builder_closes_owned_embedder_on_construction_failure(
    sqlite_env, monkeypatch, failure: str
) -> None:
    from doc3gpp.models.semantic_search import (
        EmbedderUnavailableError,
        VectorIndexUnavailableError,
    )
    from doc3gpp.services import factory, listserv_semantic
    from doc3gpp.services.listserv_semantic import build_listserv_semantic_service

    class ProbeEmbedder:
        model_name = "close-probe"

        def __init__(self) -> None:
            self.close_calls = 0

        @property
        def dim(self) -> int:
            if failure == "dimension":
                raise EmbedderUnavailableError("dimension unavailable")
            return DIM

        def encode(self, texts):
            return [np.zeros(DIM, dtype=np.float32) for _ in texts]

        def close(self) -> None:
            self.close_calls += 1

    probe = ProbeEmbedder()
    monkeypatch.setattr(factory, "build_embedder", lambda _settings: probe)

    def fail_vector_repo(**_kwargs):
        if failure == "vector":
            raise VectorIndexUnavailableError("vector unavailable")
        raise RuntimeError("unexpected construction failure")

    monkeypatch.setattr(
        listserv_semantic,
        "SQLAlchemyListservVectorRepository",
        fail_vector_repo,
    )
    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )

    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="unexpected construction failure"):
            build_listserv_semantic_service(settings)
    else:
        assert build_listserv_semantic_service(settings) is None
    assert probe.close_calls == 1


def test_search_and_index_close_only_owned_lazy_semantic_services(sqlite_env) -> None:
    from types import SimpleNamespace

    from doc3gpp.services.listserv_index import ListservIndexService
    from doc3gpp.services.listserv_search import ListservSearchService

    settings = Settings(
        semantic_search=SemanticSearchSettings(enabled=True),
        listserv={"auto_index_on_sync": False, "auto_embed_on_sync": True},
    )
    env = Env(settings)
    env.add(_msg(1))
    record = env.records()[0]

    class SemanticProbe:
        def __init__(self) -> None:
            self.close_calls = 0
            self.indexed: list[object] = []
            self.vector_repository = SimpleNamespace(indexed_revisions=dict)

        def search(self, **_kwargs):
            return []

        def index_record(self, record, **_kwargs):
            self.indexed.append(record)

        def close(self) -> None:
            self.close_calls += 1

    search_builds = 0
    search_child = SemanticProbe()

    def build_search_child():
        nonlocal search_builds
        search_builds += 1
        return search_child

    search = ListservSearchService(
        repository=env.repo,
        semantic_factory=build_search_child,
        settings=settings,
    )
    search_close = getattr(search, "close", None)
    assert callable(search_close), "search service must expose deterministic cleanup"
    search_close()
    assert search_builds == 0, "closing must not force lazy semantic construction"
    assert search.search(semantic="query") == []
    search_close()
    search_close()
    assert search_builds == 1 and search_child.close_calls == 1

    index_builds = 0
    index_child = SemanticProbe()

    def build_index_child():
        nonlocal index_builds
        index_builds += 1
        return index_child

    index = ListservIndexService(
        source_repository=env.repo,
        semantic_factory=build_index_child,
        settings=settings,
    )
    index_close = getattr(index, "close", None)
    assert callable(index_close), "index service must expose deterministic cleanup"
    index_close()
    assert index_builds == 0, "closing must not force lazy semantic construction"
    index.update_records([record], operation="sync")
    index_close()
    index_close()
    assert index_builds == 1 and index_child.close_calls == 1
    assert index_child.indexed == [record]

    borrowed_search_child = SemanticProbe()
    borrowed_search = ListservSearchService(
        repository=env.repo,
        semantic_service=borrowed_search_child,
        settings=settings,
    )
    borrowed_search.close()
    assert borrowed_search_child.close_calls == 0

    borrowed_index_child = SemanticProbe()
    borrowed_index = ListservIndexService(
        source_repository=env.repo,
        semantic_service=borrowed_index_child,
        settings=settings,
    )
    borrowed_index.close()
    assert borrowed_index_child.close_calls == 0


@pytest.mark.parametrize(
    "builder_name", ["build_listserv_sync_service", "build_listserv_parse_service"]
)
def test_batch_service_builders_own_index_and_forward_shared_embedder(
    sqlite_env, monkeypatch, builder_name: str
) -> None:
    from doc3gpp.services import factory

    class CloseProbe:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    repository = object()
    source = CloseProbe()
    index = CloseProbe()
    provider = lambda: object()
    received = []
    monkeypatch.setattr(factory, "SQLAlchemyListservRepository", lambda: repository)
    monkeypatch.setattr(factory, "ListservSource", lambda **_kwargs: source)

    def build_index(_settings, *, embedder=None, embedder_factory=None):
        received.append((embedder, embedder_factory))
        return index

    monkeypatch.setattr(factory, "build_listserv_index_service", build_index)
    settings = Settings()
    service = getattr(factory, builder_name)(settings, embedder_factory=provider)
    close = getattr(service, "close", None)
    assert callable(close), "batch service must expose deterministic cleanup"
    close()
    close()

    assert received == [(None, provider)]
    assert source.close_calls == 1
    assert index.close_calls == 1



def test_explicit_none_embedder_factory_never_builds_independent_client(
    sqlite_env, monkeypatch
) -> None:
    from doc3gpp.models.semantic_search import SemanticSearchUnavailableError
    from doc3gpp.services import factory
    from doc3gpp.services.factory import build_listserv_search_service

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )
    built: list[bool] = []
    provider_calls: list[bool] = []
    monkeypatch.setattr(factory, "build_embedder", lambda _settings: built.append(True))

    def no_embedder():
        provider_calls.append(True)

    service = build_listserv_search_service(settings, embedder_factory=no_embedder)
    with pytest.raises(SemanticSearchUnavailableError):
        service.search(semantic="query")
    assert provider_calls == [True]
    assert built == []


def test_index_status_does_not_resolve_embedder_factory(sqlite_env) -> None:
    from doc3gpp.services.factory import build_listserv_index_service

    settings = Settings(
        semantic_search=SemanticSearchSettings(
            enabled=True, embedding_base_url="http://embeddings.invalid/v1"
        )
    )
    Env(settings)
    provider_calls: list[bool] = []
    service = build_listserv_index_service(
        settings, embedder_factory=lambda: provider_calls.append(True)
    )

    service.status()
    service.status()

    assert provider_calls == []


@pytest.mark.parametrize("service_name", ["sync", "parse"])
def test_batch_service_direct_index_injection_is_borrowed_and_close_is_idempotent(
    service_name: str
) -> None:
    from doc3gpp.services.listserv_parse import ListservParseService
    from doc3gpp.services.listserv_sync import ListservSyncService

    class CloseProbe:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    source = CloseProbe()
    borrowed_index = CloseProbe()
    service_type = ListservSyncService if service_name == "sync" else ListservParseService
    service = service_type(
        repository=object(), source=source, index_service=borrowed_index
    )
    close = getattr(service, "close", None)
    assert callable(close), "batch service must expose deterministic cleanup"
    close()
    close()

    assert source.close_calls == 1
    assert borrowed_index.close_calls == 0


def test_index_semantic_factory_is_initialized_once_under_concurrent_access() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeoutError
    from threading import Event

    from doc3gpp.services.listserv_index import ListservIndexService

    factory_started = Event()
    allow_factory_to_finish = Event()
    second_caller_started = Event()
    child = object()
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        factory_started.set()
        assert allow_factory_to_finish.wait(timeout=5)
        return child

    service = ListservIndexService(
        source_repository=object(), semantic_factory=factory
    )

    def second_call():
        second_caller_started.set()
        return service._get_semantic()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service._get_semantic)
        second = None
        try:
            assert factory_started.wait(timeout=2)
            second = executor.submit(second_call)
            assert second_caller_started.wait(timeout=2)
            with pytest.raises(FuturesTimeoutError):
                second.result(timeout=0.1)
        finally:
            allow_factory_to_finish.set()

        assert first.result(timeout=2) is child
        assert second is not None and second.result(timeout=2) is child

    assert factory_calls == 1
