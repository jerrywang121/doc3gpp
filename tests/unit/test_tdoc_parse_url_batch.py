"""Unit tests for ``tdoc parse --from-url`` folder batch dispatch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult, TDocCRDetails
from doc3gpp.models.tdoc_document import TDocDocument
from doc3gpp.parsers.direct_extractor import NotAFolderError
from doc3gpp.parsers.tdoc_zip import ephemeral_document_id_for

Runner = CliRunner


class _FakeBatchService:
    """Fake service that records batch/direct calls and returns canned data."""

    def __init__(
        self,
        batch_result: DirectParseBatchResult | None = None,
        direct_result: DirectParseResult | None = None,
        direct_raises: Exception | None = None,
    ) -> None:
        self.batch_result = batch_result
        self.direct_result = direct_result
        self.direct_raises = direct_raises
        self.batch_calls: list[tuple[str, dict[str, object]]] = []
        self.direct_calls: list[tuple[str, dict[str, object]]] = []

    def extract_from_url_batch(
        self,
        url: str,
        *,
        max_depth: int = 2,
        force: bool = False,
        max_tdoc_size_bytes: int = 0,
        on_progress=None,
        is_cancelled=None,
    ) -> DirectParseBatchResult:
        self.batch_calls.append((url, {"max_depth": max_depth, "force": force}))
        if self.batch_result is None:
            raise NotAFolderError(f"not a folder: {url}")
        return self.batch_result

    def extract_from_url(
        self,
        url: str,
        *,
        force: bool = False,
        max_tdoc_size_bytes: int = 0,
    ) -> DirectParseResult:
        self.direct_calls.append((url, {"force": force}))
        if self.direct_raises is not None:
            raise self.direct_raises
        assert self.direct_result is not None
        return self.direct_result

    def extract_from_bytes(self, *args: object, **_: object) -> DirectParseResult:
        raise AssertionError("unexpected call")


def _build_service_factory(monkeypatch: pytest.MonkeyPatch, service: _FakeBatchService) -> None:
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: service)


def _dummy_details(tdoc_id: str) -> DirectParseResult:
    source_file = f"{tdoc_id}.docx"
    markdown = f"markdown for {tdoc_id}"
    document_id = ephemeral_document_id_for(
        hashlib.sha256(tdoc_id.encode("utf-8")).hexdigest(), source_file
    )
    document = TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=f"documents/{document_id}/{tdoc_id}.md",
        file_order=0,
        markdown_text=markdown,
        content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )
    return DirectParseResult(
        source_kind="url-3gpp",
        documents=(document,),
        main_source_file=source_file,
        details=TDocCRDetails(tdoc_id=tdoc_id),
        extract_meta=None,
        from_cache=False,
        persisted=True,
        tdoc_id=tdoc_id,
        tdoc_id_in_tdocs=True,
        source_url=f"https://www.3gpp.org/ftp/tsg_ran/WG5/Docs/{tdoc_id}.zip",
    )


def test_folder_url_routes_to_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = DirectParseBatchResult(
        results=[_dummy_details("R5s260001")],
        failures={},
    )
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app, ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/"]
    )
    assert result.exit_code == 0, result.output
    assert len(fake.batch_calls) == 1
    assert fake.batch_calls[0][0] == "https://www.3gpp.org/ftp/Docs/"
    assert fake.batch_calls[0][1]["max_depth"] == 0


def test_folder_url_without_trailing_slash_routes_to_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder URL does not need a trailing slash for CLI dispatch."""
    batch = DirectParseBatchResult(results=[], failures={})
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app, ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs"]
    )

    assert result.exit_code == 0, result.output
    assert len(fake.batch_calls) == 1
    assert not fake.direct_calls


def test_file_url_routes_to_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    direct = _dummy_details("R5s260001")
    fake = _FakeBatchService(direct_result=direct)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app,
        ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/R5s260001.zip"],
    )
    assert result.exit_code == 0, result.output
    assert len(fake.direct_calls) == 1
    assert not fake.batch_calls


def test_recursive_enables_default_depth(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = DirectParseBatchResult(results=[], failures={})
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app,
        ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/", "--recursive"],
    )
    assert result.exit_code == 0, result.output
    assert fake.batch_calls[0][1]["max_depth"] == 2  # default from settings


def test_max_depth_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = DirectParseBatchResult(results=[], failures={})
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/Docs/",
            "--recursive",
            "--max-depth", "4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.batch_calls[0][1]["max_depth"] == 4


def test_max_depth_without_recursive_implies_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = DirectParseBatchResult(results=[], failures={})
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/Docs/",
            "--max-depth", "3",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.batch_calls[0][1]["max_depth"] == 3


def test_extensionless_canonical_url_is_treated_as_a_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBatchService(
        batch_result=DirectParseBatchResult(results=[], failures={})
    )
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app,
        ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/R5s260001"],
    )
    assert result.exit_code == 0, result.output
    assert len(fake.batch_calls) == 1
    assert not fake.direct_calls


def test_batch_writes_output_mirroring_ftp_structure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch = DirectParseBatchResult(
        results=[_dummy_details("R5s260001")],
        failures={},
    )
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    output_dir = tmp_path / "out"
    result = Runner().invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/tsg_ran/WG5/Docs/",
            "--output", str(output_dir),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.output
    expected = output_dir / "tsg_ran" / "wg5" / "docs" / "r5s260001.json"
    assert expected.exists()
    payload = json.loads(expected.read_text())
    assert payload["tdoc_id"] == "R5s260001"


def test_batch_summary_without_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = DirectParseBatchResult(
        results=[_dummy_details("R5s260001")],
        failures={"https://www.3gpp.org/ftp/Docs/R5s260002.zip": "ValueError: bad"},
    )
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app, ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/"]
    )
    assert result.exit_code == 0, result.output
    assert "Scanned:                         2" in result.output
    assert "Newly parsed:                    1" in result.output
    assert "Failures:                        1" in result.output


def test_batch_summary_splits_size_and_type_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = DirectParseBatchResult(
        results=[],
        failures={},
        skipped={
            "https://www.3gpp.org/ftp/Docs/R5s260003.zip": (
                "TDocTooLargeError: 2048 KB exceeds the 1000 KB limit"
            ),
            "https://www.3gpp.org/ftp/Docs/R5s260004.zip": (
                "TDocTypeSkippedError: type 'other' is not parsed"
            ),
        },
    )
    fake = _FakeBatchService(batch_result=batch)
    _build_service_factory(monkeypatch, fake)

    result = Runner().invoke(
        app, ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/"]
    )

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "Skipped (exceeds max_tdoc_size_kb): 1" in lines
    generic = [line.split()[-1] for line in lines if line.startswith("Skipped:")]
    assert generic == ["1"]
