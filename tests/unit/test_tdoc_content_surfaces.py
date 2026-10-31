"""Raw TDoc content on the CLI, HTTP, and MCP surfaces (spec section 9)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from mcp.shared.exceptions import MCPError
from typer.testing import CliRunner

from doc3gpp.cli import app as cli_app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name
from doc3gpp.parsers.normalizers import normalize_ftp_path
from doc3gpp.parsers.tdoc_zip import (
    TDocZipLimitError,
    TDocZipValidationError,
    build_markdown_archive,
    document_id_for,
)
from doc3gpp.scraping.cache_keys import derive_cache_file
from doc3gpp.scraping.tdoc_zip_source import TDocTooLargeError
from doc3gpp.services.tdoc_errors import TDocNotYetOnFTPError
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import (
    CacheSettings,
    MCPSettings,
    ServerSettings,
    Settings,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_content_sql import (
    SQLAlchemyTDocContentRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.web.app import build_app, build_state
from doc3gpp.web.errors import MCP_CODE_CACHE_MISS, MCP_CODE_NOT_FOUND
from doc3gpp.web.mcp_server import build_mcp_server

STORES = {store.key: store for store in TDOC_CONTENT_STORES}
WID_TYPE = "WID new"
FAMILY_MAIN = "CRs/R5-000003.docx"
FAMILY_ANNEX = "annex/Ünïcode name > x.docx"
CR_MAIN = "CR/main.docx"
CR_ANNEX = "Annex/support.docx"
BROKEN_SOURCE = "annex/broken.doc"
INELIGIBLE_MESSAGE = (
    "TDoc 'R5-260102' has type 'other'; raw Markdown is available for CR, WID, LS, "
    "and discussion TDocs"
)
_BOX_DRAWING = re.compile(r"[│╭╮╰╯─]")


def _plain(output: str) -> str:
    return " ".join(_BOX_DRAWING.sub(" ", output).split())


@pytest.fixture(autouse=True)
def _no_auto_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "doc3gpp.cli.trigger_auto_sync", MagicMock(return_value=(0, 0))
    )


@pytest.fixture(autouse=True)
def _refuse_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("raw content read must not download the ZIP")

    monkeypatch.setattr("doc3gpp.services.tdoc_cr_service.download_tdoc_zip", refuse)
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_family_service.download_tdoc_zip", refuse
    )


@pytest.fixture()
def cache_dir(sqlite_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    monkeypatch.setenv("DOC3GPP_CACHE__DIR", str(path))
    get_settings.cache_clear()
    create_schema()
    return path


@pytest.fixture()
def web_app(cache_dir: Path):
    settings = Settings(
        server=ServerSettings(enabled=True),
        mcp=MCPSettings(enabled=True),
        cache=CacheSettings(dir=cache_dir),
    )
    return build_app(settings)


@pytest.fixture()
def mcp_server(cache_dir: Path):
    return build_mcp_server(build_state(get_settings()))


@pytest.fixture()
def no_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoExtraction:
        def extract(self, tdoc_id: str, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _NoExtraction(),
    )


def _document(
    ftp_url: str,
    source_file: str,
    markdown: str,
    *,
    file_order: int = 0,
) -> TDocDocument:
    document_id = document_id_for(ftp_url, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )


def _seed_family(
    tdoc_id: str,
    ftp_url: str,
    sources: tuple[tuple[str, str], ...],
    *,
    tdoc_type: str = WID_TYPE,
    skipped_sources: tuple[str, ...] = (),
) -> None:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type=tdoc_type, ftp_url=ftp_url)
    )
    documents = tuple(
        _document(ftp_url, source, markdown) for source, markdown in sources
    )
    meta = TDocExtractMeta(
        ftp_url=ftp_url,
        tdoc_id=tdoc_id,
        cache_file=derive_cache_file(ftp_url),
        doc_filename=sources[0][0],
        extraction_version=2,
        document_count=len(documents),
        skipped_sources=skipped_sources,
    )
    SQLAlchemyTDocContentRepository().replace_family_bundle(
        STORES["wid"], meta, documents
    )


def _seed_cr(
    tdoc_id: str,
    ftp_url: str,
    cache_dir: Path,
    *,
    write_archive: bool = True,
) -> None:
    normalized = normalize_ftp_path(ftp_url)
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url))
    main = _document(normalized, CR_MAIN, "# Archive main\n")
    annex = _document(normalized, CR_ANNEX, "# SQL annex\n", file_order=1)
    cache_file = derive_cache_file(ftp_url)
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        TDocCRDetails(tdoc_id=tdoc_id, ftp_url=ftp_url, title="Stored CR"),
        None,
        None,
        TDocExtractMeta(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            cache_file=cache_file,
            doc_filename=CR_MAIN,
            document_count=1,
        ),
        (annex,),
    )
    if write_archive:
        archive = build_markdown_archive(
            (main, annex),
            ftp_url=normalized,
            main_source_file=CR_MAIN,
        )
        path = cache_dir / "markdown" / cache_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive)


def _family_sources() -> tuple[tuple[str, str], ...]:
    return (
        (FAMILY_MAIN, "# Main body\n"),
        (FAMILY_ANNEX, "# Annex body\n"),
    )


def _mcp_call(server, args: dict) -> str:
    async def run():
        return await server.call_tool("get_tdoc_content", args)

    result = asyncio.run(run())
    assert result.is_error is False, result
    return result.content[0].text


def test_cli_raw_multi_document_named_blocks(sqlite_env, cache_dir) -> None:
    _seed_family("R5-260100", "r5/docs/r5-260100.zip", _family_sources())

    result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--tdoc", "R5-260100", "--format", "raw"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        '<!-- doc3gpp-source: "CRs/R5-000003.docx" -->\n'
        "# Main body\n"
        "\n"
        '<!-- doc3gpp-source: "annex/Ünïcode name \\u003e x.docx" -->\n'
        "# Annex body\n"
    )


def test_cli_raw_source_file_selects_one_document(sqlite_env, cache_dir) -> None:
    _seed_family("R5-260101", "r5/docs/r5-260101.zip", _family_sources())

    result = CliRunner().invoke(
        cli_app,
        [
            "tdoc", "show", "--tdoc", "R5-260101", "--format", "raw",
            "--source-file", FAMILY_ANNEX,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "# Annex body\n"



def test_cli_raw_output_is_byte_identical_with_and_without_inventory(
    sqlite_env, cache_dir,
) -> None:
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    tdoc_id = "R5-260180"
    ftp_url = "r5/docs/r5-260180.zip"
    _seed_family(tdoc_id, ftp_url, _family_sources())
    args = ["tdoc", "show", "--tdoc", tdoc_id, "--format", "raw"]

    without_inventory = CliRunner().invoke(cli_app, args)
    assert without_inventory.exit_code == 0, without_inventory.output

    SQLAlchemyTDocZipInventoryRepository().upsert(
        TDocZipInventory(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            zip_files=("metadata.docx", "support.pdf", "support.pdf"),
        )
    )
    with_inventory = CliRunner().invoke(cli_app, args)

    assert with_inventory.exit_code == 0, with_inventory.output
    assert with_inventory.stdout == without_inventory.stdout


def test_cli_raw_source_file_selection_remains_exact_with_inventory(
    sqlite_env, cache_dir,
) -> None:
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    tdoc_id = "R5-260181"
    ftp_url = "r5/docs/r5-260181.zip"
    _seed_family(tdoc_id, ftp_url, _family_sources())
    SQLAlchemyTDocZipInventoryRepository().upsert(
        TDocZipInventory(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            zip_files=(
                f"../../{FAMILY_ANNEX}",
                FAMILY_ANNEX,
                "support.pdf",
            ),
        )
    )

    result = CliRunner().invoke(
        cli_app,
        [
            "tdoc", "show", "--tdoc", tdoc_id, "--format", "raw",
            "--source-file", FAMILY_ANNEX,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "# Annex body\n"


def test_cli_raw_ineligible_type_error_names_type(sqlite_env, cache_dir) -> None:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260102", type="other", ftp_url="r5/docs/r5-260102.zip")
    )

    result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--tdoc", "R5-260102", "--format", "raw"]
    )

    assert result.exit_code != 0
    assert INELIGIBLE_MESSAGE in _plain(result.output)
    assert "CR-type" not in result.output


def test_cli_raw_incomplete_warns_with_skipped_sources(
    sqlite_env, cache_dir, no_extraction,
) -> None:
    _seed_family(
        "R5-260103",
        "r5/docs/r5-260103.zip",
        _family_sources(),
        skipped_sources=(BROKEN_SOURCE,),
    )

    result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--tdoc", "R5-260103", "--format", "raw"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith('<!-- doc3gpp-source: "CRs/R5-000003.docx" -->')
    assert "# Annex body" in result.stdout
    assert "Warning" in result.stderr
    assert BROKEN_SOURCE in result.stderr
    assert BROKEN_SOURCE not in result.stdout


def test_cli_raw_by_url_incomplete_warns_without_extraction(sqlite_env, cache_dir) -> None:
    url = "r5/docs/r5-260114.zip"
    _seed_family(
        "R5-260114",
        url,
        _family_sources(),
        skipped_sources=(BROKEN_SOURCE,),
    )

    result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--ftp-url", url, "--format", "raw"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith('<!-- doc3gpp-source: "CRs/R5-000003.docx" -->')
    assert "Warning" in result.stderr
    assert BROKEN_SOURCE in result.stderr


def test_cr_raw_uses_archive_main(sqlite_env, cache_dir) -> None:
    _seed_cr("R5-260104", "r5/docs/r5-260104.zip", cache_dir)

    result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--tdoc", "R5-260104", "--format", "raw"]
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        '<!-- doc3gpp-source: "CR/main.docx" -->\n'
        "# Archive main\n"
        "\n"
        '<!-- doc3gpp-source: "Annex/support.docx" -->\n'
        "# SQL annex\n"
    )


def test_cr_raw_without_archive_is_cache_miss_on_all_surfaces(
    sqlite_env, cache_dir, web_app, mcp_server, no_extraction,
) -> None:
    tdoc_id = "R5-260105"
    _seed_cr(tdoc_id, "r5/docs/r5-260105.zip", cache_dir, write_archive=False)
    hint = f"run: doc3gpp tdoc parse --tdoc {tdoc_id} --force"

    cli_result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--tdoc", tdoc_id, "--format", "raw"]
    )
    assert cli_result.exit_code != 0
    assert f"doc3gpp tdoc parse --tdoc {tdoc_id} --force" in _plain(cli_result.output)

    with TestClient(web_app) as client:
        http_response = client.get(f"/tdocs/{tdoc_id}/content?format=markdown")
    assert http_response.status_code == 404
    assert http_response.json()["error"] == "cache_miss"
    assert http_response.json()["hint"] == hint

    async def run():
        return await mcp_server.call_tool(
            "get_tdoc_content", {"tdoc_id": tdoc_id, "format": "markdown"}
        )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(run())
    assert exc_info.value.code == MCP_CODE_CACHE_MISS
    assert exc_info.value.data["hint"] == hint


def test_http_markdown_sets_incomplete_header(sqlite_env, web_app) -> None:
    _seed_family(
        "R5-260106",
        "r5/docs/r5-260106.zip",
        _family_sources(),
        skipped_sources=(BROKEN_SOURCE,),
    )

    with TestClient(web_app) as client:
        response = client.get("/tdocs/R5-260106/content?format=markdown")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["x-doc3gpp-extraction-complete"] == "false"
    assert "# Main body" in response.text


def test_http_complete_markdown_omits_incomplete_header(sqlite_env, web_app) -> None:
    _seed_family("R5-260107", "r5/docs/r5-260107.zip", _family_sources())

    with TestClient(web_app) as client:
        response = client.get("/tdocs/R5-260107/content?format=markdown")

    assert response.status_code == 200, response.text
    assert "x-doc3gpp-extraction-complete" not in response.headers


def test_http_html_content_escapes_raw_html(sqlite_env, web_app) -> None:
    payload = (
        "# Main body\n\n"
        "<script>alert('xss')</script>\n\n"
        "<img src=x onerror=alert(1)>\n\n"
        "<ins>added</ins>\n\n"
        "[bad](javascript:alert(1))\n"
    )
    main_source = _family_sources()[0][0]
    _seed_family("R5-260109", "r5/docs/r5-260109.zip", ((main_source, payload),))

    with TestClient(web_app) as client:
        response = client.get("/tdocs/R5-260109/content?format=html")

    assert response.status_code == 200, response.text
    assert "<script>alert('xss')</script>" not in response.text
    assert "<img src=x onerror=" not in response.text
    assert 'href="javascript:' not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<ins>added</ins>" in response.text


def test_http_json_matches_mcp_json_bytes(sqlite_env, web_app, mcp_server) -> None:
    tdoc_id = "R5-260108"
    ftp_url = "r5/docs/r5-260108.zip"
    _seed_family(tdoc_id, ftp_url, _family_sources())
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    SQLAlchemyTDocZipInventoryRepository().upsert(
        TDocZipInventory(
            ftp_url=ftp_url,
            tdoc_id=tdoc_id,
            zip_files=("main.docx", "support.pdf", "support.pdf"),
        )
    )

    with TestClient(web_app) as client:
        http_bytes = client.get(
            f"/tdocs/{tdoc_id}/content", params={"format": "json"}
        ).content.decode("utf-8")
    mcp_text = _mcp_call(mcp_server, {"tdoc_id": tdoc_id, "format": "json"})

    assert mcp_text == http_bytes
    payload = json.loads(mcp_text)
    assert payload["complete"] is True
    assert payload["skipped_sources"] == []
    assert payload["zip_files"] == ["main.docx", "support.pdf", "support.pdf"]



def test_detail_json_is_shared_across_cli_http_mcp_without_download_or_parse(
    sqlite_env,
    web_app,
    mcp_server,
    monkeypatch,
) -> None:
    """Detail reads expose inventory identically without touching ZIP parsing."""
    import asyncio

    from typer.testing import CliRunner

    from doc3gpp.cli import app as cli_app
    from doc3gpp.cli_auto_sync import trigger_auto_sync
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.services.tdoc_cr_service import TDocCrService
    from doc3gpp.settings.schema import Settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    create_schema("main")
    tdoc_id = "R5-260170"
    ftp_url = "r5/docs/r5-260170.zip"
    zip_files = ("a.docx", "support.pdf", "support.pdf")
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url)
    )
    SQLAlchemyTDocZipInventoryRepository().upsert(
        TDocZipInventory(ftp_url=ftp_url, tdoc_id=tdoc_id, zip_files=zip_files)
    )
    SQLAlchemyTDocFileRepository().upsert_many(
        [
            TDocFile(
                ftp_url="r5/docs/r5-260170-review.zip",
                tdoc_id=tdoc_id,
                type="review",
                file="R5-260170_review.zip",
            )
        ]
    )

    def fail_if_extracted(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("detail inventory lookup must not parse or extract a ZIP")

    monkeypatch.setattr(TDocCrService, "extract", fail_if_extracted)
    settings = Settings()
    settings.sync.auto_sync = False
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", trigger_auto_sync)

    with TestClient(web_app) as client:
        http_id = client.get(f"/tdocs/{tdoc_id}?format=json")
        assert http_id.status_code == 200, http_id.text
        http_url = client.get(
            "/tdocs/by-url",
            params={"ftp_url": ftp_url, "format": "json"},
        )
        assert http_url.status_code == 200, http_url.text

    async def call_tdoc(args: dict) -> str:
        result = await mcp_server.call_tool("get_tdoc", args)
        assert result.is_error is False, result
        return result.content[0].text

    runner = CliRunner()
    cli_id = runner.invoke(
        cli_app,
        ["tdoc", "show", "--tdoc", tdoc_id, "--format", "json", "--compact"],
    )
    assert cli_id.exit_code == 0, cli_id.output
    cli_url = runner.invoke(
        cli_app,
        [
            "tdoc", "show", "--ftp-url", ftp_url,
            "--format", "json", "--compact",
        ],
    )
    assert cli_url.exit_code == 0, cli_url.output

    mcp_id = asyncio.run(call_tdoc({"tdoc_id": tdoc_id}))
    mcp_url = asyncio.run(call_tdoc({"ftp_url": ftp_url}))
    assert cli_id.stdout == http_id.content.decode("utf-8") == mcp_id
    assert cli_url.stdout == http_url.content.decode("utf-8") == mcp_url
    assert json.loads(cli_id.stdout)["zip_files"] == list(zip_files)
    assert json.loads(cli_id.stdout)["files"][0]["type"] == "review"


def test_cli_tdoc_show_keeps_metadata_auto_sync_when_enabled(
    sqlite_env,
    monkeypatch,
) -> None:
    """The inventory read path still runs configured CLI metadata auto-sync."""
    from unittest.mock import MagicMock

    from typer.testing import CliRunner

    from doc3gpp.cli import app as cli_app
    from doc3gpp.cli_auto_sync import trigger_auto_sync
    from doc3gpp.models.sync import SyncOutcome
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.settings.schema import Settings
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema("main")
    tdoc_id = "R5-260171"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url="r5/docs/r5-260171.zip")
    )
    settings = Settings()
    settings.sync.auto_sync = True
    monkeypatch.setattr("doc3gpp.cli.get_settings", lambda: settings)
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", trigger_auto_sync)
    meeting_service = MagicMock()
    meeting_service.list_recent.return_value = []
    meeting_service.sync.return_value = SyncOutcome(
        status="synced",
        reason="metadata auto-sync ran",
        synced_count=1,
    )
    monkeypatch.setattr(
        "doc3gpp.cli.build_meeting_service", lambda: meeting_service
    )
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_sync_coordinator", lambda: MagicMock()
    )

    result = CliRunner().invoke(
        cli_app,
        ["tdoc", "show", "--tdoc", tdoc_id, "--format", "json", "--compact"],
    )

    assert result.exit_code == 0, result.output
    meeting_service.sync.assert_called_once()
    assert "metadata auto-sync ran" in result.output



def test_inventory_only_historical_url_keeps_content_unavailable(
    sqlite_env,
    web_app,
) -> None:
    """A historical inventory hit is detail-only without Markdown metadata."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    create_schema("main")
    tdoc_id = "R5-260172"
    current_url = "r5/docs/r5-260172-current.zip"
    historical_url = "r5/docs/r5-260172-historical.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=current_url)
    )
    SQLAlchemyTDocZipInventoryRepository().upsert(
        TDocZipInventory(
            ftp_url=historical_url,
            tdoc_id=tdoc_id,
            zip_files=(),
        )
    )

    with TestClient(web_app) as client:
        detail = client.get(
            "/tdocs/by-url",
            params={"ftp_url": historical_url, "format": "json"},
        )
        content = client.get(
            "/tdocs/by-url/content",
            params={"ftp_url": historical_url, "format": "json"},
        )

    assert detail.status_code == 200, detail.text
    assert detail.json()["zip_files"] == []
    assert "tdoc" not in detail.json()
    assert content.status_code == 404
    assert content.json()["error"] == "tdoc_url_not_found"


def test_mcp_markdown_matches_http_markdown(sqlite_env, web_app, mcp_server) -> None:
    tdoc_id = "R5-260109"
    _seed_family(tdoc_id, "r5/docs/r5-260109.zip", _family_sources())

    with TestClient(web_app) as client:
        http_text = client.get(f"/tdocs/{tdoc_id}/content?format=markdown").text
    mcp_text = _mcp_call(mcp_server, {"tdoc_id": tdoc_id, "format": "markdown"})

    assert mcp_text == http_text


def test_mcp_incomplete_markdown_raises_with_hint(sqlite_env, mcp_server) -> None:
    tdoc_id = "R5-260110"
    _seed_family(
        tdoc_id,
        "r5/docs/r5-260110.zip",
        _family_sources(),
        skipped_sources=(BROKEN_SOURCE,),
    )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(
            mcp_server.call_tool(
                "get_tdoc_content", {"tdoc_id": tdoc_id, "format": "markdown"}
            )
        )

    assert exc_info.value.code == MCP_CODE_CACHE_MISS
    assert BROKEN_SOURCE in exc_info.value.message
    assert exc_info.value.data["hint"] == (
        f"run: doc3gpp tdoc parse --tdoc {tdoc_id} --force"
    )


def test_by_url_content_never_downloads(
    sqlite_env, web_app, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from doc3gpp.services.tdoc_cr_service import TDocCrService

    def refuse_extract(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("URL content read must not extract")

    monkeypatch.setattr(TDocCrService, "extract", refuse_extract)
    url = "r5/docs/r5-260111.zip"
    _seed_family("R5-260111", url, _family_sources())

    with TestClient(web_app) as client:
        markdown = client.get(
            "/tdocs/by-url/content", params={"ftp_url": url, "format": "markdown"}
        )
        payload = client.get(
            "/tdocs/by-url/content", params={"ftp_url": url, "format": "json"}
        )

    assert markdown.status_code == 200, markdown.text
    assert "# Annex body" in markdown.text
    assert payload.status_code == 200, payload.text
    assert payload.json()["complete"] is True


def test_http_ineligible_type_returns_not_found_with_message(
    sqlite_env, web_app,
) -> None:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260112", type="other", ftp_url="r5/docs/r5-260112.zip")
    )

    with TestClient(web_app) as client:
        response = client.get("/tdocs/R5-260112/content?format=markdown")

    assert response.status_code == 404, response.text
    assert response.json()["error"] == "tdoc_content_type_ineligible"
    assert "R5-260112" in response.json()["detail"]
    assert "has type 'other'" in response.json()["detail"]


def test_mcp_ineligible_type_maps_to_not_found(sqlite_env, mcp_server) -> None:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260113", type="other", ftp_url="r5/docs/r5-260113.zip")
    )

    with pytest.raises(MCPError) as exc_info:
        asyncio.run(
            mcp_server.call_tool(
                "get_tdoc_content", {"tdoc_id": "R5-260113", "format": "json"}
            )
        )

    assert exc_info.value.code == MCP_CODE_NOT_FOUND
    assert "has type 'other'" in exc_info.value.message


@pytest.mark.parametrize(
    "error",
    [
        TDocZipLimitError("TDoc ZIP holds 150 documents, above the 100 limit"),
        TDocZipValidationError("TDoc ZIP member path is not safe: ../x.docx"),
        TDocNotYetOnFTPError("R5-260104"),
        TDocTooLargeError(source="R5-260104.zip", size=2000, limit=1000),
    ],
    ids=["zip-limit", "zip-validation", "not-yet-on-ftp", "too-large"],
)
def test_cli_raw_extraction_errors_are_bad_parameters_not_tracebacks(
    sqlite_env, cache_dir, monkeypatch, error,
) -> None:
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260104", type="CR", ftp_url="r5/docs/r5-260104.zip")
    )

    class _FailingExtraction:
        def extract(self, tdoc_id: str, **_kwargs: object) -> None:
            raise error

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _FailingExtraction(),
    )

    result = CliRunner().invoke(
        cli_app, ["tdoc", "show", "--tdoc", "R5-260104", "--format", "raw"]
    )

    assert result.exit_code == 2, result.output
    assert str(error) in _plain(result.output)
    assert "Traceback" not in result.output


def _seed_cr_with_selected_content(
    tdoc_id: str,
    ftp_url: str,
    cache_dir: Path,
    source_file: str,
    markdown: str,
    *,
    skipped_sources: tuple[str, ...] = (),
) -> Path:
    from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
    from doc3gpp.parsers.tdoc_zip import build_markdown_archive
    from doc3gpp.scraping.tdoc_zip_source import derive_cache_file

    normalized = normalize_ftp_path(ftp_url)
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url)
    )
    main = _document(normalized, CR_MAIN, "# Archive main\n")
    annex = _document(normalized, source_file, markdown, file_order=1)
    meta = TDocExtractMeta(
        ftp_url=normalized,
        tdoc_id=tdoc_id,
        cache_file=derive_cache_file(ftp_url),
        doc_filename=CR_MAIN,
        extraction_version=2,
        document_count=1,
        skipped_sources=skipped_sources,
    )
    SQLAlchemyTDocContentRepository().replace_cr_bundle(
        TDocCRDetails(tdoc_id=tdoc_id, ftp_url=normalized, title="Stored CR"),
        None,
        None,
        meta,
        (annex,),
    )
    archive_path = cache_dir / "markdown" / meta.cache_file
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(
        build_markdown_archive(
            (main, annex),
            ftp_url=normalized,
            main_source_file=CR_MAIN,
        )
    )
    return archive_path


def test_cli_raw_complete_presentation_source_is_sql_first_after_cache_eviction(
    sqlite_env, cache_dir, monkeypatch,
) -> None:
    tdoc_id = "R5-260105"
    source_file = "slides/presentation.pptx"
    markdown = "## Slide 1\n\nspeaker secret\n"
    archive_path = _seed_cr_with_selected_content(
        tdoc_id,
        "r5/docs/r5-260105.zip",
        cache_dir,
        source_file,
        markdown,
    )
    archive_path.unlink()
    extraction_calls: list[str] = []

    class _ExtractionSpy:
        def extract(self, requested_tdoc_id: str, **_kwargs: object) -> None:
            extraction_calls.append(requested_tdoc_id)

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _ExtractionSpy(),
    )

    result = CliRunner().invoke(
        cli_app,
        [
            "tdoc",
            "show",
            "--tdoc",
            tdoc_id,
            "--format",
            "raw",
            "--source-file",
            source_file,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == markdown
    assert extraction_calls == []


@pytest.mark.parametrize(
    "case,source_file,stored_source,skipped_sources,expected_exit",
    [
        (
            "incomplete-presentation",
            "slides/presentation.pptx",
            "slides/presentation.pptx",
            ("annex/broken.docx",),
            0,
        ),
        (
            "missing-presentation",
            "slides/missing.pptx",
            "slides/presentation.pptx",
            (),
            2,
        ),
        ("non-presentation", "Annex/support.docx", "Annex/support.docx", (), 0),
        ("no-source-selector", None, "Annex/support.docx", (), 0),
    ],
    ids=["incomplete", "missing-source", "non-presentation", "no-selector"],
)
def test_cli_raw_other_sources_still_extract_first(
    sqlite_env, cache_dir, monkeypatch,
    case: str,
    source_file: str | None,
    stored_source: str,
    skipped_sources: tuple[str, ...],
    expected_exit: int,
) -> None:
    tdoc_id = "R5-260106"
    _seed_cr_with_selected_content(
        tdoc_id,
        "r5/docs/r5-260106.zip",
        cache_dir,
        stored_source,
        "# Stored source\n",
        skipped_sources=skipped_sources,
    )
    extraction_calls: list[str] = []

    class _ExtractionSpy:
        def extract(self, requested_tdoc_id: str, **_kwargs: object) -> None:
            extraction_calls.append(requested_tdoc_id)

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _ExtractionSpy(),
    )
    args = ["tdoc", "show", "--tdoc", tdoc_id, "--format", "raw"]
    if source_file is not None:
        args.extend(["--source-file", source_file])

    result = CliRunner().invoke(cli_app, args)

    assert result.exit_code == expected_exit, result.output
    assert extraction_calls == [tdoc_id], case


def test_raw_tdoc_not_found_suppresses_exception_context(monkeypatch) -> None:
    import typer

    from doc3gpp.cli import _render_tdoc_show_raw
    from doc3gpp.services.tdoc_cr_service import TDocNotFoundError

    class _MissingTDoc:
        def extract(self, tdoc_id: str) -> None:
            raise TDocNotFoundError(tdoc_id)

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda: _MissingTDoc(),
    )

    with pytest.raises(typer.BadParameter) as exc_info:
        _render_tdoc_show_raw("R5-260107", None)

    assert exc_info.value.__suppress_context__ is True
