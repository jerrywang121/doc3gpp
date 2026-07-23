"""Integration tests for the TDocFile SQLAlchemy repository and sync path."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.services.tdoc_file_service import TDocFileService
from doc3gpp.services.tdoc_sync_coordinator import TDocSyncCoordinator
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_create_schema_includes_tdoc_files_table(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    with engine.connect() as conn:
        from sqlalchemy import text

        name = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tdoc_files'"
            )
        ).first()
    assert name is not None
    assert name[0] == "tdoc_files"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_upsert_and_list_roundtrip(sqlite_env) -> None:
    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    repo = SQLAlchemyTDocFileRepository()

    tdoc_repo.upsert_many(
        [
            TDoc(tdoc_id="R5s260001"),
            TDoc(tdoc_id="R5s260002"),
        ]
    )

    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001", type="revision",
                file="R5s260001r1.zip",
                ftp_url="x/R5s260001r1.zip",
            ),
            TDocFile(
                tdoc_id="R5s260001", type="review",
                file="R5s260001_MCC160Comments.zip",
                ftp_url="x/R5s260001_MCC160Comments.zip",
            ),
            TDocFile(
                tdoc_id="R5s260002", type="support",
                file="R5s260002_draft.zip",
                ftp_url="x/R5s260002_draft.zip",
            ),
        ]
    )

    rows = repo.list(limit=10)
    by_id = {row.ftp_url: row for row in rows}
    assert len(rows) == 3
    assert by_id["x/R5s260001r1.zip"].type == "revision"
    assert by_id["x/R5s260001r1.zip"].tdoc_id == "R5s260001"
    assert by_id["x/R5s260001_MCC160Comments.zip"].type == "review"
    assert by_id["x/R5s260002_draft.zip"].type == "support"


def test_upsert_is_idempotent_on_url(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many([TDoc(tdoc_id="R5s260001")])
    repo = SQLAlchemyTDocFileRepository()

    payload = TDocFile(
        tdoc_id="R5s260001", type="revision",
        file="R5s260001r1.zip",
        ftp_url="x/r1.zip",
    )
    repo.upsert_many([payload])
    repo.upsert_many([payload])
    assert len(repo.list()) == 1


def test_upsert_and_list_roundtrip_preserves_uploaded_date(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many([TDoc(tdoc_id="R5s260001")])
    repo = SQLAlchemyTDocFileRepository()

    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001",
                type="revision",
                file="R5s260001r1.zip",
                ftp_url="x/r1.zip",
                uploaded_date=date(2026, 1, 7),
            ),
            TDocFile(
                tdoc_id="R5s260001",
                type="review",
                file="R5s260001_MCC160Comments.zip",
                ftp_url="x/r1_review.zip",
                # uploaded_date left None — legacy listings may not
                # expose the date column.
            ),
        ]
    )

    rows = repo.list(limit=10)
    by_url = {row.ftp_url: row for row in rows}
    assert by_url["x/r1.zip"].uploaded_date == date(2026, 1, 7)
    assert by_url["x/r1_review.zip"].uploaded_date is None


def test_upsert_refreshes_uploaded_date_on_re_sync(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many([TDoc(tdoc_id="R5s260001")])
    repo = SQLAlchemyTDocFileRepository()

    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001", type="revision",
                file="R5s260001r1.zip", ftp_url="x/r1.zip",
                uploaded_date=date(2025, 3, 4),
            )
        ]
    )
    assert repo.list()[0].uploaded_date == date(2025, 3, 4)

    # Re-sync surfaces a newer uploaded_date (FTP re-upload). The
    # upsert must overwrite the stored value.
    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001", type="revision",
                file="R5s260001r1.zip", ftp_url="x/r1.zip",
                uploaded_date=date(2026, 1, 7),
            )
        ]
    )
    assert repo.list()[0].uploaded_date == date(2026, 1, 7)


def test_upsert_refreshes_type_on_url_reuse(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many([TDoc(tdoc_id="R5s260001")])
    repo = SQLAlchemyTDocFileRepository()

    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001", type="revision",
                file="R5s260001r1.zip",
                ftp_url="x/r1.zip",
            )
        ]
    )
    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001", type="review",
                file="R5s260001r1.zip",  # same filename, different type
                ftp_url="x/r1.zip",
            )
        ]
    )
    rows = repo.list()
    assert len(rows) == 1
    assert rows[0].type == "review"


def test_unique_url_constraint_is_enforced(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many([TDoc(tdoc_id="R5s260001")])
    repo = SQLAlchemyTDocFileRepository()

    repo.upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260001", type="revision",
                file="R5s260001r1.zip", ftp_url="x/r1.zip",
            )
        ]
    )
    # ``upsert_many`` itself is idempotent and refreshes in place, so the
    # unique constraint fires only on a direct ORM insert that bypasses
    # the upsert path.
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocFileORM

    with get_session_factory()() as session:
        session.add(
            TDocFileORM(
                tdoc_id="R5s260001",
                type="review",
                file="R5s260001_MCC160Comments.zip",
                ftp_url="x/r1.zip",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_list_filter_by_tdoc_id_and_type(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id="R5s260001"), TDoc(tdoc_id="R5s260002")]
    )
    repo = SQLAlchemyTDocFileRepository()
    repo.upsert_many(
        [
            TDocFile(tdoc_id="R5s260001", type="revision",
                     file="R5s260001r1.zip", ftp_url="x/r1.zip"),
            TDocFile(tdoc_id="R5s260001", type="review",
                     file="R5s260001_MCC160Comments.zip",
                     ftp_url="x/r1_review.zip"),
            TDocFile(tdoc_id="R5s260002", type="revision",
                     file="R5s260002r1.zip", ftp_url="x/r2.zip"),
        ]
    )

    assert {r.ftp_url for r in repo.list(tdoc_id="R5s260001")} == {
        "x/r1.zip",
        "x/r1_review.zip",
    }
    revisions = repo.list(file_type="revision")
    assert len(revisions) == 2
    reviews = repo.list(file_type="review")
    assert len(reviews) == 1

    both = repo.list(file_type_in=["revision", "review"])
    assert len(both) == 3
    none = repo.list(file_type_in=[])
    assert none == []


def test_delete_for_tdoc_ids(sqlite_env) -> None:
    create_schema()
    SQLAlchemyTDocRepository().upsert_many(
        [TDoc(tdoc_id="R5s260001"), TDoc(tdoc_id="R5s260002")]
    )
    repo = SQLAlchemyTDocFileRepository()
    repo.upsert_many(
        [
            TDocFile(tdoc_id="R5s260001", type="revision",
                     file="R5s260001r1.zip", ftp_url="x/r1.zip"),
            TDocFile(tdoc_id="R5s260002", type="revision",
                     file="R5s260002r1.zip", ftp_url="x/r2.zip"),
        ]
    )

    assert repo.delete_for_tdoc_ids(["R5s260001"]) == 1
    assert {r.tdoc_id for r in repo.list()} == {"R5s260002"}

    # Empty input is a no-op.
    assert repo.delete_for_tdoc_ids([]) == 0
    assert len(repo.list()) == 1


def test_foreign_key_blocks_unknown_tdoc_id(sqlite_env) -> None:
    create_schema()
    repo = SQLAlchemyTDocFileRepository()
    with pytest.raises(IntegrityError):
        repo.upsert_many(
            [
                TDocFile(
                    tdoc_id="R5s999999",  # not in tdocs table
                    type="revision", file="R5s999999r1.zip",
                    ftp_url="x/orphan.zip",
                )
            ]
        )


# ---------------------------------------------------------------------------
# Service sync path (orchestration)
# ---------------------------------------------------------------------------


def test_service_sync_writes_files_to_repository(sqlite_env, monkeypatch) -> None:
    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    tdoc_repo.upsert_many(
        [
            TDoc(tdoc_id="R5s260001"),
            TDoc(tdoc_id="R5s260002"),
        ]
    )

    def fake_fetch(ftp_url, tdoc_ids):
        return [
            TDocFile(
                tdoc_id="R5s260001", type="revision",
                file="R5s260001r1.zip",
                ftp_url=f"{ftp_url}R5s260001r1.zip",
            ),
            TDocFile(
                tdoc_id="R5s260002", type="review",
                file="R5s260002_MCC160Comments.zip",
                ftp_url=f"{ftp_url}R5s260002_MCC160Comments.zip",
            ),
        ]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_file_service.fetch_tdoc_files_from_meeting_ftp",
        fake_fetch,
    )

    service = TDocFileService(SQLAlchemyTDocFileRepository())
    count = service.sync_from_meeting_ftp(
        ftp_url="tsg_ran/WG5/", tdoc_ids=["R5s260001", "R5s260002"]
    )

    assert count == 2
    rows = SQLAlchemyTDocFileRepository().list()
    assert {r.tdoc_id for r in rows} == {"R5s260001", "R5s260002"}


# ---------------------------------------------------------------------------
# Coordinator chains the two syncs
# ---------------------------------------------------------------------------


def test_coordinator_sync_persists_tdocs_and_files(sqlite_env, monkeypatch) -> None:
    create_schema()
    meeting_repo = SQLAlchemyMeetingRepository()
    meeting_repo.upsert_many(
        [
            Meeting(
                meeting_id=1,
                name="R5--TTCN e-mail 999",
                title="TTCN e-mail meeting",
                location="Online",
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 2),
                ftp_url="tsg_ran/WG5/email/",
            )
        ]
    )

    xlsx_bytes = Path(
        "tests/fixtures/tdoc_xlsx/TDoc_List_Meeting_RAN5#111.xlsx"
    ).read_bytes()

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def get_text(self, url: str) -> str:
            if url.rstrip("/").endswith(("Inbox", "inbox", "Inbox/")):
                # The fixture XLSX contains ``R5-261700`` (the RAN5#111
                # agenda opening TDoc) so the Inbox listing attaches a
                # matching revision here.
                return (
                    '<a class="file" href="https://x/R5-261700r1.zip">'
                    "R5-261700r1.zip</a>"
                )
            if url.rstrip("/").endswith(
                ("Docs", "docs", "Tdocs", "tdocs", "Review", "review")
            ):
                return ""
            return (
                '<html><body>'
                '<a href="TDoc_List_Meeting_RAN5#111.xlsx">xlsx</a>'
                '</body></html>'
            )

        def get_bytes(self, url: str) -> bytes:
            return xlsx_bytes

        def close(self) -> None:
            return None

    monkeypatch.setattr("doc3gpp.scraping.ftp_source.ScraperClient", _DummyClient)
    monkeypatch.setattr("doc3gpp.scraping.portal_source.ScraperClient", _DummyClient)

    coord = TDocSyncCoordinator(
        SQLAlchemyMeetingRepository(),
        SQLAlchemyTDocRepository(),
        SQLAlchemyTDocFileRepository(),
    )
    outcome = coord.sync_for_meeting_id(1)

    tdocs = SQLAlchemyTDocRepository().list(limit=10)
    tdoc_files = SQLAlchemyTDocFileRepository().list(limit=10)
    assert tdocs, "TDoc sync should have stored rows from the fixture XLSX"
    assert tdoc_files, "TDoc file sync should have stored the Inbox/ revision"
    meeting_tdoc_ids = set(
        SQLAlchemyTDocRepository().list_tdoc_ids_for_meeting(1)
    )
    assert all(f.tdoc_id in meeting_tdoc_ids for f in tdoc_files)
    assert outcome.status == "synced"
    assert "TDoc sync complete:" in outcome.reason


# ---------------------------------------------------------------------------
# CLI end-to-end: ``tdoc show`` reads ``tdoc_files`` matching ``tdoc_id``
# ---------------------------------------------------------------------------


def test_tdoc_show_cli_surfaces_tdoc_files_rows(sqlite_env) -> None:
    """End-to-end smoke test: seed parent + two ``tdoc_files`` rows,
    drive ``doc3gpp tdoc show --tdoc <id>``, and assert the table
    output renders the auxiliary files block in ``(type, ftp_url)``
    ASC order with the four informative fields.

    This catches regressions in the wiring from the CLI through the
    factory, repo, DTO, and table renderer in one sweep.
    """
    from typer.testing import CliRunner

    from doc3gpp.cli import app

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260030", type="CR", ftp_url="x/R5s260030.zip")
    )
    SQLAlchemyTDocFileRepository().upsert_many(
        [
            TDocFile(
                tdoc_id="R5s260030",
                type="revision",
                file="R5s260030r1.zip",
                ftp_url="tsg_ran/WG5/TSGR5_128/Inbox/R5s260030r1.zip",
                uploaded_date=date(2026, 7, 4),
            ),
            TDocFile(
                tdoc_id="R5s260030",
                type="review",
                file="R5s260030_MCC160Comments.zip",
                ftp_url=(
                    "tsg_ran/WG5/TSGR5_128/Review/"
                    "R5s260030_MCC160Comments.zip"
                ),
                uploaded_date=date(2026, 7, 3),
            ),
        ]
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260030"])
    assert result.exit_code == 0, result.output
    aux_block = result.output.split("[Auxiliary Files]", 1)[1]
    assert "type: revision" in aux_block
    assert "file: R5s260030r1.zip" in aux_block
    assert "uploaded_date: 2026-07-04" in aux_block
    assert "type: review" in aux_block
    assert "file: R5s260030_MCC160Comments.zip" in aux_block
    assert "uploaded_date: 2026-07-03" in aux_block
    review_idx = aux_block.index("type: review")
    revision_idx = aux_block.index("type: revision")
    assert review_idx < revision_idx