"""An executable test double for the external LibreOffice conversion boundary."""

from __future__ import annotations

import base64
import os
import sys


def install_fake_libreoffice(monkeypatch, tmp_path, conversions: dict[bytes, bytes]) -> None:
    """Let real subprocesses/pool workers obtain controlled OOXML conversion output."""
    executable_dir = tmp_path / "office-bin"
    executable_dir.mkdir()
    outputs = {source: base64.b64encode(output).decode("ascii") for source, output in conversions.items()}
    executable = executable_dir / ("libreoffice.py" if os.name == "nt" else "libreoffice")
    executable.write_text(
        f"#!{sys.executable}\n"
        "import base64, sys\n"
        "from pathlib import Path\n"
        f"outputs = {outputs!r}\n"
        "source = Path(sys.argv[-1])\n"
        "output_dir = Path(sys.argv[sys.argv.index('--outdir') + 1])\n"
        "encoded = outputs.get(source.read_bytes())\n"
        "if encoded is None:\n"
        "    print('Invalid legacy Word document', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "(output_dir / (source.stem + '.docx')).write_bytes(base64.b64decode(encoded))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    if os.name == "nt":
        # Windows locates batch files via PATHEXT rather than Unix shebangs.
        (executable_dir / "libreoffice.cmd").write_text(
            f'@"{sys.executable}" "{executable}" %*\r\n', encoding="utf-8", newline=""
        )
    monkeypatch.setenv("PATH", str(executable_dir), prepend=os.pathsep)
