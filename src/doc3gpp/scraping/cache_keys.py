"""Cache key derivation for 3GPP TDoc zip downloads.

The zip cache in :mod:`doc3gpp.scraping.cache` is keyed by an opaque
string drawn from the ``[A-Za-z0-9._-]`` alphabet so a malicious TDoc id
can never escape the cache root via path traversal. This module centralises
the rule for **deriving** that key from a TDoc's ``ftp_url`` so the service
layer and the cache layer never disagree on the shape of a key.

The function lives in ``scraping`` (not ``parsers``) because it is a
transport-adjacent concern: it converts a transport-layer URL into the
cache-layer key, and contains no HTML / Excel / docx parsing. The
``_KEY_PATTERN`` regex is re-exported from
:mod:`doc3gpp.scraping.cache` so downstream tests and re-exporters share
exactly one source of truth — bumping the cap is a single-file edit.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from doc3gpp.scraping.cache import _KEY_PATTERN  # noqa: F401  re-exported for tests

__all__ = ["derive_cache_file"]


def derive_cache_file(ftp_url: str) -> str:
    """Return ``<stem>-<md5(ftp_url).hexdigest()>.zip`` for a 3GPP ftp_url.

    The ``ftp_url`` is the **relative** URL stored on
    ``tdocs.ftp_url`` / ``tdoc_extracts.ftp_url`` (e.g.
    ``'tsg_ran/WG5_.../R5s260162.zip'``). The service layer is expected
    to normalise the URL via :func:`doc3gpp.parsers.normalizers.normalize_ftp_path`
    before this function sees it; we only handle the basename.

    Steps:

    1. Take the basename of ``ftp_url`` (via :class:`pathlib.Path`).
    2. Sanitise to ``[A-Za-z0-9._-]`` via ``re.sub`` (replace anything
       else with ``_``).
    3. Strip a **trailing** ``.zip`` suffix (case-insensitive, at most once
       — ``foo.zip.zip`` becomes ``foo.zip`` with a single ``.zip`` re-added
       in step 5, and ``foo.zip.docx`` is left untouched).
    4. Compute ``md5`` of the **full relative ftp_url** as UTF-8 bytes.
    5. Append ``-{digest}.zip``.

    The output always matches :data:`_KEY_PATTERN` and is at most 200
    characters. The same ``ftp_url`` always produces the same cache key,
    so cache hits work across runs.
    """
    stem = Path(ftp_url).name
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    # Cap the stem so the final "-{md5}.zip" result stays within
    # _KEY_PATTERN's 200-char budget and the md5 digest is never truncated.
    digest = hashlib.md5(ftp_url.encode("utf-8")).hexdigest()
    stem = stem[: 200 - (1 + len(digest) + 4)]
    return f"{stem}-{digest}.zip"
