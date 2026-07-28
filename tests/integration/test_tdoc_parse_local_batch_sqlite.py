"""Integration: ``tdoc parse --from-path DIR`` honours max_tdoc_size_kb."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app


def _docx_available() -> bool:
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture
def sized_dir(tmp_path: Path) -> Path:
    """Create a directory with a small file and a large file (relative to the cap)."""
    small = tmp_path / "R5-260100.docx"
    small.write_bytes(b"x" * 100)
    big = tmp_path / "R5-260200.docx"
    big.write_bytes(b"y" * (3 * 1024 * 1024))
    return tmp_path


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_local_batch_skips_files_over_size_limit(
    sized_dir, tmp_path, monkeypatch,
) -> None:
    """With ``--max-tdoc-size-kb=1`` (1024 B cap), the 3 MiB file is skipped.

    The CLI uses the pre-read ``Path.stat()`` guard, so the oversized
    file is detected BEFORE ``read_bytes()`` is called and the operator
    sees a ``Skipped (exceeds max_tdoc_size_kb): 1`` summary line.
    """
    output_dir = tmp_path / "out"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--from-path", str(sized_dir),
            "--output", str(output_dir),
            "--format", "raw",
            "--max-tdoc-size-kb", "1",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output

    outputs = sorted(p.name for p in output_dir.iterdir()) if output_dir.exists() else []
    assert not any(name.startswith("R5-260200") for name in outputs), (
        "3 MiB file must be skipped under 1 KB cap"
    )

    assert "exceeds max_tdoc_size_kb" in result.output


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
def test_local_batch_zero_kb_is_unlimited(sized_dir, tmp_path) -> None:
    """``--max-tdoc-size-kb=0`` disables the cap; both files are attempted.

    The 3 MiB file still fails to parse (it's not a real .docx), but
    the pre-read stat guard does NOT skip it. The failure is counted
    under ``Failures``, not under the size-skip bucket.
    """
    output_dir = tmp_path / "out"

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--from-path", str(sized_dir),
            "--output", str(output_dir),
            "--format", "raw",
            "--max-tdoc-size-kb", "0",
        ],
        catch_exceptions=False,
    )

    assert "Skipped (exceeds max_tdoc_size_kb): 0" in result.output
