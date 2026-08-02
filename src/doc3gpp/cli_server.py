from __future__ import annotations

import click
import typer

from doc3gpp.config import get_settings


server_app = typer.Typer(
    help="manage the doc3gpp HTTP server (FastAPI + uvicorn) and the MCP sub-app",
    no_args_is_help=True,
)


def _require_server_enabled(settings) -> None:
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


@server_app.command("start")
def server_start(
    host: str | None = typer.Option(None, "--host", help="Bind host (overrides the server.host setting)."),
    port: int | None = typer.Option(None, "--port", help="Bind port (overrides the server.port setting)."),
    open: bool = typer.Option(False, "--open", help="Open the bound URL in a browser once the server is up."),
    reload: bool = typer.Option(False, "--reload", help="Run uvicorn in auto-reload mode (development only)."),
    no_open: bool = typer.Option(False, "--no-open", help="Suppress the auto-open behaviour even when configured."),
) -> None:
    """Start the doc3gpp HTTP server in the foreground."""
    _require_server_enabled(get_settings())
    raise NotImplementedError("task 11")


@server_app.command("stop")
def server_stop() -> None:
    """Stop a running background HTTP server (best-effort)."""
    _require_server_enabled(get_settings())
    raise NotImplementedError("task 11")


@server_app.command("status")
def server_status() -> None:
    """Report whether the HTTP server is running."""
    _require_server_enabled(get_settings())
    raise NotImplementedError("task 11")


@server_app.command("logs")
def server_logs(
    job: str | None = typer.Option(None, "--job", help="Limit logs to a specific job id."),
    follow: bool = typer.Option(False, "-f", help="Follow the log file (like `tail -f`)."),
) -> None:
    """Print recent server logs (or follow them)."""
    _require_server_enabled(get_settings())
    raise NotImplementedError("task 11")


@server_app.command("install")
def server_install(
    target: str = typer.Argument(..., help="systemd | launchd"),
    scope: bool = typer.Option(True, "--user/--system", help="Install scope: --user (default) or --system."),
    no_start: bool = typer.Option(False, "--no-start", help="Install only — do not start the service."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the unit file instead of installing."),
) -> None:
    """Install a service unit (systemd | launchd) for the HTTP server."""
    _require_server_enabled(get_settings())
    raise NotImplementedError("task 10")


@server_app.command("uninstall")
def server_uninstall(
    target: str = typer.Argument(..., help="systemd | launchd"),
    scope: bool = typer.Option(True, "--user/--system", help="Uninstall scope: --user (default) or --system."),
) -> None:
    """Uninstall the service unit (systemd | launchd) for the HTTP server."""
    _require_server_enabled(get_settings())
    raise NotImplementedError("task 10")
