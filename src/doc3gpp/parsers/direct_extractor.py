"""Helpers for the ``tdoc parse --from-path/--from-url`` direct path.

Pure parsers + tiny I/O shims — no network fetch, no database access.
The CLI dispatcher composes on these primitives together with the
existing ``TDocCrService`` to deliver the behaviour matrix described in
:file:`.omo/plans/tdoc-parse-direct-file.md`.

Boundary discipline:

- ``read_source_bytes`` distinguishes **local** from **URL** sources
  but does no parsing; downstream code paths branch on the returned
  ``source_kind``.
- ``is_3gpp_ftp_url`` is the single point that decides whether a URL
  hits the cache + DB write paths (3GPP FTP) or the in-memory parse-
  only path (everything else). Case-insensitive scheme + path check
  against :data:`doc3gpp.parsers.normalizers.FTP_BASE_URL`.
- ``extract_tdoc_id_from_filename`` reuses the existing
  :data:`doc3gpp.parsers.cr_parser._TDOC_HEADER_PATTERN` so direct-mode
  extraction agrees with the regular filter path on which id the
  parser sees.
- ``derive_zip_cache_key`` returns the **original** (sanitised)
  filename so multiple revisions of the same tdoc_id (e.g.
  ``R5s260008_MCC160Comments_r1.zip`` vs ``..._r2.zip``) get distinct
  cache slots. The fix is plumbed through
  :func:`doc3gpp.scraping.tdoc_zip_source.download_tdoc_zip` via the
  ``ftp_url`` keyword (which forwards into
  :func:`doc3gpp.scraping.cache_keys.derive_cache_file`).

Helpers in this module never raise for *user* errors (missing files,
malformed sources) — those surface as plain ``ValueError`` /
``FileNotFoundError`` so the CLI can map them to the documented exit
codes without inspecting exception hierarchies.
"""

from __future__ import annotations

import re
import typing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from doc3gpp.models.tdoc_cr import TDocCRParseResult
from doc3gpp.parsers.cr_parser import (
    _TDOC_HEADER_PATTERN,
    extract_docx_from_zip,
)
from doc3gpp.parsers.ls.header import is_ls_header_present
from doc3gpp.parsers.ls.ls_parsers import LSParserBase
from doc3gpp.parsers.normalizers import FTP_BASE_URL
from doc3gpp.parsers.tdoc_parsers import build_default_registry

if typing.TYPE_CHECKING:
    from doc3gpp.scraping.client import ScraperClient


#: Maximum filename length accepted for the zip-cache key. Matches
#: ``scraping.cache._KEY_PATTERN`` (128 chars) so the sanitised
#: basename never overflows the cache key validator. Long filenames
#: are preserved verbatim (the cache layer's own regex rejects the
#: oversized value, surfacing a clear ``ValueError`` to the operator).
_CACHE_KEY_MAX_LEN = 128


def is_3gpp_ftp_url(url: str) -> bool:
    """Return ``True`` iff ``url`` is on the canonical 3GPP FTP root.

    The check is a scheme-validated prefix match: the URL must parse
    with an ``http`` / ``https`` scheme (case-insensitive — ``HTTP``
    is accepted) and its ``netloc`` + ``path`` must begin with
    :data:`doc3gpp.parsers.normalizers.FTP_BASE_URL`. The comparison
    is host-agnostic: ``HTTP://www.3gpp.org/ftp/...`` and
    ``https://www.3gpp.org/ftp/...`` both pass, while
    ``https://example.com/...`` is rejected regardless of scheme. Other
    schemes — ``ftp``, ``sftp``, ``file`` — are rejected; operators
    should download those out-of-band and use ``--from-path``.

    Args:
        url: The URL to test. Strings that don't parse return ``False``
            rather than raising so the CLI can branch on a single bool
            without a try/except.

    Returns:
        ``True`` only when the scheme is ``http(s)`` and the host +
        path begin with the FTP root (case-insensitive).
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False
    ftp_root = FTP_BASE_URL.lower()
    host_path = (parsed.netloc + parsed.path).lower()
    ftp_host_path = ftp_root[len("https://"):]
    return host_path.startswith(ftp_host_path)


@dataclass(slots=True, frozen=True)
class FtpListing:
    """Result of probing a 3GPP FTP directory listing."""

    folder_url: str
    file_urls: tuple[str, ...]
    subfolder_urls: tuple[str, ...]


class NotAFolderError(ValueError):
    """Raised when a URL expected to be a 3GPP FTP folder is not a folder."""


def list_3gpp_directory(url: str, *, client: "ScraperClient") -> FtpListing:
    """Probe ``url`` and return a structured 3GPP FTP folder listing.

    The function fetches ``url`` as text via the injected ``client``. If
    the response is HTML and contains anchor tags, it classifies each
    anchor:

    - ``<a class="file" href="...">`` pointing to a ``.docx`` or ``.zip``
      whose basename contains a 3GPP TDoc id pattern → ``file_urls``.
    - Other anchors that resolve to folders (trailing slash, no file
      extension, or a folder icon) → ``subfolder_urls``.

    Parent-directory links, breadcrumb ancestors, sort links, and
    duplicate URLs are skipped. Non-3GPP URLs are rejected immediately.
    If the response is not parseable HTML, :class:`NotAFolderError` is
    raised so callers can fall back to treating the URL as a single file.

    Args:
        url: Absolute HTTP(S) URL under ``https://www.3gpp.org/ftp/``.
        client: Injected scraper client; ``get_text`` is used for the
            probe and for resolving relative hrefs.

    Returns:
        An :class:`FtpListing` with absolute file and subfolder URLs.

    Raises:
        ValueError: ``url`` is not a 3GPP FTP URL.
        NotAFolderError: the response is not an HTML directory listing.
    """
    if not is_3gpp_ftp_url(url):
        raise ValueError(f"URL is not a 3GPP FTP URL: {url}")

    try:
        html = client.get_text(url)
    except Exception as exc:
        raise NotAFolderError(
            f"URL does not appear to be a folder: {url}"
        ) from exc

    stripped = html.lstrip()
    if not stripped.startswith(("<", "<!")):
        raise NotAFolderError(f"URL does not appear to be a folder: {url}")

    soup = BeautifulSoup(html, "lxml")
    anchors = soup.find_all("a", href=True)
    if not anchors:
        raise NotAFolderError(f"URL does not appear to be a folder: {url}")

    file_urls: list[str] = []
    subfolder_urls: list[str] = []
    seen: set[str] = set()

    for anchor in anchors:
        href = str(anchor["href"])
        abs_url = urljoin(url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)

        if href.startswith("?"):
            continue
        if href in ("..", "../") or href.rstrip("/").endswith(".."):
            continue
        if _is_ancestor_or_self(abs_url, url):
            continue

        anchor_classes = set(anchor.get("class") or [])
        if "file" in anchor_classes:
            basename = abs_url.rsplit("/", 1)[-1]
            lower_name = basename.lower()
            if lower_name.endswith((".docx", ".zip")):
                if extract_tdoc_id_from_filename(basename) is not None:
                    file_urls.append(abs_url)
        elif _looks_like_folder(anchor, href):
            subfolder_urls.append(abs_url)

    return FtpListing(
        folder_url=url,
        file_urls=tuple(file_urls),
        subfolder_urls=tuple(subfolder_urls),
    )


def _is_ancestor_or_self(candidate: str, current: str) -> bool:
    """Return True when ``candidate`` points to ``current`` or an ancestor folder."""
    c = candidate.rstrip("/") + "/"
    cur = current.rstrip("/") + "/"
    return c == cur or cur.startswith(c)


def _looks_like_folder(anchor, href: str) -> bool:
    """Classify a directory-listing anchor as a folder.

    3GPP FTP folder links may omit the trailing slash (e.g. ``Docs``),
    so the trailing-slash test alone misses content subfolders. The
    fallback chain is: trailing slash → folder icon → no file extension.
    """
    if href.endswith("/"):
        return True

    tr = anchor.find_parent("tr")
    if tr is not None:
        img = tr.find("img", class_="icon")
        if img is not None:
            src = str(img.get("src", ""))
            if src.endswith("?file="):
                return True

    basename = href.rstrip("/").rsplit("/", 1)[-1]
    return basename != "" and "." not in basename


def read_source_bytes(source: Path | str) -> tuple[bytes, str]:
    """Return ``(bytes, source_kind)`` for a local path or remote URL.

    The function is intentionally thin: it just decides *which* byte
    source to read. URL handling is left to the caller (the service
    layer injects a :class:`ScraperClient`); this helper rejects URL
    inputs with a clear ``ValueError`` so a misrouted CLI flag fails
    fast at the boundary rather than in the middle of a parse.

    Args:
        source: A :class:`pathlib.Path` or a string. Strings are treated
            as local paths — callers that need a URL download must use
            the service-layer entry point instead.

    Returns:
        ``(payload_bytes, source_kind)`` where ``source_kind`` is the
            literal ``"local"``.

    Raises:
        ValueError: ``source`` is a string that looks like a URL (scheme
            followed by ``://``). The CLI must dispatch URL sources to
            the service layer rather than this helper.
        FileNotFoundError: the resolved path does not exist on disk.
        OSError: the read itself fails (permission, etc.).
    """
    if isinstance(source, str):
        if "://" in source:
            raise ValueError(
                "URL sources must be downloaded by the service layer; "
                "use --from-url for the CLI dispatcher"
            )
        path = Path(source)
    else:
        path = source
    return path.read_bytes(), "local"


def extract_tdoc_id_from_filename(filename: str) -> str | None:
    """Return the first 3GPP TDoc id found in ``filename`` or ``None``.

    Reuses :data:`doc3gpp.parsers.cr_parser._TDOC_HEADER_PATTERN`
    directly so the pattern is defined in exactly one place. The
    pattern is anchored on the literal character class (not greedy),
    so ``R5s260043_MCC160Comments_r1.zip`` returns ``"R5s260043"`` and
    not the trailing ``_r1`` — the regex consumes exactly 9 characters
    per match.

    Args:
        filename: Source filename or path; only the basename is
            considered by the regex, but passing the full path is
            safe (``findall`` works on the full string).

    Returns:
        The first matching id (case-preserved from the input) or
        ``None`` when the filename has no 3GPP TDoc id shape.
    """
    if not filename:
        return None
    match = _TDOC_HEADER_PATTERN.search(filename)
    if match is None:
        return None
    return match.group(1)


def derive_zip_cache_key(source: str | Path) -> str:
    """Return the sanitised basename of ``source`` for cache keying.

    The legacy cache key was ``tdoc.lower()``; that choice silently
    collided when multiple revisions of the same tdoc_id shared a
    cache slot (e.g. ``R5s260008_MCC160Comments_r1.zip`` and
    ``R5s260008_MCC160Comments_r2.zip``). The direct-parse path keys
    the zip cache on the **original filename** so revisions land in
    distinct slots. After the ``feat(tdoc): unify zip + markdown
    cache`` change, the production cache key is derived from the
    upstream URL via
    :func:`doc3gpp.scraping.cache_keys.derive_cache_file` — this
    helper exists for ad-hoc cache lookups on a filename / URL that
    do not have the full URL in hand.

    For a URL, only the URL path's basename is considered (3GPP serves
    assets like ``R5s260008.zip`` — the URL path matches the file
    name). For a :class:`pathlib.Path`, only ``Path.name`` is used.

    Sanitisation mirrors :data:`scraping.cache._KEY_PATTERN` —
    ``[A-Za-z0-9._-]{1,128}``. Filenames longer than the cap are
    truncated to the last 128 valid characters (so the extension is
    preserved when possible) and a trailing run of invalid characters
    is stripped. Hostile filenames cannot escape the cache root; the
    cache layer's own validator will reject any residual invalid value
    with a clear ``ValueError``.

    Args:
        source: URL string or :class:`pathlib.Path`.

    Returns:
        A cache-safe string of at most 128 characters drawn from
        ``[A-Za-z0-9._-]``.

    Raises:
        ValueError: the resolved basename is empty (e.g. the URL ended
            in a slash and the path component has no name).
    """
    if isinstance(source, str):
        if "://" in source:
            parsed = urlparse(source)
            basename = Path(parsed.path).name
        else:
            basename = Path(source).name
    else:
        basename = source.name

    if not basename:
        raise ValueError(f"Cannot derive a cache key from source {source!r}")

    sanitised = re.sub(r"[^A-Za-z0-9._-]", "_", basename)
    sanitised = sanitised[:_CACHE_KEY_MAX_LEN]
    if not sanitised or sanitised in {".", ".."}:
        raise ValueError(
            f"Cannot derive a cache key from source {source!r}: "
            f"sanitised basename {sanitised!r} is unusable"
        )
    return sanitised


def direct_parse_bytes(
    payload: bytes,
    *,
    filename: str,
    full: bool = False,
    max_bytes: int = 0,
) -> tuple[str, str, "TDocCRParseResult"]:
    """Parse ``payload`` (a ``.docx`` or a zip-wrapped ``.docx``) into markdown + parse result.

    Dispatch is by ``filename`` extension (not magic bytes — a docx
    is itself a zip, so the ``b"PK"`` prefix is not a reliable
    differentiator):

    - ``.zip`` (case-insensitive) → the payload is treated as a
      3GPP-style zip; the inner ``.docx`` is extracted via
      :func:`doc3gpp.parsers.cr_parser.extract_docx_from_zip`.
    - ``.docx`` (or anything else) → the payload is treated as a
      bare ``.docx``; the supplied ``filename`` is used to populate
      the docx extension guard.

    The conversion + parse steps reuse the existing
    :func:`convert_document_to_markdown` and :func:`parse_cr_details`
    helpers; this function only glues them together for the direct
    path so a single source of truth stays in the parser module.

    Args:
        payload: Raw bytes of the document (``.docx`` or ``.zip``).
        filename: The source filename — used to drive the docx/zip
            dispatch, the docx extension guard, and the auto-extracted
            ``tdoc_id`` (a ``LOCAL-<stem>`` synthetic id is used when
            no 3GPP pattern matches).
        full: Forwarded to :func:`parse_cr_details` as ``full=True``
            for the TTCN corrections sub-parser.
        max_bytes: When ``> 0``, the function raises
            :class:`TDocTooLargeError` before any unzip /
            ``python-docx`` work when ``len(payload) > max_bytes``.
            ``0`` (default) disables the guard — callers that do not
            want the size check simply omit the kwarg.

    Returns:
        ``(markdown, docx_filename, parsed)`` — the converted
        markdown, the docx filename actually parsed (after the zip
        unwrap, when applicable), and the
        :class:`TDocCRParseResult` from the parser. Callers that need
        the slim cover-page dataclass read ``parsed.cover``; TTCN-CR
        callers read ``parsed.ttcn``.

    Raises:
        ValueError: the payload is a zip with no ``.docx`` entry
            (``extract_docx_from_zip`` contract), or the filename has
            a non-``.docx`` / non-``.zip`` extension.
        zipfile.BadZipFile: the payload looks like a zip but is
            malformed.
        CRHeaderMissingError: the parsed markdown lacks a
            ``| CHANGE REQUEST |`` line and the structural CR
            cover-page row (forwarded from ``parse_cr_details``).
        PythonDocxNotInstalledError: python-docx is not installed
            (forwarded from :func:`convert_document_to_markdown`).
        TDocTooLargeError: ``max_bytes > 0`` and ``len(payload)`` exceeds
            the cap.
    """
    if max_bytes > 0 and len(payload) > max_bytes:
        # Deferred import — ``direct_extractor`` sits below the
        # scraping layer where ``TDocTooLargeError`` is canonically
        # defined, but the class is re-exported from
        # ``doc3gpp.services.tdoc_cr_service`` for callers that don't
        # want to depend on the scraping module directly.
        from doc3gpp.services.tdoc_cr_service import TDocTooLargeError
        raise TDocTooLargeError(
            source=filename,
            size=len(payload),
            limit=max_bytes,
        )

    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        docx_filename, docx_bytes = extract_docx_from_zip(payload)
    elif suffix == ".docx":
        docx_filename = filename
        docx_bytes = payload
    else:
        raise ValueError(
            f"Unsupported extension {suffix!r} for direct-parse; "
            "expected .docx or .zip"
        )

    # Deferred so importing this module (for URL classification, ZIP-cache
    # helpers, etc.) does not require python-docx to be installed.
    from doc3gpp.parsers.docx_converter import convert_document_to_markdown

    markdown = convert_document_to_markdown(docx_bytes, docx_filename)

    tdoc_id = extract_tdoc_id_from_filename(filename) or _synthetic_tdoc_id(filename)
    ls_present, _ = is_ls_header_present(markdown)
    tdoc_type = "LS" if ls_present else "CR"
    parser = build_default_registry().resolve(tdoc_id, tdoc_type=tdoc_type)
    if isinstance(parser, LSParserBase):
        parsed = parser.parse_ls(markdown, tdoc_id=tdoc_id)
    else:
        parsed = parser.parse(markdown, tdoc_id=tdoc_id, full=full)
    return markdown, docx_filename, parsed


def _synthetic_tdoc_id(filename: str) -> str:
    """Return a ``LOCAL-<stem>`` identifier for files lacking a real TDoc id.

    Used by :func:`direct_parse_bytes` so the parser always has a
    non-empty ``tdoc_id`` to feed the cover-page header validation.
    The stem is lower-cased and stripped of every non-alphanumeric
    character (except ``-``) so the result survives
    :func:`doc3gpp.parsers.cr_parser.parse_cr_details`'s
    ``_year_from_tdoc_id`` derivation (positions 3–4 must be digits;
    a synthetic id has none, so ``year`` is left as ``None`` — that
    is the expected behaviour for ``LOCAL-`` ids).
    """
    stem = Path(filename).stem
    cleaned = re.sub(r"[^A-Za-z0-9-]", "-", stem).strip("-").lower()
    if not cleaned:
        cleaned = "unknown"
    return f"LOCAL-{cleaned}"[:64]


def build_missing_tdoc_id_warning_message(extracted_id: str, filename: str) -> str:
    """Return the multi-line warning shown when ``tdoc_id ∈ tdocs`` lookup misses.

    The message is intentionally two paragraphs: the first describes
    the failure (no FK target), the second offers the recipe to add
    the missing row. The TSG short name in the recipe is derived
    from the first two characters of the extracted id (mirrors
    :func:`doc3gpp.parsers.cr_parser.parse_cr_details`'s fallback).
    """
    tsg_short = extracted_id[:2].upper() if len(extracted_id) >= 2 else ""
    lines = [
        f"warning: extracted tdoc_id '{extracted_id}' from filename '{filename}'",
        "         is not present in the 'tdocs' table; skipping cache and DB writes.",
        "",
        "  To add this TDoc to the database so the result can be persisted, run:",
        "",
        f"      doc3gpp meeting sync --tsg {tsg_short}",
        f"      doc3gpp meeting list --tdoc {extracted_id}",
        "      doc3gpp tdoc sync --meeting-id <meeting_id_from_previous_step>",
    ]
    return "\n".join(lines)


def build_no_pattern_warning_message(filename: str) -> str:
    """Return the warning shown when no TDoc id pattern is found in ``filename``.

    The pattern documented in the message matches
    :data:`doc3gpp.parsers.cr_parser._TDOC_HEADER_PATTERN`
    (i.e. ``[RSC][1-6][-sw]\\d{6}`` case-insensitive).
    """
    return (
        f"warning: filename '{filename}' does not match the 3GPP TDoc id\n"
        f"         pattern ([RSC][1-6][-sw]\\d{{6}}); skipping cache and DB writes."
    )


__all__ = [
    "build_missing_tdoc_id_warning_message",
    "build_no_pattern_warning_message",
    "derive_zip_cache_key",
    "direct_parse_bytes",
    "extract_tdoc_id_from_filename",
    "is_3gpp_ftp_url",
    "read_source_bytes",
]
