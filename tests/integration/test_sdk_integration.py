"""SDK-only integration tests — validate the library path without importing CLI.

All imports come from core `doc3gpp.*` modules (models, services, storage, etc.),
never from `doc3gpp.cli` or `typer`. This ensures the SDK works independently
of the CLI (`pip install doc3gpp` without `[cli]` extra).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text

from doc3gpp.config import Settings, get_settings as config_get_settings
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc, TDocWithMeeting
from doc3gpp.models.tsg import Tsg
from doc3gpp.services.tsg_service import TsgService, build_tsg_url
from doc3gpp.settings.loader import get_settings as loader_get_settings
from doc3gpp.settings.schema import Settings as SettingsModel
from doc3gpp.storage.backends import configure_sqlite_engine
from doc3gpp.storage.cache import FileCache
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.db.session import get_engine, get_session_factory
from doc3gpp.storage.export import export_tdocs_csv
from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository


def test_sdk_imports() -> None:
    """All public SDK modules import without typer/CLI dependency."""
    # Models
    assert Meeting.__name__ == "Meeting"
    assert TDoc.__name__ == "TDoc"
    assert Tsg.__name__ == "Tsg"

    # Settings — both paths
    assert SettingsModel is Settings
    assert callable(config_get_settings)
    assert callable(loader_get_settings)

    # Storage
    assert callable(configure_sqlite_engine)
    assert callable(create_schema)
    assert callable(get_engine)
    assert callable(get_session_factory)
    assert callable(export_tdocs_csv)

    # Repositories
    assert SQLAlchemyMeetingRepository.__name__ == "SQLAlchemyMeetingRepository"
    assert SQLAlchemyTDocRepository.__name__ == "SQLAlchemyTDocRepository"
    assert SQLAlchemyTsgRepository.__name__ == "SQLAlchemyTsgRepository"

    # TSG helpers
    assert callable(build_tsg_url)
    assert TsgService.__name__ == "TsgService"

    # Cache
    assert FileCache.__name__ == "FileCache"


def test_sdk_model_construction() -> None:
    """Domain models can be constructed with minimal and full fields."""
    # Meeting — minimum required fields
    m = Meeting(
        meeting_id=1,
        name="RAN5#111",
        title="Test Meeting",
        location="Online",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 5),
    )
    assert m.meeting_id == 1
    assert m.name == "RAN5#111"
    assert m.ftp_url is None
    assert m.updated_at is None

    # Meeting — all optional fields
    m2 = Meeting(
        meeting_id=2,
        name="SA2#150",
        title="SA2 Meeting",
        location="Paris",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 5),
        ftp_url="ftp://example.com",
        start_doc="S2-000001",
        end_doc="S2-000100",
    )
    assert m2.ftp_url == "ftp://example.com"
    assert m2.start_doc == "S2-000001"

    # TDoc — minimum required fields
    t = TDoc(tdoc_id="R1-000001", title="Test Document")
    assert t.tdoc_id == "R1-000001"
    assert t.title == "Test Document"
    assert t.meeting_id is None
    assert t.source is None

    # TDoc — all optional fields
    t2 = TDoc(
        tdoc_id="R5-260001",
        title="RedCap CR",
        meeting_id=1,
        url="https://example.com/doc",
        source="Qualcomm",
        type="CR",
        status="Agreed",
        cr_cat="F",
        spec="38.331",
        related_wis="NR_ext",
        cr_pack="RP-000123",
    )
    assert t2.source == "Qualcomm"
    assert t2.type == "CR"
    assert t2.cr_pack == "RP-000123"

    # Slots check — no __dict__ allowed on slotted dataclasses
    assert not hasattr(m, "__dict__")
    assert not hasattr(t2, "__dict__")

    # Tsg — minimum required fields
    t3 = Tsg(tsg_name="RAN WG1", short_name="R1", description="Layer 1")
    assert t3.tsg_name == "RAN WG1"
    assert t3.url is None

    # Tsg — all optional fields populated
    t4 = Tsg(
        tsg_name="SA WG3",
        short_name="S3",
        description="Security and Privacy",
        url="https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg3",
    )
    assert t4.url == "https://www.3gpp.org/3gpp-groups/service-system-aspects-sa/sa-wg3"

    # Tsg slots
    assert not hasattr(t4, "__dict__")


def test_sdk_config_chain(tmp_path, monkeypatch) -> None:
    """Config re-export resolves to the same settings instance."""
    # Start clean
    config_get_settings.cache_clear()
    loader_get_settings.cache_clear()

    db_path = tmp_path / "test_config.db"
    monkeypatch.setenv("DOC3GPP_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")

    # Both import paths produce a Settings object with the same URL
    from_config = config_get_settings()
    from_loader = loader_get_settings()
    assert from_config.database_url == f"sqlite+pysqlite:///{db_path}"
    assert from_loader.database_url == from_config.database_url

    # The same cached instance
    assert config_get_settings() is from_config
    assert loader_get_settings() is from_loader

    config_get_settings.cache_clear()
    loader_get_settings.cache_clear()


def test_sdk_backend_engine_kwargs() -> None:
    """Backend configuration functions produce correct engine kwargs."""
    sqlite_kwargs = configure_sqlite_engine(
        database_url="sqlite+pysqlite:///:memory:",
        db_echo=False,
    )
    assert sqlite_kwargs["echo"] is False
    assert sqlite_kwargs["future"] is True
    assert sqlite_kwargs.get("connect_args") == {"check_same_thread": False}

    sqlite_file_kwargs = configure_sqlite_engine(
        database_url="sqlite+pysqlite:////tmp/test.db",
        db_echo=True,
    )
    assert sqlite_file_kwargs["echo"] is True


def test_sdk_tsg_seed_and_list(sqlite_env) -> None:
    """The TSG service seeds and queries the ``tsgs`` table end-to-end."""
    create_schema()
    service = TsgService(SQLAlchemyTsgRepository())
    seeded = service.seed_defaults()
    assert seeded == 16

    all_rows = service.list_all()
    assert len(all_rows) == 16

    # Case-insensitive lookup for both short name and tsg_name
    r5 = service.get_by_short_name("r5")
    assert r5 is not None
    assert r5.tsg_name == "RAN WG5"
    assert r5.url == "https://www.3gpp.org/3gpp-groups/radio-access-networks-ran/ran-wg5"

    rt = service.get_by_tsg_name("ran ah1")
    assert rt is not None
    assert rt.short_name == "RT"

    # Validation helper
    assert service.is_known_short_name("S2")
    assert not service.is_known_short_name("r99")

    # Re-seeding is idempotent (no row duplication)
    service.seed_defaults()
    assert len(service.list_all()) == 16

    # URL builder composes the project URL pattern
    assert (
        build_tsg_url("CT WG4")
        == "https://www.3gpp.org/3gpp-groups/core-network-terminals-ct/ct-wg4"
    )


def test_sdk_full_round_trip(sqlite_env) -> None:
    """Create schema, upsert meetings and TDocs, list them back."""
    create_schema()

    # --- Meeting round-trip ---
    meeting_repo = SQLAlchemyMeetingRepository()

    meetings = [
        Meeting(
            meeting_id=100,
            name="RAN3#100",
            title="RAN3 meeting",
            location="Online",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
        ),
        Meeting(
            meeting_id=101,
            name="RAN3#101",
            title="RAN3 meeting 2",
            location="Munich",
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 2),
        ),
    ]
    count = meeting_repo.upsert_many(meetings)
    assert count == 2

    listed = meeting_repo.list(limit=10)
    assert len(listed) == 2
    names = {m.name for m in listed}
    assert names == {"RAN3#100", "RAN3#101"}

    # Round-trip preserves field values
    found = meeting_repo.get_by_id(100)
    assert found is not None
    assert found.title == "RAN3 meeting"
    assert found.location == "Online"
    assert found.start_date == date(2026, 1, 1)

    found_by_name = meeting_repo.get_by_name("RAN3#101")
    assert found_by_name is not None
    assert found_by_name.location == "Munich"

    # --- TDoc round-trip ---
    tdoc_repo = SQLAlchemyTDocRepository()

    td = TDoc(
        tdoc_id="R5s260001",
        title="Test TDoc",
        meeting_id=100,
        url="https://example.test/1",
        source="Qualcomm",
        type="CR",
        status="Agreed",
    )
    tdoc_repo.upsert(td)

    td_updated = TDoc(
        tdoc_id="R5s260001",
        title="Test TDoc Updated",
        meeting_id=100,
        url="https://example.test/1",
        source="Ericsson",
        type="CR",
        status="Noted",
    )
    tdoc_repo.upsert(td_updated)

    tdocs = tdoc_repo.list(limit=5)
    assert len(tdocs) == 1
    assert tdocs[0].title == "Test TDoc Updated"
    assert tdocs[0].source == "Ericsson"

    tdoc_repo.upsert(TDoc(tdoc_id="R6s260002", title="Second TDoc", meeting_id=101))
    tdocs_with_meetings = tdoc_repo.list_with_meeting(limit=5)
    tdoc_map = {item.tdoc.tdoc_id: item for item in tdocs_with_meetings}
    assert tdoc_map["R5s260001"].tdoc.tdoc_id == "R5s260001"
    assert tdoc_map["R6s260002"].tdoc.tdoc_id == "R6s260002"
    assert tdoc_map["R6s260002"].meeting_name is not None

    # Verify DB connectivity via engine directly
    engine = get_engine()
    with engine.connect() as conn:
        meeting_count = conn.execute(text("SELECT COUNT(*) FROM meetings")).scalar()
        tdoc_count = conn.execute(text("SELECT COUNT(*) FROM tdocs")).scalar()
    assert meeting_count == 2
    assert tdoc_count == 2


def test_sdk_file_cache(tmp_path) -> None:
    """FileCache stores and resolves paths correctly."""
    cache = FileCache(tmp_path / "doc3gpp_cache")

    # Directory created on init
    assert (tmp_path / "doc3gpp_cache").exists()

    path = cache.path_for("meetings/R5/list")
    assert path.name == "meetings_R5_list.html"
    assert path.parent == tmp_path / "doc3gpp_cache"

    # Nested key
    nested = cache.path_for("tsg_ran/WG5_Test_ex-T1/docs")
    assert nested.name == "tsg_ran_WG5_Test_ex-T1_docs.html"


def test_sdk_export_csv(tmp_path) -> None:
    """export_tdocs_csv writes correct format."""
    out = tmp_path / "export.csv"
    records = [
        TDocWithMeeting(
            tdoc=TDoc(tdoc_id="R1-000001", title="First", url="https://x/1"),
            meeting_name="RAN1#100",
        ),
        TDocWithMeeting(tdoc=TDoc(tdoc_id="R1-000002", title="Second")),
    ]
    export_tdocs_csv(out, records)

    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "tdoc_id,title,meeting,url"
    assert lines[1] == "R1-000001,First,RAN1#100,https://x/1"
    assert lines[2] == "R1-000002,Second,,"
