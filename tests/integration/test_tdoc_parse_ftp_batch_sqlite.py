"""Integration tests for ``tdoc parse --from-url`` 3GPP FTP folder batch."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
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

    def __enter__(self) -> "_DummyScraperClient":
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
    dummy.payloads[f"{root}R5s260009.zip"] = fixture.read_bytes()
    dummy.payloads[f"{root}R5s260043.zip"] = fixture.read_bytes()
    monkeypatch.setattr("doc3gpp.services.factory.ScraperClient", lambda: dummy)

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--from-url", root])
    assert result.exit_code == 0, result.output

    cr_repo = SQLAlchemyTDocCrRepository()
    assert len(cr_repo.get("R5s260009")) == 1
    assert len(cr_repo.get_extract_meta("R5s260009")) == 1
    assert len(cr_repo.get("R5s260043")) == 0

    combined = (result.stdout or "") + (result.stderr or "")
    assert "R5s260043" in combined
    assert "not present in the 'tdocs' table" in combined


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
