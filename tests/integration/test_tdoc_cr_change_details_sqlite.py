"""Integration test: tdoc_cr_change_details table is created."""

from __future__ import annotations

from sqlalchemy import text

from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine


def test_table_is_created_with_expected_columns() -> None:
    create_schema()
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(tdoc_cr_change_details)")).all()
    cols = {row[1] for row in rows}
    assert "ftp_url" in cols
    assert "tdoc_id" in cols
    assert "clauses" in cols
    assert "changes" in cols


def test_upsert_and_get_round_trip(sqlite_env) -> None:
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )

    create_schema()
    repo = SQLAlchemyTDocCrChangeDetailsRepository()
    details = TDocCRChangeDetails(
        ftp_url="tsg_wg1/CR123.zip",
        tdoc_id="R5-999999",
        clauses=("5.2.3", "Table 5.2.3-1"),
        changes=(
            {"clauses": ["5.2.3"], "text": "line A\nline B"},
            {"clauses": ["5.2.3"], "text": "line C"},
        ),
    )
    # FK needs the parent tdoc row; create one. Note: TDocORM has
    # no `tsg` column — that lives on the parent meeting row.
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocORM
    sf = get_session_factory()
    with sf() as s:
        s.add(TDocORM(tdoc_id="R5-999999", ftp_url="tsg_wg1/CR123.zip",
                      meeting_id=None))
        s.commit()
    repo.upsert(details)

    fetched = repo.get_by_url("tsg_wg1/CR123.zip")
    assert fetched is not None
    assert fetched.tdoc_id == "R5-999999"
    assert fetched.clauses == ("5.2.3", "Table 5.2.3-1")
    assert fetched.changes == (
        {"clauses": ["5.2.3"], "text": "line A\nline B"},
        {"clauses": ["5.2.3"], "text": "line C"},
    )

    by_id = repo.get_for_tdoc_id("R5-999999")
    assert len(by_id) == 1
    assert by_id[0].ftp_url == "tsg_wg1/CR123.zip"


def test_get_by_url_returns_none_on_miss(sqlite_env) -> None:
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )

    create_schema()
    assert SQLAlchemyTDocCrChangeDetailsRepository().get_by_url("nope") is None


def test_cascade_delete_with_parent_tdoc(sqlite_env) -> None:
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocORM
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )

    create_schema()
    repo = SQLAlchemyTDocCrChangeDetailsRepository()
    sf = get_session_factory()
    with sf() as s:
        s.add(TDocORM(tdoc_id="R5-CASCADE", ftp_url="x/y.zip",
                      meeting_id=None))
        s.commit()
    repo.upsert(TDocCRChangeDetails(
        ftp_url="x/y.zip", tdoc_id="R5-CASCADE",
        clauses=("1.0",), changes=({"clauses": ["1.0"], "text": "a"},),
    ))
    with sf() as s:
        s.query(TDocORM).filter_by(tdoc_id="R5-CASCADE").delete()
        s.commit()
    assert repo.get_by_url("x/y.zip") is None


def test_end_to_end_extract_writes_sidecar(tmp_path, sqlite_env) -> None:
    """Loading a real CR zip end-to-end writes the new sidecar row,
    and ``tdoc show --tdoc <id> --format json`` surfaces it.

    The fixture's CR markdown may or may not contain
    ``<ins>`` / ``<del>`` markers; the test bypasses the real parser
    and injects a fake one that always returns a populated
    :class:`TDocCRChangeDetails` so the fan-out is exercised end-to-end
    deterministically. Then ``tdoc show --format json`` is exercised to
    confirm the CLI surfaces the new ``changes`` key on the rendered
    DTO.
    """
    from typer.testing import CliRunner
    from unittest.mock import MagicMock

    from doc3gpp.cli import app
    from doc3gpp.models.tdoc_cr import (
        TDocCRDetails,
        TDocCRParseResult,
    )
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
    from doc3gpp.scraping.cache import TDocCache
    from doc3gpp.services.tdoc_cr_service import TDocCrService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_session_factory
    from doc3gpp.storage.db.models import TDocORM
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )
    from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    # Pick a small CR zip fixture.
    fixtures_dir = __import__("pathlib").Path("tests/fixtures/tdoc_cr_doc")
    fixture = next(
        p for p in fixtures_dir.iterdir() if p.suffix == ".zip"
    )
    sf = get_session_factory()
    with sf() as s:
        s.add(TDocORM(
            tdoc_id="R5-000001", ftp_url="tsg_wg1/CR_fixture.zip",
            type="CR", meeting_id=None,
        ))
        s.commit()

    # Build a service with a tmp_path-rooted cache so the test never
    # touches the user's real cache, and stub the scraper so the
    # download returns the fixture's bytes without any network I/O.
    cache = TDocCache(root=tmp_path / "cache", size_limit_bytes=0)
    scraper_stub = MagicMock()
    fixture_bytes = fixture.read_bytes()
    scraper_stub.get_bytes = MagicMock(
        side_effect=lambda url: fixture_bytes,
    )

    # Inject a fake parser that always returns a populated
    # TDocCRChangeDetails so the sidecar fan-out fires deterministically.
    class _StubParser:
        parser_version = "test-stub"

        def supports(
            self, tdoc_id: str, *, tdoc_type: str | None = None,
            spec: str | None = None,
        ) -> bool:
            return True

        def parse(
            self, markdown: str, *, tdoc_id: str,
            max_text_length: int = 0, full: bool = False,
        ) -> TDocCRParseResult:
            return TDocCRParseResult(
                cover=TDocCRDetails(
                    tdoc_id=tdoc_id,
                    spec="38.523-3",
                    cr_num="9999",
                ),
                ttcn=None,
                changes=TDocCRChangeDetails(
                    ftp_url=None, tdoc_id=None,
                    clauses=("5.2.3",),
                    changes=({"clauses": ["5.2.3"], "text": "line A\n<ins>X</ins>"},),
                ),
            )

    service = TDocCrService(
        cache=cache,
        scraper_client=scraper_stub,
        cr_repository=SQLAlchemyTDocCrRepository(),
        cr_ttcn_repository=SQLAlchemyTDocCrTtcnRepository(),
        cr_change_details_repository=SQLAlchemyTDocCrChangeDetailsRepository(),
        tdoc_repository=SQLAlchemyTDocRepository(),
        parser=_StubParser(),  # type: ignore[arg-type]
    )

    service.extract("R5-000001", force=True)

    # The sidecar row exists at the resolved URL.
    repo = SQLAlchemyTDocCrChangeDetailsRepository()
    fetched = repo.get_by_url("tsg_wg1/CR_fixture.zip")
    assert fetched is not None
    assert fetched.tdoc_id == "R5-000001"
    assert fetched.clauses == ("5.2.3",)

    # tdoc show --format json includes the new key.
    runner = CliRunner()
    show = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5-000001", "--format", "json"],
    )
    assert show.exit_code == 0, show.output
    import json as _json
    payload = _json.loads(show.output)
    assert "changes" in payload
    assert payload["changes"]["clauses"] == ["5.2.3"]