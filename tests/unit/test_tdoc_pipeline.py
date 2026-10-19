"""Preparation/persistence boundary tests for queued TDoc parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.services.tdoc_cr_service import PreparedDirectTDoc

from test_tdoc_cr_service import FIXTURES_DIR, _build_service, _docx_available


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_direct_url_writer_persists_prepared_result(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    service, _scraper, _cache, cr_repo, cr_ttcn_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes()
    )
    tdoc_repo.get_by_id.return_value = TDoc(tdoc_id="R5s260009", type="CR")

    prepared = service.prepare_from_url(
        "https://www.3gpp.org/ftp/.../R5s260009.zip",
        force=True,
    )
    assert isinstance(prepared, PreparedDirectTDoc)

    result = service.persist_prepared_url(prepared)

    assert result.persisted is True
    cr_repo.upsert.assert_called_once_with(prepared.details)
    cr_repo.upsert_extract_meta.assert_called_once_with(prepared.extract_meta)
    if prepared.ttcn is not None:
        cr_ttcn_repo.upsert.assert_called_once_with(prepared.ttcn)
