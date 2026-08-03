"""Unit tests for the systemd / launchd install helpers.

These never touch the OS service manager: every ``install_*`` /
``uninstall_*`` call passes a ``runner`` that records (or refuses) the
commands, and a ``unit_path`` / ``plist_path`` pointing into a tmp dir.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from doc3gpp.web.install import (
    InstallNotManagedError,
    LAUNCHD_LABEL,
    install_launchd,
    install_systemd,
    render_launchd_plist,
    render_systemd_unit,
    uninstall_launchd,
    uninstall_systemd,
)


def _noop_runner(command: list) -> None:
    pass


def test_render_systemd_unit_includes_marker() -> None:
    body = render_systemd_unit(
        exec_start="/venv/bin/doc3gpp server start --no-open",
        working_dir="/home/jerry/doc3gpp",
        env={"DOC3GPP_CONFIG": "/home/jerry/doc3gpp/doc3gpp.toml"},
        log_file="/var/log/doc3gpp/server.log",
        pid_file="/var/run/doc3gpp/server.pid",
    )
    assert "X-Doc3gpp-Managed=true" in body.split("[Unit]")[1].split("[Service]")[0]
    assert "ExecStart=/venv/bin/doc3gpp server start --no-open" in body
    assert "Environment=DOC3GPP_CONFIG=/home/jerry/doc3gpp/doc3gpp.toml" in body
    assert "WantedBy=default.target" in body
    assert "Restart=on-failure" in body


def test_render_systemd_unit_system_scope_wanted_by() -> None:
    body = render_systemd_unit(
        exec_start="/x server start --no-open",
        working_dir="/x",
        env={},
        log_file="/x/server.log",
        pid_file="/x/server.pid",
        wanted_by="multi-user.target",
    )
    assert "WantedBy=multi-user.target" in body


def test_render_launchd_plist_includes_marker() -> None:
    body = render_launchd_plist(
        label=LAUNCHD_LABEL,
        program_args=["/venv/bin/doc3gpp", "server", "start", "--no-open"],
        working_dir="/home/jerry/doc3gpp",
        log_file="/home/jerry/.local/share/doc3gpp/server.log",
        pid_file="/home/jerry/.local/share/doc3gpp/server.pid",
        env={"DOC3GPP_CONFIG": "/home/jerry/doc3gpp/doc3gpp.toml"},
    )
    assert "X-Doc3gpp-Managed=true" in body
    # Valid XML
    ET.fromstring(body)
    assert "<key>RunAtLoad</key>" in body
    assert "<true/>" in body
    assert "<key>KeepAlive</key>" in body
    assert LAUNCHD_LABEL in body
    root = ET.fromstring(body)
    dict_el = root.find("./dict")
    current_key: str | None = None
    label_value = None
    for child in dict_el:
        if child.tag == "key":
            current_key = child.text
        elif child.tag == "string" and current_key == "Label":
            label_value = child.text
    assert label_value == LAUNCHD_LABEL


def test_install_systemd_dry_run_does_not_write(tmp_path, monkeypatch) -> None:
    target = tmp_path / "doc3gpp.service"
    monkeypatch.setenv("DOC3GPP_CONFIG", "")
    returned = install_systemd(
        dry_run=True,
        unit_path=target,
        runner=_noop_runner,
    )
    assert not target.exists()
    assert str(target) == returned


def test_install_launchd_dry_run_does_not_write(tmp_path) -> None:
    target = tmp_path / "org.doc3gpp.server.plist"
    returned = install_launchd(dry_run=True, plist_path=target, runner=_noop_runner)
    assert not target.exists()
    assert str(target) == returned


def test_install_systemd_writes_and_starts(tmp_path, monkeypatch) -> None:
    target = tmp_path / "doc3gpp.service"
    recorded: list = []
    monkeypatch.setenv("DOC3GPP_CONFIG", "")

    def runner(command: list) -> None:
        recorded.append(command)

    returned = install_systemd(unit_path=target, runner=runner)
    assert target.exists()
    assert "X-Doc3gpp-Managed=true" in target.read_text(encoding="utf-8")
    # no_start=False -> daemon-reload + enable + start
    assert any("daemon-reload" in cmd for cmd in recorded)
    assert any("start" in cmd for cmd in recorded)
    assert str(target) == returned


def test_install_systemd_no_start_skips_start(tmp_path, monkeypatch) -> None:
    target = tmp_path / "doc3gpp.service"
    recorded: list = []

    def runner(command: list) -> None:
        recorded.append(command)

    monkeypatch.setenv("DOC3GPP_CONFIG", "")
    install_systemd(no_start=True, unit_path=target, runner=runner)
    assert not any(cmd and cmd[-1] == "start" for cmd in recorded)


def test_install_launchd_writes_and_loads(tmp_path, monkeypatch) -> None:
    target = tmp_path / "org.doc3gpp.server.plist"
    recorded: list = []

    def runner(command: list) -> None:
        recorded.append(command)

    monkeypatch.setenv("DOC3GPP_CONFIG", "")
    install_launchd(plist_path=target, runner=runner)
    assert target.exists()
    ET.fromstring(target.read_text(encoding="utf-8"))
    assert any(cmd and cmd[0] == "launchctl" and "load" in cmd for cmd in recorded)


def test_uninstall_refuses_unmanaged(tmp_path) -> None:
    target = tmp_path / "doc3gpp.service"
    target.write_text("[Unit]\nDescription=other\n[Service]\nExecStart=/x\n", encoding="utf-8")
    with pytest.raises(InstallNotManagedError):
        uninstall_systemd(unit_path=target, runner=_noop_runner)
    assert target.exists()


def test_uninstall_refuses_missing(tmp_path) -> None:
    target = tmp_path / "doc3gpp.service"
    with pytest.raises(InstallNotManagedError):
        uninstall_systemd(unit_path=target, runner=_noop_runner)


def test_uninstall_removes_managed(tmp_path) -> None:
    target = tmp_path / "doc3gpp.service"
    body = render_systemd_unit(
        exec_start="/x server start --no-open",
        working_dir="/x",
        env={},
        log_file="/x/server.log",
        pid_file="/x/server.pid",
    )
    target.write_text(body, encoding="utf-8")
    recorded: list = []

    def runner(command: list) -> None:
        recorded.append(command)

    uninstall_systemd(unit_path=target, runner=runner)
    assert not target.exists()
    assert any("disable" in cmd for cmd in recorded)


def test_uninstall_launchd_removes_managed(tmp_path) -> None:
    target = tmp_path / "org.doc3gpp.server.plist"
    body = render_launchd_plist(
        label=LAUNCHD_LABEL,
        program_args=["/x", "server", "start", "--no-open"],
        working_dir="/x",
        log_file="/x/server.log",
        pid_file="/x/server.pid",
        env={},
    )
    target.write_text(body, encoding="utf-8")
    recorded: list = []

    def runner(command: list) -> None:
        recorded.append(command)

    uninstall_launchd(plist_path=target, runner=runner)
    assert not target.exists()
    assert any(cmd and cmd[0] == "launchctl" and "unload" in cmd for cmd in recorded)
