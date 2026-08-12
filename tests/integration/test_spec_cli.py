"""Integration tests for ``doc3gpp spec`` CLI commands.

Uses a stubbed :class:`SpecService` injected via monkeypatch on
``doc3gpp.cli.build_spec_service`` so the CLI commands can be
exercised without any network or schema bootstrap.
"""

from __future__ import annotations

import json
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

    def list_distinct_tsgs(self) -> list[str]:
        return list(self._specs_per_tsg)

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


def test_spec_sync_no_tsg_iterates_specs_distinct_tsgs(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no selector loops over the distinct TSGs in the
    specs table and calls :meth:`SpecService.sync` once per TSG."""
    from doc3gpp.models.spec import Spec
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()
    repo = SQLAlchemySpecRepository()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    repo.upsert(Spec(spec_id="38.523-3", type="TS", title="NR signalling", tsg="R5"))
    repo.upsert(Spec(spec_id="23.100", type="TR", title="Arch", tsg="S2"))

    svc = MagicMock()
    svc.list_distinct_tsgs.return_value = ["R5", "S2"]
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
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())

    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    # Two distinct TSGs → two sync calls.
    assert svc.sync.call_count == 2
    synced_tsgs = {call.args[0] for call in svc.sync.call_args_list}
    assert synced_tsgs == {"R5", "S2"}
    assert "Spec sync complete for TSG R5" in result.stdout
    assert "Spec sync complete for TSG S2" in result.stdout


def test_spec_sync_no_tsg_empty_specs_is_noop(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no selector and no stored specs prints a
    friendly no-op message and never invokes :meth:`SpecService.sync`."""
    from doc3gpp.storage.db.migrate import create_schema

    create_schema()

    svc = MagicMock()
    svc.list_distinct_tsgs.return_value = []
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    assert "No stored specs with a TSG found" in result.stdout
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


def test_spec_list_rapporteurs_filter(monkeypatch) -> None:
    svc = MagicMock()
    svc.list_recent.return_value = [
        Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", rapporteurs="Ericsson LM")
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "list", "--rapporteurs", "%Ericsson%", "--format", "json"])
    assert result.exit_code == 0, result.stdout
    svc.list_recent.assert_called_once()
    kwargs = svc.list_recent.call_args.kwargs
    assert kwargs["rapporteurs"] == "%Ericsson%"
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


def test_spec_sync_no_tsg_loop_shows_progress_bar_per_tsg(sqlite_env, monkeypatch) -> None:
    """``spec sync`` (no selector) must open a fresh tqdm bar for every TSG
    in the specs table, with the per-TSG desc ('spec R5', 'spec S2', ...)."""
    from doc3gpp.models.spec import Spec
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()
    repo = SQLAlchemySpecRepository()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    repo.upsert(Spec(spec_id="23.100", type="TR", title="Arch", tsg="S2"))

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


def test_spec_show_forwards_limit_offset_version(monkeypatch) -> None:
    """``spec show`` passes ``--limit``/``--offset``/``--version`` to the service."""
    spec = Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5")
    version = SpecVersion(spec_id="36.579-5", version="18.3.0", ftp_url="u", release="Rel-18")
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions = MagicMock(return_value=[version])
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--limit", "5", "--offset", "2", "--version", "19.%"])
    assert result.exit_code == 0, result.stdout
    svc.list_versions.assert_called_once_with("36.579-5", limit=5, offset=2, version="19.%")


def test_spec_show_no_wis_crs_drops_fields(monkeypatch) -> None:
    """``--no-wis-crs`` drops ``wis`` from the header and ``crs`` from versions."""
    spec = Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", wis="eNB")
    version = SpecVersion(
        spec_id="36.579-5", version="18.3.0", ftp_url="u",
        release="Rel-18", crs="R5-1,R5-2",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions = MagicMock(return_value=[version])
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    result = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json", "--no-wis-crs"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "wis" not in payload["spec"]
    assert "crs" not in payload["versions"][0]


def test_spec_show_json_parity_keeps_wis_crs_by_default(monkeypatch) -> None:
    """``spec show`` default JSON keeps ``wis``/``crs`` — the slim
    ``--no-wis-crs`` variant drops them. Mirrors the CLI-vs-web/MCP
    byte-consistency contract (the ``spec show`` header includes ``wis``
    by default, distinct from ``spec list`` which omits it)."""
    spec = Spec(
        spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5", wis="eNB",
    )
    version = SpecVersion(
        spec_id="36.579-5", version="18.3.0", ftp_url="u",
        release="Rel-18", crs="R5-1,R5-2",
    )
    svc = MagicMock()
    svc.get.return_value = spec
    svc.list_versions = MagicMock(return_value=[version])
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)

    full = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json"])
    assert full.exit_code == 0, full.stdout
    full_payload = json.loads(full.stdout)
    assert "wis" in full_payload["spec"]
    assert "crs" in full_payload["versions"][0]

    slim = runner.invoke(app, ["spec", "show", "36.579-5", "--format", "json", "--no-wis-crs"])
    assert slim.exit_code == 0, slim.stdout
    slim_payload = json.loads(slim.stdout)
    assert "wis" not in slim_payload["spec"]
    assert "crs" not in slim_payload["versions"][0]


def test_spec_sync_spec_id_syncs_single(monkeypatch) -> None:
    """``spec sync --spec-id 36.579-5`` calls ``sync_spec`` once."""
    from doc3gpp.models.spec import Spec
    from doc3gpp.models.sync import SyncOutcome

    svc = MagicMock()
    svc.get.return_value = Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5")
    svc.sync_spec.return_value = SyncOutcome(
        status="synced",
        reason="Spec sync complete for 36.579-5: 1 spec, 2 versions stored",
        synced_count=1,
        version_count=2,
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--spec-id", "36.579-5"])
    assert result.exit_code == 0, result.stdout
    args, kwargs = svc.sync_spec.call_args
    assert args == ("36.579-5",)
    assert kwargs.get("force") is False
    assert kwargs.get("on_progress") is not None
    assert "Spec sync complete for 36.579-5" in result.stdout


def test_spec_sync_tsg_and_spec_id_conflict(monkeypatch) -> None:
    """Passing both ``--tsg`` and ``--spec-id`` is rejected."""
    svc = MagicMock()
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--tsg", "R5", "--spec-id", "36.579-5"])
    assert result.exit_code != 0
    assert "mutually exclusive" in (result.output + result.stdout)


def test_spec_sync_no_selector_iterates_specs_tsgs(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no selector syncs every TSG in the specs table."""
    from doc3gpp.models.sync import SyncOutcome

    svc = MagicMock()
    svc.list_distinct_tsgs.return_value = ["R5", "S2"]
    svc.sync.side_effect = [
        SyncOutcome(status="synced", reason="Spec sync complete for TSG R5: 1 spec, 1 version stored", synced_count=1, version_count=1),
        SyncOutcome(status="synced", reason="Spec sync complete for TSG S2: 1 spec, 1 version stored", synced_count=1, version_count=1),
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())
    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    assert svc.sync.call_count == 2
    synced_tsgs = {call.args[0] for call in svc.sync.call_args_list}
    assert synced_tsgs == {"R5", "S2"}
    svc.list_distinct_tsgs.assert_called_once()


def test_spec_sync_spec_id_dynareport_404_bad_parameter(sqlite_env, monkeypatch) -> None:
    """``spec sync --spec-id`` of a missing spec surfaces BadParameter.

    Pre-flights with an empty `specs` table, mocks `service.sync_spec`
    to raise `SpecUnknownOnUpstreamError`, and asserts the CLI maps it
    to `typer.BadParameter` carrying the upstream message.
    """
    from doc3gpp.services.spec_service import SpecUnknownOnUpstreamError
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()

    svc = MagicMock()
    svc.sync_spec.side_effect = SpecUnknownOnUpstreamError(
        "38.523-1", "missing fields: title, type, tsg_long_name"
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())

    result = runner.invoke(app, ["spec", "sync", "--spec-id", "38.523-1"])
    assert result.exit_code != 0
    assert "38.523-1" in result.output
    assert "unknown on the 3GPP DynaReport upstream" in result.output


def test_spec_sync_spec_id_unknown_tsg_bad_parameter(sqlite_env, monkeypatch) -> None:
    """``spec sync --spec-id`` of a spec whose TSG is not in tsgs surfaces BadParameter."""
    from doc3gpp.services.spec_service import UnknownTsgError
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()

    svc = MagicMock()
    svc.sync_spec.side_effect = UnknownTsgError("38.523-1", "R5", "RAN 5")
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())

    result = runner.invoke(app, ["spec", "sync", "--spec-id", "38.523-1"])
    assert result.exit_code != 0
    assert "unknown TSG short name" in result.output
    assert "R5" in result.output
