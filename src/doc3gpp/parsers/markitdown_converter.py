"""markitdown wrapper for TDoc .docx/.doc → markdown conversion.

The reference implementation in ``docs/ttcn_cr_cli_example.py`` silently
returns ``None`` on missing markitdown (and silently returns an empty
string when markitdown returns an empty result), which masks
missing-dep bugs and degrades the CR parser silently. This wrapper
instead fails loud:

  * A non-``.docx``/``.doc`` filename raises :class:`ValueError` — the
    caller is the only one who could feed us a different format, and
    silently doing nothing is worse than refusing.
  * A missing :mod:`markitdown` raises
    :class:`MarkitdownNotInstalledError` (a :class:`ImportError`
    subclass) with an actionable install hint.
  * A markitdown result with no extractable text raises
    :class:`RuntimeError` rather than returning ``""``.

Install via :code:`pip install doc3gpp[extract]` (which pulls in
``markitdown[all]``).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ``markitdown`` is an optional dependency; tests import it lazily so the
# unit test suite runs without the binary wheels (the install-required
# tests use ``@pytest.mark.skipif`` to auto-skip in that environment).


class MarkitdownNotInstalledError(ImportError):
    """Raised when markitdown is required but not installed.

    Surfaces an actionable install hint instead of silently returning empty
    markdown (which would mask missing-dep bugs downstream).
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or (
                "markitdown is required for TDoc extraction. "
                "Install with `pip install doc3gpp[extract]`."
            )
        )


def is_docx_or_doc(filename: str) -> bool:
    """Return True if filename ends in .docx or .doc (case-insensitive)."""
    suffix = Path(filename).suffix.lower()
    return suffix in {".docx", ".doc"}


def convert_document_to_markdown(doc_bytes: bytes, filename: str) -> str:
    """Convert .docx/.doc bytes to markdown using markitdown.

    Args:
        doc_bytes: Raw bytes of the Word document.
        filename: Source filename; only the extension is consulted.

    Returns:
        The extracted markdown text.

    Raises:
        ValueError: if ``filename``'s extension is not ``.docx`` or ``.doc``.
        MarkitdownNotInstalledError: if markitdown cannot be imported.
        RuntimeError: if markitdown returns no extractable text.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in {".docx", ".doc"}:
        raise ValueError(
            f"Unsupported extension {suffix!r}; expected .docx or .doc"
        )

    try:
        import markitdown
    except ImportError:
        logger.error(
            "markitdown is not installed; it is required for document conversion. "
            "Install with `pip install doc3gpp[extract]`."
        )
        raise MarkitdownNotInstalledError() from None

    # Persist to a temp file because markitdown's MarkItDown converter
    # takes a path (not bytes). ``delete=False`` so we control the
    # unlink in the finally block (best-effort cleanup).
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(doc_bytes)
        tmp_path = tmp.name

    try:
        result = markitdown.MarkItDown().convert(tmp_path)
        text = _extract_text(result, filename)
        if not text:
            raise RuntimeError(
                f"markitdown returned empty result for {filename}"
            )
        return text
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            # Best-effort cleanup — a leaked temp file is not worth
            # masking the real result/exception from the caller.
            pass


def _extract_text(result: object, filename: str) -> str:
    """Pull text out of a markitdown ``DocumentConverterResult``.

    Tolerates markitdown's version drift: try ``text_content`` first,
    then ``markdown``, then fall back to ``str(result)``. Returns the
    first non-empty stripped value, or an empty string if every
    candidate is missing/blank.
    """
    for attr in ("text_content", "markdown"):
        candidate = getattr(result, attr, None)
        if isinstance(candidate, str) and candidate.strip():
            logger.debug(
                "markitdown %s extracted for %s", attr, filename
            )
            return candidate

    if isinstance(result, str) and result.strip():
        logger.debug("markitdown returned raw string for %s", filename)
        return result

    fallback = str(result)
    return fallback.strip()