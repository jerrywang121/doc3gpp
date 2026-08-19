"""Integration test: tdoc_cr_ls_details table is created and keyed correctly."""

from __future__ import annotations

from sqlalchemy import inspect

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine, get_session_factory


def test_table_created_with_expected_columns(sqlite_env) -> None:
    create_schema()
    get_session_factory()  # force init
    engine = get_engine()
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("tdoc_cr_ls_details")}
    expected = {
        "ftp_url", "tdoc_id", "variant",
        "title", "response_to",
        "release", "work_item_name", "work_item_code",
        "source", "to_groups", "cc_groups", "attachments_json",
        "parser_version", "extracted_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_pks_and_fks(sqlite_env) -> None:
    create_schema()
    insp = inspect(get_engine())
    pk = insp.get_pk_constraint("tdoc_cr_ls_details")
    assert pk["constrained_columns"] == ["ftp_url"]
    fks = insp.get_foreign_keys("tdoc_cr_ls_details")
    tdoc_fk = [f for f in fks if f["referred_table"] == "tdocs"]
    assert tdoc_fk, "no FK to tdocs"
    assert "tdoc_id" in tdoc_fk[0]["constrained_columns"]
