import io
import zipfile
from unittest.mock import MagicMock

from docx import Document

from doc3gpp.models.spec import SpecVersion
from doc3gpp.models.spec_doc import SpecDocSource
from doc3gpp.services.spec_doc_service import SpecDocService


def _zip_bytes() -> bytes:
    doc = Document()
    doc.add_heading("1 Scope", level=1)
    doc.add_paragraph("prepared body")
    doc_buffer = io.BytesIO()
    doc.save(doc_buffer)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("38331-j30.docx", doc_buffer.getvalue())
    return zip_buffer.getvalue()


def _service(repo, tmp_path):
    spec_repo = MagicMock()
    spec_repo.list_versions.return_value = [
        SpecVersion("38.331", "18.5.0", "https://example.test/doc.zip", "Rel-18")
    ]
    return SpecDocService(
        spec_repo=spec_repo,
        doc_repo=repo,
        fetcher=lambda _: _zip_bytes(),
        cache_dir=tmp_path / "spec-cache",
    )


def test_prepare_parse_does_not_mutate_repository(tmp_path):
    repo = MagicMock()
    repo.get_source.return_value = None
    service = _service(repo, tmp_path)

    prepared = service.prepare_parse("38.331")

    assert prepared.source.version == "18.5.0"
    assert prepared.toc.entries[0].section_no == "1"
    assert prepared.drafts
    repo.record_download.assert_not_called()
    repo.upsert_toc.assert_not_called()
    repo.replace_chunks.assert_not_called()
    repo.record_parsed.assert_not_called()


def test_persist_prepared_parse_writes_and_returns_source(tmp_path):
    repo = MagicMock()
    source = SpecDocSource("38.331", "18.5.0", parsed_at=object())
    repo.get_source.side_effect = [None, source]
    service = _service(repo, tmp_path)

    prepared = service.prepare_parse("38.331")
    result = service.persist_prepared_parse(prepared)

    assert result is source
    repo.record_download.assert_called_once()
    repo.upsert_toc.assert_called_once()
    repo.replace_chunks.assert_called_once()
    repo.record_parsed.assert_called_once_with("38.331", "18.5.0", chunk_count=1)
