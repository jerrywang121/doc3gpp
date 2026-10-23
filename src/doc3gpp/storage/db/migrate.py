from __future__ import annotations

import re

from sqlalchemy import inspect, text

from doc3gpp.storage.compression import compress_json
from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import (
    MeetingORM,  # noqa: F401 - ensures model metadata is loaded
    SpecDocChunkORM,  # noqa: F401 - registers specdata tables on SpecDataBase
    SpecDocSourceORM,  # noqa: F401 - registers specdata tables on SpecDataBase
    SpecDocTocORM,  # noqa: F401 - registers specdata tables on SpecDataBase
    SpecORM,  # noqa: F401 - ensures model metadata is loaded
    SpecVersionORM,  # noqa: F401 - ensures model metadata is loaded
    TDocCrAnnexOrm,  # noqa: F401 - registers URL-owned CR annex content
    TDocCrChangeDetailOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocCrDetailOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocCrTtcnDetailOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocDiscussionContentOrm,  # noqa: F401 - registers URL-owned discussion content
    TDocExtractOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocFileORM,  # noqa: F401 - ensures model metadata is loaded
    TDocLsContentOrm,  # noqa: F401 - registers URL-owned LS content
    TDocORM,  # noqa: F401 - ensures model metadata is loaded
    TDocWidContentOrm,  # noqa: F401 - registers URL-owned WID content
    TestCaseORM,  # noqa: F401 - ensures model metadata is loaded
    TestCaseSourceORM,  # noqa: F401 - ensures model metadata is loaded
    TestCaseStatusORM,  # noqa: F401 - ensures model metadata is loaded
    TsgORM,  # noqa: F401 - ensures model metadata is loaded
    WiORM,  # noqa: F401 - ensures model metadata is loaded
)
from doc3gpp.storage.db.session import (
    get_engine,
    get_specdata_engine,
    get_testcase_engine,
)
from doc3gpp.storage.db.specdata_base import SpecDataBase
from doc3gpp.storage.db.testcase_base import TestCaseBase


def _migrate_rename_tdoc_cr_details() -> None:
    """Rename legacy ``tdoc_cr_details`` table to ``tdoc_cr_cover_page``.

    One-shot, idempotent: runs at every ``create_schema`` call but is a
    no-op once the legacy name is gone. ``Base.metadata.create_all``
    does not rename existing tables, so databases created by prior
    releases would otherwise carry an orphan ``tdoc_cr_details`` table
    while reads/writes went to the new ``tdoc_cr_cover_page``.

    Syntax notes:

    * ``ALTER TABLE ... RENAME TO`` is the only DDL SQLite supports
      that mutates a table. ``CREATE TABLE IF NOT EXISTS`` is used as
      the post-condition probe so a pre-existing new-name table
      (partial migration) does not raise.
    """
    engine = get_engine()
    with engine.begin() as conn:
        # Probe sqlite_master for the legacy name.
        legacy_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_details' LIMIT 1"
            )
        ).first()
        if legacy_exists:
            # Drop the empty new table (if any) so RENAME can land;
            # legacy data must survive — only rename, never drop.
            conn.execute(
                text("DROP TABLE IF EXISTS tdoc_cr_cover_page")
            )
            conn.execute(
                text("ALTER TABLE tdoc_cr_details RENAME TO tdoc_cr_cover_page")
            )


def _migrate_drop_tsg_spec_last_sync() -> None:
    """Drop the obsolete ``tsgs.spec_last_sync`` column from databases
    that carried it before the per-spec skip rule landed.

    One-shot, idempotent: ``ALTER TABLE ... DROP COLUMN`` raises if the
    column is already absent, so we probe ``PRAGMA table_info`` first
    and only issue the ALTER when the column is genuinely present.
    ``Base.metadata.create_all`` is a no-op on tables that already
    exist, so pre-existing ``tsgs`` rows on older databases carry the
    legacy column forever.
    """
    engine = get_engine()
    with engine.begin() as conn:
        # sqlite_master entry for the table — guard against a fresh DB
        # (no ``tsgs`` yet, in which case ``Base.metadata.create_all``
        # will create it without the column).
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tsgs' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(tsgs)")).all()
        column_names = {row[1] for row in rows}
        if "spec_last_sync" not in column_names:
            return
        conn.execute(text("ALTER TABLE tsgs DROP COLUMN spec_last_sync"))


def _migrate_spec_rapporteurs() -> None:
    """Add ``specs.rapporteurs`` to databases created before that column
    existed. Idempotent: probe ``PRAGMA table_info`` first."""
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='specs' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(specs)")).all()
        column_names = {row[1] for row in rows}
        if "rapporteurs" in column_names:
            return
        conn.execute(
            text("ALTER TABLE specs ADD COLUMN rapporteurs VARCHAR(128)")
        )


def _migrate_tdoc_cr_cover_page_summary_of_change() -> None:
    """Add ``tdoc_cr_cover_page.summary_of_change`` to databases created
    before that column existed. Idempotent: probe
    ``PRAGMA table_info`` first (same shape as
    :func:`_migrate_spec_rapporteurs`)."""
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdoc_cr_cover_page' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(
            text("PRAGMA table_info(tdoc_cr_cover_page)")
        ).all()
        column_names = {row[1] for row in rows}
        if "summary_of_change" in column_names:
            return
        conn.execute(
            text("ALTER TABLE tdoc_cr_cover_page ADD COLUMN summary_of_change TEXT")
        )


def _migrate_tdocs_xlsx_metadata() -> None:
    """Add six XLSX-metadata columns to ``tdocs``.

    Idempotent: probes ``PRAGMA table_info(tdocs)`` and only issues
    ``ALTER TABLE`` statements for the columns that are absent. Same
    shape as :func:`_migrate_spec_rapporteurs` /
    :func:`_migrate_tdoc_cr_cover_page_summary_of_change`.
    """
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tdocs' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(tdocs)")).all()
        column_names = {row[1] for row in rows}
        additions = [
            ("tdoc_for",          "VARCHAR(64)"),
            ("abstract",          "TEXT"),
            ("secretary_remarks", "TEXT"),
            ("ls_to",             "VARCHAR(256)"),
            ("ls_cc",             "VARCHAR(256)"),
            ("original_ls",       "TEXT"),
        ]
        for name, ddl_type in additions:
            if name in column_names:
                continue
            conn.execute(
                text(f"ALTER TABLE tdocs ADD COLUMN {name} {ddl_type}")
            )


def _migrate_spec_versions_drop_comment() -> None:
    """Drop the unused ``spec_versions.comment`` column. Idempotent;
    degrades to leaving the orphan column on sqlite < 3.35 (no
    ``DROP COLUMN``)."""
    engine = get_engine()
    with engine.begin() as conn:
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='spec_versions' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(spec_versions)")).all()
        column_names = {row[1] for row in rows}
        if "comment" not in column_names:
            return
        try:
            conn.execute(text("ALTER TABLE spec_versions DROP COLUMN comment"))
        except Exception:  # noqa: BLE001 - older sqlite lacks DROP COLUMN
            return


def _create_search_schema() -> None:
    """Create or migrate the FTS5 table, metadata sidecar, and filter indexes.

    FTS5 creation is gated on the runtime compile options so a sqlite build
    without FTS5 does not block the rest of schema creation. Existing virtual
    tables are recreated when their ordered column/indexing layout differs;
    the metadata sidecar and regular-table filter indexes are left intact.
    """
    engine = get_engine()
    expected_layout = (
        ("tdoc_id", True),
        ("title", False),
        ("ftp_url", False),
        ("meeting_title", False),
        ("meeting_location", False),
        ("wis", False),
        ("cover_text", False),
        ("change_text", False),
        ("ttcn_text", False),
        ("content_text", False),
        ("row_kind", True),
        ("document_id", True),
        ("source_file", True),
        ("extraction_version", True),
        ("content_hash", True),
    )
    with engine.begin() as conn:
        try:
            opts = conn.execute(text("PRAGMA compile_options")).all()
        except Exception:  # noqa: BLE001 - best-effort schema creation
            return
        if not any(row[0] == "ENABLE_FTS5" for row in opts):
            return

        existing_sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'tdoc_search'"
            )
        ).scalar()
        if existing_sql is not None:
            try:
                columns_sql = (
                    existing_sql.lower()
                    .split("fts5(", 1)[1]
                    .rsplit(")", 1)[0]
                )
                actual_layout = tuple(
                    (
                        parts[0],
                        len(parts) > 1 and parts[1] == "unindexed",
                    )
                    for declaration in columns_sql.split(",")
                    if (parts := declaration.strip().split())
                )
            except IndexError:
                actual_layout = ()
            if actual_layout != expected_layout:
                conn.execute(text("DROP TABLE tdoc_search"))

        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS tdoc_search USING fts5(
                    tdoc_id UNINDEXED,
                    title,
                    ftp_url,
                    meeting_title,
                    meeting_location,
                    wis,
                    cover_text,
                    change_text,
                    ttcn_text,
                    content_text,
                    row_kind UNINDEXED,
                    document_id UNINDEXED,
                    source_file UNINDEXED,
                    extraction_version UNINDEXED,
                    content_hash UNINDEXED
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tdoc_search_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        )
        # Composite indexes for filter push-down on the regular tdocs and
        # meetings tables. FTS5 is sqlite-only by convention.
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tdocs_release_spec "
                "ON tdocs (release, spec)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_tdocs_uploaded_date "
                "ON tdocs (uploaded_date)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_meetings_name_tsg "
                "ON meetings (name, tsg)"
            )
        )


_VEC_METADATA_COLUMNS = frozenset({"meeting_id", "tsg", "uploaded_date"})


def _create_vector_schema(dim: int | None = None) -> None:
    """Create the sqlite-vec virtual table + meta sidecar.

    Gated on the runtime availability of the sqlite-vec extension —
    when missing this is a no-op. The virtual table stores one row per
    chunk and the dimension is pinned at table-creation time to the
    default embedding dimension (384 for the remote default model); callers
    may pass an explicit ``dim`` which is stamped into ``vec_meta.embedding_dim``.

    Idempotent: ``IF NOT EXISTS`` makes a second ``create_schema`` call
    a no-op.
    """
    engine = get_engine()
    try:
        import sqlite_vec  # noqa: F401 - confirm optional extension is installed
    except ImportError:
        return
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    with engine.begin() as conn:
        try:
            load_sqlite_vec(conn.connection.driver_connection)
        except Exception:  # noqa: BLE001 - best-effort schema creation
            return
        existing_width = None
        layout_dropped = False
        columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(vec_tdoc_embeddings)")).all()
        }
        if columns and not _VEC_METADATA_COLUMNS <= columns:
            # vec0 cannot add columns in place, so the old layout is dropped and
            # recreated empty at its existing width. Rebuild with
            # `doc3gpp tdoc index --rebuild-embeddings` (SQL only, no download).
            existing_sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'vec_tdoc_embeddings'")
            ).scalar_one()
            width_match = re.search(r"FLOAT\[(\d+)\]", existing_sql)
            if width_match is not None:
                existing_width = int(width_match.group(1))
            conn.execute(text("DROP TABLE vec_tdoc_embeddings"))
            layout_dropped = True
        width = int(dim) if dim is not None else (existing_width or 384)
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_tdoc_embeddings USING vec0(\n"
                "    chunk_id TEXT PRIMARY KEY,\n"
                "    tdoc_id TEXT,\n"
                "    chunk_index INTEGER,\n"
                "    meeting_id INTEGER,\n"
                "    tsg TEXT,\n"
                "    uploaded_date TEXT,\n"
                f"    embedding FLOAT[{width}] distance_metric=cosine\n"
                ")"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS vec_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        )
        if layout_dropped:
            conn.execute(
                text(
                    "DELETE FROM vec_meta WHERE key IN "
                    "('last_rebuild_at', 'last_indexed_uploaded_date', "
                    "'last_rebuild_last_tdoc_id')"
                )
            )
        if dim is not None:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO vec_meta (key, value) "
                    "VALUES ('embedding_dim', :d)"
                ),
                {"d": str(width)},
            )


def _create_specdata_orm_tables() -> None:
    """Create the three ``spec_doc_*`` relational tables on the specdata engine.

    The ORM classes hang off
    :class:`~doc3gpp.storage.db.specdata_base.SpecDataBase` and are
    registered on its metadata by the ``models`` import above, so this
    ``create_all`` creates exactly those three tables. Idempotent.
    """
    SpecDataBase.metadata.create_all(bind=get_specdata_engine())


def _create_specdata_search_schema() -> None:
    """Create the specdata FTS5 virtual table + meta sidecar.

    Mirrors :func:`_create_search_schema` but targets the specdata
    engine with the six-indexed-column ``spec_doc_search`` table from the
    spec-doc design (``chunk_id UNINDEXED, text, sections, tables,
    spec_id, version, release``). Gated on FTS5
    availability; idempotent via ``IF NOT EXISTS``.
    """
    engine = get_specdata_engine()
    with engine.begin() as conn:
        try:
            opts = conn.execute(text("PRAGMA compile_options")).all()
        except Exception:  # noqa: BLE001 - best-effort schema creation
            return
        fts5_available = any(row[0] == "ENABLE_FTS5" for row in opts)
        if not fts5_available:
            return
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS spec_doc_search USING fts5(
                    chunk_id UNINDEXED,
                    text,
                    sections,
                    tables,
                    spec_id,
                    version,
                    release
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS spec_doc_search_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        )


def _create_specdata_vector_schema(dim: int | None = None) -> None:
    """Create the specdata sqlite-vec virtual table + meta sidecar.

    Mirrors :func:`_create_vector_schema` but targets the specdata
    engine with the ``vec_spec_doc_embeddings`` / ``vec_spec_doc_meta``
    tables from the spec-doc design. Gated on sqlite-vec; idempotent
    via ``IF NOT EXISTS``.
    """
    engine = get_specdata_engine()
    try:
        import sqlite_vec  # noqa: F401 - confirm optional extension is installed
    except ImportError:
        return
    from doc3gpp.storage.backends.sqlite import load_sqlite_vec

    with engine.begin() as conn:
        try:
            load_sqlite_vec(conn.connection.driver_connection)
        except Exception:  # noqa: BLE001 - best-effort schema creation
            return
        width = int(dim) if dim is not None else 384
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS vec_spec_doc_embeddings USING vec0(\n"
                "    chunk_id TEXT PRIMARY KEY,\n"
                "    spec_id TEXT,\n"
                "    version TEXT,\n"
                "    chunk_index INTEGER,\n"
                f"    embedding FLOAT[{width}] distance_metric=cosine\n"
                ")"
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS vec_spec_doc_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        )
        if dim is not None:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO vec_spec_doc_meta (key, value) "
                    "VALUES ('embedding_dim', :d)"
                ),
                {"d": str(width)},
            )


def _migrate_tdoc_extract_completeness() -> None:
    """Add extraction layout/count/skip columns while preserving legacy rows.

    Existing databases receive version 1, a zero document count, and a NULL
    skipped-source list. SQLAlchemy's inspector keeps the table/column probes
    dialect-safe, while a missing table is left for Base.metadata.create_all
    to create with all columns.
    """
    engine = get_engine()
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("tdoc_extracts"):
            return

        column_names = {
            column["name"] for column in inspector.get_columns("tdoc_extracts")
        }
        additions = (
            ("extraction_version", "INTEGER NOT NULL DEFAULT 1"),
            ("document_count", "INTEGER NOT NULL DEFAULT 0"),
            ("skipped_sources", "TEXT"),
        )
        for name, ddl in additions:
            if name in column_names:
                continue
            conn.execute(
                text(f"ALTER TABLE tdoc_extracts ADD COLUMN {name} {ddl}")
            )


_SPEC_DOC_CHUNK_TEXT_BATCH = 500


def _migrate_spec_doc_chunk_text_blob() -> None:
    """Gzip-JSON compress legacy plain-text ``spec_doc_chunks.text`` rows.

    Rows are selected by ``typeof(text) = 'text'`` in ``chunk_id`` keyset
    batches, one transaction per batch. A rerun after an interruption
    re-selects only the rows still in plain text, and a completed run selects
    nothing. The column keeps its declared ``TEXT`` type on upgraded
    databases because SQLite stores BLOB values unchanged under TEXT
    affinity, so the table is not rebuilt.
    """
    engine = get_specdata_engine()
    with engine.connect() as conn:
        if not inspect(conn).has_table("spec_doc_chunks"):
            return
    after = ""
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT chunk_id, text FROM spec_doc_chunks "
                    "WHERE chunk_id > :after AND typeof(text) = 'text' "
                    "ORDER BY chunk_id LIMIT :limit"
                ),
                {"after": after, "limit": _SPEC_DOC_CHUNK_TEXT_BATCH},
            ).all()
            if not rows:
                return
            for chunk_id, body in rows:
                conn.execute(
                    text(
                        "UPDATE spec_doc_chunks SET text = :blob "
                        "WHERE chunk_id = :chunk_id"
                    ),
                    {"blob": compress_json(body), "chunk_id": chunk_id},
                )
        after = rows[-1][0]


def create_schema(scope: str = "all") -> None:
    """Create database tables for configured backend(s).

    Args:
        scope: ``"main"`` creates the main schema (one-shot
            migrations + ``Base`` tables + FTS5/vector sidecars) on
            the main engine; ``"testcase"`` creates exactly the three
            testcase tables on the testcase engine; ``"specdata"``
            creates exactly the three spec-doc tables plus the
            specdata FTS5/vector sidecars on the specdata engine;
            ``"all"`` (default) does all three. Anything else raises
            :class:`ValueError`.
    """
    if scope not in ("main", "testcase", "specdata", "all"):
        raise ValueError(
            "unknown schema scope "
            f"{scope!r}; choose from: main, testcase, specdata, all."
        )
    if scope in ("main", "all"):
        engine = get_engine()
        _migrate_rename_tdoc_cr_details()
        _migrate_drop_tsg_spec_last_sync()
        _migrate_spec_rapporteurs()
        _migrate_tdoc_cr_cover_page_summary_of_change()
        _migrate_tdocs_xlsx_metadata()
        _migrate_spec_versions_drop_comment()
        _migrate_tdoc_extract_completeness()
        Base.metadata.create_all(bind=engine)
        _create_search_schema()
        _create_vector_schema()
    if scope in ("testcase", "all"):
        TestCaseBase.metadata.create_all(bind=get_testcase_engine())
    if scope in ("specdata", "all"):
        _migrate_spec_doc_chunk_text_blob()
        _create_specdata_orm_tables()
        _create_specdata_search_schema()
        _create_specdata_vector_schema()
