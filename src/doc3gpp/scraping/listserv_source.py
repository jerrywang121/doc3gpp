"""Restricted HTTPS transport for public ETSI LISTSERV archive and body-part pages."""

from __future__ import annotations

import codecs
import logging
import posixpath
import re
import time
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
from typing_extensions import Self

from doc3gpp.config import get_settings
from doc3gpp.models.listserv import normalize_list_id
from doc3gpp.scraping.client import _RETRYABLE_STATUS_CODES, ScraperClient

if TYPE_CHECKING:
    from doc3gpp.settings.schema import Settings

logger = logging.getLogger(__name__)

LISTSERV_HOST = "list.etsi.org"
LISTSERV_WA_URL = f"https://{LISTSERV_HOST}/scripts/wa.exe"

_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_BODY_MIME_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "multipart/related",
        "application/xhtml+xml",
    }
)
_ATTACHMENT_EXTENSIONS = frozenset(
    {
        ".zip",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".xls",
        ".xlsx",
        ".pdf",
        ".7z",
        ".tar",
        ".gz",
        ".tgz",
        ".rar",
        ".bin",
    }
)
_CHARSET_PARAM_RE = re.compile(
    r"""charset\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s;,"'>]+))""",
    re.IGNORECASE,
)
_META_TAG_RE = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_RAW_RESPONSE_CHUNK_SIZE = 64 * 1024
_MAX_DECODED_CHUNK_SIZE = 64 * 1024
_DEFLATE_FALLBACK_PROBE_LIMIT = 64 * 1024
_GZIP_WBITS = zlib.MAX_WBITS | 16


class ListservSourceError(ValueError):
    """Base error raised for rejected or non-public LISTSERV source requests."""


class ListservAuthRequiredError(httpx.HTTPStatusError, ListservSourceError):
    """Raised when an ETSI LISTSERV endpoint returns 401/403 authentication required."""


class ListservRedirectError(httpx.TooManyRedirects, ListservSourceError):
    """Raised when a LISTSERV redirect chain loops, exceeds the hop limit, or lacks Location."""


@dataclass(slots=True, frozen=True)
class FetchedPage:
    """Decoded text response from a validated public ETSI LISTSERV URL."""

    text: str
    content_type: str
    final_url: str


def archive_index_url(list_id: str) -> str:
    """Return the canonical ETSI LISTSERV archive index URL for ``list_id``."""
    normalized = normalize_list_id(list_id)
    if any(
        not ch.isprintable() or ch.isspace() or not (ch.isalnum() or ch in "-_.")
        for ch in normalized
    ):
        raise ValueError(f"Invalid LISTSERV list_id {list_id!r}.")
    encoded_list = quote(normalized, safe="")
    return f"{LISTSERV_WA_URL}?A0={encoded_list}"


def _is_attachment_query(query: str) -> bool:
    if not query:
        return False
    params: dict[str, list[str]] = {}
    for key, values in parse_qs(query, keep_blank_values=True).items():
        params.setdefault(key.upper(), []).extend(values)
    for raw_t in params.get("T", []):
        media_type = raw_t.partition(";")[0].strip().lower()
        if media_type and media_type not in _ALLOWED_BODY_MIME_TYPES:
            return True
    if "A3" in params and any(v.strip() for v in params.get("N", [])):
        return True
    for raw_disp in params.get("DISPOSITION", []):
        if raw_disp.strip().lower().startswith("attachment"):
            return True
    return False


def _is_attachment_path(path: str) -> bool:
    lowered = path.lower()
    segments = [seg for seg in lowered.split("/") if seg]
    if any(seg in ("att", "attachments") for seg in segments):
        return True
    _, ext = posixpath.splitext(lowered)
    return ext in _ATTACHMENT_EXTENSIONS


def validate_listserv_url(url: str) -> str:
    """Validate that ``url`` targets public ``https://list.etsi.org`` and is not an attachment."""
    stripped = url.strip()
    if not stripped or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in stripped):
        raise ValueError(f"Invalid LISTSERV URL {url!r}.")

    parsed = urlsplit(stripped)
    if parsed.scheme.lower() != "https":
        raise ValueError(
            f"Unsupported URL scheme in {url!r}; only HTTPS requests to {LISTSERV_HOST} are permitted."
        )
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"Credentials are not permitted in LISTSERV URL {url!r}.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"Invalid port in LISTSERV URL {url!r}.") from exc

    hostname = (parsed.hostname or "").lower()
    netloc_lower = parsed.netloc.lower()
    if hostname != LISTSERV_HOST or port not in (None, 443) or netloc_lower not in (
        LISTSERV_HOST,
        f"{LISTSERV_HOST}:443",
    ):
        raise ValueError(
            f"Untrusted host or port in {url!r}; only https://{LISTSERV_HOST} is permitted."
        )

    path = parsed.path or "/"
    decoded_path = unquote(path)
    if ".." in decoded_path.split("/"):
        raise ValueError(f"Path traversal is not permitted in LISTSERV URL {url!r}.")
    if _is_attachment_path(decoded_path) or _is_attachment_query(parsed.query):
        raise ValueError(
            f"Refusing to fetch attachment target {url!r}; attachments are not downloaded."
        )

    return urlunsplit(("https", LISTSERV_HOST, path, parsed.query, ""))


def _extract_charset_from_header(content_type_header: str) -> str | None:
    match = _CHARSET_PARAM_RE.search(content_type_header)
    if match is None:
        return None
    candidate = (match.group(1) or match.group(2) or match.group(3) or "").strip()
    return candidate or None


def _extract_charset_from_meta(raw_bytes: bytes) -> str | None:
    head_slice = raw_bytes[:4096]
    for meta_match in _META_TAG_RE.finditer(head_slice):
        tag_text = meta_match.group(0).decode("ascii", errors="ignore")
        charset = _extract_charset_from_header(tag_text)
        if charset:
            return charset
    return None


def _decode_response_bytes(raw_bytes: bytes, content_type_header: str) -> str:
    declared = _extract_charset_from_header(content_type_header) or _extract_charset_from_meta(
        raw_bytes
    )
    if declared:
        try:
            codecs.lookup(declared)
            return raw_bytes.decode(declared, errors="replace")
        except LookupError:
            logger.warning("Unknown declared charset %r; falling back to UTF-8.", declared)
    return raw_bytes.decode("utf-8", errors="replace")


def _content_encoding_for_response(response: httpx.Response, url: str) -> str:
    """Return the one supported Content-Encoding without consuming the body."""
    raw_encoding = response.headers.get("content-encoding", "").strip()
    if not raw_encoding:
        return "identity"

    encodings = [part.strip().lower() for part in raw_encoding.split(",")]
    if len(encodings) != 1 or encodings[0] not in {"identity", "gzip", "deflate"}:
        raise ListservSourceError(
            f"Unsupported LISTSERV content encoding {raw_encoding!r} from {url}; "
            "only identity, gzip and deflate are supported."
        )
    return encodings[0]


def _decode_response_chunks(
    chunks: Iterable[bytes], encoding: str, url: str
) -> bytes:
    """Decode an allowed response encoding while bounding every output allocation."""
    body = bytearray()

    def append_decoded(decoded: bytes) -> None:
        if len(body) + len(decoded) > _MAX_RESPONSE_BYTES:
            raise ListservSourceError(
                f"LISTSERV response from {url} exceeds the 16 MiB decoded body limit."
            )
        body.extend(decoded)

    if encoding == "identity":
        for chunk in chunks:
            append_decoded(chunk)
        return bytes(body)

    is_gzip = encoding == "gzip"
    decoder = None if is_gzip else zlib.decompressobj(zlib.MAX_WBITS)
    gzip_members = 0
    deflate_complete = False
    output_seen = False
    raw_deflate = False
    fallback_window_open = encoding == "deflate"
    fallback_probe = bytearray()

    def feed(pending: bytes) -> None:
        nonlocal decoder, gzip_members, deflate_complete
        nonlocal output_seen, raw_deflate, fallback_window_open

        while pending:
            if is_gzip and decoder is None:
                decoder = zlib.decompressobj(_GZIP_WBITS)
            assert decoder is not None
            max_length = min(
                _MAX_DECODED_CHUNK_SIZE,
                _MAX_RESPONSE_BYTES - len(body) + 1,
            )
            try:
                decoded = decoder.decompress(pending, max_length)
            except zlib.error as exc:
                if (
                    encoding == "deflate"
                    and not raw_deflate
                    and not output_seen
                    and fallback_window_open
                    and fallback_probe
                ):
                    pending = bytes(fallback_probe)
                    fallback_probe.clear()
                    fallback_window_open = False
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    raw_deflate = True
                    continue
                raise ListservSourceError(
                    f"Malformed or incomplete {encoding} encoded LISTSERV response from {url}."
                ) from exc

            append_decoded(decoded)
            if decoded and encoding == "deflate" and not raw_deflate:
                output_seen = True
                fallback_probe.clear()
                fallback_window_open = False

            if is_gzip:
                if decoder.eof:
                    gzip_members += 1
                    pending = decoder.unused_data
                    decoder = None
                else:
                    pending = decoder.unconsumed_tail
            elif decoder.eof:
                if decoder.unused_data:
                    raise ListservSourceError(
                        f"Malformed or incomplete {encoding} encoded LISTSERV response from {url}."
                    )
                deflate_complete = True
                pending = b""
            else:
                pending = decoder.unconsumed_tail

    for chunk in chunks:
        if not chunk:
            continue
        if encoding == "deflate" and fallback_window_open:
            if len(fallback_probe) + len(chunk) <= _DEFLATE_FALLBACK_PROBE_LIMIT:
                fallback_probe.extend(chunk)
            else:
                fallback_probe.clear()
                fallback_window_open = False
        feed(chunk)

    if (
        encoding == "deflate"
        and not deflate_complete
        and not raw_deflate
        and not output_seen
        and fallback_window_open
        and fallback_probe
    ):
        pending = bytes(fallback_probe)
        fallback_probe.clear()
        fallback_window_open = False
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        raw_deflate = True
        feed(pending)

    if is_gzip and (gzip_members == 0 or decoder is not None):
        raise ListservSourceError(
            f"Malformed or incomplete {encoding} encoded LISTSERV response from {url}."
        )
    if encoding == "deflate" and not deflate_complete:
        raise ListservSourceError(
            f"Malformed or incomplete {encoding} encoded LISTSERV response from {url}."
        )
    return bytes(body)


class ListservSource:
    """Restricted HTTP transport for public ETSI LISTSERV pages with manual redirect validation."""

    DEFAULT_TIMEOUT_SECONDS = ScraperClient.DEFAULT_TIMEOUT_SECONDS
    DEFAULT_MAX_REDIRECTS = 5
    USER_AGENT = ScraperClient.USER_AGENT

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("Cannot provide both an httpx.Client and a transport.")
        if max_redirects < 0:
            raise ValueError(f"max_redirects must be nonnegative, got {max_redirects}.")

        resolved_settings = get_settings() if settings is None else settings
        self._max_retries = (
            resolved_settings.http_max_retries if max_retries is None else max_retries
        )
        self._backoff = (
            resolved_settings.http_retry_backoff if retry_backoff is None else retry_backoff
        )
        self._max_redirects = max_redirects
        self._closed = False

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                headers={"User-Agent": self.USER_AGENT},
                follow_redirects=False,
                verify=resolved_settings.http_verify,
                transport=transport,
            )
            self._client.timeout = timeout_seconds
            self._owns_client = True

    def fetch(self, url: str) -> FetchedPage:
        """Fetch a validated ETSI LISTSERV page, manually validating each redirect hop."""
        current_url = validate_listserv_url(url)
        visited: set[str] = {current_url}
        redirects_followed = 0

        while True:
            response, raw_body = self._request_with_retry(
                current_url,
                lambda target=current_url: self._client.send(
                    self._client.build_request(
                        "GET",
                        target,
                        headers={"Accept-Encoding": "gzip, deflate, identity"},
                    ),
                    follow_redirects=False,
                    stream=True,
                ),
            )
            try:
                if response.status_code in _REDIRECT_STATUS_CODES:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise ListservRedirectError(
                            f"Redirect response ({response.status_code}) from {current_url} lacked a Location header.",
                            request=response.request,
                        )
                    next_url = validate_listserv_url(urljoin(current_url, location))
                    if next_url in visited:
                        raise ListservRedirectError(
                            f"Redirect loop detected while fetching {url}: {next_url} already visited.",
                            request=response.request,
                        )
                    if redirects_followed >= self._max_redirects:
                        raise ListservRedirectError(
                            f"Exceeded maximum redirects ({self._max_redirects}) while fetching {url}.",
                            request=response.request,
                        )
                    visited.add(next_url)
                    redirects_followed += 1
                    current_url = next_url
                    continue

                raw_ct = response.headers.get("content-type", "")
                media_type = raw_ct.partition(";")[0].strip().lower() or "text/html"
                text = _decode_response_bytes(raw_body or b"", raw_ct)
                return FetchedPage(
                    text=text,
                    content_type=media_type,
                    final_url=current_url,
                )
            finally:
                response.close()

    @staticmethod
    def _validate_response_not_attachment(response: httpx.Response, url: str) -> None:
        disposition = response.headers.get("content-disposition", "").strip().lower()
        if disposition.startswith("attachment"):
            raise ListservSourceError(
                f"Refusing attachment response from {url}; attachments are not downloaded."
            )
        raw_ct = response.headers.get("content-type", "")
        media_type = raw_ct.partition(";")[0].strip().lower()
        if media_type and media_type not in _ALLOWED_BODY_MIME_TYPES:
            raise ListservSourceError(
                f"Refusing non-page content-type {media_type!r} from {url}; attachments are not downloaded."
            )

    def _request_with_retry(
        self, url: str, request_fn: Callable[[], httpx.Response]
    ) -> tuple[httpx.Response, bytes | None]:
        attempt = 0
        last_exc: httpx.HTTPError | None = None
        while attempt <= self._max_retries:
            try:
                response = request_fn()
            except httpx.HTTPError as exc:
                last_exc = exc
                if (
                    not ScraperClient._is_retryable_exception(exc)
                    or attempt == self._max_retries
                ):
                    logger.error(
                        "Failed to fetch LISTSERV URL %s after %s attempt(s): %s",
                        url,
                        attempt + 1,
                        type(exc).__name__,
                    )
                    raise
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "Transient error fetching LISTSERV URL %s (attempt %s/%s): %s; retrying in %.2fs",
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    type(exc).__name__,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue

            if response.status_code in (401, 403):
                msg = (
                    f"ETSI LISTSERV archive at {url} requires authentication or is private "
                    f"(HTTP {response.status_code}); only public ETSI LISTSERV archives are supported."
                )
                logger.error(
                    "Authentication required for LISTSERV URL %s (HTTP %s).",
                    url,
                    response.status_code,
                )
                error = ListservAuthRequiredError(
                    msg, request=response.request, response=response
                )
                response.close()
                raise error

            if response.status_code in _RETRYABLE_STATUS_CODES:
                msg = f"HTTP {response.status_code}"
                last_exc = httpx.HTTPStatusError(
                    msg, request=response.request, response=response
                )
                response.close()
                if attempt == self._max_retries:
                    logger.error(
                        "Failed to fetch LISTSERV URL %s after %s attempt(s): %s",
                        url,
                        attempt + 1,
                        msg,
                    )
                    raise last_exc
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "Retryable status %s fetching LISTSERV URL %s (attempt %s/%s); retrying in %.2fs",
                    response.status_code,
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue

            if response.status_code in _REDIRECT_STATUS_CODES:
                return response, None

            try:
                response.raise_for_status()
                self._validate_response_not_attachment(response, url)
                encoding = _content_encoding_for_response(response, url)
                if response.is_stream_consumed:
                    if encoding != "identity":
                        raise ListservSourceError(
                            f"Cannot safely process a pre-consumed compressed LISTSERV response from {url}."
                        )
                    response_chunks = (response.content,)
                else:
                    response_chunks = response.iter_raw(
                        chunk_size=_RAW_RESPONSE_CHUNK_SIZE
                    )
                raw_body = _decode_response_chunks(response_chunks, encoding, url)
            except httpx.HTTPError as exc:
                response.close()
                last_exc = exc
                if (
                    not ScraperClient._is_retryable_exception(exc)
                    or attempt == self._max_retries
                ):
                    logger.error(
                        "Failed to fetch LISTSERV URL %s after %s attempt(s): %s",
                        url,
                        attempt + 1,
                        type(exc).__name__,
                    )
                    raise
                delay = self._backoff * (2**attempt)
                logger.warning(
                    "Transient error fetching LISTSERV URL %s (attempt %s/%s): %s; retrying in %.2fs",
                    url,
                    attempt + 1,
                    self._max_retries + 1,
                    type(exc).__name__,
                    delay,
                )
                self._sleep(delay)
                attempt += 1
                continue
            except BaseException:
                response.close()
                raise

            return response, bytes(raw_body)

        assert last_exc is not None  # pragma: no cover - loop invariant
        raise last_exc

    @staticmethod
    def _sleep(seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def close(self) -> None:
        """Close the underlying HTTP client when owned by this source."""
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


__all__ = [
    "LISTSERV_HOST",
    "LISTSERV_WA_URL",
    "FetchedPage",
    "ListservAuthRequiredError",
    "ListservRedirectError",
    "ListservSource",
    "ListservSourceError",
    "archive_index_url",
    "validate_listserv_url",
]
