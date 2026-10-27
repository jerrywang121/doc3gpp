"""SQLite coverage for source-aware FTS rows and index freshness."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import text

from doc3gpp.models.search import SearchFilters
from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine
from doc3gpp.storage.repositories.search_sql import SQLAlchemySearchIndexRepository

_METADATA_ID = "R5-9000001"
_DOCUMENT_ID = "R5-9000002"
_OTHER_DOCUMENT_ID = "R5-9000003"
_METADATA_URL = "https://www.3gpp.org/ftp/R5-9000001.zip"
_DOCUMENT_URL = "https://www.3gpp.org/ftp/R5-9000002.zip"
_OTHER_DOCUMENT_URL = "https://www.3gpp.org/ftp/R5-9000003.zip"
_DOCUMENT_SOURCE_A = "https://www.3gpp.org/ftp/archive/R5-9000002-r1.zip"
_DOCUMENT_SOURCE_B = "https://www.3gpp.org/ftp/archive/R5-9000002-r2.zip"
_OTHER_DOCUMENT_SOURCE = "https://www.3gpp.org/ftp/archive/R5-9000003-r1.zip"


def _insert_tdoc(
    connection,
    tdoc_id: str,
    title: str,
    ftp_url: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO tdocs (
                tdoc_id, meeting_id, title, ftp_url, type, status, source,
                uploaded_date, release, spec
            ) VALUES (
                :tdoc_id, 100, :title, :ftp_url, 'CR', 'Agreed', 'TSG',
                '2026-01-02T00:00:00', 'Rel-18', '38.300'
            )
            """
        ),
        {"tdoc_id": tdoc_id, "title": title, "ftp_url": ftp_url},
    )


def _insert_extraction(
    connection,
    tdoc_id: str,
    ftp_url: str,
    documents: tuple[tuple[str, str], ...],
    extraction_version: int = 1,
) -> dict[str, str]:
    connection.execute(
        text(
            """
            INSERT INTO tdoc_extracts (
                ftp_url, tdoc_id, cache_file, doc_filename, parser_version,
                extraction_version, document_count
            ) VALUES (
                :ftp_url, :tdoc_id, 'source.zip', 'main.docx', '1.0.0',
                :extraction_version, :document_count
            )
            """
        ),
        {
            "ftp_url": ftp_url,
            "tdoc_id": tdoc_id,
            "extraction_version": extraction_version,
            "document_count": len(documents),
        },
    )
    document_ids: dict[str, str] = {}
    for source_file, markdown_text in documents:
        document_id = hashlib.sha256(
            json.dumps(
                [ftp_url, source_file],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        content_hash = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()
        connection.execute(
            text(
                """
                INSERT INTO tdoc_cr_annexes (
                    ftp_url, doc_filename, document_id, tdoc_id, content,
                    content_hash
                ) VALUES (
                    :ftp_url, :doc_filename, :document_id, :tdoc_id, :content,
                    :content_hash
                )
                """
            ),
            {
                "ftp_url": ftp_url,
                "doc_filename": source_file,
                "document_id": document_id,
                "tdoc_id": tdoc_id,
                "content": compress_json(markdown_text),
                "content_hash": content_hash,
            },
        )
        document_ids[source_file] = document_id
    return document_ids


def _seed_search_corpus(sqlite_env):
    create_schema()
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    100, 'RAN1#100', 'RAN1#100', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_100',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )
        _insert_tdoc(
            connection,
            _METADATA_ID,
            "MetadataMarker reference",
            _METADATA_URL,
        )
        _insert_tdoc(
            connection,
            _DOCUMENT_ID,
            "Source neutral document",
            _DOCUMENT_URL,
        )
        _insert_tdoc(
            connection,
            _OTHER_DOCUMENT_ID,
            "Other source neutral document",
            _OTHER_DOCUMENT_URL,
        )
        document_ids = _insert_extraction(
            connection,
            _DOCUMENT_ID,
            _DOCUMENT_SOURCE_A,
            (("docs/alpha.docx", "attachmentneedle alphaonly sharedparentmatch"),),
        )
        document_ids.update(
            _insert_extraction(
                connection,
                _DOCUMENT_ID,
                _DOCUMENT_SOURCE_B,
                (("support/beta.docx", "betaonly sharedparentmatch"),),
            )
        )
        document_ids.update(
            _insert_extraction(
                connection,
                _OTHER_DOCUMENT_ID,
                _OTHER_DOCUMENT_SOURCE,
                (("support/other.docx", "sharedparentmatch"),),
            )
        )
    repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in (_METADATA_ID, _DOCUMENT_ID, _OTHER_DOCUMENT_ID):
        repo.upsert(tdoc_id)
    return engine, repo, document_ids


def test_attachment_search_returns_matching_document_source_and_snippet(
    sqlite_env,
) -> None:
    _engine, repo, _document_ids = _seed_search_corpus(sqlite_env)

    hits = repo.search("attachmentneedle", SearchFilters(limit=10))

    assert len(hits) == 1
    hit = hits[0]
    assert hit.tdoc_id == _DOCUMENT_ID
    assert hit.ftp_url == _DOCUMENT_URL
    assert hasattr(hit, "matched_documents"), (
        "document matches must be carried on the parent SearchHit"
    )
    assert len(hit.matched_documents) == 1
    matched = hit.matched_documents[0]
    assert type(matched).__name__ == "MatchedDocument"
    assert matched.ftp_url == _DOCUMENT_SOURCE_A
    assert matched.source_file == "docs/alpha.docx"
    assert "content_text" in matched.previews
    assert "<<" in matched.previews["content_text"]
    assert ">>" in matched.previews["content_text"]


def test_metadata_match_stays_one_parent_hit_without_document_matches(
    sqlite_env,
) -> None:
    _engine, repo, _document_ids = _seed_search_corpus(sqlite_env)

    hits = repo.search("metadatamarker", SearchFilters(limit=10))

    assert len(hits) == 1
    assert hits[0].tdoc_id == _METADATA_ID
    assert hasattr(hits[0], "matched_documents"), (
        "metadata-only matches must expose an empty matched_documents tuple"
    )
    assert hits[0].matched_documents == ()


def test_document_conjunction_does_not_match_across_source_files(sqlite_env) -> None:
    _engine, repo, _document_ids = _seed_search_corpus(sqlite_env)

    alpha_hits = repo.search("alphaonly", SearchFilters(limit=10))
    beta_hits = repo.search("betaonly", SearchFilters(limit=10))
    cross_source_hits = repo.search(
        "alphaonly AND betaonly",
        SearchFilters(limit=10),
    )

    assert len(alpha_hits) == 1
    assert len(beta_hits) == 1
    assert cross_source_hits == []


def test_parent_limit_is_applied_after_document_rows_are_grouped(sqlite_env) -> None:
    _engine, repo, _document_ids = _seed_search_corpus(sqlite_env)

    hits = repo.search("sharedparentmatch", SearchFilters(limit=2))

    assert len(hits) == 2
    assert {hit.tdoc_id for hit in hits} == {
        _DOCUMENT_ID,
        _OTHER_DOCUMENT_ID,
    }
    document_hit = next(hit for hit in hits if hit.tdoc_id == _DOCUMENT_ID)
    assert len(document_hit.matched_documents) == 2


def test_reupsert_replaces_stale_physical_document_rows(sqlite_env) -> None:
    engine, repo, _document_ids = _seed_search_corpus(sqlite_env)

    with engine.begin() as connection:
        initial_rows = connection.execute(
            text("SELECT COUNT(*) FROM tdoc_search WHERE tdoc_id = :tdoc_id"),
            {"tdoc_id": _DOCUMENT_ID},
        ).scalar_one()
    assert initial_rows == 3

    replacement_url = "https://www.3gpp.org/ftp/archive/R5-9000002-r3.zip"
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM tdoc_extracts WHERE tdoc_id = :tdoc_id"),
            {"tdoc_id": _DOCUMENT_ID},
        )
        _insert_extraction(
            connection,
            _DOCUMENT_ID,
            replacement_url,
            (("replacement/new.docx", "replacementonlytoken"),),
            extraction_version=2,
        )
    repo.upsert(_DOCUMENT_ID)

    with engine.begin() as connection:
        replacement_rows = connection.execute(
            text("SELECT COUNT(*) FROM tdoc_search WHERE tdoc_id = :tdoc_id"),
            {"tdoc_id": _DOCUMENT_ID},
        ).scalar_one()
    assert replacement_rows == 2
    assert repo.search("attachmentneedle", SearchFilters(limit=10)) == []
    replacement_hits = repo.search("replacementonlytoken", SearchFilters(limit=10))
    assert len(replacement_hits) == 1
    assert replacement_hits[0].matched_documents[0].ftp_url == replacement_url


def test_status_and_stale_rebuild_detect_document_hash_and_version_changes(
    sqlite_env,
) -> None:
    engine, repo, document_ids = _seed_search_corpus(sqlite_env)

    status = repo.status()
    assert status.row_count == 3
    assert status.is_stale is False
    with engine.begin() as connection:
        physical_row_count = connection.execute(
            text("SELECT COUNT(*) FROM tdoc_search")
        ).scalar_one()
    assert physical_row_count == 6

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tdoc_cr_annexes SET content_hash = :content_hash "
                "WHERE document_id = :document_id"
            ),
            {
                "content_hash": "f" * 64,
                "document_id": document_ids["docs/alpha.docx"],
            },
        )
    assert repo.status().is_stale is True
    assert repo.count_tdocs_to_index(stale_only=True) == 1
    assert list(
        repo.rebuild_batch(batch_size=10, after_id=None, stale_only=True)
    ) == [[_DOCUMENT_ID]]

    repo.upsert(_DOCUMENT_ID)
    assert repo.status().is_stale is False
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tdoc_extracts SET extraction_version = 2 "
                "WHERE ftp_url = :ftp_url"
            ),
            {"ftp_url": _DOCUMENT_SOURCE_A},
        )
    assert repo.status().is_stale is True
    assert repo.count_tdocs_to_index(stale_only=True) == 1
    assert list(
        repo.rebuild_batch(batch_size=10, after_id=None, stale_only=True)
    ) == [[_DOCUMENT_ID]]



def test_large_parent_page_uses_bounded_sqlite_parameters(sqlite_env) -> None:
    import sqlite3

    import pytest
    from sqlalchemy import event

    from doc3gpp.models.search import SearchIndexCorruptError
    from doc3gpp.settings.loader import get_settings

    variable_limit_category = getattr(
        sqlite3,
        "SQLITE_LIMIT_VARIABLE_NUMBER",
        None,
    )
    if variable_limit_category is None or not hasattr(sqlite3.Connection, "setlimit"):
        pytest.skip("sqlite3 variable-limit control is unavailable")

    create_schema()
    engine = get_engine()
    parent_ids = [f"R5-LIMIT-{index:04d}" for index in range(26)]
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tsgs (tsg_name, short_name, description) "
                "VALUES ('RAN WG1', 'RAN1', 'TSG RAN WG1')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    meeting_id, name, title, location, tsg, start_date,
                    end_date, ftp_url, tdoc_list_last_sync
                ) VALUES (
                    100, 'RAN1#100', 'RAN1#100', 'Online', 'RAN1',
                    '2026-01-01', '2026-01-05',
                    'https://www.3gpp.org/ftp/meetings/RAN1_100',
                    '2026-01-05T00:00:00'
                )
                """
            )
        )
        for tdoc_id in parent_ids:
            _insert_tdoc(
                connection,
                tdoc_id,
                f"Limited page parent {tdoc_id}",
                f"https://www.3gpp.org/ftp/{tdoc_id}.zip",
            )
            _insert_extraction(
                connection,
                tdoc_id,
                f"https://www.3gpp.org/ftp/archive/{tdoc_id}-r1.zip",
                (
                    (f"support/{tdoc_id}-a.docx", "boundedselectionneedle alpha"),
                    (f"support/{tdoc_id}-b.docx", "boundedselectionneedle beta"),
                ),
            )

    indexing_repo = SQLAlchemySearchIndexRepository()
    for tdoc_id in parent_ids:
        indexing_repo.upsert(tdoc_id)

    search_settings = get_settings().search
    original_weights = search_settings.bm25_weights
    object.__setattr__(
        search_settings,
        "bm25_weights",
        (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    )
    search_repo = SQLAlchemySearchIndexRepository()
    previous_limits: dict[int, int] = {}
    observed_limits: list[int] = []

    def _lower_variable_limit(dbapi_connection, connection_record, _proxy) -> None:
        key = "task5_original_variable_limit"
        previous_limits[id(connection_record)] = dbapi_connection.getlimit(
            variable_limit_category
        )
        dbapi_connection.setlimit(variable_limit_category, 26)
        observed_limits.append(dbapi_connection.getlimit(variable_limit_category))
        connection_record.info[key] = previous_limits[id(connection_record)]

    def _restore_variable_limit(dbapi_connection, connection_record) -> None:
        original = connection_record.info.pop(
            "task5_original_variable_limit",
            None,
        )
        if original is not None:
            dbapi_connection.setlimit(variable_limit_category, original)

    event.listen(engine.pool, "checkout", _lower_variable_limit)
    event.listen(engine.pool, "checkin", _restore_variable_limit)
    try:
        try:
            hits = search_repo.search(
                "boundedselectionneedle",
                SearchFilters(limit=len(parent_ids)),
            )
        except SearchIndexCorruptError as exc:
            pytest.fail(
                "large parent pages must not exceed SQLite's bind-variable "
                f"limit: {exc}"
            )
    finally:
        event.remove(engine.pool, "checkout", _lower_variable_limit)
        event.remove(engine.pool, "checkin", _restore_variable_limit)
        object.__setattr__(search_settings, "bm25_weights", original_weights)

    assert observed_limits
    assert set(observed_limits) == {26}
    assert {hit.tdoc_id for hit in hits} == set(parent_ids)
    assert all(len(hit.matched_documents) == 2 for hit in hits)
    for hit in hits:
        assert {document.source_file for document in hit.matched_documents} == {
            f"support/{hit.tdoc_id}-a.docx",
            f"support/{hit.tdoc_id}-b.docx",
        }



def test_freshness_plan_is_set_based_and_detects_missing_changed_rows(
    sqlite_env,
) -> None:
    from sqlalchemy import event

    engine, repo, _document_ids = _seed_search_corpus(sqlite_env)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM tdoc_search "
                "WHERE tdoc_id = :tdoc_id AND row_kind = 'document'"
            ),
            {"tdoc_id": _OTHER_DOCUMENT_ID},
        )
        connection.execute(
            text(
                "UPDATE tdoc_extracts SET extraction_version = 2 "
                "WHERE ftp_url = :ftp_url"
            ),
            {"ftp_url": _DOCUMENT_SOURCE_A},
        )

    freshness_queries: list[tuple[str, object]] = []

    def _capture_freshness_query(
        _connection,
        _cursor,
        statement,
        parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            "tdoc_search_meta" in statement
            and (
                "FROM tdoc_search " in statement
                or "FROM tdoc_search\n" in statement
            )
        ):

            freshness_queries.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", _capture_freshness_query)
    try:
        status = repo.status()
    finally:
        event.remove(engine, "before_cursor_execute", _capture_freshness_query)

    assert status.row_count == 3
    assert status.is_stale is True
    assert repo.count_tdocs_to_index(stale_only=True) == 2
    rebuild_queries: list[str] = []

    def _capture_rebuild_query(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if "FROM tdoc_search\n" in statement:
            rebuild_queries.append(statement)

    event.listen(engine, "before_cursor_execute", _capture_rebuild_query)
    try:
        rebuild_batches = list(
            repo.rebuild_batch(batch_size=1, after_id=None, stale_only=True)
        )
    finally:
        event.remove(engine, "before_cursor_execute", _capture_rebuild_query)
    assert rebuild_batches == [[_DOCUMENT_ID], [_OTHER_DOCUMENT_ID]]
    assert len(rebuild_queries) == 1
    assert len(freshness_queries) == 1

    statement, parameters = freshness_queries[0]
    with engine.begin() as connection:
        plan = connection.exec_driver_sql(
            f"EXPLAIN QUERY PLAN {statement}",
            parameters,
        ).all()
    details = [str(row[3]).upper() for row in plan]
    assert not any("CORRELATED" in detail for detail in details), details
    virtual_table_scans = [
        detail for detail in details if "VIRTUAL TABLE INDEX" in detail
    ]
    assert len(virtual_table_scans) <= 2, details



def test_freshness_sql_avoids_sqlite_335_only_materialized_hint(
    sqlite_env,
) -> None:
    from sqlalchemy import event

    engine, repo, _document_ids = _seed_search_corpus(sqlite_env)
    freshness_sql: list[str] = []

    def _capture_freshness_sql(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if (
            "tdoc_search_meta" in statement
            and (
                "FROM tdoc_search " in statement
                or "FROM tdoc_search\n" in statement
            )
        ):
            freshness_sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capture_freshness_sql)
    try:
        repo.status()
    finally:
        event.remove(engine, "before_cursor_execute", _capture_freshness_sql)

    assert len(freshness_sql) == 1
    assert "AS MATERIALIZED" not in freshness_sql[0].upper()
