from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from doc3gpp.models.schema_info import schema_payload
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine

_LISTSERV_CATALOG_SHA256 = (
    "b7a2a990478d6934be221d608ac97c0bf357c406189b7cb4e55e3c09282a8ae7"
)


def _foreign_key_pairs(connection, table: str) -> set[tuple[str, str, str]]:
    return {
        (str(row[2]), str(row[3]), str(row[4]))
        for row in connection.exec_driver_sql(
            f'PRAGMA foreign_key_list("{table}")'
        ).all()
    }


def test_main_schema_seeds_exact_catalog_and_enforces_foreign_keys(sqlite_env) -> None:
    create_schema("main")
    engine = get_engine()

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        rows = [
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT list_id, tsg, description "
                    "FROM listserv_list ORDER BY list_id"
                )
            ).all()
        ]
        r6 = tuple(
            connection.execute(
                text(
                    "SELECT tsg_name, short_name, description, url "
                    "FROM tsgs WHERE short_name = 'R6'"
                )
            ).one()
        )
        catalog_fks = _foreign_key_pairs(connection, "listserv_list")
        message_fks = _foreign_key_pairs(connection, "listserv_messages")
        page_fks = _foreign_key_pairs(connection, "listserv_archive_pages")

    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(rows) == 97
    assert hashlib.sha256(raw).hexdigest() == _LISTSERV_CATALOG_SHA256
    assert "3GPP_TSG_RAN_WG6_DRAFTS" not in {row[0] for row in rows}
    assert r6 == (
        "RAN WG6",
        "R6",
        "(CLOSED) GSM/EDGE Radio Access Network (GERAN) and UMTS Radio Access (UTRA)",
        None,
    )
    assert ("tsgs", "tsg", "short_name") in catalog_fks
    assert ("listserv_list", "list_id", "list_id") in message_fks
    assert ("listserv_list", "list_id", "list_id") in page_fks

    reported_list_fields = [
        (row["field"], row["nullable"])
        for row in schema_payload("listserv")
        if row["table"] == "listserv_list"
    ]
    assert reported_list_fields == [
        ("list_id", False),
        ("tsg", True),
        ("description", True),
    ]

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO listserv_list (list_id, tsg, description) "
                "VALUES ('INVALID_TSG', 'R99', NULL)"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO listserv_messages ("
                "url, list_id, subject, sender, local_datetime, local_date, "
                "date_precision, sort_timestamp, week, subject_tdocs, "
                "source_revision"
                ") VALUES ("
                "'https://example.test/orphan', 'UNKNOWN_LIST', 'subject', "
                "'sender', '2026-01-01 00:00:00', '2026-01-01', 'date', "
                "'2026-01-01 00:00:00', '2026-01-W1', '', 1"
                ")"
            )
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO listserv_archive_pages ("
                "list_id, page_url, period, coverage, next_urls"
                ") VALUES ("
                "'UNKNOWN_LIST', 'https://example.test/archive', '2026-01', "
                "'[]', '[]'"
                ")"
            )
        )


def test_main_schema_initialization_preserves_tsg_and_catalog_metadata(sqlite_env) -> None:
    create_schema("main")
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE listserv_list SET description = :description "
                "WHERE list_id = :list_id"
            ),
            {
                "description": "Locally maintained list description",
                "list_id": "3GPP_TSG_RAN_WG1",
            },
        )
        connection.execute(
            text("UPDATE tsgs SET description = :description WHERE short_name = 'R1'"),
            {"description": "Locally maintained TSG description"},
        )

    create_schema("main")
    create_schema("main")

    with engine.connect() as connection:
        catalog_description = connection.execute(
            text(
                "SELECT description FROM listserv_list "
                "WHERE list_id = '3GPP_TSG_RAN_WG1'"
            )
        ).scalar_one()
        tsg_description = connection.execute(
            text("SELECT description FROM tsgs WHERE short_name = 'R1'")
        ).scalar_one()

    assert catalog_description == "Locally maintained list description"
    assert tsg_description == "Locally maintained TSG description"


def test_initialization_preserves_renamed_canonical_tsg_and_seeded_references(sqlite_env) -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tsgs (short_name, tsg_name, description, url) "
            "VALUES ('R1', 'My renamed group', 'Local description', 'https://example.test/local')"
        ))

    create_schema("main")
    create_schema("main")

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT tsg_name, description, url FROM tsgs WHERE short_name = 'R1'"
        )).one() == ("My renamed group", "Local description", "https://example.test/local")
        assert connection.execute(text(
            "SELECT tsg FROM listserv_list WHERE list_id = '3GPP_TSG_RAN_WG1'"
        )).scalar_one() == "R1"
        assert connection.execute(text("SELECT COUNT(*) FROM listserv_list")).scalar_one() == 97
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_initialization_rejects_canonical_name_on_another_row_without_mutation(sqlite_env) -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO tsgs (short_name, tsg_name, description) VALUES "
            "('R1', 'Renamed group', 'keep 1'), ('RX', 'RAN WG1', 'keep 2')"
        ))
        before = connection.execute(text("SELECT * FROM tsgs ORDER BY short_name")).all()
    with pytest.raises(ValueError, match="conflicting TSG reference"):
        create_schema("main")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT * FROM tsgs ORDER BY short_name")).all() == before
        assert connection.execute(text("SELECT COUNT(*) FROM listserv_list")).scalar_one() == 0


def test_generic_engine_fallback_is_idempotent_and_preserves_catalog_metadata(
    sqlite_env, monkeypatch
) -> None:
    """Exercise the generic insert path using SQLite's DBAPI as a test double.

    Only ``dialect.name`` is changed to select the fallback branch. This checks
    the generic SQLAlchemy SELECT/INSERT logic, not integration with a real
    non-SQLite server or its transaction semantics.
    """
    from doc3gpp.storage.db.listserv_catalog import ensure_listserv_catalog

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(engine.dialect, "name", "generic-test")
    assert engine.dialect.name == "generic-test"

    ensure_listserv_catalog(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE listserv_list SET description = :description "
                "WHERE list_id = :list_id"
            ),
            {
                "description": "Custom description on generic fallback",
                "list_id": "3GPP_TSG_RAN_WG1",
            },
        )

    ensure_listserv_catalog(engine)

    with engine.connect() as connection:
        catalog_count = connection.execute(
            text("SELECT COUNT(*) FROM listserv_list")
        ).scalar_one()
        description = connection.execute(
            text(
                "SELECT description FROM listserv_list "
                "WHERE list_id = '3GPP_TSG_RAN_WG1'"
            )
        ).scalar_one()

    assert catalog_count == 97
    assert description == "Custom description on generic fallback"


@pytest.mark.parametrize("scope", ["testcase", "specdata"])
def test_non_main_schema_scopes_do_not_seed_main_catalog(sqlite_env, scope: str) -> None:
    create_schema(scope)
    engine = get_engine()

    with engine.connect() as connection:
        main_tables = {
            str(row[0])
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).all()
        }

    assert "tsgs" not in main_tables
    assert "listserv_list" not in main_tables


@pytest.mark.parametrize(
    ("short_name", "tsg_name"),
    [("r1", "RAN WG1"), ("RX", "RAN WG1")],
)
def test_main_schema_reports_conflicting_tsg_reference_identity(
    sqlite_env, short_name: str, tsg_name: str
) -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (short_name, tsg_name, description, url) "
                "VALUES (:short_name, :tsg_name, 'Local metadata', NULL)"
            ),
            {"short_name": short_name, "tsg_name": tsg_name},
        )

    with pytest.raises(ValueError, match="conflicting TSG reference"):
        create_schema("main")

    with engine.connect() as connection:
        catalog_rows = connection.execute(
            text("SELECT COUNT(*) FROM listserv_list")
        ).scalar_one()
        tsg_rows = connection.execute(text("SELECT COUNT(*) FROM tsgs")).scalar_one()

    assert catalog_rows == 0
    assert tsg_rows == 1
