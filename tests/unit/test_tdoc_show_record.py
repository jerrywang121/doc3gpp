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
from datetime import datetime, timezone
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
    """Build a TDocShowRepos whose methods return canned data."""
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

    inventory_repo = MagicMock()
    inventory_repo.get_by_url.return_value = None

    return TDocShowRepos(
        tdoc=tdoc_repo,
        cr=cr_repo,
        cr_ttcn=cr_ttcn_repo,
        cr_change_details=cr_change_details_repo,
        file=file_repo,
        zip_inventory=inventory_repo,
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
    extracted_at = datetime(2026, 5, 5, 12, 30, 0, tzinfo=timezone.utc)
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


def test_change_details_round_trip_matches_cli_renderer() -> None:
    """The change-details sidecar serialises exactly like the CLI.

    The CLI's ``_build_show_payload`` emits only the body-derived
    fields (``clauses`` + ``changes``) for the sidecar and drops the
    row-identity fields (``ftp_url`` / ``tdoc_id``). ``to_jsonable``
    must reproduce that shape byte-for-byte, including the per-block
    ``{clauses, text}`` structure.
    """
    tdoc = TDoc(
        tdoc_id="R5-260001",
        ftp_url="R5/26.001/R5-260001.zip",
    )
    changes = TDocCRChangeDetails(
        ftp_url="R5/26.001/R5-260001.zip",
        tdoc_id="R5-260001",
        clauses=("7.1.3.5.3", "5.2.4 (new)"),
        changes=(
            {"clauses": ["7.1.3.5.3"], "text": "line one\nline two"},
            {"clauses": ["5.2.4 (new)"], "text": "<ins>new heading</ins>"},
        ),
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

    from doc3gpp.cli import _build_show_payload
    from doc3gpp.web.render import to_jsonable

    cli_bytes = _canonicalise(_build_show_payload(record))
    http_bytes = _canonicalise(to_jsonable(record))

    assert cli_bytes == http_bytes

    # The HTTP-side payload must NOT leak the row-identity fields.
    payload = to_jsonable(record)
    assert "ftp_url" not in payload["changes"]
    assert "tdoc_id" not in payload["changes"]
    assert payload["changes"]["clauses"] == ["7.1.3.5.3", "5.2.4 (new)"]
    assert payload["changes"]["changes"] == [
        {"clauses": ["7.1.3.5.3"], "text": "line one\nline two"},
        {"clauses": ["5.2.4 (new)"], "text": "<ins>new heading</ins>"},
    ]


def test_from_ftp_url_round_trip() -> None:
    """``from_ftp_url`` mirrors the by-id composition across the same 5 repos."""
    url = "R5/26.001/R5-260001.zip"
    tdoc = TDoc(tdoc_id="R5-260001", ftp_url=url)
    cover = TDocCRDetails(tdoc_id="R5-260001", spec="38.523-3")
    extracted_at = datetime(2026, 5, 5, 12, 30, 0, tzinfo=timezone.utc)
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


def test_raw_only_extract_by_id_points_at_raw_markdown() -> None:
    """An extract without a cover row carries a note naming the raw markdown."""
    tdoc = TDoc(tdoc_id="R5-260002", ftp_url="R5/26.001/R5-260002.zip")
    extracted_at = datetime(2026, 5, 5, 12, 30, 0, tzinfo=timezone.utc)
    repos = _make_fake_repos(
        tdoc=tdoc, cover=None, meta_extracted_at=extracted_at,
        ttcn=None, changes=None, files=[],
    )

    record = TDocShowRecord.from_tdoc_id("R5-260002", repos)

    assert record.note is not None
    assert "get_tdoc_content" in record.note
    assert "tdoc_id='R5-260002'" in record.note
    assert "doc3gpp tdoc show --tdoc R5-260002 --format raw" in record.note

    from doc3gpp.cli import _build_show_payload
    from doc3gpp.web.render import to_jsonable

    cli_payload = _build_show_payload(record)
    assert cli_payload["note"] == record.note
    assert _canonicalise(cli_payload) == _canonicalise(to_jsonable(record))


def test_raw_only_extract_by_url_points_at_raw_markdown() -> None:
    url = "R5/26.001/R5-260002.zip"
    tdoc = TDoc(tdoc_id="R5-260002", ftp_url=url)
    extracted_at = datetime(2026, 5, 5, 12, 30, 0, tzinfo=timezone.utc)
    repos = _make_fake_repos(
        tdoc=tdoc, cover=None, meta_extracted_at=extracted_at,
        ttcn=None, changes=None, files=[],
    )

    record = TDocShowRecordByUrl.from_ftp_url(url, repos)

    assert record.note is not None
    assert f"ftp_url='{url}'" in record.note
    assert f"doc3gpp tdoc show --ftp-url {url} --format raw" in record.note

    from doc3gpp.cli import _build_show_payload
    from doc3gpp.web.render import to_jsonable

    cli_payload = _build_show_payload(
        record, anchor_key="ftp_url", anchor_value=url
    )
    assert cli_payload["note"] == record.note
    assert _canonicalise(cli_payload) == _canonicalise(to_jsonable(record))


def test_no_note_without_extract_or_when_cover_present() -> None:
    """The note appears only for an extract that has no cover row."""
    tdoc = TDoc(tdoc_id="R5-260003", ftp_url="R5/26.001/R5-260003.zip")
    no_extract = _make_fake_repos(
        tdoc=tdoc, cover=None, meta_extracted_at=None,
        ttcn=None, changes=None, files=[],
    )
    record = TDocShowRecord.from_tdoc_id("R5-260003", no_extract)
    assert record.note is None
    from doc3gpp.web.render import to_jsonable

    assert "note" not in to_jsonable(record)

    cover = TDocCRDetails(tdoc_id="R5-260003", spec="38.523-3")
    with_cover = _make_fake_repos(
        tdoc=tdoc, cover=cover,
        meta_extracted_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        ttcn=None, changes=None, files=[],
    )
    record = TDocShowRecord.from_tdoc_id("R5-260003", with_cover)
    assert record.note is None
    assert "note" not in to_jsonable(record)



def test_detail_selectors_expose_unknown_empty_and_ordered_inventory(sqlite_env) -> None:
    """Both URL and ID details expose the persisted outer-ZIP inventory."""
    import json

    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.models.tdoc_show import (
        TDocShowRecord,
        TDocShowRecordByUrl,
        TDocShowRepos,
    )
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    create_schema("main")
    tdoc_repo = SQLAlchemyTDocRepository()
    inventory_repo = SQLAlchemyTDocZipInventoryRepository()
    file_repo = SQLAlchemyTDocFileRepository()
    urls = {
        "unknown": "archive/R5-INV-UNKNOWN.zip",
        "empty": "archive/R5-INV-EMPTY.zip",
        "known": "archive/R5-INV-KNOWN.zip",
    }
    tdoc_ids = {
        key: f"R5-INV-{key.upper()}" for key in urls
    }
    for key, url in urls.items():
        tdoc_repo.upsert(
            TDoc(tdoc_id=tdoc_ids[key], type="CR", ftp_url=url)
        )
    known_files = ("a.docx", "support.pdf", "support.pdf")
    inventory_repo.upsert(
        TDocZipInventory(
            ftp_url=urls["empty"],
            tdoc_id=tdoc_ids["empty"],
            zip_files=(),
        )
    )
    inventory_repo.upsert(
        TDocZipInventory(
            ftp_url=urls["known"],
            tdoc_id=tdoc_ids["known"],
            zip_files=known_files,
        )
    )
    file_repo.upsert_many(
        [
            TDocFile(
                ftp_url="archive/R5-INV-KNOWN-review.zip",
                tdoc_id=tdoc_ids["known"],
                type="review",
                file="R5-INV-KNOWN-review.zip",
            )
        ]
    )
    repos = TDocShowRepos(
        tdoc=tdoc_repo,
        cr=SQLAlchemyTDocCrRepository(),
        cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
        file=file_repo,
        zip_inventory=inventory_repo,
    )

    from doc3gpp.cli import _build_show_payload
    from doc3gpp.web.render import to_jsonable

    expected = {
        "unknown": None,
        "empty": [],
        "known": list(known_files),
    }
    records = []
    for key, tdoc_id in tdoc_ids.items():
        records.append((TDocShowRecord.from_tdoc_id(tdoc_id, repos), key, None))
        records.append(
            (
                TDocShowRecordByUrl.from_ftp_url(urls[key], repos),
                key,
                urls[key],
            )
        )

    for record, key, url in records:
        assert record.zip_files == (None if expected[key] is None else tuple(expected[key]))
        anchor = (
            {"anchor_key": "ftp_url", "anchor_value": url}
            if url is not None
            else {}
        )
        cli_payload = _build_show_payload(record, **anchor)
        http_payload = to_jsonable(record)
        assert cli_payload["zip_files"] == expected[key]
        assert http_payload["zip_files"] == expected[key]
        assert list(cli_payload) == list(http_payload)
        assert json.dumps(
            cli_payload, ensure_ascii=False, separators=(",", ":")
        ) == json.dumps(
            http_payload, ensure_ascii=False, separators=(",", ":")
        )
        if key == "known" and url is None:
            assert cli_payload["files"][0]["type"] == "review"
            assert cli_payload["zip_files"] == [
                "a.docx",
                "support.pdf",
                "support.pdf",
            ]



def test_show_records_preserve_positional_fields_and_json_order() -> None:
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_file import TDocFile
    from doc3gpp.models.tdoc_show import TDocShowRecord, TDocShowRecordByUrl
    from doc3gpp.web.render import to_jsonable

    tdoc = TDoc(tdoc_id="R5-POSITIONAL", type="CR")
    files = (
        TDocFile(
            tdoc_id=tdoc.tdoc_id,
            type="review",
            file="R5-POSITIONAL-review.zip",
            ftp_url="archive/R5-POSITIONAL-review.zip",
        ),
    )
    note = "read the raw Markdown"
    url = "archive/R5-POSITIONAL.zip"

    by_id = TDocShowRecord(tdoc, None, None, None, None, files, note)
    assert by_id.files == files
    assert by_id.note == note
    assert by_id.zip_files is None

    by_url = TDocShowRecordByUrl(url, tdoc, None, None, None, None, files, note)
    assert by_url.files == files
    assert by_url.note == note
    assert by_url.zip_files is None

    by_id_with_inventory = TDocShowRecord(
        tdoc,
        None,
        None,
        None,
        None,
        files,
        note,
        zip_files=("main.docx",),
    )
    by_url_with_inventory = TDocShowRecordByUrl(
        url,
        tdoc,
        None,
        None,
        None,
        None,
        files,
        note,
        zip_files=("main.docx",),
    )
    assert by_id_with_inventory.zip_files == ("main.docx",)
    assert by_url_with_inventory.zip_files == ("main.docx",)

    by_id_payload = to_jsonable(by_id_with_inventory)
    by_url_payload = to_jsonable(by_url_with_inventory)
    assert list(by_id_payload) == ["tdoc", "zip_files", "files", "note"]
    assert list(by_url_payload) == [
        "ftp_url",
        "tdoc",
        "zip_files",
        "files",
        "note",
    ]
    assert by_id_payload["zip_files"] == ["main.docx"]
    assert by_id_payload["files"][0]["file"] == "R5-POSITIONAL-review.zip"
    assert by_id_payload["note"] == note


def test_by_id_uses_current_url_and_by_url_finds_old_inventory(sqlite_env) -> None:
    """ID reads use the current URL while URL reads can find historical inventory."""
    from doc3gpp.models.tdoc import TDoc
    from doc3gpp.models.tdoc_show import (
        TDocShowRecord,
        TDocShowRecordByUrl,
        TDocShowRepos,
    )
    from doc3gpp.models.tdoc_zip_inventory import TDocZipInventory
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_file_sql import (
        SQLAlchemyTDocFileRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
    from doc3gpp.storage.repositories.tdoc_zip_inventory_sql import (
        SQLAlchemyTDocZipInventoryRepository,
    )

    create_schema("main")
    tdoc_id = "R5s260001"
    old_url = "archive/R5s260001-r1.zip"
    current_url = "archive/r5s260001-r2.zip"
    tdoc_repo = SQLAlchemyTDocRepository()
    inventory_repo = SQLAlchemyTDocZipInventoryRepository()
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=current_url))
    inventory_repo.upsert(
        TDocZipInventory(ftp_url=old_url, tdoc_id=tdoc_id, zip_files=())
    )
    inventory_repo.upsert(
        TDocZipInventory(
            ftp_url=current_url,
            tdoc_id=tdoc_id,
            zip_files=("current.docx",),
        )
    )
    repos = TDocShowRepos(
        tdoc=tdoc_repo,
        cr=SQLAlchemyTDocCrRepository(),
        cr_ttcn=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details=SQLAlchemyTDocCrChangeDetailsRepository(),
        file=SQLAlchemyTDocFileRepository(),
        zip_inventory=inventory_repo,
    )

    by_id = TDocShowRecord.from_tdoc_id(tdoc_id, repos)
    historical = TDocShowRecordByUrl.from_ftp_url(old_url, repos)

    assert by_id.tdoc.ftp_url == current_url
    assert by_id.zip_files == ("current.docx",)
    assert historical.tdoc is None
    assert historical.zip_files == ()
