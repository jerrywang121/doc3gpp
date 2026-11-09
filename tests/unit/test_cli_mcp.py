from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from doc3gpp import cli
from doc3gpp.settings.schema import MCPSettings, ServerSettings, Settings


def _run_fresh_python(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("DOC3GPP_")}
    src = str(Path(__file__).resolve().parents[2] / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (src, env.get("PYTHONPATH"))))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_server_and_mcp_settings_default_to_enabled(monkeypatch, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("DOC3GPP_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)

    settings = Settings(_env_file=None)

    assert ServerSettings().enabled is True
    assert MCPSettings().enabled is True
    assert settings.server.enabled is True
    assert settings.mcp.enabled is True


def test_importing_cli_does_not_load_web_or_mcp(tmp_path) -> None:
    result = _run_fresh_python(
        """
import sys
import doc3gpp.cli

for prefix in ("doc3gpp.web", "fastapi", "mcp"):
    loaded = [name for name in sys.modules if name == prefix or name.startswith(prefix + ".")]
    assert not loaded, loaded
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_mcp_is_a_top_level_command() -> None:
    result = CliRunner().invoke(cli.app, ["--help"])

    assert result.exit_code == 0, result.output
    assert re.search(r"(?m)^\s*[│|]?\s*mcp(?:\s{2,}|\s*[│|])", result.output), result.output


def test_disabled_mcp_rejects_before_importing_runtime(tmp_path) -> None:
    result = _run_fresh_python(
        """
import sys
from typer.testing import CliRunner
import doc3gpp.cli as cli
from doc3gpp.settings.schema import MCPSettings, Settings

assert not any(name == "doc3gpp.web" or name.startswith("doc3gpp.web.")
               for name in sys.modules)
attempts = []
class BlockWebImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "doc3gpp.web" or fullname.startswith("doc3gpp.web."):
            attempts.append(fullname)
            raise ImportError("disabled MCP must not import its runtime")
        return None

sys.meta_path.insert(0, BlockWebImports())
settings = Settings(_env_file=None, mcp=MCPSettings(enabled=False))
cli.get_settings = lambda: settings
result = CliRunner().invoke(cli.app, ["mcp"])
assert not attempts, attempts
assert result.exit_code != 0, result.output
assert result.stdout == "", result.stdout
assert "[mcp]" in result.stderr and "enabled = true" in result.stderr, result.stderr
""",
        tmp_path,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_enabled_mcp_runs_when_http_server_is_disabled(monkeypatch) -> None:
    from doc3gpp.web import mcp_stdio

    settings = Settings(
        _env_file=None,
        server=ServerSettings(enabled=False),
        mcp=MCPSettings(enabled=True),
    )
    calls: list[Settings] = []

    async def fake_run_stdio(received: Settings) -> None:
        calls.append(received)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_stdio, "run_stdio", fake_run_stdio)

    result = CliRunner().invoke(cli.app, ["mcp"])

    assert result.exit_code == 0, result.output
    assert calls == [settings]
    assert result.output == ""


def test_missing_web_dependency_reports_install_command(monkeypatch) -> None:
    from doc3gpp.web import mcp_stdio

    settings = Settings(_env_file=None, mcp=MCPSettings(enabled=True))
    original_import = mcp_stdio.importlib.import_module

    def import_without_mcp(name: str, package: str | None = None):
        if name == "mcp.server.mcpserver":
            raise ModuleNotFoundError("No module named 'mcp'")
        return original_import(name, package)

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_stdio.importlib, "import_module", import_without_mcp)

    result = CliRunner().invoke(cli.app, ["mcp"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert 'pip install "doc3gpp[web]"' in result.stderr, result.stderr
    assert "Traceback" not in result.stderr


def test_incompatible_mcp_sdk_reports_upgrade_on_stderr(monkeypatch) -> None:
    from doc3gpp.web import mcp_stdio

    class Handle:
        def __init__(self, *, max_concurrent_jobs: int) -> None:
            self.task = None

        async def shutdown(self) -> None:
            if self.task is not None:
                await self.task

    class Worker:
        def __init__(self, state: object) -> None:
            pass

        async def run(self) -> None:
            pass

    engine = SimpleNamespace(dispose=lambda: None)
    state = SimpleNamespace(
        jobs=None,
        close_docx_pool=None,
        engine=engine,
        testcase_engine=engine,
        specdata_engine=None,
    )
    deps = mcp_stdio._StdioDependencies(
        build_state=lambda _settings: state,
        build_mcp_server=lambda _state: object(),
        worker_type=Worker,
        handle_type=Handle,
    )
    settings = Settings(_env_file=None, mcp=MCPSettings(enabled=True))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(mcp_stdio, "_load_dependencies", lambda: deps)

    result = CliRunner().invoke(cli.app, ["mcp"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "pip install --upgrade mcp" in result.stderr, result.stderr
    assert "Traceback" not in result.stderr
