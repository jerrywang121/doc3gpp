from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc_ls import TDocLSDetails
from doc3gpp.services.tdoc_cr_service import TDocCrService


@pytest.fixture
def ls_repo_stub():
    stub = MagicMock()
    stub.upsert = MagicMock()
    return stub


_LS_MD = """3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001

Title:	LS on 5G_eHealth WI status update
Response to:	LS R5-234567 on 5G_eHealth WI status from RAN WG3
Source:	3GPP TSG RAN WG2
To:	RAN WG3
"""


def test_ls_sidecar_is_written_for_ls_rows(ls_repo_stub, tmp_path, monkeypatch):
    # Stub the rest of the service dependencies to avoid hitting the
    # network / cache / FTS5.
    cache = MagicMock()
    scraper = MagicMock()
    scraper.fetch_bytes = MagicMock(return_value=_LS_MD.encode("utf-8"))
    cr_repo = MagicMock()
    cr_repo.upsert = MagicMock()
    cr_ttcn_repo = MagicMock()
    cr_change_repo = MagicMock()
    tdoc_repo = MagicMock()
    tdoc_repo.get = MagicMock(return_value=MagicMock(tdoc_id="R5-240001", ftp_url="tsg/ls/R5-240001.doc", tdoc_type="LS", source="3GPP TSG"))

    svc = TDocCrService(
        cache=cache, scraper_client=scraper,
        cr_repository=cr_repo, cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_repo,
        tdoc_repository=tdoc_repo,
        ls_repository=ls_repo_stub,
    )

    svc.extract_from_bytes(
        _LS_MD.encode("utf-8"), tdoc_id="R5-240001",
        ftp_url="tsg/ls/R5-240001.doc", tdoc_type="LS", source="3GPP TSG",
    )

    ls_repo_stub.upsert.assert_called_once()
    details = ls_repo_stub.upsert.call_args[0][0]
    assert isinstance(details, TDocLSDetails)
    assert details.variant == "3gpp"
    assert details.title == "LS on 5G_eHealth WI status update"


def test_ls_sidecar_is_not_written_for_cr_rows(ls_repo_stub):
    cache = MagicMock()
    scraper = MagicMock()
    cr_repo = MagicMock()
    cr_repo.upsert = MagicMock()
    cr_ttcn_repo = MagicMock()
    cr_change_repo = MagicMock()
    tdoc_repo = MagicMock()

    svc = TDocCrService(
        cache=cache, scraper_client=scraper,
        cr_repository=cr_repo, cr_ttcn_repository=cr_ttcn_repo,
        cr_change_details_repository=cr_change_repo,
        tdoc_repository=tdoc_repo,
        ls_repository=ls_repo_stub,
    )

    # CR markdown (header has CHANGE REQUEST) — should NOT touch ls_repo
    cr_md = "| CHANGE REQUEST |\n|  | 38.523-3 | CR | 3790 | rev | - | Current version: | 18.4.0 |  |\n"
    svc.extract_from_bytes(
        cr_md.encode("utf-8"), tdoc_id="R5-240099",
        ftp_url="tsg/cr/R5-240099.doc", tdoc_type="CR", source="Ericsson",
    )

    ls_repo_stub.upsert.assert_not_called()
