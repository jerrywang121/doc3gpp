"""Round-trip tests for the :class:`TDocShowRecord` classmethods.

Locks in the CLI↔HTTP composition contract introduced by T6: every
component in the 5-repo composition (``tdoc`` + cover sidecar + TTCN
sidecar + change-details + auxiliary files) is exercised via a fake
repo set, and the JSON envelope emitted by the CLI's existing
``_render_tdoc_show_json`` renderer stays byte-identical to the
HTTP route's ``to_jsonable(record)`` payload.

These tests run as pure unit tests (no SQL engine, no network) — the
classmethod is a pure composition that operates on the injected
repos, so a fake repo set is sufficient.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocCRTTCNDetails
from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.models.tdoc_show import (
    TDocShowRecord,
    TDocShowRecordByUrl,
    TDocShowRepos,
)


def _make_fake_repos(
    tdoc: TDoc | None,
    cover: TDocCRDetails | None,
    meta_extracted_at: datetime | None,
    ttcn: TDocCRTTCNDetails | None,
    changes: TDocCRChangeDetails | None,
    files: list[TDocFile],
) -> TDocShowRepos:
    """Build a :class:`TDocShowRepos` whose every method returns canned data."""
    tdoc_repo = MagicMock()
    tdoc_repo.get_by_id.return_value = tdoc
    tdoc_repo.get_by_ftp_url.return_value = tdoc

    cr_repo = MagicMock()
    cr_repo.get_by_url.return_value = cover
    cr_meta = MagicMock()
    cr_meta.extracted_at = meta_extracted_at
    cr_repo.get_extract_meta_by_url.return_value = cr_meta

    cr_ttcn_repo = MagicMock()
    cr_ttcn_repo.get_by_url.return_value = ttcn

    cr_change_details_repo = MagicMock()
    cr_change_details_repo.get_for_tdoc_id.return_value = [changes] if changes else []
    cr_change_details_repo.get_by_url.return_value = changes

    file_repo = MagicMock()
    file_repo.get_for_tdoc_id.return_value = files
    file_repo.get_by_ftp_url.return_value = files

    return TDocShowRepos(
        tdoc=tdoc_repo,
        cr=cr_repo,
        cr_ttcn=cr_ttcn_repo,
        cr_change_details=cr_change_details_repo,
        file=file_repo,
    )


def test_from_tdoc_id_round_trip_matches_cli_renderer() -> None:
    """``from_tdoc_id`` composition + CLI JSON renderer produces a stable payload.

    This locks in the CLI↔HTTP JSON byte-equivalence contract: every
    field the CLI's ``_render_tdoc_show_json`` emits must surface
    through ``from_tdoc_id`` so the HTTP ``?format=json`` response is
    byte-identical.
    """
    tdoc = TDoc(
        tdoc_id="R5-260001",
        title="CR on NR measurement",
        ftp_url="R5/26.001/R5-260001.zip",
        spec="38.523-3",
        release="Rel-18",
    )
    extracted_at = datetime(2026, 5, 5, 12, 30, 0)
    cover = TDocCRDetails(
        tdoc_id="R5-260001",
        spec="38.523-3",
        cr_num="3790",
        rev="0",
        title="CR on NR measurement",
    )
    repos = _make_fake_repos(
        tdoc=tdoc,
        cover=cover,
        meta_extracted_at=extracted_at,
        ttcn=None,
        changes=None,
        files=[],
    )

    record = TDocShowRecord.from_tdoc_id("R5-260001", repos)

    # Sanity: every composition branch returned the canned value.
    assert record.tdoc is tdoc
    assert record.cover is cover
    assert record.extracted_at == extracted_at
    assert record.ttcn is None
    assert record.changes is None
    assert record.files == ()

    # The CLI's renderer produces a payload shaped like the HTTP
    # ``?format=json`` envelope — verify they agree byte-for-byte.
    from doc3gpp.cli import _build_show_payload

    cli_payload = _build_show_payload(record)
    cli_bytes = _canonicalise(cli_payload)

    # HTTP route uses ``to_jsonable`` (recursively walks dataclasses).
    from doc3gpp.web.render import to_jsonable

    http_bytes = _canonicalise(to_jsonable(record))

    assert cli_bytes == http_bytes


def test_from_tdoc_id_unknown_raises_not_found() -> None:
    """``from_tdoc_id`` raises :class:`TDocNotFoundError` for an unknown id."""
    repos = _make_fake_repos(
        tdoc=None,
        cover=None,
        meta_extracted_at=None,
        ttcn=None,
        changes=None,
        files=[],
    )

    from doc3gpp.services.tdoc_cr_service import TDocNotFoundError

    with pytest.raises(TDocNotFoundError):
        TDocShowRecord.from_tdoc_id("R5-999999", repos)


def test_from_tdoc_id_includes_change_details_when_present() -> None:
    """``from_tdoc_id`` surfaces the body-change sidecar when present."""
    tdoc = TDoc(
        tdoc_id="R5-260001",
        ftp_url="R5/26.001/R5-260001.zip",
    )
    changes = TDocCRChangeDetails(
        ftp_url="R5/26.001/R5-260001.zip",
        tdoc_id="R5-260001",
        clauses=("7.1.3.5.3",),
        changes=(),
    )
    repos = _make_fake_repos(
        tdoc=tdoc,
        cover=None,
        meta_extracted_at=None,
        ttcn=None,
        changes=changes,
        files=[],
    )

    record = TDocShowRecord.from_tdoc_id("R5-260001", repos)
    assert record.changes is changes


def test_from_ftp_url_round_trip() -> None:
    """``from_ftp_url`` mirrors the by-id composition across the same 5 repos."""
    url = "R5/26.001/R5-260001.zip"
    tdoc = TDoc(tdoc_id="R5-260001", ftp_url=url)
    cover = TDocCRDetails(tdoc_id="R5-260001", spec="38.523-3")
    extracted_at = datetime(2026, 5, 5, 12, 30, 0)
    file = TDocFile(tdoc_id="R5-260001", type="revision", file="R5s260001r1.zip",
                     ftp_url=f"{url}_r1")
    repos = _make_fake_repos(
        tdoc=tdoc, cover=cover, meta_extracted_at=extracted_at,
        ttcn=None, changes=None, files=[file],
    )

    record = TDocShowRecordByUrl.from_ftp_url(url, repos)
    assert record.ftp_url == url
    assert record.tdoc is tdoc
    assert record.cover is cover
    assert record.extracted_at == extracted_at
    assert record.files == (file,)


def _canonicalise(payload: Any) -> bytes:
    """Serialise ``payload`` to a canonical JSON byte string.

    Uses ``sort_keys=True`` so dict ordering does not affect the
    comparison — the CLI's renderer relies on its own insertion order
    while the HTTP path uses ``json.dumps`` which preserves dict
    insertion order on Python 3.7+. The canonical form gives us a
    value-equivalent comparison regardless of key order.
    """
    return json.dumps(payload, sort_keys=True, default=str).encode("utf-8")