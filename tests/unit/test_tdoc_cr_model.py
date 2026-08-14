"""Unit tests for the slim :class:`doc3gpp.models.tdoc_cr.TDocCRDetails`
plus the new :class:`TDocCRTTCNDetails` / :class:`TDocCRParseResult`
value objects.

The dataclasses are frozen value objects; the tests focus on:

* :py:meth:`TDocCRDetails.__post_init__` rejects blank ``tdoc_id``
  values (the parser always supplies one, but a stale bug check is
  cheap insurance against future regressions).
* :py:meth:`TDocCRTTCNDetails.__post_init__` rejects blank ``tdoc_id``
  (FK target must exist) and blank ``ftp_url`` when set.
* :class:`TDocCRParseResult` bundles cover-page + optional TTCN
  sidecar.

The ORM tests at the bottom of the file exercise the persistence
shape — :class:`TDocCrDetailOrm` and :class:`TDocExtractOrm` —
against an in-memory sqlite engine to confirm the metadata-based
``create_all`` registers the tables and the column types match the
dataclass's ``to_persisted()`` contract. Both ORM tables are keyed
on the immutable download ``url`` so multiple revisions of the same
``tdoc_id`` live at distinct rows.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import TDocCrDetailOrm
from doc3gpp.storage.db.models import TDocExtractOrm
from doc3gpp.storage.db.models import TDocORM


def test_default_construction_has_none_fields() -> None:
    """A blank ``TDocCRDetails`` (just a tdoc_id) has all optional fields None."""
    details = TDocCRDetails(tdoc_id="R5s260009")
    assert details.tdoc_id == "R5s260009"
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
        "extracted_tdoc_id",
        "ftp_url",
    ):
        assert getattr(details, field_name) is None, f"{field_name} should be None"


def test_details_has_no_blob_or_parser_version_fields() -> None:
    """The slim dataclass no longer carries ``details`` or ``parser_version``."""
    details = TDocCRDetails(tdoc_id="R5s260009")
    # The legacy attributes must be gone — accessing them raises
    # ``AttributeError`` on a ``slots=True`` dataclass.
    with pytest.raises(AttributeError):
        details.details  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        details.parser_version  # type: ignore[attr-defined]


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


def test_to_persisted_omits_details_and_parser_version() -> None:
    """``to_persisted`` shape carries cover-page columns only."""
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
    )

    payload = details.to_persisted()
    assert payload["tdoc_id"] == "R5s260176"
    assert payload["spec"] == "36.523-3"
    # Legacy fields are gone from the persisted shape.
    assert "details_json" not in payload
    assert "details" not in payload
    assert "parser_version" not in payload


def test_tdoc_cr_details_summary_of_change_default_is_none() -> None:
    details = TDocCRDetails(tdoc_id="R5-227476")
    assert details.summary_of_change is None


def test_tdoc_cr_details_summary_of_change_round_trip() -> None:
    details = TDocCRDetails(
        tdoc_id="R5-227476",
        summary_of_change="Add USIM config setter.",
    )
    assert details.summary_of_change == "Add USIM config setter."
    persisted = details.to_persisted()
    assert persisted["summary_of_change"] == "Add USIM config setter."
    assert persisted["tdoc_id"] == "R5-227476"


def test_tdoc_cr_details_summary_of_change_none_in_persisted() -> None:
    details = TDocCRDetails(tdoc_id="R5-227476")
    persisted = details.to_persisted()
    assert persisted["summary_of_change"] is None


def test_dataclass_is_frozen() -> None:
    """Mutation of any field raises ``FrozenInstanceError``."""
    details = TDocCRDetails(tdoc_id="R5s260009")
    with pytest.raises(Exception):  # FrozenInstanceError, not raised by us
        details.tdoc_id = "R5s260010"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TDocCRTTCNDetails — slim sidecar dataclass.
# ---------------------------------------------------------------------------


def test_ttcn_details_default_construction() -> None:
    """A blank :class:`TDocCRTTCNDetails` (just a tdoc_id) has empty optionals."""
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009")
    assert ttcn.tdoc_id == "R5s260009"
    assert ttcn.ftp_url is None
    assert ttcn.testcase is None
    assert ttcn.ue is None
    assert ttcn.ss is None
    assert ttcn.ats_version is None
    assert ttcn.ttcn_release is None
    assert ttcn.test_suite is None
    assert ttcn.required_changes == []


def test_ttcn_details_carries_overview_fields_and_corrections() -> None:
    """All six overview fields and a populated corrections list round-trip."""
    changes = [
        {
            "function_name": "fl_TC_11_3_9_Body",
            "reason_for_change": "not in line with NW behaviour",
            "summary_of_change": "called new function instead",
            "ttcn_module": "LTEIMS_Test.ttcn",
        },
    ]
    ttcn = TDocCRTTCNDetails(
        tdoc_id="R5s260176",
        ftp_url="stored/R5s260176.zip",
        testcase="11.3.9",
        ue="UE1",
        ss="Anritsu Protocol Conformance Test System",
        ats_version="iwd-TTCN3-B2026-03_D26wk18",
        ttcn_release="26wk18",
        test_suite="IMS_EUTRA",
        required_changes=changes,
    )
    assert ttcn.testcase == "11.3.9"
    assert ttcn.required_changes == changes


def test_ttcn_details_post_init_rejects_blank_tdoc_id() -> None:
    """Blank ``tdoc_id`` raises (FK target must exist)."""
    with pytest.raises(ValueError, match="non-empty tdoc_id"):
        TDocCRTTCNDetails(tdoc_id="", ftp_url="stored/R5s260009.zip")


def test_ttcn_details_post_init_strips_tdoc_id_whitespace() -> None:
    """Leading/trailing whitespace around the id is stripped."""
    ttcn = TDocCRTTCNDetails(tdoc_id="  R5s260009\n", ftp_url="stored/x.zip")
    assert ttcn.tdoc_id == "R5s260009"


def test_ttcn_details_post_init_rejects_blank_ftp_url() -> None:
    """Setting ``ftp_url`` to an empty/whitespace string raises."""
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocCRTTCNDetails(tdoc_id="R5s260009", ftp_url="")

    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocCRTTCNDetails(tdoc_id="R5s260009", ftp_url="   ")


def test_ttcn_details_post_init_strips_ftp_url_whitespace() -> None:
    """Setting ``ftp_url`` with surrounding whitespace normalises the value."""
    ttcn = TDocCRTTCNDetails(
        tdoc_id="R5s260009", ftp_url="  stored/R5s260009.zip\n",
    )
    assert ttcn.ftp_url == "stored/R5s260009.zip"


def test_ttcn_details_allows_none_ftp_url() -> None:
    """``ftp_url=None`` is the parser-side default; the URL is supplied
    later by the service layer before persistence."""
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009", ftp_url=None)
    assert ttcn.ftp_url is None


def test_ttcn_details_default_changed_functions() -> None:
    """Constructing a :class:`TDocCRTTCNDetails` without an explicit
    ``changed_functions`` kwarg yields an empty list (the dataclass
    field default). The parser-side helper is responsible for filling
    the aggregate; a bare constructor leaves it untouched."""
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009")
    assert ttcn.changed_functions == []


def test_ttcn_details_changed_functions_round_trip() -> None:
    """An explicit ``changed_functions`` value round-trips through
    :class:`TDocCRTTCNDetails` unchanged. The dataclass is a frozen
    value object — it does no derivation of its own."""
    ttcn = TDocCRTTCNDetails(
        tdoc_id="R5s260009",
        changed_functions=["A.f1", "B.f2"],
    )
    assert ttcn.changed_functions == ["A.f1", "B.f2"]


# ---------------------------------------------------------------------------
# TDocCRParseResult — cover + optional TTCN bundle.
# ---------------------------------------------------------------------------


def test_parse_result_with_cover_only() -> None:
    """``TDocCRParseResult`` accepts a bare cover when no TTCN slice applies."""
    cover = TDocCRDetails(
        tdoc_id="R5-227476",
        spec="38.508-1",
        cr_num="2678",
        rev="1",
    )
    result = TDocCRParseResult(cover=cover, ttcn=None)
    assert result.cover == cover
    assert result.ttcn is None


def test_parse_result_with_cover_and_ttcn() -> None:
    """``TDocCRParseResult`` bundles cover + TTCN sidecar when the parser sees one."""
    cover = TDocCRDetails(tdoc_id="R5s260009", spec="38.523-3", cr_num="3790")
    ttcn = TDocCRTTCNDetails(tdoc_id="R5s260009", testcase="7.1.3.5.3")
    result = TDocCRParseResult(cover=cover, ttcn=ttcn)
    assert result.cover is cover
    assert result.ttcn is ttcn


# ---------------------------------------------------------------------------
# TDocExtractMeta — unchanged shape (extract metadata + provenance).
# ---------------------------------------------------------------------------


def test_extract_meta_post_init_rejects_blank_url() -> None:
    """A blank ``ftp_url`` raises ``ValueError``; URL is the row identity."""
    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocExtractMeta(
            ftp_url="",
            tdoc_id="R5s260009",
            cache_file="R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
            doc_filename="R5s260009.docx",
        )

    with pytest.raises(ValueError, match="non-empty ftp_url"):
        TDocExtractMeta(
            ftp_url="   ",
            tdoc_id="R5s260009",
            cache_file="R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
            doc_filename="R5s260009.docx",
        )


def test_extract_meta_post_init_rejects_blank_tdoc_id() -> None:
    """A blank ``tdoc_id`` raises ``ValueError`` (FK target must exist)."""
    with pytest.raises(ValueError, match="non-empty tdoc_id"):
        TDocExtractMeta(
            ftp_url="example.com/r5s260009.zip",
            tdoc_id="",
            cache_file="R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
            doc_filename="R5s260009.docx",
        )


def test_extract_meta_post_init_strips_whitespace() -> None:
    """Leading/trailing whitespace on required fields is stripped."""
    meta = TDocExtractMeta(
        ftp_url="  example.com/r5s260009.zip\n",
        tdoc_id=" R5s260009 ",
        cache_file="R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
        doc_filename="R5s260009.docx",
    )
    assert meta.ftp_url == "example.com/r5s260009.zip"
    assert meta.tdoc_id == "R5s260009"


def test_tdoc_extract_meta_requires_cache_file() -> None:
    """Omitting the required ``cache_file`` arg raises ``TypeError``.

    ``cache_file`` is a required positional/keyword arg on the
    dataclass (no default) — the constructor must reject calls
    that omit it so callers always pair an extract row with the
    on-disk basename.
    """
    with pytest.raises(TypeError):
        TDocExtractMeta(ftp_url="x", tdoc_id="y")  # no cache_file


def test_cache_file_must_be_present(tmp_path) -> None:
    """The QA lock: ``cache_file`` is mandatory even with a fresh tmp dir."""
    with pytest.raises(TypeError):
        TDocExtractMeta(ftp_url="x", tdoc_id="y")  # no cache_file


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
    assert "tdoc_cr_cover_page" in table_names
    assert "tdoc_extracts" in table_names
    # Sanity check: the pre-existing tables are still registered.
    assert "tdocs" in table_names
    assert "tdoc_files" in table_names
    # And the on-disk tables match the metadata registration.
    inspector = inspect(engine)
    on_disk_tables = set(inspector.get_table_names())
    assert "tdoc_cr_cover_page" in on_disk_tables
    assert "tdoc_extracts" in on_disk_tables


def test_tdoc_cr_detail_orm_round_trip() -> None:
    """Insert a fully-populated detail row, refresh, and assert equality.

    The slimmed cover-page table no longer carries a ``details`` blob
    or a ``parser_version`` column; only the cover-page fields survive.
    """
    engine = _make_engine()
    Session = sessionmaker(bind=engine)

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
        assert loaded.extracted_tdoc_id == "R5s260176"


def test_tdoc_extract_orm_round_trip() -> None:
    """Insert a populated sidecar row, refresh, and assert equality."""
    engine = _make_engine()
    Session = sessionmaker(bind=engine)

    url = "stored/R5s260009.zip"
    cache_file = "R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip"
    with Session() as session:
        _seed_tdoc(session, "R5s260009")
        row = TDocExtractOrm(
            ftp_url=url,
            tdoc_id="R5s260009",
            cache_file=cache_file,
            doc_filename="R5s260009.docx",
        )
        session.add(row)
        session.commit()

    with Session() as session:
        loaded = session.get(TDocExtractOrm, url)
        assert loaded is not None
        assert loaded.tdoc_id == "R5s260009"
        assert loaded.cache_file == cache_file
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
        )
        session.add(row)
        session.commit()

    with Session() as session:
        loaded = session.get(TDocCrDetailOrm, url)
        assert loaded is not None
        assert loaded.ftp_url == url
        assert loaded.tdoc_id == "R5-227476"
        assert loaded.spec is None
        assert loaded.cr_num is None
        assert loaded.title is None


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
                cache_file="R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
                doc_filename="R5s260051.docx",
            )
        )
        session.add(
            TDocExtractOrm(
                ftp_url=url_b,
                tdoc_id="R5s260051",
                cache_file="R5s260162-5186a7d62c6ae3ab3a0c02fa128e41da.zip",
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
