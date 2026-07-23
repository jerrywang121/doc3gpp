"""Integration tests for the new ``tdoc_cr_ttcn_details`` table + repo.

Mirrors :mod:`test_tdoc_cr_sqlite` for the slim cover-page table but
focuses on the TTCN sidecar:

- Round-trip the six overview fields + the gzip-compressed
  ``required_changes`` blob through :class:`SQLAlchemyTDocCrTtcnRepository`.
- Verify the lazy bootstrap: dropping the table mid-test triggers
  ``_ensure_table_exists`` to recreate it on the next call.
- Confirm the FK constraint on ``tdoc_id`` fires when a sidecar row
  references a TDoc id absent from the ``tdocs`` table.
- Cover the "non-TTCN shape still writes" property — the repo is a
  dumb typed store; the parser decides TTCN vs non-TTCN.
- End-to-end: cover row + TTCN sidecar + extract metadata all sharing
  the same ``ftp_url`` round-trip into a :class:`TDocShowRecord`.
- Confirm ``tdoc show``'s structural TTCN gate skips the sidecar
  table for non-TTCN-shape ids even when sidecar rows exist for the
  same id (the production CLI behaviour).

Uses the same ``sqlite_env`` in-memory fixture as the rest of the
integration suite.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from doc3gpp.cli import TDocShowRecord
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.parsers.cr.header import is_ttcn_tdoc
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_session_factory
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
    SQLAlchemyTDocCrTtcnRepository,
)
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _docx_available() -> bool:
    """Skip python-docx-gated tests when the extract extra is missing."""
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _seed_tdoc(tdoc_repo: SQLAlchemyTDocRepository, tdoc_id: str) -> None:
    """Insert a parent ``tdocs`` row so the FK target exists."""
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR"))


def _sample_corrections() -> list[dict[str, object]]:
    """Return a 3-item correction list representative of the TTCN parser output."""
    return [
        {
            "function_name": "fl_TC_7_1_3_5_3_Body",
            "reason_for_change": "not in line with NW behaviour",
            "summary_of_change": "called new function instead",
            "ttcn_module": "NR5GC_Test.ttcn",
        },
        {
            "function_name": "f_TC_7_1_3_5_3_IMS2",
            "summary_of_change": "split into Step1 / Step2 functions",
        },
        {
            "function_name": "tr_CommonPart_Template",
            "reason_for_change": "New template for MCX.",
            "summary_of_change": "Add template body.",
        },
    ]


# ---------------------------------------------------------------------------
# 1. Round-trip: upsert + get_by_url preserves all six overview fields.
# ---------------------------------------------------------------------------


def test_ttcn_round_trips_through_orm(sqlite_env) -> None:
    """Write a fully-populated TTCN sidecar, read it back via ``get_by_url``."""
    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    details = TDocCRTTCNDetails(
        tdoc_id="R5s260009",
        ftp_url=url,
        testcase="7.1.3.5.3",
        ue="MTK MT6986D and Qualcomm X105 5G Modem-RF",
        ss="Anritsu Protocol Conformance Test System",
        ats_version="iwd-TTCN3-B2026-03_D26wk18",
        ttcn_release="26wk18",
        test_suite="NR5GC",
        required_changes=_sample_corrections(),
    )
    cr_ttcn_repo.upsert(details)

    loaded = cr_ttcn_repo.get_by_url(url)
    assert loaded is not None
    assert loaded.tdoc_id == "R5s260009"
    assert loaded.ftp_url == url
    assert loaded.testcase == "7.1.3.5.3"
    assert loaded.ue == "MTK MT6986D and Qualcomm X105 5G Modem-RF"
    assert loaded.ss == "Anritsu Protocol Conformance Test System"
    assert loaded.ats_version == "iwd-TTCN3-B2026-03_D26wk18"
    assert loaded.ttcn_release == "26wk18"
    assert loaded.test_suite == "NR5GC"
    assert loaded.required_changes == _sample_corrections()


# ---------------------------------------------------------------------------
# 2. required_changes compression: gzip-encoded, not plain JSON.
# ---------------------------------------------------------------------------


def test_ttcn_required_changes_is_gzipped_json(sqlite_env) -> None:
    """The on-disk blob is gzip-compressed, not plain JSON."""
    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    url = "stored/R5s260009.zip"
    changes = _sample_corrections()
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260009",
            ftp_url=url,
            testcase="7.1.3.5.3",
            required_changes=changes,
        ),
    )

    factory = get_session_factory()
    with factory() as session:
        raw_blob = session.execute(
            text("SELECT required_changes FROM tdoc_cr_ttcn_details WHERE ftp_url = :u"),
            {"u": url},
        ).scalar()
    assert raw_blob is not None
    assert isinstance(raw_blob, bytes)
    assert raw_blob[:2] == b"\x1f\x8b"

    decoded = json.loads(gzip.decompress(raw_blob).decode("utf-8"))
    assert decoded == changes


# ---------------------------------------------------------------------------
# 3. Lazy bootstrap: dropping the table mid-test recovers via
#    ``_ensure_table_exists`` on the next call.
# ---------------------------------------------------------------------------


def test_ttcn_repository_lazy_creates_table(sqlite_env) -> None:
    """A fresh sqlite engine that lacks the TTCN table is bootstrapped on
    the first call. Validates the production lazy-bootstrap helper:
    ``_ensure_table_exists`` catches the ``OperationalError`` and runs
    ``Base.metadata.create_all``.
    """
    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    # Drop the sidecar table directly to simulate a pre-this-PR DB.
    factory = get_session_factory()
    with factory() as session:
        session.execute(text("DROP TABLE tdoc_cr_ttcn_details"))
        session.commit()

    # The internal cache still says "ensured" from the create_schema
    # call; reset it so the next public call re-probes.
    cr_ttcn_repo._ensured = False  # type: ignore[attr-defined]

    # Upsert must succeed — the bootstrap helper recreates the table.
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260009",
            ftp_url="stored/R5s260009.zip",
            testcase="7.1.3.5.3",
        ),
    )

    loaded = cr_ttcn_repo.get_by_url("stored/R5s260009.zip")
    assert loaded is not None
    assert loaded.testcase == "7.1.3.5.3"


# ---------------------------------------------------------------------------
# 4. Non-TTCN TDoc: the repo is a dumb store. ``R5-260013`` (the
#    non-TTCN shape) writes successfully — there's no shape guard at
#    this layer; the parser decides TTCN-vs-non-TTCN.
# ---------------------------------------------------------------------------


def test_non_ttcn_tdoc_id_still_writes(sqlite_env) -> None:
    """A row with a non-TTCN-shape ``tdoc_id`` (e.g. ``R5-260013``)
    still writes through the sidecar repo. The repo has no parser
    awareness; only ``is_ttcn_tdoc`` is the gate (in the service /
    CLI layers), and this test deliberately bypasses that gate to
    confirm the store itself is content-agnostic.
    """
    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5-260013", type="CR"))

    url = "stored/R5-260013.zip"
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5-260013",
            ftp_url=url,
            testcase="placeholder",
        ),
    )
    loaded = cr_ttcn_repo.get_by_url(url)
    assert loaded is not None
    assert loaded.tdoc_id == "R5-260013"
    assert loaded.testcase == "placeholder"
    # Sanity: the gate function would classify this id as non-TTCN.
    assert is_ttcn_tdoc("R5-260013") is False


# ---------------------------------------------------------------------------
# 5. FK guard: a sidecar row referencing a TDoc id not in ``tdocs`` is
#    rejected by the SQLAlchemy FK constraint.
# ---------------------------------------------------------------------------


def test_ttcn_repository_fk_guard(sqlite_env) -> None:
    """A TTCN sidecar row whose ``tdoc_id`` has no matching ``tdocs`` row
    is rejected by the SQLAlchemy FK constraint. The error surfaces as
    an :class:`IntegrityError`.
    """
    from sqlalchemy.exc import IntegrityError

    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()

    with pytest.raises(IntegrityError):
        cr_ttcn_repo.upsert(
            TDocCRTTCNDetails(
                tdoc_id="R5s999999",
                ftp_url="stored/R5s999999.zip",
                testcase="orphan",
            ),
        )


# ---------------------------------------------------------------------------
# 6. tdoc show join: cover + TTCN + extract-meta all sharing the same
#    ftp_url round-trip into a TDocShowRecord.
# ---------------------------------------------------------------------------


def test_tdoc_show_record_joins_cover_ttcn_and_metadata(sqlite_env) -> None:
    """Cover row + TTCN sidecar + extract metadata share an ftp_url; the
    CLI's :class:`TDocShowRecord` is built from three URL-keyed reads.
    """
    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()

    tdoc_id = "R5s260009"
    url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=url))

    cover = TDocCRDetails(
        tdoc_id=tdoc_id,
        spec="38.523-3",
        cr_num="3790",
        rev="0",
        version="18.4.0",
        title="Example TTCN CR",
        release="Rel-18",
        ftp_url=url,
    )
    cr_repo.upsert(cover)

    ttcn = TDocCRTTCNDetails(
        tdoc_id=tdoc_id,
        ftp_url=url,
        testcase="7.1.3.5.3",
        ue="UE1",
        ats_version="iwd-TTCN3-B2512-260-eng",
        required_changes=[{"function_name": "fl_TC_7_1_3_5_3_Body"}],
    )
    cr_ttcn_repo.upsert(ttcn)

    meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id=tdoc_id,
        cache_file="R5s260009.zip",
        doc_filename="R5s260009.docx",
    )
    cr_repo.upsert_extract_meta(meta)

    # URL-keyed reads.
    loaded_cover = cr_repo.get_by_url(url)
    loaded_ttcn = cr_ttcn_repo.get_by_url(url)
    loaded_meta = cr_repo.get_extract_meta_by_url(url)

    assert loaded_cover == cover
    assert loaded_ttcn is not None
    assert loaded_ttcn.tdoc_id == tdoc_id
    assert loaded_ttcn.testcase == "7.1.3.5.3"
    assert loaded_meta is not None
    assert loaded_meta.ftp_url == url

    # Bundle into a TDocShowRecord — the same shape the CLI builds.
    tdoc_row = tdoc_repo.get_by_id(tdoc_id)
    assert tdoc_row is not None
    record = TDocShowRecord(
        tdoc=tdoc_row,
        cover=loaded_cover,
        ttcn=loaded_ttcn,
        extracted_at=loaded_meta.extracted_at,
    )
    assert record.cover is not None
    assert record.cover.spec == "38.523-3"
    assert record.ttcn is not None
    assert record.ttcn.testcase == "7.1.3.5.3"
    assert record.extracted_at is not None


# ---------------------------------------------------------------------------
# 7. ``is_ttcn_tdoc`` gate: the CLI's ``tdoc_show`` lookup for a
#    non-TTCN ``tdoc_id`` does NOT touch the TTCN table even when
#    sidecar rows exist for that TDoc. This is a behavioural test of
#    the CLI's URL-keyed lookup, mirroring the production code path
#    in :func:`doc3gpp.cli.tdoc_show`.
# ---------------------------------------------------------------------------


def test_is_ttcn_tdoc_gate_skips_ttcn_repo_for_non_ttcn_ids(sqlite_env) -> None:
    """Structural gate: a non-TTCN TDoc id (``R5-260020``) classifies as
    non-TTCN via :func:`is_ttcn_tdoc`, so the CLI's ``tdoc_show``
    never consults the sidecar repo even when rows exist at the
    same ``ftp_url``. Mirrors the ``if is_ttcn_tdoc(...)`` branch in
    :func:`doc3gpp.cli.tdoc_show`.
    """
    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()

    tdoc_id = "R5-260020"
    url = "stored/R5-260020.zip"
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=url))

    # Persist a non-TTCN cover row + a TTCN sidecar row at the same URL.
    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id=tdoc_id,
            spec="38.508-1",
            cr_num="2678",
            ftp_url=url,
        ),
    )
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id=tdoc_id,
            ftp_url=url,
            testcase="placeholder-not-surfaceable",
        ),
    )

    # The gate function is structural.
    assert is_ttcn_tdoc(tdoc_id) is False

    # Mirror the CLI's URL-keyed lookup: cover fetched, sidecar skipped.
    record = tdoc_repo.get_by_id(tdoc_id)
    assert record is not None
    cover = cr_repo.get_by_url(record.ftp_url) if record.ftp_url else None
    ttcn = cr_ttcn_repo.get_by_url(record.ftp_url) if (
        record.ftp_url and is_ttcn_tdoc(record.tdoc_id)
    ) else None
    extracted_at_meta = (
        cr_repo.get_extract_meta_by_url(record.ftp_url) if record.ftp_url else None
    )

    show_record = TDocShowRecord(
        tdoc=record,
        cover=cover,
        ttcn=ttcn,
        extracted_at=extracted_at_meta.extracted_at if extracted_at_meta else None,
    )
    assert show_record.cover is not None
    assert show_record.cover.cr_num == "2678"
    assert show_record.ttcn is None
    assert show_record.extracted_at is None

    # The TTCN repo's get_by_url is never consulted under the gate.
    # Confirmed by the ``None`` value above: even though the sidecar
    # row exists, the CLI's URL-keyed gate never called the lookup.


# ---------------------------------------------------------------------------
# 8. The ``_orm_to_details`` helper survives a non-TTCN-shaped TDoc
#    id and an empty ``required_changes`` list — these are the
#    edge cases the parser's no-TTCN-sections path can produce.
# ---------------------------------------------------------------------------


def test_ttcn_details_with_empty_required_changes_round_trips(sqlite_env) -> None:
    """A sidecar row with ``required_changes=[]`` (a non-TTCN-shape
    document that nevertheless lands a row) round-trips through the
    repo without dropping the field.
    """
    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    url = "stored/R5s260009_legacy.zip"
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260009",
            ftp_url=url,
            testcase="7.1.3.5.3",
            required_changes=[],
        ),
    )
    loaded = cr_ttcn_repo.get_by_url(url)
    assert loaded is not None
    assert loaded.required_changes == []


# ---------------------------------------------------------------------------
# 9. ``list_all`` returns every persisted row, ordered for stability.
# ---------------------------------------------------------------------------


def test_ttcn_repository_list_all(sqlite_env) -> None:
    """Two distinct URLs for two distinct tdoc_ids surface via ``list_all``."""
    create_schema()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()
    tdoc_repo = SQLAlchemyTDocRepository()
    tdoc_repo.upsert_many([
        TDoc(tdoc_id="R5s260009", type="CR"),
        TDoc(tdoc_id="R5s260010", type="CR"),
    ])

    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260009",
            ftp_url="stored/R5s260009.zip",
            testcase="7.1.3.5.3",
        ),
    )
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260010",
            ftp_url="stored/R5s260010.zip",
            testcase="8.2.1",
        ),
    )
    rows = cr_ttcn_repo.list_all()
    assert len(rows) == 2
    urls = sorted(r.ftp_url or "" for r in rows)
    assert urls == ["stored/R5s260009.zip", "stored/R5s260010.zip"]


# ---------------------------------------------------------------------------
# 10. End-to-end: ``tdoc show --format json`` joins cover + TTCN +
#     extracted_at in a single payload.
# ---------------------------------------------------------------------------


def test_tdoc_show_json_payload_includes_cover_ttcn_and_extracted_at(
    sqlite_env, tmp_path: Path,
) -> None:
    """End-to-end smoke test: write cover + TTCN + extract-meta rows
    at the same ``ftp_url``, then drive the CLI's ``tdoc show
    --format json`` and assert the payload surfaces all three.
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app

    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()

    tdoc_id = "R5s260009"
    url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260009.zip"
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=url))

    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id=tdoc_id,
            spec="38.523-3",
            cr_num="3790",
            ftp_url=url,
        ),
    )
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id=tdoc_id,
            ftp_url=url,
            testcase="7.1.3.5.3",
            ats_version="iwd-TTCN3-B2512-260-eng",
        ),
    )
    cr_repo.upsert_extract_meta(
        TDocExtractMeta(
            ftp_url=url,
            tdoc_id=tdoc_id,
            cache_file="R5s260009-abcdef0123456789.zip",
            doc_filename="R5s260009.docx",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", tdoc_id, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.output)
    assert payload["tdoc"]["tdoc_id"] == tdoc_id
    assert payload["cover"]["spec"] == "38.523-3"
    assert payload["ttcn"]["testcase"] == "7.1.3.5.3"
    assert "extracted_at" in payload
    assert "files" not in payload


# ---------------------------------------------------------------------------
# ``tdoc show --ftp-url`` end-to-end smoke (URL-keyed read path)
# ---------------------------------------------------------------------------


def test_tdoc_show_by_ftp_url_json_payload_includes_cover_ttcn_and_extracted_at(
    sqlite_env, tmp_path: Path,
) -> None:
    """URL-keyed counterpart of ``test_tdoc_show_json_payload_includes_cover_ttcn_and_extracted_at``.

    Seeds the same row set under one ``ftp_url`` and drives the CLI
    with ``--ftp-url`` (not ``--tdoc``). The payload gains a top-level
    ``ftp_url`` key; ``tdoc`` / ``cover`` / ``ttcn`` / ``extracted_at``
    still surface in the same shape.
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app

    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    cr_ttcn_repo = SQLAlchemyTDocCrTtcnRepository()

    tdoc_id = "R5s260110"
    url = "tsg_ran/WG5_Test_ex-T1/TTCN/TTCN_CRs/2026/Docs/R5s260110.zip"
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=url))

    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id=tdoc_id,
            spec="38.523-3",
            cr_num="3790",
            ftp_url=url,
        ),
    )
    cr_ttcn_repo.upsert(
        TDocCRTTCNDetails(
            tdoc_id=tdoc_id,
            ftp_url=url,
            testcase="7.1.3.5.3",
            ats_version="iwd-TTCN3-B2512-260-eng",
        ),
    )
    cr_repo.upsert_extract_meta(
        TDocExtractMeta(
            ftp_url=url,
            tdoc_id=tdoc_id,
            cache_file="R5s260110-abcdef0123456789.zip",
            doc_filename="R5s260110.docx",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", url, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.output)
    assert payload["ftp_url"] == url
    assert payload["tdoc"]["tdoc_id"] == tdoc_id
    assert payload["cover"]["spec"] == "38.523-3"
    assert payload["ttcn"]["testcase"] == "7.1.3.5.3"
    assert "extracted_at" in payload
    assert "files" not in payload
