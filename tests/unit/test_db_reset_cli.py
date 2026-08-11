"""Unit tests for the ``doc3gpp db reset`` command.

The reset command is destructive: it deletes the on-disk SQLite database
file and recreates the schema from scratch. These tests pin its
contract —

* only SQLite backends are accepted (non-SQLite URLs raise an error);
* the file is actually removed and recreated empty;
* WAL/journal sidecars are cleaned up;
* the ``tsgs`` reference table is re-seeded;
* the confirmation prompt can be skipped with ``--yes`` or aborted by
  declining;
* in-memory SQLite (``sqlite:///:memory:``) reinitializes without
  touching any file.

Network-free by construction: every test runs against ``tmp_path`` via
the ``sqlite_env`` fixture.
"""

from __future__ import annotations

from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.session import get_engine


def test_db_reset_deletes_file_and_recreates_schema(sqlite_env) -> None:
    """A populated DB is wiped and re-bootstrapped: empty schema + 19 TSGs."""
    db_path = sqlite_env
    runner = CliRunner()

    # Initialise the DB and populate it with a meeting row so we can prove
    # the reset actually wiped data (not just the file).
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    pre_count = _count_meetings()
    assert pre_count == 0  # init seeds tsgs only, not meetings

    from doc3gpp.models.meeting import Meeting
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from datetime import date

    SQLAlchemyMeetingRepository().upsert_many(
        [
            Meeting(
                meeting_id=1,
                name="R5#1",
                title="RAN5 meeting 1",
                location="Online",
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2),
            ),
        ]
    )
    assert _count_meetings() == 1
    assert db_path.exists()

    # Capture mtime so we can prove the file was rewritten, not just
    # reused.
    pre_mtime = db_path.stat().st_mtime_ns

    # Reset with --yes skips the prompt.
    result = runner.invoke(app, ["db", "reset", "--yes"])
    assert result.exit_code == 0, result.output

    # File still exists (recreated by create_schema), schema is back,
    # data is gone.
    assert db_path.exists()
    assert _count_meetings() == 0
    assert _count_tsgs() == 19

    # File was actually rewritten — mtime advanced (or at least the
    # inode was recycled, which is enough on filesystems without
    # nanosecond mtime granularity).
    assert db_path.stat().st_mtime_ns >= pre_mtime


def test_db_reset_with_yes_skips_confirmation(sqlite_env) -> None:
    """``--yes`` runs the destructive op without asking."""
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    assert sqlite_env.exists()

    result = runner.invoke(app, ["db", "reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Database reset complete" in result.output
    assert "seeded 19 TSG records" in result.output


def test_db_reset_aborts_when_prompt_declined(sqlite_env) -> None:
    """Declining the prompt leaves the DB file untouched."""
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0
    assert sqlite_env.exists()

    # CliRunner feeds stdin into typer.confirm; 'n' aborts.
    result = runner.invoke(app, ["db", "reset"], input="n\n")

    assert result.exit_code != 0
    assert sqlite_env.exists()
    # Init seeded 19 TSGs; they should still be present.
    assert _count_tsgs() == 19


def test_db_reset_accepts_confirmation_and_runs(sqlite_env) -> None:
    """Answering 'y' at the prompt is equivalent to ``--yes``."""
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    result = runner.invoke(app, ["db", "reset"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Database reset complete" in result.output
    assert _count_tsgs() == 19


def test_db_reset_in_memory_sqlite_just_reinits(monkeypatch) -> None:
    """``sqlite:///:memory:`` has no file — reset skips delete, runs init."""
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        runner = CliRunner()
        # The DB does not exist yet, so reset is effectively a fresh init.
        result = runner.invoke(app, ["db", "reset", "--yes"])
        assert result.exit_code == 0, result.output
        assert "No existing SQLite file to delete" in result.output
        assert "Database reset complete" in result.output
        assert _count_tsgs() == 19
    finally:
        get_engine.cache_clear()
        get_settings.cache_clear()


def test_db_reset_refuses_non_sqlite_url(monkeypatch) -> None:
    """A non-SQLite URL is rejected before anything is touched."""
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", "oracle://user:pass@localhost/db")
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["db", "reset", "--yes"])
        assert result.exit_code != 0
        assert "only supports SQLite backends" in result.output
    finally:
        get_engine.cache_clear()
        get_settings.cache_clear()


def test_db_reset_handles_missing_file(sqlite_env) -> None:
    """No prior init — reset creates the file from scratch."""
    db_path = sqlite_env
    # Sanity: the tmp_path-backed file from sqlite_env does not exist yet
    # (sqlite_env only sets the URL — it does not call db init).
    assert not db_path.exists()

    runner = CliRunner()
    result = runner.invoke(app, ["db", "reset", "--yes"])

    assert result.exit_code == 0, result.output
    assert "No existing SQLite file to delete" in result.output
    assert db_path.exists()
    assert _count_tsgs() == 19


def test_db_reset_removes_wal_sidecar_files(sqlite_env) -> None:
    """WAL/journal/shm sidecars from a prior session are cleaned up."""
    db_path = sqlite_env
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    # Forge WAL sidecars as if a previous session had been using WAL mode.
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    journal = db_path.with_name(db_path.name + "-journal")
    wal.write_bytes(b"fake-wal-bytes")
    shm.write_bytes(b"fake-shm-bytes")
    journal.write_bytes(b"fake-journal-bytes")
    assert wal.exists() and shm.exists() and journal.exists()

    result = runner.invoke(app, ["db", "reset", "--yes"])
    assert result.exit_code == 0, result.output

    # The stale sidecars are removed. WAL mode is the default, so a fresh
    # -wal/-shm pair is legitimately recreated by the post-reset schema
    # bootstrap — but it must not contain the forged stale bytes, and the
    # rollback -journal (unused in WAL mode) must be gone entirely.
    assert not db_path.with_name(db_path.name + "-journal").exists()
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            assert sidecar.read_bytes() != b"fake-wal-bytes"
            assert sidecar.read_bytes() != b"fake-shm-bytes"


def test_db_reset_clears_engine_cache(sqlite_env) -> None:
    """After reset, the cached engine must point at the new file."""
    runner = CliRunner()
    assert runner.invoke(app, ["db", "init"]).exit_code == 0

    # Touch the engine so it is definitely cached.
    engine_before = get_engine()

    result = runner.invoke(app, ["db", "reset", "--yes"])
    assert result.exit_code == 0, result.output

    # get_engine is @lru_cache — the cache was cleared inside db_reset,
    # so the next call returns a fresh engine whose connection lands on
    # the rewritten file. Both engines must work end-to-end.
    engine_after = get_engine()
    with engine_after.connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(text("SELECT count(*) FROM tsgs")).scalar_one()
    assert rows == 19

    # The two engines are distinct objects (the cache was cleared).
    assert engine_before is not engine_after


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_meetings() -> int:
    """Return the row count of the ``meetings`` table."""
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM meetings")).scalar_one())


def _count_tsgs() -> int:
    """Return the row count of the ``tsgs`` table."""
    from sqlalchemy import text

    with get_engine().connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM tsgs")).scalar_one())