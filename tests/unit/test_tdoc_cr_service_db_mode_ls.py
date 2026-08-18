from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.services.tdoc_cr_service import (
    LSResult,
    TDocCrService,
)


@pytest.fixture
def svc_factory():
    """Build a TDocCrService with mocked I/O.

    Tests set the mocks' return values; the fixture returns a callable
    that constructs the service and binds the returned mocks so the
    test body can wire them.
    """
    cache = MagicMock()
    scraper = MagicMock()
    scraper.get_bytes = MagicMock(return_value=b"zip-bytes-not-used-on-cache-hit")
    cr_repo = MagicMock()
    cr_repo.upsert = MagicMock()
    cr_repo.get_by_url = MagicMock(return_value=None)
    cr_repo.get_extract_meta_by_url = MagicMock(return_value=None)
    cr_ttcn_repo = MagicMock()
    cr_change_repo = MagicMock()
    tdoc_repo = MagicMock()

    def factory(tdoc_row):
        tdoc_repo.get_by_id = MagicMock(return_value=tdoc_row)
        ls_repo = MagicMock()
        ls_repo.upsert = MagicMock()
        ls_repo.get_by_url = MagicMock(return_value=None)
        svc = TDocCrService(
            cache=cache,
            scraper_client=scraper,
            cr_repository=cr_repo,
            cr_ttcn_repository=cr_ttcn_repo,
            cr_change_details_repository=cr_change_repo,
            tdoc_repository=tdoc_repo,
            ls_repository=ls_repo,
        )
        return svc, ls_repo, cr_repo

    return factory


_LS_MD = (
    "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-260017\n\n"
    "Title:	LS on frequency separation for Type 4b UE NR-CA PDSCH demodulation requirements\n"
    "Source:	TSG WG RAN4\n"
    "To:	RAN WG5\n"
)


def test_db_mode_extract_writes_ls_sidecar(svc_factory, tmp_path, monkeypatch):
    """DB-mode extract() dispatches to LS parser for an LS-in row."""
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5-260017",
        meeting_id=110,
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        source="TSG WG RAN4",
        type="LS in",
        status="noted",
    )
    svc, ls_repo, cr_repo = svc_factory(tdoc)

    # Short-circuit the cache + zip download paths so the test focuses
    # on the parse-and-persist branch.
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        lambda *a, **kw: MagicMock(
            path=MagicMock(read_bytes=lambda: b"unused"),
            url=None,
        ),
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.extract_docx_from_zip",
        lambda _: ("R5-260017.docx", _LS_MD.encode("utf-8")),
    )
    svc._cache.put_bytes = MagicMock()
    svc._cache.get_bytes = MagicMock(return_value=None)
    svc._load_or_render_markdown = MagicMock(return_value=_LS_MD)  # type: ignore[method-assign]

    result = svc.extract("R5-260017", force=True)

    assert isinstance(result, LSResult)
    assert result.from_cache is False
    assert isinstance(result.details, TDocLSDetails)
    assert result.details.tdoc_id == "R5-260017"
    assert result.details.variant == "3gpp"
    ls_repo.upsert.assert_called_once()
    cr_repo.upsert.assert_not_called()
    # extract_meta still gets written so tdoc_content / FTS5 find the markdown.
    cr_repo.upsert_extract_meta.assert_called_once()
    assert cr_repo.upsert_extract_meta.call_args[0][0].cache_file.endswith(".zip")


def test_db_mode_extract_returns_from_cache_for_cached_ls(svc_factory, tmp_path, monkeypatch):
    """An LS row already in tdoc_cr_ls_details returns from_cache=True without downloading."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_ls import TDocLSDetails

    tdoc = TDoc(
        tdoc_id="R5-260017",
        meeting_id=110,
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        source="TSG WG RAN4",
        type="LS in",
        status="noted",
    )
    svc, ls_repo, cr_repo = svc_factory(tdoc)

    cached_meta = MagicMock(cache_file="R5s260017.zip", doc_filename="R5-260017.docx",
                            ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
                            tdoc_id="R5-260017")
    cached_details = TDocLSDetails(
        tdoc_id="R5-260017",
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        variant="3gpp",
        title="LS on frequency separation",
    )
    ls_repo.get_by_url = MagicMock(return_value=cached_details)
    cr_repo.get_extract_meta_by_url = MagicMock(return_value=cached_meta)

    download_called = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        download_called,
    )

    result = svc.extract("R5-260017", force=False)

    assert isinstance(result, LSResult)
    assert result.from_cache is True
    assert result.details is cached_details
    ls_repo.upsert.assert_not_called()
    download_called.assert_not_called()


def test_extract_many_populates_ls_successes(svc_factory, tmp_path, monkeypatch):
    """extract_many routes an LS row to ls_successes (not successes)."""
    from doc3gpp.models.tdoc import TDoc

    tdoc = TDoc(
        tdoc_id="R5-260017",
        meeting_id=110,
        ftp_url="tsg_ran/WG4_Radio/TSGR4_110/Inbox/R5-260017.zip",
        source="TSG WG RAN4",
        type="LS in",
        status="noted",
    )
    svc, ls_repo, cr_repo = svc_factory(tdoc)

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.download_tdoc_zip",
        lambda *a, **kw: MagicMock(
            path=MagicMock(read_bytes=lambda: b"unused"),
            url=None,
        ),
    )
    monkeypatch.setattr(
        "doc3gpp.services.tdoc_cr_service.extract_docx_from_zip",
        lambda _: ("R5-260017.docx", _LS_MD.encode("utf-8")),
    )
    svc._cache.put_bytes = MagicMock()
    svc._cache.get_bytes = MagicMock(return_value=None)
    svc._load_or_render_markdown = MagicMock(return_value=_LS_MD)  # type: ignore[method-assign]

    result = svc.extract_many(["R5-260017"], force=True)

    assert result.successes == {}
    assert "R5-260017" in result.ls_successes
    assert isinstance(result.ls_successes["R5-260017"], LSResult)
    assert result.failures == {}
