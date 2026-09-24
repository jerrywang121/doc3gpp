from datetime import datetime, timezone
from unittest.mock import MagicMock

from doc3gpp.models.spec_doc import ChunkDraft, SpecDocChunk, SpecDocSource
from doc3gpp.settings.schema import Settings, SpecDocSettings
from doc3gpp.services.spec_doc_service import SpecDocService


def test_get_source_delegates_to_specdata_repo() -> None:
    repo = MagicMock()
    expected = SpecDocSource(
        "38.331",
        "18.5.0",
        parsed_at=datetime.now(timezone.utc),
    )
    repo.get_source.return_value = expected
    service = SpecDocService(spec_repo=MagicMock(), doc_repo=repo)

    assert service.get_source("38.331", "18.5.0") is expected
    repo.get_source.assert_called_once_with("38.331", "18.5.0")


def test_spec_doc_service_uses_dedicated_cache_root(tmp_path) -> None:
    cache_dir = tmp_path / "specs"
    service = SpecDocService(
        spec_repo=MagicMock(),
        settings=Settings(spec_doc=SpecDocSettings(cache_dir=cache_dir)),
    )

    assert service._zip_path("36.508", "19.2.0") == (
        cache_dir / "zips" / "36.508" / "19.2.0.zip"
    )
    assert service._markdown_dir("36.508", "19.2.0") == (
        cache_dir / "markdown" / "36.508" / "19.2.0"
    )
    assert "tdocs" not in str(service._zip_path("36.508", "19.2.0"))


def test_list_chunks_forwards_version_filters_and_paging() -> None:
    repo = MagicMock()
    expected = [
        SpecDocChunk(
            file_order=0,
            source_file="a.docx",
            chunk_id="38.331@18.5.0#0",
        )
    ]
    repo.list_chunks.return_value = expected
    service = SpecDocService(spec_repo=MagicMock(), doc_repo=repo)

    assert service.list_chunks(
        "38.331",
        version="18.5.0",
        release="Rel-18",
        sections="%5.1%",
        tables="%UE%",
        limit=21,
        offset=40,
    ) == expected
    repo.list_chunks.assert_called_once_with(
        "38.331",
        version="18.5.0",
        release="Rel-18",
        sections="%5.1%",
        tables="%UE%",
        limit=21,
        offset=40,
    )


def test_embedding_text_includes_chunk_metadata_before_body() -> None:
    semantic = MagicMock(spec=["upsert_for_version"])
    embedder = MagicMock()
    embedder.encode.return_value = [[0.1]]
    settings = Settings(spec_doc=SpecDocSettings(auto_embed_on_parse=True))
    service = SpecDocService(
        spec_repo=MagicMock(),
        semantic_service=semantic,
        embedder=embedder,
        settings=settings,
    )

    service._embed_after_parse(
        "36.508",
        "19.2.0",
        [
            ChunkDraft(
                file_order=0,
                source_file="36.508.docx",
                sections="5 Scope",
                tables="Table 1 Values",
                text="chunk body",
            )
        ],
    )

    embedder.encode.assert_called_once_with(["5 Scope\nTable 1 Values\nchunk body"])
