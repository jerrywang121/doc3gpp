"""systemd / launchd install & uninstall helpers for the doc3gpp HTTP server.

The ``render_*`` functions produce a unit / plist body with an
``X-Doc3gpp-Managed=true`` marker so ``uninstall_*`` refuses to touch
files it did not create. The ``install_*`` / ``uninstall_*`` functions
resolve runtime paths from the active settings (config file, cache dir),
render, write the target file (unless ``dry_run``), and run the OS
service manager.

Every ``install_*`` / ``uninstall_*`` accepts a ``runner`` callable that
replaces the real ``subprocess`` invocation — a seam for tests that must
not touch the OS service manager.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Literal, Mapping

from doc3gpp.config import get_settings
from doc3gpp.settings.config_source import find_config_file

__all__ = [
    "InstallNotManagedError",
    "install_launchd",
    "install_systemd",
    "render_launchd_plist",
    "render_systemd_unit",
    "uninstall_launchd",
    "uninstall_systemd",
]

#: Marker header line placed in the ``[Unit]`` section of a managed unit.
_MANAGED_MARKER = "X-Doc3gpp-Managed=true"

#: launchd ``Label`` used for the doc3gpp server agent.
LAUNCHD_LABEL = "org.doc3gpp.server"

#: user-scope systemd target (the ``--user`` default).
_USER_WANTED_BY = "default.target"
#: system-scope systemd target.
_SYSTEM_WANTED_BY = "multi-user.target"


class InstallNotManagedError(RuntimeError):
    """Raised when an uninstall target lacks the ``X-Doc3gpp-Managed`` marker.

    Guards against removing a unit/plist that doc3gpp did not create.
    """


def render_systemd_unit(
    *,
    exec_start: str,
    working_dir: str,
    env: Mapping[str, str],
    log_file: str,
    pid_file: str,
    description: str = "doc3gpp web + MCP server",
    wanted_by: str = _USER_WANTED_BY,
) -> str:
    """Render a ``systemd.service`` unit body for the doc3gpp server.

    Args:
        exec_start: The ``ExecStart`` command line (already resolved).
        working_dir: The ``WorkingDirectory`` for the service.
        env: Environment variables; each becomes an ``Environment=K=V`` line.
        log_file: Standard output/error log path.
        pid_file: PID file path (informational, not consumed by systemd).
        description: ``Description`` value.
        wanted_by: ``WantedBy`` target for the ``[Install]`` section.

    Returns:
        The unit file body as a string.
    """
    env_lines = "\n".join(
        f"Environment={key}={value}" for key, value in env.items()
    )
    env_block = f"{env_lines}\n" if env_lines else ""
    return (
        "[Unit]\n"
        f"Description={description}\n"
        f"{_MANAGED_MARKER}\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        f"WorkingDirectory={working_dir}\n"
        f"StandardOutput=append:{log_file}\n"
        f"StandardError=append:{log_file}\n"
        f"# pid_file: {pid_file}\n"
        f"{env_block}"
        "\n"
        "[Install]\n"
        f"WantedBy={wanted_by}\n"
    )


def render_launchd_plist(
    *,
    label: str,
    program_args: list[str],
    working_dir: str,
    log_file: str,
    pid_file: str,
    env: Mapping[str, str],
) -> str:
    """Render a launchd plist body for the doc3gpp server.

    The managed marker is encoded into an XML comment inside the
    ``Label`` metadata block so ``uninstall_launchd`` can find it.

    Args:
        label: The launchd ``Label``.
        program_args: The ``ProgramArguments`` array.
        working_dir: The ``WorkingDirectory``.
        log_file: Both ``StandardOutPath`` and ``StandardErrorPath``.
        pid_file: Informational PID path in the plist comment.
        env: ``EnvironmentVariables`` dict.

    Returns:
        The plist body as a string.
    """
    from xml.sax.saxutils import escape

    def esc(value: str) -> str:
        return escape(value)

    env_items = "".join(
        f"      <key>{esc(key)}</key>\n      <string>{esc(value)}</string>\n"
        for key, value in env.items()
    )
    arg_items = "".join(f"      <string>{esc(arg)}</string>\n" for arg in program_args)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        f"  <!-- {_MANAGED_MARKER}; pid_file: {pid_file} -->\n"
        "  <key>Label</key>\n"
        f"  <string>{esc(label)}</string>\n"
        "  <key>RunAtLoad</key>\n  <true/>\n"
        "  <key>KeepAlive</key>\n  <true/>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{arg_items}"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{esc(working_dir)}</string>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{esc(log_file)}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{esc(log_file)}</string>\n"
        "  <key>EnvironmentVariables</key>\n"
        "  <dict>\n"
        f"{env_items}"
        "  </dict>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _resolve_server_command() -> tuple[str, str]:
    """Resolve ``(exec_start, working_dir)`` for the service.

    ``exec_start`` prefers the ``doc3gpp`` console script on ``PATH``,
    falling back to running the CLI through the current interpreter.
    """
    console = shutil.which("doc3gpp")
    if console:
        base = console
    else:
        base = f"{sys.executable} -m doc3gpp"
    return f"{base} server start --no-open", str(Path.cwd())


def _resolve_env() -> dict[str, str]:
    """Build the service environment: the active config path if any."""
    env: dict[str, str] = {}
    config = find_config_file()
    if config is not None:
        env["DOC3GPP_CONFIG"] = str(config)
    return env


def _resolve_log_pid(settings) -> tuple[str, str]:
    """Resolve ``(log_file, pid_file)`` from server + cache settings."""
    cache_dir = str(settings.cache.dir)
    log_file = settings.server.log_file or f"{cache_dir}/server.log"
    pid_file = settings.server.pid_file or f"{cache_dir}/server.pid"
    return log_file, pid_file


def _systemd_unit_path(scope: Literal["user", "system"]) -> Path:
    if scope == "system":
        return Path("/etc/systemd/system/doc3gpp.service")
    return Path.home() / ".config" / "systemd" / "user" / "doc3gpp.service"


def install_systemd(
    *,
    scope: Literal["user", "system"] = "user",
    no_start: bool = False,
    dry_run: bool = False,
    unit_path: Path | None = None,
    runner: Callable[[list[str]], None] | None = None,
) -> str:
    """Install (or dry-run) the systemd unit for the doc3gpp server.

    Args:
        scope: ``"user"`` or ``"system"`` install scope.
        no_start: Write the unit but do not ``enable --now``.
        dry_run: Only render; do not write or start.
        unit_path: Override the target file (tests).
        runner: Replaces the subprocess call (tests).

    Returns:
        The path the unit was (or would be) written to, as a string.
    """
    exec_start, working_dir = _resolve_server_command()
    env = _resolve_env()
    log_file, pid_file = _resolve_log_pid(get_settings())
    wanted_by = _SYSTEM_WANTED_BY if scope == "system" else _USER_WANTED_BY
    body = render_systemd_unit(
        exec_start=exec_start,
        working_dir=working_dir,
        env=env,
        log_file=log_file,
        pid_file=pid_file,
        wanted_by=wanted_by,
    )
    target = unit_path or _systemd_unit_path(scope)
    if dry_run:
        print(body, end="")
        return str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _run_service_commands(
        runner,
        [
            ["systemctl", f"--{scope}", "daemon-reload"],
            ["systemctl", f"--{scope}", "enable", "doc3gpp.service"],
        ]
        + ([] if no_start else [["systemctl", f"--{scope}", "start", "doc3gpp.service"]]),
    )
    return str(target)


def install_launchd(
    *,
    no_start: bool = False,
    dry_run: bool = False,
    plist_path: Path | None = None,
    runner: Callable[[list[str]], None] | None = None,
) -> str:
    """Install (or dry-run) the launchd agent plist.

    Args:
        no_start: Write the plist but do not ``launchctl load``.
        dry_run: Only render; do not write or load.
        plist_path: Override the target file (tests).
        runner: Replaces the subprocess call (tests).

    Returns:
        The path the plist was (or would be) written to, as a string.
    """
    exec_start, working_dir = _resolve_server_command()
    env = _resolve_env()
    log_file, pid_file = _resolve_log_pid(get_settings())
    body = render_launchd_plist(
        label=LAUNCHD_LABEL,
        program_args=exec_start.split(),
        working_dir=working_dir,
        log_file=log_file,
        pid_file=pid_file,
        env=env,
    )
    target = plist_path or (
        Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    )
    if dry_run:
        print(body, end="")
        return str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    if not no_start:
        _run_service_commands(runner, [["launchctl", "load", "-w", str(target)]])
    return str(target)


def uninstall_systemd(
    *,
    scope: Literal["user", "system"] = "user",
    unit_path: Path | None = None,
    runner: Callable[[list[str]], None] | None = None,
) -> None:
    """Uninstall the systemd unit, refusing unmanaged files.

    Raises:
        InstallNotManagedError: when the unit lacks the managed marker.
    """
    target = unit_path or _systemd_unit_path(scope)
    _assert_managed(target)
    _run_service_commands(
        runner,
        [
            ["systemctl", f"--{scope}", "disable", "doc3gpp.service"],
            ["systemctl", f"--{scope}", "stop", "doc3gpp.service"],
        ],
    )
    target.unlink(missing_ok=True)


def uninstall_launchd(
    *,
    plist_path: Path | None = None,
    runner: Callable[[list[str]], None] | None = None,
) -> None:
    """Uninstall the launchd plist, refusing unmanaged files.

    Raises:
        InstallNotManagedError: when the plist lacks the managed marker.
    """
    target = plist_path or (
        Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    )
    _assert_managed(target)
    _run_service_commands(runner, [["launchctl", "unload", "-w", str(target)]])
    target.unlink(missing_ok=True)


def _assert_managed(path: Path) -> None:
    """Raise :class:`InstallNotManagedError` unless the file is managed."""
    if not path.exists():
        raise InstallNotManagedError(
            f"{path} does not exist; nothing to uninstall."
        )
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        raise InstallNotManagedError(f"cannot read {path}: {exc}") from exc
    if _MANAGED_MARKER not in body:
        raise InstallNotManagedError(
            f"{path} is not managed by doc3gpp "
            f"(missing {_MANAGED_MARKER!r}); refusing to remove it."
        )


def _run_service_commands(
    runner: Callable[[list[str]], None] | None,
    commands: list[list[str]],
) -> None:
    """Run each service-manager command, honouring the ``runner`` seam."""
    run = runner or _default_runner
    for command in commands:
        run(command)


def _default_runner(command: list[str]) -> None:
    subprocess.run(command, check=False)
