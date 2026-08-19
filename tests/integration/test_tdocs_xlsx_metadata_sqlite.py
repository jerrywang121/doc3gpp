"""End-to-end integration test for the six XLSX-metadata fields.

Drives the parser → domain → repository → CLI path with a synthetic
XLSX so every layer of the TDoc-list vertical slice introduced by this
branch is exercised against a real SQLite database.
"""

from __future__ import annotations

import io
from datetime import datetime

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.parsers.tdoc_parser import read_tdoc_sheet
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import MeetingORM
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


def _build_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append([
        "TDoc", "Title", "Source", "Type",
        "For", "Abstract", "Secretary Remarks",
        "To", "Cc", "Original LS",
    ])
    ws.append([
        "R5-260001", "Doc A", "Acme", "LS",
        "Information", "TL;DR here.", "Sec note.",
        "RAN2", "RAN3, RAN4", "C1-260001",
    ])
    ws.append([
        "R5-260002", "Doc B", "Acme", "CR",
        "Approval", "", "",
        "", "", "",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def engine():
    eng = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=eng)
    sf = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    with sf() as session:
        session.add(MeetingORM(
            meeting_id=1, name="RAN5#111", title="t", location="loc",
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 5),
        ))
        session.commit()
    return sf, eng


def test_xlsx_metadata_round_trips_end_to_end(engine):
    sf, _ = engine
    xlsx_bytes = _build_xlsx()
    # Bypass the network by calling the parser directly + stamping meeting_id.
    rows = read_tdoc_sheet(xlsx_bytes)
    tdocs = [
        TDoc(
            tdoc_id=row["tdoc"], meeting_id=1,
            title=row.get("title"),
            source=row.get("source"),
            type=row.get("type"),
            tdoc_for=row.get("tdoc_for"),
            abstract=row.get("abstract"),
            secretary_remarks=row.get("secretary_remarks"),
            ls_to=row.get("ls_to"),
            ls_cc=row.get("ls_cc"),
            original_ls=row.get("original_ls"),
        )
        for row in rows
    ]
    repo = SQLAlchemyTDocRepository()
    repo._session_factory = sf
    repo.upsert_many(tdocs)
    fetched = {t.tdoc_id: t for t in repo.list()}
    assert fetched["R5-260001"].tdoc_for == "Information"
    assert fetched["R5-260001"].abstract == "TL;DR here."
    assert fetched["R5-260001"].secretary_remarks == "Sec note."
    assert fetched["R5-260001"].ls_to == "RAN2"
    assert fetched["R5-260001"].ls_cc == "RAN3, RAN4"
    assert fetched["R5-260001"].original_ls == "C1-260001"

    assert fetched["R5-260002"].tdoc_for == "Approval"
    assert fetched["R5-260002"].ls_to is None   # empty cell
    assert fetched["R5-260002"].abstract is None


def test_cli_tdoc_list_filters_by_xlsx_metadata(engine, monkeypatch):
    _, _ = engine
    runner = CliRunner()
    observed = {}

    def fake(self, **kwargs):
        observed.update(kwargs)
        sample = TDocWithMeeting(
            tdoc=TDoc(tdoc_id="R5-260001"),
            meeting_name="RAN5#111",
        )
        return [sample]

    monkeypatch.setattr(
        "doc3gpp.services.tdoc_service.TDocService.list_recent_with_meeting",
        fake,
    )
    result = runner.invoke(app, [
        "tdoc", "list",
        "--abstract", "%TL;DR%",
        "--ls-to", "RAN2",
        "--secretary-remarks", "not-null",
        "--for", "Information",
    ])
    assert result.exit_code == 0
    assert observed["abstract"] == "%TL;DR%"
    assert observed["ls_to"] == "RAN2"
    assert observed["secretary_remarks"] == "not-null"
    assert observed["tdoc_for"] == "Information"
