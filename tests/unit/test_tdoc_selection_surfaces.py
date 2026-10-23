"""Selection and list-output behaviour shared by the CLI, web, and MCP surfaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import (
    TDOC_CONTENT_STORES,
    eligible_type_values,
)
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.models.tdoc_document import TDocDocument
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.services.tdoc_cr_service import BatchExtractResult, ExtractResult
from doc3gpp.services.tdoc_family_service import FamilyExtractResult
from doc3gpp.services.tdoc_parse_selection import exact_tdoc_id, parse_selection_kwargs
from doc3gpp.settings.loader import get_settings
from doc3gpp.settings.schema import Settings
from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.models import (
    CONTENT_ORM_BY_STORE_KEY,
    TDocCrDetailOrm,
    TDocExtractOrm,
)
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.web.app import build_app, build_state
from doc3gpp.web.mcp_server import build_mcp_server
from doc3gpp.web.workers.handlers import _parse_tdocs

STORES = {store.key: store for store in TDOC_CONTENT_STORES}

DEFAULT_TDOC_FIELDS = [
    "tdoc_id",
    "meeting_name",
    "title",
    "source",
    "type",
    "status",
    "cr_cat",
    "spec",
    "version",
    "related_wis",
    "parsed",
]


class _RecordingCrService:
    """Records the ids passed to ``extract_many`` and returns a fixed batch."""

    def __init__(self, result: BatchExtractResult | None = None) -> None:
        self._result = result or BatchExtractResult(successes={}, failures={}, skipped={})
        self.calls: list[list[str]] = []

    def extract_many(self, tdoc_ids, *, force=False, full=False, runner=None, **_kwargs):
        self.calls.append(list(tdoc_ids))
        return self._result


@pytest.fixture()
def cli_env(sqlite_env, monkeypatch):
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()
    create_schema()
    yield sqlite_env
    get_settings.cache_clear()


def _patch_cr_service(monkeypatch, result: BatchExtractResult | None = None) -> _RecordingCrService:
    service = _RecordingCrService(result)
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda **_kwargs: service)
    return service


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_tdoc(tdoc_id: str, tdoc_type: str | None) -> None:
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id=tdoc_id, type=tdoc_type))


def _seed_complete_cr(tdoc_id: str) -> None:
    """Store a v2 CR extraction with a cover row and one annex."""
    url = f"docs/{tdoc_id.lower()}.zip"
    _seed_tdoc(tdoc_id, "CR")
    with get_session_factory()() as session:
        session.add(
            TDocExtractOrm(
                ftp_url=url,
                tdoc_id=tdoc_id,
                cache_file=f"{tdoc_id}.zip",
                doc_filename="CRs/main.docx",
                extraction_version=2,
                document_count=1,
                skipped_sources=None,
            )
        )
        session.add(TDocCrDetailOrm(ftp_url=url, tdoc_id=tdoc_id))
        session.flush()
        session.add(
            CONTENT_ORM_BY_STORE_KEY["cr"](
                ftp_url=url,
                doc_filename="annex.docx",
                document_id=document_id_for(url, "annex.docx"),
                tdoc_id=tdoc_id,
                content=compress_json("annex text"),
                content_hash=_sha("annex text"),
            )
        )
        session.commit()


def _document(source_file: str, order: int) -> TDocDocument:
    text = f"text of {source_file}"
    return TDocDocument(
        document_id=_sha(source_file),
        source_file=source_file,
        markdown_file=f"documents/{_sha(source_file)}/{source_file.rsplit('.', 1)[0]}.md",
        file_order=order,
        markdown_text=text,
        content_hash=_sha(text),
    )


def test_exact_tdoc_id_classifier() -> None:
    assert exact_tdoc_id("R5-123456") is True
    assert exact_tdoc_id("R5-12%") is False
    assert exact_tdoc_id("R5_1") is False
    assert exact_tdoc_id("R5*1") is False
    assert exact_tdoc_id("!R5-1") is False


def test_parse_selection_default_and_intersection() -> None:
    assert parse_selection_kwargs(None) == {
        "tdoc_type": None,
        "eligible_types": ("CR", "WID new", "WID revised", "LS in", "LS out", "discussion"),
    }
    assert parse_selection_kwargs(None, exact_tdoc_id=True) == {
        "tdoc_type": None,
        "eligible_types": None,
    }
    assert parse_selection_kwargs("WID%", exact_tdoc_id=True) == {
        "tdoc_type": "WID%",
        "eligible_types": eligible_type_values(),
    }
    assert parse_selection_kwargs("pCR") == {
        "tdoc_type": "pCR",
        "eligible_types": eligible_type_values(),
    }
    assert parse_selection_kwargs("") == {
        "tdoc_type": None,
        "eligible_types": eligible_type_values(),
    }


def test_cli_default_parse_selects_cr_and_families(cli_env, monkeypatch) -> None:
    service = _patch_cr_service(monkeypatch)
    _seed_tdoc("R5s260031", "CR")
    _seed_tdoc("R5s260032", "WID new")
    _seed_tdoc("R5s260033", "discussion")
    _seed_tdoc("R5s260034", "other")

    CliRunner().invoke(app, ["tdoc", "parse", "--tdoc", "R5s26003%", "--yes"])

    assert len(service.calls) == 1
    assert sorted(service.calls[0]) == ["R5s260031", "R5s260032", "R5s260033"]


def test_cli_explicit_type_intersection_reports_no_match(cli_env, monkeypatch) -> None:
    service = _patch_cr_service(monkeypatch)
    _seed_tdoc("R5s260051", "pCR")

    result = CliRunner().invoke(app, ["tdoc", "parse", "--type", "pCR", "--yes"])

    assert result.exit_code == 1
    assert "No TDoc matched" in result.output
    assert service.calls == []


def test_cli_exact_ineligible_tdoc_reaches_skipped(cli_env) -> None:
    _seed_tdoc("R5s260041", "other")

    result = CliRunner().invoke(app, ["tdoc", "parse", "--tdoc", "R5s260041", "--yes"])

    assert "R5s260041: SKIPPED" in result.output
    assert "has type 'other'" in result.output


def test_cli_family_success_reports_documents_and_skipped_sources(cli_env, monkeypatch) -> None:
    family = FamilyExtractResult(
        tdoc_id="R5s260061",
        store=STORES["wid"],
        extract_meta=None,
        documents=(_document("main.docx", 0), _document("annex.docx", 1)),
        skipped_sources=("broken.docx",),
        from_cache=False,
    )
    _patch_cr_service(
        monkeypatch,
        BatchExtractResult(successes={"R5s260061": family}, failures={}, skipped={}),
    )
    _seed_tdoc("R5s260061", "WID new")

    result = CliRunner().invoke(app, ["tdoc", "parse", "--tdoc", "R5s260061", "--yes"])

    assert result.exception is None, result.output
    assert "TDoc R5s260061: wid, 2 documents, extraction_version 2" in result.output
    assert "warning: skipped broken.docx" in result.output


def test_cli_cr_success_reports_skipped_annex(cli_env, monkeypatch) -> None:
    meta = TDocExtractMeta(
        ftp_url="docs/r5s260071.zip",
        tdoc_id="R5s260071",
        cache_file="R5s260071.zip",
        doc_filename="CRs/main.docx",
        extracted_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        skipped_sources=("bad.docx",),
    )
    cr = ExtractResult(
        details=TDocCRDetails(tdoc_id="R5s260071", spec="38.523-3", cr_num="3790", title="Example CR"),
        extract_meta=meta,
        from_cache=False,
    )
    _patch_cr_service(
        monkeypatch,
        BatchExtractResult(successes={"R5s260071": cr}, failures={}, skipped={}),
    )
    _seed_tdoc("R5s260071", "CR")

    result = CliRunner().invoke(app, ["tdoc", "parse", "--tdoc", "R5s260071", "--yes"])

    assert result.exception is None, result.output
    assert "R5s260071: spec=38.523-3 cr_num=3790 title=Example CR" in result.output
    assert "warning: skipped bad.docx" in result.output


def test_cli_tdoc_list_parsed_filter_and_fields(cli_env) -> None:
    _seed_complete_cr("R5s260020")
    _seed_tdoc("R5s260021", "WID new")
    runner = CliRunner()

    complete = runner.invoke(app, ["tdoc", "list", "--parsed", "true", "--format", "json"])
    assert complete.exit_code == 0, complete.output
    rows = json.loads(complete.output)
    assert [row["tdoc_id"] for row in rows] == ["R5s260020"]
    assert list(rows[0]) == DEFAULT_TDOC_FIELDS
    assert rows[0]["parsed"] is True

    counted = runner.invoke(
        app,
        ["tdoc", "list", "--parsed", "true", "--format", "json", "--fields", "tdoc_id,document_count"],
    )
    assert json.loads(counted.output) == [{"tdoc_id": "R5s260020", "document_count": 1}]

    pending = runner.invoke(
        app,
        ["tdoc", "list", "--parsed", "false", "--format", "json", "--fields", "tdoc_id,parsed"],
    )
    assert json.loads(pending.output) == [{"tdoc_id": "R5s260021", "parsed": False}]


def test_web_parse_selection_excludes_other() -> None:
    rows = [TDoc(tdoc_id="R5s260081", type="CR"), TDoc(tdoc_id="R5s260082", type="other")]
    list_calls: list[dict] = []
    extract_calls: list[list[str]] = []

    def list_rows(**kwargs):
        list_calls.append(kwargs)
        eligible = kwargs.get("eligible_types")
        return [row for row in rows if eligible is None or row.type in eligible]

    def extract_many(tdoc_ids, **_kwargs):
        extract_calls.append(list(tdoc_ids))
        return BatchExtractResult(successes={}, failures={}, skipped={})

    services = SimpleNamespace(
        pipeline=None,
        tdoc_repo=SimpleNamespace(list=list_rows),
        tdoc_cr=SimpleNamespace(extract_many=extract_many),
    )
    job = SimpleNamespace(params={"filter": {"tdoc_id": "R5s2600%"}})

    asyncio.run(
        _parse_tdocs(
            job,
            services,
            Settings(),
            progress=lambda *_args, **_kwargs: None,
            cancel_event=asyncio.Event(),
        )
    )

    assert list_calls[0]["tdoc_type"] is None
    assert list_calls[0]["eligible_types"] == eligible_type_values()
    assert extract_calls == [["R5s260081"]]


def test_web_tdoc_list_parsed_filter(sqlite_env) -> None:
    create_schema()
    _seed_complete_cr("R5s260091")
    _seed_tdoc("R5s260092", "WID new")

    with TestClient(build_app()) as client:
        complete = client.get("/tdocs", params={"parsed": "true", "format": "json"})
        invalid = client.get("/tdocs", params={"parsed": "yes", "format": "json"})

    assert complete.status_code == 200, complete.text
    payload = complete.json()
    assert [row["tdoc_id"] for row in payload] == ["R5s260091"]
    assert list(payload[0]) == DEFAULT_TDOC_FIELDS
    assert payload[0]["parsed"] is True
    assert invalid.status_code == 400


def test_mcp_parse_and_list_defaults(sqlite_env) -> None:
    create_schema()
    _seed_complete_cr("R5s260101")
    _seed_tdoc("R5s260102", "WID new")
    state = build_state(get_settings())
    server = build_mcp_server(state)

    listed = asyncio.run(server.call_tool("list_tdocs", {"parsed": True}))
    rows = json.loads(listed.content[0].text)
    assert [row["tdoc_id"] for row in rows] == ["R5s260101"]
    assert list(rows[0]) == DEFAULT_TDOC_FIELDS
    assert rows[0]["parsed"] is True

    enqueued = asyncio.run(
        server.call_tool("parse_tdocs", {"filter": {"tdoc_id": "R5s2601%"}})
    )
    job_id = json.loads(enqueued.content[0].text)["job_id"]
    job = state.services.job_repo.get(job_id)
    assert job is not None
    assert job.params["filter"] == {"tdoc_id": "R5s2601%"}
