from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.base import Base
from doc3gpp.storage.db.models import (
    MeetingORM,  # noqa: F401 - ensures model metadata is loaded
    SpecORM,  # noqa: F401 - ensures model metadata is loaded
    SpecVersionORM,  # noqa: F401 - ensures model metadata is loaded
    TDocCrChangeDetailOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocCrDetailOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocCrTtcnDetailOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocExtractOrm,  # noqa: F401 - ensures model metadata is loaded
    TDocFileORM,  # noqa: F401 - ensures model metadata is loaded
    TDocORM,  # noqa: F401 - ensures model metadata is loaded
    TsgORM,  # noqa: F401 - ensures model metadata is loaded
    WiORM,  # noqa: F401 - ensures model metadata is loaded
)
from doc3gpp.storage.db.session import get_engine


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
    """Create the FTS5 virtual table + meta sidecar.

    Gated on the runtime availability of FTS5 — when missing this is
    a no-op. The check uses ``PRAGMA compile_options`` (FTS5 is
    reported as
    ``ENABLE_FTS5`` when compiled in) and is wrapped in a
    ``try/except`` so an older sqlite without FTS5 (very rare) does
    not block the rest of ``create_schema``.

    The DDL itself matches
    ``docs/superpowers/specs/2026-07-29-fts5-search-design.md`` §"FTS5
    schema". We use stock sqlite's ``unicode61`` tokenizer (no
    ``tokenize=`` directive) — Python's bundled sqlite lacks
    ``ENABLE_FTS5_TOKENIZER`` so a custom Python tokenizer
    registered via ``fts5_tokenizer()`` is unavailable. The
    index-time normalization that fills the gap lives in
    :mod:`doc3gpp.storage.db.fts5_query`.

    Idempotent: ``IF NOT EXISTS`` makes a second ``create_schema``
    call a no-op.
    """
    engine = get_engine()
    with engine.begin() as conn:
        try:
            opts = conn.execute(text("PRAGMA compile_options")).all()
        except Exception:  # noqa: BLE001 - best-effort schema creation
            return
        fts5_available = any(
            row[0] == "ENABLE_FTS5" for row in opts
        )
        if not fts5_available:
            return
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
                    ttcn_text
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
        # Composite indexes for filter push-down on the regular
        # ``tdocs`` and ``meetings`` tables. These complement both the
        # FTS5 virtual table and the sqlite-vec KNN path by accelerating
        # the structured WHERE clauses issued alongside vector/FTS5
        # queries: release/spec/date on tdocs and name/tsg on meetings.
        # Plain CREATE INDEX works cross-dialect, but the surrounding
        # FTS5 gate narrows this to sqlite — FTS5 is sqlite-only by
        # convention. Locked down by
        # ``tests/integration/test_search_indexes.py``.
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


def _create_vector_schema() -> None:
    """Create the sqlite-vec virtual table + meta sidecar.

    Gated on the runtime availability of the sqlite-vec extension —
    when missing this is a no-op. The check tries to import
    ``sqlite_vec`` and
    load it into the underlying pysqlite connection, mirroring the
    runtime probe in
    :class:`~doc3gpp.storage.repositories.vector_sql.SQLAlchemyVectorIndexRepository`.

    The DDL matches ``docs/superpowers/specs/2026-07-31-embedding-search-design.md``
    §"Vector schema". The virtual table stores one row per chunk and the
    dimension is pinned at table-creation time to the default embedding
    dimension (384 for ``all-MiniLM-L6-v2``).

    Idempotent: ``IF NOT EXISTS`` makes a second ``create_schema`` call
    a no-op.
    """
    engine = get_engine()
    try:
        import sqlite_vec
    except ImportError:
        return
    with engine.begin() as conn:
        try:
            sqlite_vec.load(conn.connection.driver_connection)
        except Exception:  # noqa: BLE001 - best-effort schema creation
            return
        conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_tdoc_embeddings USING vec0(
                    chunk_id TEXT PRIMARY KEY,
                    tdoc_id TEXT,
                    chunk_index INTEGER,
                    embedding FLOAT[384] distance_metric=cosine
                )
                """
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


def create_schema() -> None:
    """Create database tables for configured backend."""

    engine = get_engine()
    _migrate_rename_tdoc_cr_details()
    _migrate_drop_tsg_spec_last_sync()
    _migrate_spec_rapporteurs()
    _migrate_tdoc_cr_cover_page_summary_of_change()
    _migrate_spec_versions_drop_comment()
    Base.metadata.create_all(bind=engine)
    _create_search_schema()
    _create_vector_schema()
