"""Integration tests for ``tdoc parse --from-url`` 3GPP FTP folder batch."""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner
from typing_extensions import Self

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_extraction_sql import (
    SQLAlchemyTDocExtractionRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


class _DummyScraperClient:
    """In-memory scraper returning canned text listings and binary payloads."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.texts: dict[str, str] = {}
        self.payloads: dict[str, bytes] = {}

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        if url in self.texts:
            return self.texts[url]
        raise RuntimeError(f"No canned text for {url}")

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.payloads:
            return self.payloads[url]
        raise RuntimeError(f"No canned payload for {url}")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def close(self) -> None:
        return None


def _folder_html(*entries: str) -> str:
    return f"<html><body>{''.join(entries)}</body></html>"


def _zip_payload(fixture: Path, inner_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, fixture.read_bytes())
    return buf.getvalue()


def _build_multifile_zip(main_zip: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(main_zip)) as source:
        main_docx = source.read(next(
            name for name in source.namelist() if name.lower().endswith(".docx")
        ))
    from docx import Document

    def support_docx(text: str) -> bytes:
        document = Document()
        document.add_paragraph(text)
        payload = io.BytesIO()
        document.save(payload)
        return payload.getvalue()

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("a/support-alpha.docx", support_docx("FTP support alpha"))
        archive.writestr("b/support-beta.docx", support_docx("FTP support beta"))
        archive.writestr("z/R5s260009.docx", main_docx)
    return payload.getvalue()


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("docx"),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_url_batch_persists_fk_hits_and_warns_on_miss(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """Folder batch writes DB rows for ids in tdocs and warns for missing ids."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    assert fixture.exists()

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    root = "https://www.3gpp.org/ftp/Docs/"
    dummy = _DummyScraperClient()
    dummy.texts[root] = _folder_html(
        f'<a class="file" href="{root}R5s260009.zip">R5s260009.zip</a>',
        f'<a class="file" href="{root}R5s260043.zip">R5s260043.zip</a>',
    )
    dummy.payloads[f"{root}R5s260009.zip"] = _build_multifile_zip(
        fixture.read_bytes()
    )
    dummy.payloads[f"{root}R5s260043.zip"] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--from-url", root])
    assert result.exit_code == 0, result.output

    cr_repo = SQLAlchemyTDocCrRepository()
    assert len(cr_repo.get("R5s260009")) == 1
    metadata = cr_repo.get_extract_meta("R5s260009")
    assert len(metadata) == 1
    assert metadata[0].extraction_version == 2
    assert metadata[0].document_count == 3
    documents = SQLAlchemyTDocExtractionRepository().list_documents(
        metadata[0].ftp_url
    )
    assert [document.source_file for document in documents] == [
        "z/R5s260009.docx",
        "a/support-alpha.docx",
        "b/support-beta.docx",
    ]
    assert "FTP support alpha" in documents[1].markdown_text
    assert "FTP support beta" in documents[2].markdown_text
    assert len(cr_repo.get("R5s260043")) == 0

    combined = (result.stdout or "") + (result.stderr or "")
    assert "R5s260043" in combined
    assert "not present in the 'tdocs' table" in combined


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("docx"),
    reason="python-docx not installed",
)
def test_url_batch_prepares_concurrently_and_emits_discovery_order(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """FTP preparation overlaps while CLI emission follows URL discovery."""
    create_schema()
    from doc3gpp.settings.loader import get_settings

    monkeypatch.setenv("DOC3GPP_PIPELINE__PREPARE_WORKERS", "2")
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    get_settings.cache_clear()

    first_fixture = FIXTURES_DIR / "R5s260009.zip"
    second_fixture = FIXTURES_DIR / "R5s260051.zip"
    first_url = "https://www.3gpp.org/ftp/Docs/R5s260009.zip"
    second_url = "https://www.3gpp.org/ftp/Docs/R5s260051.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260051", type="CR"))

    root = "https://www.3gpp.org/ftp/Docs/"
    dummy = _DummyScraperClient()
    dummy.texts[root] = _folder_html(
        f'<a class="file" href="{first_url}">R5s260009.zip</a>',
        f'<a class="file" href="{second_url}">R5s260051.zip</a>',
    )
    dummy.payloads[first_url] = first_fixture.read_bytes()
    dummy.payloads[second_url] = second_fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    started = threading.Barrier(2)
    second_finished = threading.Event()
    active = 0
    active_lock = threading.Lock()
    preparations_overlapped = False
    finished: list[str] = []
    original_prepare = TDocCrService.prepare_from_url

    def prepare(self, url: str, **kwargs):
        nonlocal active, preparations_overlapped
        with active_lock:
            active += 1
            if active > 1:
                preparations_overlapped = True
        try:
            started.wait(timeout=2)
            if url == first_url:
                second_finished.wait(timeout=2)
                result = original_prepare(self, url, **kwargs)
            else:
                result = original_prepare(self, url, **kwargs)
                second_finished.set()
            finished.append(url)
            return result
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(TDocCrService, "prepare_from_url", prepare)

    import doc3gpp.cli as cli_module

    emitted: list[str] = []
    original_emit = cli_module._emit_record

    def emit(record, fmt, output, *, compact=False):
        emitted.append(record.tdoc_id)
        return original_emit(record, fmt, output, compact=compact)

    monkeypatch.setattr(cli_module, "_emit_record", emit)
    output = tmp_path / "out"
    result = CliRunner().invoke(
        app,
        [
            "tdoc", "parse", "--from-url", root,
            "--format", "table", "--output", str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert preparations_overlapped
    assert finished == [second_url, first_url]
    assert emitted == ["R5s260009", "R5s260051"]
    assert (output / "docs" / "r5s260009.tsv").exists()
    assert (output / "docs" / "r5s260051.tsv").exists()


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("docx"),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_url_batch_recursive_respects_max_depth(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """--recursive with max depth stops at the configured level."""
    create_schema()

    fixture = FIXTURES_DIR / "R5s260009.zip"
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    root = "https://www.3gpp.org/ftp/Docs/"
    sub = f"{root}sub/"
    subsub = f"{sub}subsub/"

    dummy = _DummyScraperClient()
    dummy.texts[root] = _folder_html(
        '<a href="sub/">sub/</a>',
    )
    dummy.texts[sub] = _folder_html(
        f'<a class="file" href="{sub}R5s260009.zip">R5s260009.zip</a>',
        '<a href="subsub/">subsub/</a>',
    )
    dummy.texts[subsub] = _folder_html(
        f'<a class="file" href="{subsub}R5s260009.zip">R5s260009.zip</a>',
    )
    dummy.payloads[f"{sub}R5s260009.zip"] = fixture.read_bytes()
    dummy.payloads[f"{subsub}R5s260009.zip"] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(tmp_path / "cache"))
    from doc3gpp.settings.loader import get_settings
    get_settings.cache_clear()

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "parse", "--from-url", root, "--recursive", "--max-depth", "1"]
    )
    assert result.exit_code == 0, result.output

    cr_repo = SQLAlchemyTDocCrRepository()
    assert len(cr_repo.get("R5s260009")) == 1
    assert subsub not in dummy.calls
