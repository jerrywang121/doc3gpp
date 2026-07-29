"""Tests for the TDocCRChangeDetails dataclass."""

from __future__ import annotations

import pytest

from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails


def test_default_construction() -> None:
    d = TDocCRChangeDetails(ftp_url="tsg_x/CR123.zip", tdoc_id="R5-123456")
    assert d.ftp_url == "tsg_x/CR123.zip"
    assert d.tdoc_id == "R5-123456"
    assert d.clauses == ()
    assert d.changes == ()


def test_clauses_are_tuple() -> None:
    d = TDocCRChangeDetails(
        ftp_url="u", tdoc_id="R5-1", clauses=("5.2.3", "5.2.4"),
    )
    assert d.clauses == ("5.2.3", "5.2.4")


def test_changes_are_tuple_of_tuples() -> None:
    d = TDocCRChangeDetails(
        ftp_url="u", tdoc_id="R5-1",
        changes=(("line one", "line two"), ("line three",)),
    )
    assert d.changes == (("line one", "line two"), ("line three",))


def test_ftp_url_none_is_accepted() -> None:
    """``None`` is the parser-side "unknown yet" sentinel; the service
    layer fills in the URL via :func:`dataclasses.replace` before
    persistence."""
    d = TDocCRChangeDetails(ftp_url=None, tdoc_id="R5-1")
    assert d.ftp_url is None


def test_tdoc_id_none_is_accepted() -> None:
    """``None`` is the parser-side "unknown yet" sentinel; the service
    layer fills in the TDoc id via :func:`dataclasses.replace` before
    persistence."""
    d = TDocCRChangeDetails(ftp_url="u", tdoc_id=None)
    assert d.tdoc_id is None


def test_empty_ftp_url_rejected() -> None:
    """An empty string is still a programmer error — the validation
    only relaxes for ``None``."""
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocCRChangeDetails(ftp_url="", tdoc_id="R5-1")


def test_empty_tdoc_id_rejected() -> None:
    """An empty string is still a programmer error — the validation
    only relaxes for ``None``."""
    with pytest.raises(ValueError, match="non-empty tdoc_id"):
        TDocCRChangeDetails(ftp_url="u", tdoc_id="")


def test_whitespace_stripped() -> None:
    d = TDocCRChangeDetails(ftp_url="  u  ", tdoc_id="  R5-1  ")
    assert d.ftp_url == "u"
    assert d.tdoc_id == "R5-1"
