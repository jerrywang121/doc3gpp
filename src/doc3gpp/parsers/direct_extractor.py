"""Helpers for the ``tdoc parse --from-path/--from-url`` direct path.

Pure parsers + tiny I/O shims — no network fetch, no database access.
The CLI dispatcher composes on these primitives together with the
existing ``TDocCrService`` to deliver the direct-mode behavior documented
in ``docs/cli.md``.

Boundary discipline:

- ``read_source_bytes`` distinguishes **local** from **URL** sources
  but does no parsing; downstream code paths branch on the returned
  ``source_kind``.
- ``normalize_3gpp_ftp_url`` and ``validate_tdoc_ftp_url`` are the shared
  boundary policy for canonical HTTPS 3GPP FTP paths, including bare
  host/path input and TDoc-shaped file basenames. ``is_3gpp_ftp_url``
  reports the normalized-site check used by lower-level branching.
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

import hashlib
import logging
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from doc3gpp.models.tdoc_cr import TDocCRParseResult
from doc3gpp.models.tdoc_document import (
    TDocDocument,
    TDocParseBundle,
    markdown_member_name,
    source_sort_key,
)
from doc3gpp.parsers.cr.header import CRHeaderMissingError, is_cr_header_present
from doc3gpp.parsers.cr_parser import _TDOC_HEADER_PATTERN
from doc3gpp.parsers.docx_converter import DocxConvertPoolError, DocxMemberPool
from doc3gpp.parsers.tdoc_family_main import is_tdoc_member
from doc3gpp.parsers.tdoc_parsers import build_default_registry
from doc3gpp.parsers.tdoc_zip import (
    TDocZipLimitError,
    enumerate_docx_members,
    ephemeral_document_id_for,
)
from doc3gpp.parsers.tdoc_zip_convert import is_empty_text_error, member_outcomes

if typing.TYPE_CHECKING:
    from doc3gpp.scraping.client import ScraperClient

logger = logging.getLogger(__name__)


#: Maximum filename length accepted for the zip-cache key. Matches
#: ``scraping.cache._KEY_PATTERN`` (128 chars) so the sanitised
#: basename never overflows the cache key validator. Long filenames
#: are preserved verbatim (the cache layer's own regex rejects the
#: oversized value, surfacing a clear ``ValueError`` to the operator).
_CACHE_KEY_MAX_LEN = 128


_TDOC_FILE_SUFFIXES = (".docx", ".zip")


def normalize_3gpp_ftp_url(url: str) -> str:
    """Return the canonical HTTPS form of a 3GPP FTP URL.

    Bare ``www.3gpp.org/ftp/...`` inputs are accepted and prefixed with
    ``https://``. Explicit schemes are never upgraded: only HTTPS is
    accepted, and the host/path must be exactly the canonical 3GPP FTP
    site.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError(
            "url must be a canonical HTTPS 3GPP FTP URL "
            "(https://www.3gpp.org/ftp/...);"
            f" got {url!r}"
        )

    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "url must be a canonical HTTPS 3GPP FTP URL "
            "(https://www.3gpp.org/ftp/...);"
            f" got {url!r}"
        ) from exc

    if parsed.scheme.lower() != "https":
        raise ValueError(
            "3GPP FTP URLs must use https://; explicit HTTP or other "
            f"schemes are not accepted: {url!r}"
        )
    if (
        hostname is None
        or hostname.lower() != "www.3gpp.org"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/ftp/")
    ):
        raise ValueError(
            "url must be a canonical HTTPS 3GPP FTP URL "
            "(https://www.3gpp.org/ftp/...);"
            f" got {url!r}"
        )

    return urlunparse(
        ("https", "www.3gpp.org", parsed.path, "", parsed.query, parsed.fragment)
    )


def is_3gpp_ftp_candidate_url(url: str) -> bool:
    """Return whether ``url`` targets the canonical FTP host/path.

    Unlike :func:`is_3gpp_ftp_url`, this deliberately includes explicit
    HTTP and other schemes so callers that also support unrelated URLs can
    reject an unsafe 3GPP URL instead of misclassifying it as an arbitrary
    in-memory source.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return False
    return (
        parsed.hostname is not None
        and parsed.hostname.lower() == "www.3gpp.org"
        and parsed.path.startswith("/ftp/")
    )


def _url_path_basename(url: str) -> str:
    """Return the URL path basename without query or fragment components."""
    path = urlparse(url).path.rstrip("/")
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def is_3gpp_file_url(url: str) -> bool:
    """Return whether a canonical 3GPP URL names a document file."""
    try:
        normalized = normalize_3gpp_ftp_url(url)
    except ValueError:
        return False
    return _url_path_basename(normalized).lower().endswith(_TDOC_FILE_SUFFIXES)


def validate_tdoc_ftp_url(url: str) -> str:
    """Normalize and validate a TDoc file-or-folder URL.

    A URL with a ``.docx`` or ``.zip`` basename is a file URL and its
    basename must contain the shared TDoc identifier pattern. Any other
    canonical FTP path is treated as a folder, whether or not it ends in
    ``/``.
    """
    normalized = normalize_3gpp_ftp_url(url)
    if is_3gpp_file_url(normalized):
        basename = _url_path_basename(normalized)
        if extract_tdoc_id_from_filename(basename) is None:
            raise ValueError(
                "url must point to a TDoc .docx or .zip file whose basename "
                f"contains a TDoc id; got {url!r}"
            )
    return normalized


def _normalize_3gpp_folder_url(url: str) -> str:
    """Normalize a canonical FTP folder URL and ensure a trailing slash."""
    normalized = normalize_3gpp_ftp_url(url)
    parsed = urlparse(normalized)
    path = parsed.path.rstrip("/") + "/"
    return urlunparse(parsed._replace(path=path))


def is_3gpp_ftp_url(url: str) -> bool:
    """Return ``True`` for a canonical HTTPS 3GPP FTP URL.

    Bare ``www.3gpp.org/ftp/...`` paths are recognized because callers
    may normalize them at their boundary. Explicit HTTP, other schemes,
    non-canonical hosts, and paths outside ``/ftp/`` return ``False``.
    """
    try:
        normalize_3gpp_ftp_url(url)
    except ValueError:
        return False
    return True


@dataclass(slots=True, frozen=True)
class FtpListing:
    """Result of probing a 3GPP FTP directory listing."""

    folder_url: str
    file_urls: tuple[str, ...]
    subfolder_urls: tuple[str, ...]


class NotAFolderError(ValueError):
    """Raised when a URL expected to be a 3GPP FTP folder is not a folder."""


def list_3gpp_directory(url: str, *, client: ScraperClient) -> FtpListing:
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
        url: A canonical HTTPS URL under ``https://www.3gpp.org/ftp/`` or
            its bare ``www.3gpp.org/ftp/`` host/path form.
        client: Injected scraper client; ``get_text`` is used for the
            probe and for resolving relative hrefs.

    Returns:
        An :class:`FtpListing` with absolute file and subfolder URLs.

    Raises:
        ValueError: ``url`` is not a 3GPP FTP URL.
        NotAFolderError: the response is not an HTML directory listing.
    """
    try:
        folder_url = _normalize_3gpp_folder_url(url)
    except ValueError as exc:
        raise ValueError(f"URL is not a 3GPP FTP URL: {url}") from exc

    try:
        html = client.get_text(folder_url)
    except Exception as exc:
        raise NotAFolderError(
            f"URL does not appear to be a folder: {folder_url}"
        ) from exc

    stripped = html.lstrip()
    if not stripped.startswith(("<", "<!")):
        raise NotAFolderError(
            f"URL does not appear to be a folder: {folder_url}"
        )

    soup = BeautifulSoup(html, "lxml")
    anchors = soup.find_all("a", href=True)
    if not anchors:
        raise NotAFolderError(
            f"URL does not appear to be a folder: {folder_url}"
        )

    file_urls: list[str] = []
    subfolder_urls: list[str] = []
    seen: set[str] = set()

    for anchor in anchors:
        href = str(anchor["href"])
        abs_url = urljoin(folder_url, href)
        try:
            abs_url = normalize_3gpp_ftp_url(abs_url)
        except ValueError:
            continue
        if abs_url in seen:
            continue
        seen.add(abs_url)

        if href.startswith("?"):
            continue
        if href in ("..", "../") or href.rstrip("/").endswith(".."):
            continue
        if _is_ancestor_or_self(abs_url, folder_url):
            continue

        anchor_classes = set(anchor.get("class") or [])
        if "file" in anchor_classes:
            basename = abs_url.rsplit("/", 1)[-1]
            lower_name = basename.lower()
            if (
                lower_name.endswith((".docx", ".zip"))
                and extract_tdoc_id_from_filename(basename) is not None
            ):
                file_urls.append(abs_url)
        elif _looks_like_folder(anchor, href):
            subfolder_urls.append(abs_url)

    return FtpListing(
        folder_url=folder_url,
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


def _source_file_from_filename(filename: str) -> str:
    parsed = urlparse(filename)
    source = parsed.path if parsed.scheme else filename
    source = source.replace("\\", "/")
    return source.rsplit("/", 1)[-1]


def _direct_parse_payload(
    payload: bytes,
    *,
    filename: str,
    max_bytes: int,
    max_documents: int,
    max_uncompressed_bytes: int,
    tdoc_id: str | None = None,
    parse_main: Callable[[str, str], TDocCRParseResult] | None = None,
    docx_pool: DocxMemberPool | None = None,
) -> TDocParseBundle:
    if max_bytes > 0 and len(payload) > max_bytes:
        from doc3gpp.scraping.tdoc_zip_source import TDocTooLargeError

        raise TDocTooLargeError(
            source=filename,
            size=len(payload),
            limit=max_bytes,
        )

    if (
        isinstance(max_documents, bool)
        or not isinstance(max_documents, int)
        or max_documents < 0
    ):
        raise ValueError("max_documents must be a non-negative integer")
    if (
        isinstance(max_uncompressed_bytes, bool)
        or not isinstance(max_uncompressed_bytes, int)
        or max_uncompressed_bytes < 0
    ):
        raise ValueError("max_uncompressed_bytes must be a non-negative integer")

    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        members = enumerate_docx_members(
            payload,
            max_documents=max_documents,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    elif suffix == ".docx":
        if max_uncompressed_bytes and len(payload) > max_uncompressed_bytes:
            raise TDocZipLimitError(
                "TDoc ZIP aggregate uncompressed byte limit exceeded by actual bytes: "
                f"{len(payload)} bytes > {max_uncompressed_bytes}"
            )
        members = ((_source_file_from_filename(filename), payload),)
    else:
        raise ValueError(
            f"Unsupported extension {suffix!r} for direct-parse; "
            "expected .docx or .zip"
        )

    from doc3gpp.parsers.docx_converter import (
        PythonDocxNotInstalledError,
        convert_document_to_markdown,
    )

    resolved_tdoc_id = (
        tdoc_id or extract_tdoc_id_from_filename(filename) or _synthetic_tdoc_id(filename)
    )
    converted: list[tuple[str, str]] = []
    failures: list[tuple[str, Exception]] = []
    member_pool = docx_pool if suffix == ".zip" else None
    outcomes = member_outcomes(convert_document_to_markdown, members, member_pool)
    for (source_file, _), outcome in zip(members, outcomes, strict=True):
        if isinstance(outcome, (DocxConvertPoolError, PythonDocxNotInstalledError)):
            raise outcome
        if not isinstance(outcome, Exception):
            converted.append((source_file, outcome))
        elif _is_empty_annex_error(outcome, source_file, resolved_tdoc_id):
            converted.append((source_file, ""))
        else:
            failures.append((source_file, outcome))

    cover_candidates = [
        source_file for source_file, markdown in converted if is_cr_header_present(markdown)[0]
    ]
    if not cover_candidates and failures:
        source_file, exc = failures[0]
        raise _failed_conversion(source_file, exc) from exc
    if not cover_candidates:
        raise CRHeaderMissingError(
            f"No supported DOCX member in {filename!r} contains a structural CR cover page"
        )
    if len(cover_candidates) != 1:
        tdoc_named = [
            source_file for source_file in cover_candidates
            if source_file.rsplit("/", 1)[-1].casefold()
            == f"{resolved_tdoc_id}.docx".casefold()
        ]
        if len(tdoc_named) != 1:
            raise ValueError(
                f"Expected exactly one DOCX member with a CR cover page in {filename!r}; "
                f"found {len(cover_candidates)} candidates: {', '.join(cover_candidates)}"
            )
        cover_candidates = tdoc_named

    main_source_file = cover_candidates[0]
    for source_file, exc in failures:
        if is_tdoc_member(source_file, resolved_tdoc_id.casefold()):
            raise _failed_conversion(source_file, exc) from exc
    for source_file, exc in failures:
        logger.warning("Skipping TDoc annex %r: conversion failed: %s", source_file, exc)
    skipped_sources = tuple(
        sorted((source_file for source_file, _ in failures), key=source_sort_key)
    )
    main_markdown = next(
        markdown for source_file, markdown in converted
        if source_file == main_source_file
    )
    if parse_main is None:
        parser = build_default_registry().resolve(resolved_tdoc_id, tdoc_type="CR")
        parsed = parser.parse(main_markdown, tdoc_id=resolved_tdoc_id)
    else:
        parsed = parse_main(main_markdown, resolved_tdoc_id)

    payload_hash = hashlib.sha256(payload).hexdigest()
    support_documents = sorted(
        (entry for entry in converted if entry[0] != main_source_file),
        key=lambda entry: source_sort_key(entry[0]),
    )
    ordered = [(main_source_file, main_markdown), *support_documents]
    documents: list[TDocDocument] = []
    for file_order, (source_file, markdown) in enumerate(ordered):
        document_id = ephemeral_document_id_for(payload_hash, source_file)
        documents.append(
            TDocDocument(
                document_id=document_id,
                source_file=source_file,
                markdown_file=markdown_member_name(document_id, source_file),
                file_order=file_order,
                markdown_text=markdown,
                content_hash=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            )
        )
    return TDocParseBundle(
        documents=tuple(documents),
        main_source_file=main_source_file,
        main_parse_result=parsed,
        skipped_sources=skipped_sources,
    )


def _failed_conversion(source_file: str, exc: Exception) -> ValueError:
    return ValueError(f"Failed to convert supported DOCX member {source_file!r}: {exc}")


def _is_empty_annex_error(exc: Exception, source_file: str, tdoc_id: str) -> bool:
    """Return whether a zero-text member can only be an annex, never the TDoc main."""
    return is_empty_text_error(exc) and not is_tdoc_member(source_file, tdoc_id.casefold())


def direct_parse_bytes(
    payload: bytes,
    *,
    filename: str,
    max_bytes: int = 0,
    max_documents: int = 100,
    max_uncompressed_bytes: int = 0,
    docx_pool: DocxMemberPool | None = None,
) -> TDocParseBundle:
    """Convert every supported DOCX member and parse the unique CR cover.

    ZIP members are enumerated and converted sequentially, or through
    ``docx_pool`` when one is given. The structural CR detector selects
    exactly one main document; supporting documents remain content-only and do
    not enter the CR parser. An annex that fails to convert is logged and
    listed in ``skipped_sources`` unless it is named for the TDoc.
    Standalone DOCX input follows the same one-document bundle contract.
    """
    return _direct_parse_payload(
        payload,
        filename=filename,
        max_bytes=max_bytes,
        max_documents=max_documents,
        max_uncompressed_bytes=max_uncompressed_bytes,
        docx_pool=docx_pool,
    )


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
    (i.e. ``[RSC][1-6P][-sw]\\d{6}`` case-insensitive).
    """
    return (
        f"warning: filename '{filename}' does not match the 3GPP TDoc id\n"
        f"         pattern ([RSC][1-6P][-sw]\\d{{6}}); skipping cache and DB writes."
    )


__all__ = [
    "build_missing_tdoc_id_warning_message",
    "build_no_pattern_warning_message",
    "derive_zip_cache_key",
    "direct_parse_bytes",
    "extract_tdoc_id_from_filename",
    "is_3gpp_file_url",
    "is_3gpp_ftp_candidate_url",
    "is_3gpp_ftp_url",
    "normalize_3gpp_ftp_url",
    "read_source_bytes",
    "validate_tdoc_ftp_url",
]
