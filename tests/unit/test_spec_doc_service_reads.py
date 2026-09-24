from datetime import datetime, timezone
from unittest.mock import MagicMock

from doc3gpp.models.spec_doc import SpecDocChunk, SpecDocSource
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
        section="%5.1%",
        limit=21,
        offset=40,
    ) == expected
    repo.list_chunks.assert_called_once_with(
        "38.331",
        version="18.5.0",
        release="Rel-18",
        section="%5.1%",
        limit=21,
        offset=40,
    )
