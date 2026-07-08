"""Unit tests for :class:`doc3gpp.models.tdoc_cr.TDocCRDetails`.

The dataclass is a frozen value object; the tests focus on the two
non-trivial aspects of its contract:

* :py:meth:`TDocCRDetails.__post_init__` rejects blank ``tdoc_id``
  values (the parser always supplies one, but a stale bug check is
  cheap insurance against future regressions).
* :py:meth:`TDocCRDetails.to_persisted` JSON-serialises the
  ``corrections`` field into a single ``corrections_json`` string
  suitable for the SQL ``TEXT`` column.

The ORM tests at the bottom of the file exercise the persistence
shape — :class:`TDocCrDetailOrm` and :class:`TDocExtractOrm` —
against an in-memory sqlite engine to confirm the metadata-based
``create_all`` registers the new tables and the column types match
the dataclass's ``to_persisted()`` contract. Both ORM tables are
keyed on the immutable download ``url`` so multiple revisions of the
same ``tdoc_id`` live at distinct rows.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import TDocCrDetailOrm
from doc3gpp.storage.db.models import TDocExtractOrm
from doc3gpp.storage.db.models import TDocORM


def test_default_construction_is_non_empty_corrections_list() -> None:
    """A blank ``TDocCRDetails`` (just a tdoc_id) has ``corrections == []``."""
    details = TDocCRDetails(tdoc_id="R5s260009")
    assert details.tdoc_id == "R5s260009"
    assert details.corrections == []
    # All other fields default to None.
    for field_name in (
        "spec",
        "cr_num",
        "rev",
        "version",
        "title",
        "source",
        "tsg",
        "related_wis",
        "date",
        "cr_cat",
        "release",
        "ats_version",
        "ttcn_release",
        "test_case",
        "test_suite",
        "ue",
        "ss",
        "year",
        "tech",
        "extracted_tdoc_id",
    ):
        assert getattr(details, field_name) is None, f"{field_name} should be None"


def test_post_init_rejects_blank_tdoc_id() -> None:
    """Pure whitespace or empty ``tdoc_id`` raises ``ValueError``.

    The dataclass is frozen, so a caller bypasses our guard if it
    sets ``tdoc_id`` directly via ``object.__setattr__``; the
    contract is enforced through the normal constructor path.
    """
    for blank in ["", "   ", "\t", "\n"]:
        with pytest.raises(ValueError, match="non-empty tdoc_id"):
            TDocCRDetails(tdoc_id=blank)


def test_post_init_strips_tdoc_id_whitespace() -> None:
    """Leading / trailing whitespace around a non-empty id is stripped."""
    details = TDocCRDetails(tdoc_id="  R5s260009\n")
    assert details.tdoc_id == "R5s260009"


def test_to_persisted_serialises_corrections_to_json_string() -> None:
    """``to_persisted`` joins corrections into a JSON string column."""
    details = TDocCRDetails(
        tdoc_id="R5s260176",
        spec="36.523-3",
        cr_num="4971",
        rev="0",
        version="18.11.0",
        title="Addition of LTE CEN test case 11.3.9",
        tsg="R5",
        cr_cat="B",
        release="Rel-18",
        year=2026,
        tech="LTE",
        ats_version="iwd-TTCN3-B2026-03_D26wk18",
        ttcn_release="26wk18",
        test_case="11.3.9",
        test_suite="IMS_EUTRA",
        corrections=[
            {
                "function_name": "fl_TC_11_3_9_Body",
                "reason_for_change": "not in line with NW behaviour",
                "summary_of_change": "called new function instead",
                "ttcn_module": "LTEIMS_Test.ttcn",
            },
            {
                "function_name": "f_TC_11_3_9_IMS2",
                "summary_of_change": "split into Step1 / Step2 functions",
            },
        ],
    )

    payload = details.to_persisted()
    assert payload["tdoc_id"] == "R5s260176"
    assert payload["spec"] == "36.523-3"
    assert payload["year"] == 2026
    assert payload["tech"] == "LTE"
    # ``corrections`` is replaced by a JSON string under
    # ``corrections_json``.
    assert "corrections" not in payload
    assert "corrections_json" in payload
    decoded = json.loads(payload["corrections_json"])
    assert decoded == details.corrections
    assert len(decoded) == 2
    assert decoded[0]["function_name"] == "fl_TC_11_3_9_Body"


def test_to_persisted_serialises_empty_corrections_list() -> None:
    """An empty corrections list serialises to ``"[]"`` rather than null."""
    details = TDocCRDetails(tdoc_id="R5-227476")
    payload = details.to_persisted()
    assert payload["corrections_json"] == "[]"
    assert details.corrections == []


def test_to_persisted_preserves_parser_version() -> None:
    """``parser_version`` is exposed in the persisted shape."""
    details = TDocCRDetails(tdoc_id="R5s260009")
    assert details.parser_version == "1.0.0"
    assert details.to_persisted()["parser_version"] == "1.0.0"


def test_dataclass_is_frozen() -> None:
    """Mutation of any field raises ``FrozenInstanceError``."""
    details = TDocCRDetails(tdoc_id="R5s260009")
    with pytest.raises(Exception):  # FrozenInstanceError, not raised by us
        details.tdoc_id = "R5s260010"  # type: ignore[misc]


def test_extract_meta_post_init_rejects_blank_url() -> None:
    """A blank ``ftp_url`` raises ``ValueError``; URL is the row identity."""
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocExtractMeta(
            ftp_url="",
            tdoc_id="R5s260009",
            zip_path="/tmp/z",
            markdown_path="/tmp/m",
            doc_filename="R5s260009.docx",
        )

    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocExtractMeta(
            ftp_url="   ",
            tdoc_id="R5s260009",
            zip_path="/tmp/z",
            markdown_path="/tmp/m",
            doc_filename="R5s260009.docx",
        )


def test_extract_meta_post_init_rejects_blank_tdoc_id() -> None:
    """A blank ``tdoc_id`` raises ``ValueError`` (FK target must exist)."""
    with pytest.raises(ValueError, match="non-empty tdoc_id"):
        TDocExtractMeta(
            ftp_url="example.com/r5s260009.zip",
            tdoc_id="",
            zip_path="/tmp/z",
            markdown_path="/tmp/m",
            doc_filename="R5s260009.docx",
        )


def test_extract_meta_post_init_strips_whitespace() -> None:
    """Leading/trailing whitespace on required fields is stripped."""
    meta = TDocExtractMeta(
        ftp_url="  example.com/r5s260009.zip\n",
        tdoc_id=" R5s260009 ",
        zip_path="/tmp/z",
        markdown_path="/tmp/m",
        doc_filename="R5s260009.docx",
    )
    assert meta.ftp_url == "example.com/r5s260009.zip"
    assert meta.tdoc_id == "R5s260009"


# ---------------------------------------------------------------------------
# ORM round-trip tests (Phase 5).
# ---------------------------------------------------------------------------


def _make_engine(*, foreign_keys: bool = False):
    """Build an in-memory sqlite engine.

    Args:
        foreign_keys: When ``True``, attach a ``PRAGMA foreign_keys=ON``
            connect listener. Off by default because the pragma is a
            per-connection side-effect and is only relevant for the
            cascade-delete test below.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    if foreign_keys:

        @event.listens_for(engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(engine)
    return engine


def _seed_tdoc(session, tdoc_id: str) -> TDocORM:
    """Insert a minimal parent TDoc row so the FK targets exist."""
    row = TDocORM(tdoc_id=tdoc_id, title="parent")
    session.add(row)
    session.flush()
    return row


def test_metadata_creates_new_tables() -> None:
    """``create_all`` registers the Phase 5 tables on a fresh engine."""
    engine = _make_engine()
    table_names = set(Base.metadata.tables.keys())
    assert "tdoc_cr_details" in table_names
    assert "tdoc_extracts" in table_names
    # Sanity check: the pre-existing tables are still registered.
    assert "tdocs" in table_names
    assert "tdoc_files" in table_names
    # And the on-disk tables match the metadata registration.
    inspector = inspect(engine)
    on_disk_tables = set(inspector.get_table_names())
    assert "tdoc_cr_details" in on_disk_tables
    assert "tdoc_extracts" in on_disk_tables


def test_tdoc_cr_detail_orm_round_trip() -> None:
    """Insert a fully-populated detail row, refresh, and assert equality."""
    engine = _make_engine()
    Session = sessionmaker(bind=engine)

    corrections_payload = json.dumps(
        [
            {
                "function_name": "fl_TC_11_3_9_Body",
                "reason_for_change": "not in line with NW behaviour",
                "summary_of_change": "called new function instead",
                "ttcn_module": "LTEIMS_Test.ttcn",
            }
        ]
    )

    url = "stored/R5s260176.zip"
    with Session() as session:
        _seed_tdoc(session, "R5s260176")
        row = TDocCrDetailOrm(
            ftp_url=url,
            tdoc_id="R5s260176",
            spec="36.523-3",
            cr_num="4971",
            rev="0",
            version="18.11.0",
            title="Addition of LTE CEN test case 11.3.9",
            source="RAN5",
            tsg="R5",
            related_wis="IMS5",
            date=date(2026, 3, 15),
            cr_cat="B",
            release="Rel-18",
            reason_for_change="NW behaviour diverged",
            consequences_if_not_approved="TC fails on the live NW",
            clauses_affected="11.3.9",
            other_comments="none",
            revision_history="rev0: initial",
            ats_version="iwd-TTCN3-B2026-03_D26wk18",
            ttcn_release="26wk18",
            test_case="11.3.9",
            test_suite="IMS_EUTRA",
            ue="UE-1",
            ss="SS-1",
            corrections=corrections_payload,
            year=2026,
            tech="LTE",
            extracted_tdoc_id="R5s260176",
        )
        session.add(row)
        session.commit()

    with Session() as session:
        loaded = session.get(TDocCrDetailOrm, url)
        assert loaded is not None
        assert loaded.ftp_url == url
        assert loaded.tdoc_id == "R5s260176"
        assert loaded.spec == "36.523-3"
        assert loaded.cr_num == "4971"
        assert loaded.rev == "0"
        assert loaded.version == "18.11.0"
        assert loaded.title == "Addition of LTE CEN test case 11.3.9"
        assert loaded.source == "RAN5"
        assert loaded.tsg == "R5"
        assert loaded.related_wis == "IMS5"
        assert loaded.date == date(2026, 3, 15)
        assert loaded.cr_cat == "B"
        assert loaded.release == "Rel-18"
        assert loaded.reason_for_change == "NW behaviour diverged"
        assert loaded.consequences_if_not_approved == "TC fails on the live NW"
        assert loaded.clauses_affected == "11.3.9"
        assert loaded.other_comments == "none"
        assert loaded.revision_history == "rev0: initial"
        assert loaded.ats_version == "iwd-TTCN3-B2026-03_D26wk18"
        assert loaded.ttcn_release == "26wk18"
        assert loaded.test_case == "11.3.9"
        assert loaded.test_suite == "IMS_EUTRA"
        assert loaded.ue == "UE-1"
        assert loaded.ss == "SS-1"
        assert loaded.year == 2026
        assert loaded.tech == "LTE"
        assert loaded.extracted_tdoc_id == "R5s260176"
        assert loaded.parser_version == "1.0.0"
        # ``corrections`` must round-trip as a JSON string at the
        # storage layer, not a parsed object — that is the contract
        # the dataclass's ``to_persisted`` honours.
        assert isinstance(loaded.corrections, str)
        assert json.loads(loaded.corrections) == json.loads(corrections_payload)
        assert loaded.extracted_at is not None


def test_tdoc_extract_orm_round_trip() -> None:
    """Insert a populated sidecar row, refresh, and assert equality."""
    engine = _make_engine()
    Session = sessionmaker(bind=engine)

    url = "stored/R5s260009.zip"
    with Session() as session:
        _seed_tdoc(session, "R5s260009")
        row = TDocExtractOrm(
            ftp_url=url,
            tdoc_id="R5s260009",
            zip_path="/cache/zips/R5s260009.zip",
            markdown_path="/cache/markdown/R5s260009.md",
            doc_filename="R5s260009.docx",
        )
        session.add(row)
        session.commit()

    with Session() as session:
        loaded = session.get(TDocExtractOrm, url)
        assert loaded is not None
        assert loaded.tdoc_id == "R5s260009"
        assert loaded.zip_path == "/cache/zips/R5s260009.zip"
        assert loaded.markdown_path == "/cache/markdown/R5s260009.md"
        assert loaded.doc_filename == "R5s260009.docx"
        assert loaded.parser_version == "1.0.0"
        assert loaded.extracted_at is not None


def test_tdoc_cr_detail_orm_minimal() -> None:
    """A detail row with just ``url`` + ``tdoc_id`` survives.

    All cover-page / TTCN columns are nullable; an early parse that
    only knows the URL + TDoc id (e.g. before the markdown body has
    been classified as a CR) must still be persistable.
    """
    engine = _make_engine()
    Session = sessionmaker(bind=engine)

    url = "stored/R5-227476.zip"
    with Session() as session:
        _seed_tdoc(session, "R5-227476")
        row = TDocCrDetailOrm(
            ftp_url=url,
            tdoc_id="R5-227476",
            parser_version="1.0.0",
        )
        session.add(row)
        session.commit()

    with Session() as session:
        loaded = session.get(TDocCrDetailOrm, url)
        assert loaded is not None
        assert loaded.ftp_url == url
        assert loaded.tdoc_id == "R5-227476"
        assert loaded.parser_version == "1.0.0"
        assert loaded.spec is None
        assert loaded.cr_num is None
        assert loaded.title is None
        assert loaded.corrections is None
        assert loaded.year is None
        assert loaded.tech is None
        assert loaded.extracted_at is not None


def test_multiple_revisions_for_same_tdoc_id() -> None:
    """Two URLs for the same ``tdoc_id`` land in distinct rows.

    Validates the URL-as-PK invariant: the same logical TDoc id can
    occupy multiple rows when the 3GPP asset is hosted at distinct
    URLs (e.g. ``R5s260009`` and a later ``R5s260009_rev2``).
    """
    engine = _make_engine()
    Session = sessionmaker(bind=engine)

    url_a = "stored/R5s260009.zip"
    url_b = "stored/R5s260009_rev2.zip"
    with Session() as session:
        _seed_tdoc(session, "R5s260009")
        session.add(
            TDocCrDetailOrm(
                ftp_url=url_a, tdoc_id="R5s260009", spec="38.523-3",
                cr_num="3790", rev="0",
            )
        )
        session.add(
            TDocCrDetailOrm(
                ftp_url=url_b, tdoc_id="R5s260009", spec="38.523-3",
                cr_num="3790", rev="2",
            )
        )
        session.commit()

    with Session() as session:
        rows = (
            session.scalars(
                select(TDocCrDetailOrm).where(
                    TDocCrDetailOrm.tdoc_id == "R5s260009"
                )
            )
            .all()
        )
        assert len(rows) == 2
        urls = {row.ftp_url for row in rows}
        assert urls == {url_a, url_b}
        revs = {row.rev for row in rows}
        assert revs == {"0", "2"}


def test_cascade_delete_via_fk() -> None:
    """Deleting a parent TDoc wipes the dependent detail + extract rows.

    The ``foreign_keys=True`` engine option attaches a
    ``PRAGMA foreign_keys=ON`` connect listener; sqlite's default is
    OFF, so we opt in for this test. The cascade is declared via
    ``ForeignKey(..., ondelete="CASCADE")`` on both child tables.
    Both child tables are now keyed by URL; the cascade fires when
    the parent TDoc row vanishes regardless of how many revisions
    exist.
    """
    engine = _make_engine(foreign_keys=True)
    Session = sessionmaker(bind=engine)

    url_a = "stored/R5s260051.zip"
    url_b = "stored/R5s260051_rev2.zip"
    with Session() as session:
        _seed_tdoc(session, "R5s260051")
        session.add(
            TDocCrDetailOrm(
                ftp_url=url_a,
                tdoc_id="R5s260051",
                spec="38.523-3",
                cr_num="3806",
            )
        )
        session.add(
            TDocCrDetailOrm(
                ftp_url=url_b,
                tdoc_id="R5s260051",
                spec="38.523-3",
                cr_num="3806",
            )
        )
        session.add(
            TDocExtractOrm(
                ftp_url=url_a,
                tdoc_id="R5s260051",
                zip_path="/z",
                markdown_path="/m",
                doc_filename="R5s260051.docx",
            )
        )
        session.add(
            TDocExtractOrm(
                ftp_url=url_b,
                tdoc_id="R5s260051",
                zip_path="/z2",
                markdown_path="/m2",
                doc_filename="R5s260051.docx",
            )
        )
        session.commit()

    with Session() as session:
        parent = session.get(TDocORM, "R5s260051")
        assert parent is not None
        session.delete(parent)
        session.commit()

    with Session() as session:
        assert session.get(TDocORM, "R5s260051") is None
        assert session.get(TDocCrDetailOrm, url_a) is None
        assert session.get(TDocCrDetailOrm, url_b) is None
        assert session.get(TDocExtractOrm, url_a) is None
        assert session.get(TDocExtractOrm, url_b) is None
