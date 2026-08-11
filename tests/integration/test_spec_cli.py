"""Integration tests for ``doc3gpp spec`` CLI commands.

Uses a stubbed :class:`SpecService` injected via monkeypatch on
``doc3gpp.cli.build_spec_service`` so the CLI commands can be
exercised without any network or schema bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from unittest.mock import MagicMock

import tqdm
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.sync import SyncOutcome

runner = CliRunner()


@dataclass
class _FakeTqdmCall:
    total: int
    desc: str
    unit: str
    dynamic_ncols: bool
    updates: list[int] = field(default_factory=list)
    closed: bool = False


class _FakeTqdm:
    """Drop-in tqdm replacement that records every constructor call.

    Mirrors the real ``tqdm`` API: ``update`` and ``close`` are callable
    on the instance itself (``__enter__`` returns ``self``), so the
    CLI's progress-bar wiring can be exercised in tests where
    ``stderr`` is not a TTY (where real ``tqdm`` is silent).
    """

    instances: list["_FakeTqdm"] = []
    calls: list[_FakeTqdmCall] = []

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.calls = []

    def __init__(self, *, total: int, desc: str, unit: str, dynamic_ncols: bool = False, **_kwargs) -> None:
        self._call = _FakeTqdmCall(total=total, desc=desc, unit=unit, dynamic_ncols=dynamic_ncols)
        _FakeTqdm.calls.append(self._call)
        _FakeTqdm.instances.append(self)
        self.total = total
        self.n = 0

    def __enter__(self) -> "_FakeTqdm":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def update(self, n: int) -> None:
        self.n += n
        self._call.updates.append(n)

    def close(self) -> None:
        self._call.closed = True


def _install_fake_tqdm(monkeypatch) -> type[_FakeTqdm]:
    _FakeTqdm.reset()
    monkeypatch.setattr(tqdm, "tqdm", _FakeTqdm)
    return _FakeTqdm


class _ProgressFakeSpecService:
    """SpecService double that drives the CLI's progress callback.

    Mocks of the service used elsewhere in this module short-circuit
    :meth:`SpecService.sync` and therefore never invoke the
    ``on_progress`` callback the CLI passes in. This fake invokes
    ``list_parsed`` once and ``spec_done`` once per fake spec so the
    CLI's tqdm bar logic runs end-to-end.
    """

    def __init__(self, specs_per_tsg: dict[str, int]) -> None:
        self._specs_per_tsg = specs_per_tsg
        self.sync_calls: list[str] = []

    def sync(self, tsg: str, *, force: bool = False, on_progress=None) -> SyncOutcome:
        self.sync_calls.append(tsg)
        n = self._specs_per_tsg.get(tsg, 0)
        if on_progress is not None:
            on_progress("list_parsed", {"total": n})
            for i in range(n):
                on_progress("spec_done", {"spec_id": f"{tsg}-spec-{i}"})
        return SyncOutcome(
            status="synced",
            reason=f"Spec sync complete for TSG {tsg}: {n} specs, {n} versions stored",
            synced_count=n,
            version_count=n,
        )


def test_spec_sync_help() -> None:
    result = runner.invoke(app, ["spec", "sync", "--help"])
    assert result.exit_code == 0
    assert "--tsg" in result.stdout
    assert "--force" in result.stdout
    # Every TSG short name is listed explicitly (matches the meeting/wi sync style).
    for tsg in ("R1", "R2", "R3", "R4", "R5", "RT", "RP", "C1", "C3", "C4", "C6",
                "CP", "S1", "S2", "S3", "S4", "S5", "S6", "SP"):
        assert tsg in result.stdout, f"--help missing TSG {tsg}"
    # Documents the no-tsg fallback behaviour.
    assert "every distinct TSG" in result.stdout


def test_spec_sync_no_tsg_iterates_meetings_distinct_tsgs(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no ``--tsg`` loops over the distinct TSGs in the
    meetings table and calls :meth:`SpecService.sync` once per TSG."""
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()
    SQLAlchemyMeetingRepository().upsert_many(
        [
            Meeting(meeting_id=1, name="R5-1", title="R5 mtg 1", location="X", tsg="R5", ftp_url="u1"),
            Meeting(meeting_id=2, name="R5-2", title="R5 mtg 2", location="X", tsg="R5", ftp_url="u2"),
            Meeting(meeting_id=3, name="S2-1", title="S2 mtg 1", location="Y", tsg="S2", ftp_url="u3"),
        ]
    )

    svc = MagicMock()
    svc.sync.side_effect = [
        SyncOutcome(
            status="synced",
            reason="Spec sync complete for TSG R5: 1 spec, 1 version stored",
            synced_count=1,
            version_count=1,
        ),
        SyncOutcome(
            status="synced",
            reason="Spec sync complete for TSG S2: 1 spec, 1 version stored",
            synced_count=1,
            version_count=1,
        ),
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    # Two distinct TSGs → two sync calls.
    assert svc.sync.call_count == 2
    synced_tsgs = {call.args[0] for call in svc.sync.call_args_list}
    assert synced_tsgs == {"R5", "S2"}
    assert "Spec sync complete for TSG R5" in result.stdout
    assert "Spec sync complete for TSG S2" in result.stdout


def test_spec_sync_no_tsg_empty_meetings_is_noop(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no ``--tsg`` and no stored meetings prints a
    friendly no-op message and never invokes :meth:`SpecService.sync`."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()

    svc = MagicMock()
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    assert "No stored meetings" in result.stdout
    svc.sync.assert_not_called()


def test_spec_list(monkeypatch) -> None:
    svc = MagicMock()
    svc.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5")
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "list", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert "36.579-5" in result.stdout


def test_spec_sync(sqlite_env, monkeypatch) -> None:
    svc = MagicMock()
    svc.sync.return_value = SyncOutcome(
        status="synced",
        reason="Spec sync complete for TSG R5: 3 specs, 5 versions stored",
        synced_count=3,
        version_count=5,
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--tsg", "R5", "--force"])
    assert result.exit_code == 0, result.stdout
    assert "Spec sync complete" in result.stdout


def test_spec_show_json(monkeypatch) -> None:
    spec = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        status="published",
        radio_tech="5G",
        initial_release="Rel-15",
        tsg="R5",
        wis="eNB",
    )
    version = SpecVersion(
        spec_id="36.579-5",
        version="18.3.0",
        ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5.zip",
        release="Rel-18",
        meeting_id=108,
        meeting_name="RAN#108",
        crs="R5-260013,R5-260014",
        comment="-",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions.return_value = [version]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert "36.579-5" in result.stdout
    assert "18.3.0" in result.stdout


def test_spec_show_json_serialises_upload_date(monkeypatch) -> None:
    """``--format json`` must not crash on a ``date`` upload_date field."""
    spec = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
    )
    version = SpecVersion(
        spec_id="36.579-5",
        version="18.3.0",
        ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5.zip",
        release="Rel-18",
        upload_date=date(2026, 5, 1),
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions.return_value = [version]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    assert '"upload_date": "2026-05-01"' in result.stdout


def test_spec_show_table(monkeypatch) -> None:
    spec = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
    )
    version = SpecVersion(
        spec_id="36.579-5",
        version="18.3.0",
        ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5.zip",
        release="Rel-18",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions.return_value = [version]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5"])
    assert result.exit_code == 0, result.stdout
    assert "36.579-5" in result.stdout
    assert "18.3.0" in result.stdout


def test_spec_show_unknown(monkeypatch) -> None:
    svc = MagicMock()
    svc.get.return_value = None
    svc.list_versions.return_value = []
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "99.999-9"])
    assert result.exit_code != 0
    assert "Unknown spec id" in (result.output + result.stdout)


def test_spec_sync_single_tsg_shows_progress_bar(monkeypatch) -> None:
    """``spec sync --tsg R5`` must open a tqdm bar with desc='spec R5',
    total=1, unit='spec', dynamic_ncols=True and update it once per
    spec the service finishes."""
    fake_cls = _install_fake_tqdm(monkeypatch)
    svc = _ProgressFakeSpecService({"R5": 1})
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "sync", "--tsg", "R5", "--force"])
    assert result.exit_code == 0, result.stdout

    r5_calls = [c for c in fake_cls.calls if c.desc == "spec R5"]
    assert len(r5_calls) == 1, f"expected exactly one bar with desc='spec R5', got {fake_cls.calls}"
    call = r5_calls[0]
    assert call.total == 1
    assert call.unit == "spec"
    assert call.dynamic_ncols is True
    assert call.closed is True
    assert sum(call.updates) == 1


def test_spec_sync_no_tsg_loop_shows_progress_bar_per_tsg(monkeypatch) -> None:
    """``spec sync`` (no --tsg) must open a fresh tqdm bar for every TSG
    in the meetings table, with the per-TSG desc ('spec R5', 'spec S2', ...)."""
    from doc3gpp.models.meeting import Meeting
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.meeting_sql import SQLAlchemyMeetingRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()
    SQLAlchemyMeetingRepository().upsert_many(
        [
            Meeting(meeting_id=1, name="R5-1", title="R5 mtg 1", location="X", tsg="R5", ftp_url="u1"),
            Meeting(meeting_id=2, name="S2-1", title="S2 mtg 1", location="Y", tsg="S2", ftp_url="u2"),
        ]
    )

    fake_cls = _install_fake_tqdm(monkeypatch)
    svc = _ProgressFakeSpecService({"R5": 2, "S2": 3})
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout

    descs = [c.desc for c in fake_cls.calls]
    assert "spec R5" in descs, f"missing 'spec R5' bar; got {descs}"
    assert "spec S2" in descs, f"missing 'spec S2' bar; got {descs}"
    r5 = next(c for c in fake_cls.calls if c.desc == "spec R5")
    s2 = next(c for c in fake_cls.calls if c.desc == "spec S2")
    assert r5.total == 2
    assert s2.total == 3
    assert r5.unit == "spec" and s2.unit == "spec"
    assert r5.dynamic_ncols is True and s2.dynamic_ncols is True
    assert sum(r5.updates) == 2
    assert sum(s2.updates) == 3
    assert r5.closed is True
    assert s2.closed is True
