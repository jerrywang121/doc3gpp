"""Tests for the body-change sidecar fan-out in ``TDocCrService.extract``.

Drives the real ``TDocCrService.extract`` end-to-end with the heavy
path mocked (cache, scraper, network, file I/O) and the parser stubbed
to return a configured :class:`TDocCRParseResult`, so the fan-out
across the new ``tdoc_cr_change_details`` sidecar is exercised
without requiring a separate integration test.

Mirrors the pattern in ``tests/unit/test_tdoc_cr_service.py`` where
``test_extract_calls_three_upserts_for_ttcn_tdoc`` uses a real zip
fixture + stub parser + in-memory cache to lock the three-up
``extract`` fan-out deterministically.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRParseResult,
)
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.scraping.cache import TDocCache
from doc3gpp.services.tdoc_cr_service import TDocCrService

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


class _StubParser:
    """A :class:`TDocParser` double that returns a configured parse result.

    Mirrors the production parser contract: ``parse(markdown,
    tdoc_id=...)`` returns a :class:`TDocCRParseResult` bundling the
    cover-page fields, the optional TTCN sidecar, and the optional
    body-change sidecar.
    """

    def __init__(self, result: TDocCRParseResult) -> None:
        self._result = result

    parser_version = "1.0.0"

    def supports(
        self, tdoc_id: str, *, tdoc_type: str | None = None, spec: str | None = None,
    ) -> bool:
        return True

    def parse(
        self,
        markdown: str,
        *,
        tdoc_id: str,
        max_text_length: int = 0,
    ) -> TDocCRParseResult:
        return self._result


def _build_service(
    tmp_path: Path,
    *,
    zip_bytes: bytes,
) -> tuple[
    TDocCrService, MagicMock, MagicMock, MagicMock, MagicMock,
]:
    """Build a service with a real disk cache, a stubbed scraper
    returning ``zip_bytes`` for any URL, and stubbed repos.

    Returns ``(service, cr_repo, cr_ttcn_repo, cr_change_details_repo,
    tdoc_repo)`` so the test can assert against each four repos
    independently.
    """
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper = MagicMock()
    scraper.get_bytes.return_value = zip_bytes

    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    cr_change_details_repo = MagicMock()
    cr_change_details_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = None
    content_repo = MagicMock()

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_details_repo,
        tdoc_repository=tdoc_repo,
        content_repo=content_repo,
    )
    return service, cr_repo, cr_ttcn_repo, cr_change_details_repo, tdoc_repo


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_writes_change_details_in_atomic_bundle(tmp_path: Path) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    changes = TDocCRChangeDetails(
        ftp_url=None,
        tdoc_id=None,
        clauses=("5.2.3",),
        changes=({"clauses": ["5.2.3"], "text": "line A\n<ins>X</ins>"},),
    )
    parser = _StubParser(
        TDocCRParseResult(
            cover=TDocCRDetails(
                tdoc_id="R5s260009", spec="38.523-3", cr_num="3790"
            ),
            ttcn=None,
            changes=changes,
        )
    )

    service, cr_repo, cr_ttcn_repo, cr_change_details_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes(),
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="tsg_wg1/cr_r5s260009.zip"
    )

    service.extract("R5s260009")

    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]
    details, ttcn, written, meta, documents = service._content_repo.replace_cr_bundle.call_args.args  # type: ignore[attr-defined]
    assert details.ftp_url == meta.ftp_url == "tsg_wg1/cr_r5s260009.zip"
    assert ttcn is None
    assert isinstance(written, TDocCRChangeDetails)
    assert written.ftp_url == "tsg_wg1/cr_r5s260009.zip"
    assert written.tdoc_id == "R5s260009"
    assert written.clauses == ("5.2.3",)
    assert written.changes == ({"clauses": ["5.2.3"], "text": "line A\n<ins>X</ins>"},)
    assert documents == ()
    cr_repo.upsert.assert_not_called()
    cr_repo.upsert_extract_meta.assert_not_called()
    cr_ttcn_repo.upsert.assert_not_called()
    cr_change_details_repo.upsert.assert_not_called()


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_atomic_bundle_carries_none_for_absent_changes(
    tmp_path: Path,
) -> None:
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    parser = _StubParser(
        TDocCRParseResult(
            cover=TDocCRDetails(
                tdoc_id="R5s260009", spec="38.523-3", cr_num="3790"
            ),
            ttcn=None,
            changes=None,
        )
    )
    service, cr_repo, _cr_ttcn_repo, cr_change_details_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=fixture.read_bytes(),
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009", type="CR", ftp_url="tsg_wg1/cr_r5s260009.zip"
    )

    service.extract("R5s260009")

    service._content_repo.replace_cr_bundle.assert_called_once()  # type: ignore[attr-defined]
    _details, _ttcn, changes, _meta, _documents = (
        service._content_repo.replace_cr_bundle.call_args.args  # type: ignore[attr-defined]
    )
    assert changes is None
    cr_repo.upsert.assert_not_called()
    cr_repo.upsert_extract_meta.assert_not_called()
    cr_change_details_repo.upsert.assert_not_called()
