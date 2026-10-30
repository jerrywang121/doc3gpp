"""Normalize legacy Word bytes to OOXML before structured document parsing.

LibreOffice is optional and is invoked only for .doc sources. Input and output
files and the user profile are private to each conversion, including pool workers.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from doc3gpp.settings.loader import get_settings

_CLEANUP_TIMEOUT_SECONDS = 5


def _stop_conversion(process: subprocess.Popen) -> None:
    """Stop the launcher and its conversion children, then reap the launcher."""
    try:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:  # Windows has no killpg; taskkill /T includes descendant processes.
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CLEANUP_TIMEOUT_SECONDS,
                check=False,
            )
    finally:
        process.kill()
        try:
            process.communicate(timeout=_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # A descendant that escaped the group may still hold the pipe handles.
            # Do not let draining those handles defeat the conversion deadline.
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            process.wait(timeout=_CLEANUP_TIMEOUT_SECONDS)


def as_docx_bytes(payload: bytes, filename: str) -> bytes:
    """Return DOCX bytes, converting .doc via an optional headless LibreOffice.

    The original filename is diagnostic only: never write an untrusted source
    path to disk. A successful process exit alone does not guarantee conversion;
    LibreOffice sometimes exits zero without producing a file.
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".docx":
        return payload
    if suffix != ".doc":
        raise ValueError(f"Unsupported extension {suffix!r}; expected .docx or .doc")

    executable = shutil.which("libreoffice") or shutil.which("soffice")
    if executable is None:
        raise RuntimeError(
            f"LibreOffice is required to parse legacy .doc source {filename!r}. "
            "Install LibreOffice Writer and put 'libreoffice' or 'soffice' on PATH. "
            ".docx parsing does not require LibreOffice."
        )

    timeout_seconds = get_settings().word_conversion.timeout_seconds
    with tempfile.TemporaryDirectory(prefix="doc3gpp-doc-") as temporary:
        root = Path(temporary)
        source = root / "input.doc"
        source.write_bytes(payload)
        output = root / "input.docx"
        command = [
            executable,
            f"-env:UserInstallation={(root / 'profile').as_uri()}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            "docx:Office Open XML Text",
            "--outdir",
            str(root),
            str(source),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name == "posix",
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except BaseException:
                _stop_conversion(process)
                raise
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"LibreOffice conversion timed out after {timeout_seconds}s for "
                f"{filename!r}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot run LibreOffice for {filename!r}: {exc}") from exc

        if process.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = (stderr or stdout or "no DOCX output").strip()[:500]
            raise RuntimeError(
                f"LibreOffice failed to convert {filename!r} "
                f"(exit {process.returncode}): {detail}"
            )
        return output.read_bytes()
