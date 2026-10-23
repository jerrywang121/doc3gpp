"""Type dispatch between the CR path and the family path in ``TDocCrService``."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_tdoc_cr_service import FIXTURES_DIR, _build_service, _docx_available

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_content_families import TDOC_CONTENT_STORES
from doc3gpp.models.tdoc_cr import TDocExtractMeta
from doc3gpp.services.tdoc_cr_service import PreparedTDocExtract
from doc3gpp.services.tdoc_family_service import (
    FamilyExtractResult,
    PreparedFamilyExtract,
)

STORES = {store.key: store for store in TDOC_CONTENT_STORES}


class _FakeFamilyService:
    """Returns a fixed family result and records each prepare call."""

    def __init__(self, result: FamilyExtractResult | None = None) -> None:
        self.result = result
        self.calls: list[tuple[str, str, bool]] = []

    def prepare_extract(self, tdoc, store, *, force=False):
        self.calls.append((tdoc.tdoc_id, store.key, force))
        return self.result

    def persist_prepared_extract(self, prepared):
        return FamilyExtractResult(
            tdoc_id=prepared.tdoc_id,
            store=prepared.store,
            extract_meta=prepared.extract_meta,
            documents=prepared.documents,
            skipped_sources=prepared.skipped_sources,
            from_cache=False,
        )


class _ForbiddenFamilyService:
    """Fails the test if a CR or unlisted TDoc reaches the family service."""

    def prepare_extract(self, tdoc, store, *, force=False):
        raise AssertionError(f"family service must not run for {tdoc.tdoc_id}")


def _family_result(tdoc_id: str) -> FamilyExtractResult:
    return FamilyExtractResult(
        tdoc_id=tdoc_id,
        store=STORES["wid"],
        extract_meta=None,
        documents=(),
        skipped_sources=(),
        from_cache=True,
    )


def _refuse_network(scraper) -> None:
    scraper.get_bytes.side_effect = AssertionError("network must not be used")


def test_ineligible_type_is_skipped_without_download(tmp_path: Path) -> None:
    service, scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path,
        family_service=_ForbiddenFamilyService(),
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5-000001",
        type="other",
        ftp_url="tsg_ran/r5-000001.zip",
    )
    _refuse_network(scraper)

    result = service.extract_many(["R5-000001"])

    assert result.successes == {}
    assert result.failures == {}
    assert result.skipped["R5-000001"].startswith("TDocTypeUnsupportedError")
    assert "other" in result.skipped["R5-000001"]
    scraper.get_bytes.assert_not_called()


def test_family_tdoc_routes_to_family_service(tmp_path: Path) -> None:
    family = _FakeFamilyService(_family_result("R5-000002"))
    service, scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path,
        family_service=family,
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5-000002",
        type="WID revised",
        ftp_url="tsg_ran/r5-000002.zip",
    )
    _refuse_network(scraper)

    result = service.extract_many(["R5-000002"])

    assert result.successes == {"R5-000002": family.result}
    assert result.failures == {}
    assert result.skipped == {}
    assert family.calls == [("R5-000002", "wid", False)]
    scraper.get_bytes.assert_not_called()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_cr_tdoc_never_uses_family_service(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path,
        zip_bytes=fixture.read_bytes(),
        family_service=_ForbiddenFamilyService(),
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="tsg_ran/r5s260009.zip",
    )

    prepared = service.prepare_extract("R5s260009", force=True)

    assert isinstance(prepared, PreparedTDocExtract)
    assert prepared.main_source_file.endswith("R5s260009.docx")


def test_family_persist_runs_index_and_embed_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family = _FakeFamilyService()
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, _tdoc_repo = _build_service(
        tmp_path,
        family_service=family,
    )
    hooks: list[tuple[str, str]] = []
    monkeypatch.setattr(
        service, "_index_after_parse", lambda tdoc_id: hooks.append(("index", tdoc_id))
    )
    monkeypatch.setattr(
        service, "_embed_after_parse", lambda tdoc_id: hooks.append(("embed", tdoc_id))
    )
    prepared = PreparedFamilyExtract(
        tdoc_id="R5-000002",
        store=STORES["wid"],
        extract_meta=TDocExtractMeta(
            ftp_url="tsg_ran/r5-000002.zip",
            tdoc_id="R5-000002",
            cache_file="R5-000002.zip",
            doc_filename="R5-000002.docx",
        ),
        documents=(),
        skipped_sources=(),
    )

    result = service.persist_prepared_extract(prepared)

    assert isinstance(result, FamilyExtractResult)
    assert result.tdoc_id == "R5-000002"
    assert hooks == [("index", "R5-000002"), ("embed", "R5-000002")]


def test_family_without_ftp_url_is_skipped(tmp_path: Path) -> None:
    service, scraper, _cache, _cr_repo, _cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path,
        family_service=_ForbiddenFamilyService(),
    )
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5-000003",
        type="LS in",
        ftp_url="",
    )
    _refuse_network(scraper)

    result = service.extract_many(["R5-000003"])

    assert result.successes == {}
    assert result.failures == {}
    assert result.skipped["R5-000003"].startswith("TDocNotYetOnFTPError")
    scraper.get_bytes.assert_not_called()


def test_persist_rejects_unknown_prepared_type(tmp_path: Path) -> None:
    service, _scraper, _cache, _cr_repo, _cr_ttcn_repo, _tdoc_repo = _build_service(
        tmp_path,
        family_service=_ForbiddenFamilyService(),
    )

    with pytest.raises(TypeError, match="cannot persist object"):
        service.persist_prepared_extract(object())
