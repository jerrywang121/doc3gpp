"""Unit tests for LISTSERV catalog discovery mappings."""

from __future__ import annotations

import pytest

from doc3gpp.models.listserv_catalog import infer_listserv_tsg


@pytest.mark.parametrize(
    ("list_id", "candidate"),
    [
        ("3gpp_tsg_ran_wg6_drafts", "R6"),
        ("3GPP_TSG_SA_WG2_DISCUSSIONS", "S2"),
        ("3GPP_TSG_CT_WG99_X", "C99"),
        ("3GPP_TSG_RAN_ADHOC", "RP"),
        ("3GPP_TSG_SA", "SP"),
        ("3GPP_TSG_CT_DRAFTS", "CP"),
        ("3GPP_TSG_SA4_FS_MED", None),
        ("OTHER_LIST", None),
    ],
)
def test_infer_listserv_tsg(list_id: str, candidate: str | None) -> None:
    assert infer_listserv_tsg(list_id) == candidate
