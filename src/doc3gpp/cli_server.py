from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import click
import typer

from doc3gpp.config import get_settings
from doc3gpp.settings.schema import Settings

server_app = typer.Typer(
    help="manage the doc3gpp HTTP server (FastAPI + uvicorn) and the MCP sub-app",
    no_args_is_help=True,
)


def _require_server_enabled(settings: Settings) -> None:
    """Refuse the subcommand unless the operator opted in via ``[server] enabled = true``.

    Raises:
        click.UsageError: when ``Settings.server.enabled is False``. The
            message points the operator at the TOML flag so they can
            enable the server before retrying.
    """
    if not settings.server.enabled:
        raise click.UsageError(
            "doc3gpp server is disabled. "
            "Set `[server] enabled = true` in doc3gpp.toml and retry."
        )


def _pid_file(settings: Settings) -> Path:
    cache_dir = str(settings.cache.dir)
    return Path(settings.server.pid_file or f"{cache_dir}/server.pid")


def _log_file(settings: Settings) -> Path:
    cache_dir = str(settings.cache.dir)
    return Path(settings.server.log_file or f"{cache_dir}/server.log")


def _read_pid(settings: Settings) -> int | None:
    path = _pid_file(settings)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _wait_healthy(url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


@server_app.command("start")
def server_start(
    host: str | None = typer.Option(None, "--host", help="Bind host (overrides the server.host setting)."),
    port: int | None = typer.Option(None, "--port", help="Bind port (overrides the server.port setting)."),
    open: bool = typer.Option(None, "--open", help="Open the bound URL in a browser once the server is up."),
    reload: bool = typer.Option(False, "--reload", help="Run uvicorn in auto-reload mode (development only)."),
    no_open: bool = typer.Option(False, "--no-open", help="Suppress the auto-open behaviour even when configured."),
) -> None:
    """Start the doc3gpp HTTP server (foreground with --reload, else background)."""
    settings = get_settings()
    _require_server_enabled(settings)
    bind_host = settings.server.host if host is None else host
    bind_port = settings.server.port if port is None else port
    url = f"http://{bind_host}:{bind_port}/"
    if reload:
        import uvicorn

        uvicorn.run(
            "doc3gpp.web.app:build_app",
            factory=True,
            host=bind_host,
            port=bind_port,
            reload=True,
            reload_dirs=["src/doc3gpp/web/"],
            log_level="debug",
        )
        return

    log_path = _log_file(settings)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path = _pid_file(settings)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    from doc3gpp.settings.config_source import find_config_file

    config = find_config_file()
    env = {**os.environ}
    if config is not None:
        env["DOC3GPP_CONFIG"] = str(config)

    import builtins

    with builtins.open(log_path, "ab") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "doc3gpp.web.app:build_app",
                "--factory",
                "--host",
                bind_host,
                "--port",
                str(bind_port),
            ],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(f"{proc.pid}\n", encoding="utf-8")

    if not _wait_healthy(f"{url}healthz"):
        click.echo(f"server started (pid {proc.pid}) but did not become healthy within the timeout.", err=True)
        raise typer.Exit(code=1)

    click.echo(f"server running at {url}")
    if no_open or open is False:
        click.echo("suppressed browser open")
    else:
        _open_browser(url)

def _open_browser(url: str) -> None:
    import webbrowser

    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - best-effort
        click.echo(f"could not open a browser; visit {url} manually", err=True)


@server_app.command("stop")
def server_stop() -> None:
    """Stop a running background HTTP server (best-effort)."""
    settings = get_settings()
    _require_server_enabled(settings)
    pid = _read_pid(settings)
    pid_path = _pid_file(settings)
    if pid is None:
        click.echo("server is not running (no pid file).")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"pid {pid} is not alive; removing stale pid file.")
        pid_path.unlink(missing_ok=True)
        return
    except PermissionError as exc:
        raise click.ClickException(f"cannot signal pid {pid}: {exc}") from exc

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        click.echo("server did not exit gracefully; sent SIGKILL.")
    pid_path.unlink(missing_ok=True)
    click.echo(f"server stopped (pid {pid}).")


@server_app.command("status")
def server_status() -> None:
    """Report whether the HTTP server is running."""
    settings = get_settings()
    _require_server_enabled(settings)
    pid = _read_pid(settings)
    pid_path = _pid_file(settings)
    bind_host = settings.server.host
    bind_port = settings.server.port

    os_service = _os_service_state()

    if pid is None:
        click.echo("not-installed (no pid file)")
        if os_service:
            click.echo(f"  OS service: {os_service}")
        return

    alive = False
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True

    if not alive:
        click.echo("stopped (pid file present but process is not alive)")
        if os_service:
            click.echo(f"  OS service: {os_service}")
        return

    uptime = "unknown"
    if pid_path.exists():
        age = time.time() - pid_path.stat().st_mtime
        uptime = f"{int(age)}s"

    click.echo(f"running (pid {pid}, uptime {uptime})")
    if os_service:
        click.echo(f"  OS service: {os_service}")
    click.echo(f"  HTTP:  http://{bind_host}:{bind_port}/")
    click.echo(f"  MCP:   http://{bind_host}:{bind_port}/mcp")
    click.echo(f"  Last job: {_last_job_summary(settings)}")


def _os_service_state() -> str | None:
    """Best-effort probe of the systemd / launchd managed service state.

    Returns a human string like ``active (running)`` or ``None`` when the
    service manager is not reachable or the unit is not installed. Never
    raises; used only to enrich the ``status`` output.
    """
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(  # noqa: S603,S607
                ["launchctl", "list"], capture_output=True, text=True, timeout=3
            )
            if "org.doc3gpp.server" in proc.stdout:
                return "loaded (launchd)"
            return None
        proc = subprocess.run(  # noqa: S603,S607
            ["systemctl", "--user", "is-active", "doc3gpp.service"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        state = proc.stdout.strip()
        if not state:
            return None
        if proc.returncode != 0 and "inactive" not in state:
            return None
        return state or None
    except Exception:  # noqa: BLE001 - best-effort probe
        return None


def _last_job_summary(settings: Settings) -> str:
    try:
        from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

        repo = SQLAlchemyJobRepository()
        jobs = repo.list(limit=1)
    except Exception as exc:  # pragma: no cover - best-effort
        return f"unavailable ({exc})"
    if not jobs:
        return "none"
    job = jobs[0]
    return f"{job.id} ({job.status.value}, {job.kind.value})"


@server_app.command("logs")
def server_logs(
    job: str | None = typer.Option(None, "--job", help="Limit logs to a specific job id."),
    follow: bool = typer.Option(False, "--follow/--no-follow", "-f", help="Follow the log file (like `tail -f`)."),
) -> None:
    """Print recent server logs (or follow them)."""
    settings = get_settings()
    _require_server_enabled(settings)

    if job is not None and follow:
        raise click.UsageError("--follow cannot be combined with --job")

    if job is not None:
        _print_job_logs(settings, job)
        return

    log_path = _log_file(settings)
    if not log_path.exists():
        click.echo(f"no server log at {log_path}", err=True)
        raise typer.Exit(code=1)

    if follow:
        try:
            subprocess.run(["tail", "-f", str(log_path)])  # noqa: S603,S607
        except KeyboardInterrupt:
            pass
        return

    subprocess.run(["tail", "-n", "50", str(log_path)])  # noqa: S603,S607


def _print_job_logs(settings: Settings, job_id: str) -> None:
    from doc3gpp.storage.repositories.jobs_sql import SQLAlchemyJobRepository

    repo = SQLAlchemyJobRepository()
    job = repo.get(job_id)
    if job is None:
        click.echo(f"no job with id {job_id!r}", err=True)
        raise typer.Exit(code=1)
    for line in job.log_lines[-50:]:
        click.echo(line)


@server_app.command("install")
def server_install(
    target: str = typer.Argument(..., help="systemd | launchd"),
    scope: bool = typer.Option(True, "--user/--system", help="Install scope: --user (default) or --system."),
    no_start: bool = typer.Option(False, "--no-start", help="Install only — do not start the service."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the unit file instead of installing."),
) -> None:
    """Install a service unit (systemd | launchd) for the HTTP server."""
    settings = get_settings()
    _require_server_enabled(settings)
    target_name = target.lower()

    from doc3gpp.web.install import install_launchd, install_systemd

    if target_name == "systemd":
        install_systemd(
            scope="system" if not scope else "user",
            no_start=no_start,
            dry_run=dry_run,
        )
    elif target_name == "launchd":
        install_launchd(no_start=no_start, dry_run=dry_run)
    else:
        raise click.UsageError(f"unknown target {target!r}; expected systemd or launchd")


@server_app.command("uninstall")
def server_uninstall(
    target: str = typer.Argument(..., help="systemd | launchd"),
    scope: bool = typer.Option(True, "--user/--system", help="Uninstall scope: --user (default) or --system."),
) -> None:
    """Uninstall the service unit (systemd | launchd) for the HTTP server."""
    settings = get_settings()
    _require_server_enabled(settings)
    target_name = target.lower()

    from doc3gpp.web.install import InstallNotManagedError, uninstall_launchd, uninstall_systemd

    try:
        if target_name == "systemd":
            uninstall_systemd(scope="system" if not scope else "user")
        elif target_name == "launchd":
            uninstall_launchd()
        else:
            raise click.UsageError(f"unknown target {target!r}; expected systemd or launchd")
    except InstallNotManagedError as exc:
        raise click.ClickException(str(exc)) from exc
