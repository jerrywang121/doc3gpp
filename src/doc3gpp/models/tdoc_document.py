"""ORM-free domain values for documents contained in a TDoc archive."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doc3gpp.models.tdoc_cr import TDocCRParseResult


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:")


def _normalize_safe_relative_path(path: str, *, field_name: str) -> str:
    """Normalize ZIP separators and reject paths that are not safe archive names."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{field_name} must be a non-empty relative path")

    normalized = path.replace("\\", "/")
    if "\x00" in normalized:
        raise ValueError(f"{field_name} must not contain a NUL character")
    if normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(normalized):
        raise ValueError(f"{field_name} must not be absolute")

    components = normalized.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise ValueError(f"{field_name} must not contain empty or traversing components")
    return normalized


def _validate_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


def sanitized_source_stem(source_file: str) -> str:
    """Return the archive-safe stem of a source DOCX basename."""
    basename = source_file.rsplit("/", 1)[-1]
    stem = basename[:-5] if basename.casefold().endswith(".docx") else basename
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return sanitized or "document"


def source_sort_key(source_file: str) -> tuple[str, str]:
    """Match the archive enumerator's stable case-insensitive path ordering."""
    return source_file.casefold(), source_file


def markdown_member_name(document_id: str, source_file: str) -> str:
    """Return the derived Markdown ZIP member path for one source document."""
    return f"documents/{document_id}/{sanitized_source_stem(source_file)}.md"


def ordered_sources(sources: Sequence[str], main: str | None) -> tuple[str, ...]:
    """Return the main source first when present, then the rest path-sorted."""
    supporting = sorted((source for source in sources if source != main), key=source_sort_key)
    if main is not None and main in sources:
        return (main, *supporting)
    return tuple(supporting)


@dataclass(slots=True, frozen=True)
class TDocDocument:
    """Converted Markdown and source identity for one DOCX ZIP member.

    ``source_file`` is the full, case-preserving path within the source ZIP.
    ``markdown_file`` is its safe relative member path in the derived Markdown
    ZIP. ``document_id`` and ``content_hash`` are lowercase SHA-256 digests.
    """

    document_id: str
    source_file: str
    markdown_file: str
    file_order: int
    markdown_text: str
    content_hash: str

    def __post_init__(self) -> None:
        _validate_sha256(self.document_id, field_name="document_id")

        source_file = _normalize_safe_relative_path(
            self.source_file,
            field_name="source_file",
        )
        if source_file != self.source_file:
            object.__setattr__(self, "source_file", source_file)

        markdown_file = _normalize_safe_relative_path(
            self.markdown_file,
            field_name="markdown_file",
        )
        if not markdown_file.lower().endswith(".md"):
            raise ValueError("markdown_file must name a .md member")
        if markdown_file != self.markdown_file:
            object.__setattr__(self, "markdown_file", markdown_file)

        if isinstance(self.file_order, bool) or not isinstance(self.file_order, int):
            raise TypeError("file_order must be an integer")
        if self.file_order < 0:
            raise ValueError("file_order must be a non-negative integer")
        if not isinstance(self.markdown_text, str):
            raise TypeError("markdown_text must be a string")

        _validate_sha256(self.content_hash, field_name="content_hash")
        expected_hash = hashlib.sha256(
            self.markdown_text.encode("utf-8")
        ).hexdigest()
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match markdown_text")


@dataclass(slots=True, frozen=True)
class TDocParseBundle:
    """Ordered document collection and CR parse result for its main source."""

    documents: tuple[TDocDocument, ...]
    main_source_file: str
    main_parse_result: TDocCRParseResult
    skipped_sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        documents = tuple(self.documents)
        if not documents:
            raise ValueError("TDocParseBundle requires at least one document")
        if documents != self.documents:
            object.__setattr__(self, "documents", documents)
        skipped_sources = tuple(self.skipped_sources)
        if skipped_sources != self.skipped_sources:
            object.__setattr__(self, "skipped_sources", skipped_sources)

        main_source_file = _normalize_safe_relative_path(
            self.main_source_file,
            field_name="main_source_file",
        )
        if main_source_file != self.main_source_file:
            object.__setattr__(self, "main_source_file", main_source_file)
        if self.main_parse_result is None:
            raise ValueError("TDocParseBundle requires the main CR parse result")

        source_files = tuple(document.source_file for document in documents)
        if len(source_files) != len(set(source_files)):
            raise ValueError("TDocParseBundle contains duplicate source_file values")
        if source_files[0] != main_source_file:
            raise ValueError("TDocParseBundle must order the main source first")
        if source_files[1:] != tuple(sorted(source_files[1:], key=source_sort_key)):
            raise ValueError("TDocParseBundle support sources must be path-sorted")
        if tuple(document.file_order for document in documents) != tuple(
            range(len(documents))
        ):
            raise ValueError("TDocParseBundle file_order values must be contiguous")
        undocumented = set(skipped_sources) & set(source_files)
        if len(set(skipped_sources)) != len(skipped_sources) or undocumented:
            raise ValueError("TDocParseBundle skipped sources must be unique and undocumented")


@dataclass(slots=True, frozen=True)
class MarkdownArchive:
    """Decoded derived archive or an isolated legacy Markdown compatibility value.

    Versioned archives carry an ordered document collection and source metadata.
    Legacy plain, gzip, and single-member ZIP artifacts have no reliable source
    identity, so their text is kept separately in ``legacy_markdown`` rather than
    being assigned an invented ``TDocDocument``.
    """

    ftp_url: str | None = None
    main_source_file: str | None = None
    documents: tuple[TDocDocument, ...] = ()
    manifest_version: int | None = None
    legacy_markdown: str | None = None

    def __post_init__(self) -> None:
        documents = tuple(self.documents)
        if documents != self.documents:
            object.__setattr__(self, "documents", documents)

        if self.legacy_markdown is not None:
            if documents or self.ftp_url is not None or self.main_source_file is not None:
                raise ValueError("legacy Markdown cannot claim archive document provenance")
            if self.manifest_version is not None:
                raise ValueError("legacy Markdown cannot carry a manifest version")
            return

        if not self.ftp_url or not self.ftp_url.strip():
            raise ValueError("versioned Markdown archive requires a non-empty ftp_url")
        if not documents:
            raise ValueError("versioned Markdown archive requires at least one document")
        if type(self.manifest_version) is not int or self.manifest_version != 1:
            raise ValueError("unsupported Markdown archive manifest version")

        main_source_file = _normalize_safe_relative_path(
            self.main_source_file or "",
            field_name="main_source_file",
        )
        if main_source_file != self.main_source_file:
            object.__setattr__(self, "main_source_file", main_source_file)

        source_files = tuple(document.source_file for document in documents)
        if source_files[0] != main_source_file:
            raise ValueError("Markdown archive must order the main source first")
        if source_files[1:] != tuple(sorted(source_files[1:], key=source_sort_key)):
            raise ValueError("Markdown archive support sources must be path-sorted")
        if tuple(document.file_order for document in documents) != tuple(
            range(len(documents))
        ):
            raise ValueError("Markdown archive file_order values must be contiguous")

    @property
    def is_legacy(self) -> bool:
        """Whether this value came from a provenance-free legacy artifact."""
        return self.legacy_markdown is not None
