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
        full: bool = False,
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
    cr_repo.get_extract_meta_by_url.return_value = None
    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = None
    cr_change_details_repo = MagicMock()
    cr_change_details_repo.get_by_url.return_value = None
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = None

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper,
        cr_repository=cr_repo,
        cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_details_repo,
        tdoc_repository=tdoc_repo,
    )
    return service, cr_repo, cr_ttcn_repo, cr_change_details_repo, tdoc_repo


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_writes_change_details_when_present(tmp_path: Path) -> None:
    """When the parser returns a populated ``changes`` sidecar, the
    service fans it out to the ``cr_change_details_repo.upsert`` call
    with the resolved ``ftp_url`` and ``tdoc_id`` filled in.

    Drives the real ``TDocCrService.extract(...)`` path so the
    ``replace(parsed.changes, ftp_url=..., tdoc_id=...)`` + upsert
    fan-out is exercised end-to-end. The download / cache / scraper /
    file I/O layers are stubbed via the scraper mock (returning the
    fixture zip bytes) and an in-memory disk cache; the parser is
    stubbed via ``_StubParser`` so the result is deterministic
    regardless of fixture content.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(
        tdoc_id="R5s260009",
        spec="38.523-3",
        cr_num="3790",
    )
    changes = TDocCRChangeDetails(
        ftp_url=None,
        tdoc_id=None,
        clauses=("5.2.3",),
        changes=({"clauses": ["5.2.3"], "text": "line A\n<ins>X</ins>"},),
    )
    parser = _StubParser(TDocCRParseResult(cover=cover, ttcn=None, changes=changes))

    service, cr_repo, cr_ttcn_repo, cr_change_details_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="tsg_wg1/CR_R5s260009.zip",
    )

    service.extract("R5s260009")

    assert cr_change_details_repo.upsert.call_count == 1
    written = cr_change_details_repo.upsert.call_args.args[0]
    assert isinstance(written, TDocCRChangeDetails)
    assert written.ftp_url == "tsg_wg1/CR_R5s260009.zip"
    assert written.tdoc_id == "R5s260009"
    assert written.clauses == ("5.2.3",)
    assert written.changes == ({"clauses": ["5.2.3"], "text": "line A\n<ins>X</ins>"},)

    # The other upserts still fire (cover + extract meta) and the
    # TTCN sidecar is skipped because the stub parser returned
    # ``ttcn=None``.
    assert cr_repo.upsert.call_count == 1
    assert cr_repo.upsert_extract_meta.call_count == 1
    assert cr_ttcn_repo.upsert.call_count == 0


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_extract_skips_change_details_upsert_when_changes_none(
    tmp_path: Path,
) -> None:
    """When the parser returns ``changes=None`` (the body extractor
    found no revision marks), the service must NOT touch the change
    details repo.

    Locks the no-op branch of the new fan-out so a regression that
    unconditionally calls ``cr_change_details_repo.upsert(...)`` would
    fail here.
    """
    fixture = FIXTURES_DIR / "R5s260009.zip"
    if not fixture.exists():
        pytest.skip(f"fixture missing: {fixture}")
    zip_bytes = fixture.read_bytes()

    cover = TDocCRDetails(
        tdoc_id="R5s260009",
        spec="38.523-3",
        cr_num="3790",
    )
    parser = _StubParser(TDocCRParseResult(cover=cover, ttcn=None, changes=None))

    service, cr_repo, _cr_ttcn_repo, cr_change_details_repo, tdoc_repo = _build_service(
        tmp_path, zip_bytes=zip_bytes,
    )
    service._parser = parser  # type: ignore[attr-defined]
    tdoc_repo.get_by_id.return_value = TDoc(
        tdoc_id="R5s260009",
        type="CR",
        ftp_url="tsg_wg1/CR_R5s260009.zip",
    )

    service.extract("R5s260009")

    assert cr_change_details_repo.upsert.call_count == 0
    assert cr_repo.upsert.call_count == 1
    assert cr_repo.upsert_extract_meta.call_count == 1
