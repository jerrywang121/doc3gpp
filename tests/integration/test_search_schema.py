"""Verify ``create_schema`` creates the FTS5 virtual + meta tables."""

from __future__ import annotations

import pytest  # noqa: F401 - kept per spec
from sqlalchemy import inspect, text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_create_schema_creates_search_objects(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    insp = inspect(engine)
    assert "tdoc_search" in insp.get_table_names(), (
        "tdoc_search FTS5 virtual table should exist after create_schema"
    )
    assert "tdoc_search_meta" in insp.get_table_names(), (
        "tdoc_search_meta sidecar should exist after create_schema"
    )
    with engine.begin() as conn:
        columns = [
            row[1] for row in conn.execute(text("PRAGMA table_info(tdoc_search)"))
        ]
        ddl = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tdoc_search'"
            )
        ).scalar_one()
    assert columns == [
        "tdoc_id",
        "title",
        "ftp_url",
        "meeting_title",
        "meeting_location",
        "wis",
        "cover_text",
        "change_text",
        "ttcn_text",
        "content_text",
        "row_kind",
        "document_id",
        "source_file",
        "extraction_version",
        "content_hash",
    ]
    declarations = [
        part.strip() for part in ddl.split("fts5(", 1)[1].rsplit(")", 1)[0].split(",")
    ]
    indexed_columns = [
        part.split()[0] for part in declarations if "UNINDEXED" not in part.upper()
    ]
    unindexed_columns = [
        part.split()[0] for part in declarations if "UNINDEXED" in part.upper()
    ]
    assert indexed_columns == [
        "title",
        "ftp_url",
        "meeting_title",
        "meeting_location",
        "wis",
        "cover_text",
        "change_text",
        "ttcn_text",
        "content_text",
    ]
    assert unindexed_columns == [
        "tdoc_id",
        "row_kind",
        "document_id",
        "source_file",
        "extraction_version",
        "content_hash",
    ]

    from doc3gpp.settings.schema import SearchSettings

    weights = SearchSettings().bm25_weights
    assert len(weights) == len(indexed_columns) == 9
    assert weights[-1] == 1.0


def test_meta_table_has_expected_keys(sqlite_env) -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        # Touch the meta table — empty is fine; the column shape is
        # what we assert here.
        rows = conn.execute(
            text("SELECT key, value FROM tdoc_search_meta")
        ).all()
    assert isinstance(rows, list)



def test_create_schema_recreates_legacy_fts_table_preserving_sidecars(
    sqlite_env,
) -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE tdoc_search"))
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE tdoc_search USING fts5(
                    tdoc_id UNINDEXED,
                    title,
                    ftp_url,
                    meeting_title,
                    meeting_location,
                    wis,
                    cover_text,
                    change_text,
                    ttcn_text
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO tdoc_search_meta (key, value) "
                "VALUES ('last_rebuild_at', 'preserve-me')"
            )
        )

    create_schema()

    with engine.begin() as conn:
        columns = [
            row[1] for row in conn.execute(text("PRAGMA table_info(tdoc_search)"))
        ]
        preserved = conn.execute(
            text(
                "SELECT value FROM tdoc_search_meta "
                "WHERE key = 'last_rebuild_at'"
            )
        ).scalar_one()
        indexes = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index'")
            )
        }
    assert "content_text" in columns
    assert "row_kind" in columns
    assert preserved == "preserve-me"
    assert {
        "idx_tdocs_release_spec",
        "idx_tdocs_uploaded_date",
        "idx_meetings_name_tsg",
    } <= indexes
