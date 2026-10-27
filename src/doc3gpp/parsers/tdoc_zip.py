"""ZIP member enumeration and Markdown cache archive helpers for TDocs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import re
import stat
import zipfile
from collections.abc import Sequence

from doc3gpp.models.tdoc_document import (
    MarkdownArchive,
    TDocDocument,
    _normalize_safe_relative_path,
    markdown_member_name,
    source_sort_key,
)
from doc3gpp.models.tdoc_identity import (
    document_id_for as _domain_document_id_for,
)
from doc3gpp.models.tdoc_identity import normalize_ftp_path

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_MANIFEST_MEMBER = "manifest.json"
_MANIFEST_VERSION = 1
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"
_CHUNK_SIZE = 64 * 1024


class TDocZipValidationError(ValueError):
    """A TDoc ZIP has an unsafe member identity or invalid derived manifest."""


class TDocZipLimitError(ValueError):
    """A TDoc ZIP exceeds its configured document or expanded-byte limit."""


def _normalized_member_name(name: str) -> str:
    """Normalize separator spelling while rejecting unsafe relative names."""
    try:
        return _normalize_safe_relative_path(name, field_name="ZIP member name")
    except ValueError as exc:
        raise TDocZipValidationError(str(exc)) from exc


def _is_directory(info: zipfile.ZipInfo, normalized_name: str) -> bool:
    """Return true for directory entries, including DOS-style separators."""
    if info.is_dir() or normalized_name.endswith("/"):
        return True
    mode = info.external_attr >> 16
    return stat.S_ISDIR(mode)


def _docx_member_infos(
    archive: zipfile.ZipFile,
) -> tuple[tuple[str, zipfile.ZipInfo], ...]:
    """Return safe, sorted DOCX metadata without reading member payloads."""
    candidates: list[tuple[str, zipfile.ZipInfo]] = []
    seen_names: set[str] = set()
    for info in archive.infolist():
        name_with_normalized_separators = info.filename.replace("\\", "/")
        is_directory = _is_directory(info, name_with_normalized_separators)
        validation_name = name_with_normalized_separators
        if is_directory and validation_name.endswith("/"):
            validation_name = validation_name[:-1]
        source_file = _normalized_member_name(validation_name)

        if source_file in seen_names:
            raise TDocZipValidationError(
                f"duplicate ZIP member name after separator normalization: {source_file!r}"
            )
        seen_names.add(source_file)

        if is_directory:
            continue
        path_parts = source_file.split("/")
        if any(part.casefold() == "__macosx" for part in path_parts):
            continue
        if path_parts[-1].startswith("~$"):
            continue
        if not path_parts[-1].casefold().endswith(".docx"):
            logger.warning(
                "Skipping unsupported TDoc ZIP member %r (only .docx is supported)",
                source_file,
            )
            continue
        candidates.append((source_file, info))

    return tuple(sorted(candidates, key=lambda item: source_sort_key(item[0])))


def _sha256_json(values: list[str]) -> str:
    serialized = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def document_id_for(ftp_url: str, source_file: str) -> str:
    """Return a stable SHA-256 identity for a URL and full ZIP member path.

    This parser-layer wrapper preserves the public API and parser-specific path
    validation error while delegating canonical identity generation to the
    dependency-neutral domain module.
    """
    normalized_url = normalize_ftp_path(ftp_url)
    if not normalized_url:
        raise ValueError("ftp_url must be a non-empty ZIP URL")
    normalized_source = _normalized_member_name(source_file)
    return _domain_document_id_for(normalized_url, normalized_source)


def ephemeral_document_id_for(payload_hash: str, source_file: str) -> str:
    """Return a URL-independent ID for a direct/local source document.

    The first serialized value is a namespace marker, keeping direct/local
    identities separate from stored identities that begin with a normalized
    FTP URL.
    """
    if not isinstance(payload_hash, str) or _SHA256_RE.fullmatch(payload_hash) is None:
        raise ValueError("payload_hash must be a SHA-256 hex digest")
    normalized_source = _normalized_member_name(source_file)
    return _sha256_json(["doc3gpp-ephemeral", payload_hash.lower(), normalized_source])


def _read_member_with_limit(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_uncompressed_bytes: int,
    bytes_already_read: int,
) -> bytes:
    payload = bytearray()
    with archive.open(info, "r") as member:
        while True:
            read_size = _CHUNK_SIZE
            if max_uncompressed_bytes:
                remaining = max_uncompressed_bytes - bytes_already_read - len(payload)
                read_size = min(read_size, remaining + 1)
            chunk = member.read(read_size)
            if not chunk:
                break
            payload.extend(chunk)
            if max_uncompressed_bytes and bytes_already_read + len(payload) > max_uncompressed_bytes:
                raise TDocZipLimitError(
                    "TDoc ZIP aggregate uncompressed byte limit exceeded while reading members"
                )

    if len(payload) != info.file_size:
        raise TDocZipValidationError(
            f"ZIP member {info.filename!r} declared {info.file_size} bytes "
            f"but yielded {len(payload)} bytes"
        )
    return bytes(payload)


def _read_first_docx_member(zip_bytes: bytes) -> tuple[str, bytes] | None:
    """Read only the first safe DOCX member selected by the shared metadata rules."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        candidates = _docx_member_infos(archive)
        if not candidates:
            return None
        source_file, info = candidates[0]
        member_bytes = _read_member_with_limit(
            archive,
            info,
            max_uncompressed_bytes=0,
            bytes_already_read=0,
        )
        return source_file, member_bytes


def enumerate_docx_members(
    zip_bytes: bytes,
    *,
    max_documents: int = 100,
    max_uncompressed_bytes: int = 0,
) -> tuple[tuple[str, bytes], ...]:
    """Read supported DOCX members in stable order without extracting to disk.

    Only ``.docx`` payloads are returned. Directories, ``__MACOSX`` entries,
    Word lock files, and unsupported formats are ignored. ZIP separators are
    normalized to ``/``; unsafe or duplicate member identities are rejected.
    Both central-directory sizes and bytes yielded while reading are checked
    against the aggregate expanded-size limit. A zero limit disables its cap.
    """
    if isinstance(max_documents, bool) or not isinstance(max_documents, int) or max_documents < 0:
        raise ValueError("max_documents must be a non-negative integer")
    if (
        isinstance(max_uncompressed_bytes, bool)
        or not isinstance(max_uncompressed_bytes, int)
        or max_uncompressed_bytes < 0
    ):
        raise ValueError("max_uncompressed_bytes must be a non-negative integer")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        candidates = _docx_member_infos(archive)
        if max_documents and len(candidates) > max_documents:
            raise TDocZipLimitError(
                f"TDoc ZIP document-count limit exceeded: {len(candidates)} "
                f"DOCX members > {max_documents}"
            )

        declared_size = sum(info.file_size for _, info in candidates)
        if max_uncompressed_bytes and declared_size > max_uncompressed_bytes:
            raise TDocZipLimitError(
                "TDoc ZIP aggregate uncompressed byte limit exceeded by declared sizes: "
                f"{declared_size} bytes > {max_uncompressed_bytes}"
            )

        members: list[tuple[str, bytes]] = []
        bytes_read = 0
        for source_file, info in candidates:
            member_bytes = _read_member_with_limit(
                archive,
                info,
                max_uncompressed_bytes=max_uncompressed_bytes,
                bytes_already_read=bytes_read,
            )
            bytes_read += len(member_bytes)
            members.append((source_file, member_bytes))

    return tuple(members)


def _markdown_member_for(document: TDocDocument) -> str:
    return markdown_member_name(document.document_id, document.source_file)


def _ordered_documents(
    documents: Sequence[TDocDocument],
    *,
    ftp_url: str,
    main_source_file: str,
) -> tuple[TDocDocument, ...]:
    if not documents:
        raise TDocZipValidationError("Markdown archive requires at least one document")

    ordered = tuple(sorted(documents, key=lambda document: document.file_order))
    if tuple(document.file_order for document in ordered) != tuple(range(len(ordered))):
        raise TDocZipValidationError("document file_order values must be contiguous from zero")

    source_files = tuple(document.source_file for document in ordered)
    if len(source_files) != len(set(source_files)):
        raise TDocZipValidationError("Markdown archive contains duplicate source_file values")
    document_ids = tuple(document.document_id for document in ordered)
    if len(document_ids) != len(set(document_ids)):
        raise TDocZipValidationError("Markdown archive contains duplicate document_id values")

    if source_files[0] != main_source_file:
        raise TDocZipValidationError("main_source_file must be the first ordered document")
    if source_files[1:] != tuple(sorted(source_files[1:], key=source_sort_key)):
        raise TDocZipValidationError("supporting documents must be ordered by source path")

    for document in ordered:
        expected_id = document_id_for(ftp_url, document.source_file)
        if document.document_id != expected_id:
            raise TDocZipValidationError(
                f"document_id does not match ftp_url/source_file for {document.source_file!r}"
            )
        expected_member = _markdown_member_for(document)
        if document.markdown_file != expected_member:
            raise TDocZipValidationError(
                f"markdown_file for {document.source_file!r} must be {expected_member!r}"
            )
    return ordered


def build_markdown_archive(
    documents: Sequence[TDocDocument],
    *,
    ftp_url: str,
    main_source_file: str,
) -> bytes:
    """Build a deterministic Markdown ZIP with one manifest and one member per document."""
    if not isinstance(ftp_url, str) or not ftp_url.strip():
        raise ValueError("ftp_url must be a non-empty ZIP URL")
    ftp_url = ftp_url.strip()
    main_source_file = _normalized_member_name(main_source_file)
    ordered = _ordered_documents(
        documents,
        ftp_url=ftp_url,
        main_source_file=main_source_file,
    )

    manifest = {
        "version": _MANIFEST_VERSION,
        "ftp_url": ftp_url,
        "main_source_file": main_source_file,
        "documents": [
            {
                "document_id": document.document_id,
                "source_file": document.source_file,
                "markdown_file": document.markdown_file,
                "file_order": document.file_order,
                "content_hash": document.content_hash,
            }
            for document in ordered
        ],
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest_info = zipfile.ZipInfo(_MANIFEST_MEMBER, date_time=(1980, 1, 1, 0, 0, 0))
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(manifest_info, manifest_bytes)
        for document in ordered:
            member_info = zipfile.ZipInfo(
                document.markdown_file,
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            member_info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member_info, document.markdown_text.encode("utf-8"))
    return output.getvalue()


def _read_versioned_archive(
    archive: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    manifest_info: zipfile.ZipInfo,
) -> MarkdownArchive:
    try:
        manifest = json.loads(archive.read(manifest_info).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TDocZipValidationError("Markdown archive manifest is not valid UTF-8 JSON") from exc

    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise TDocZipValidationError("Markdown archive manifest has no integer version")
    if manifest["version"] != _MANIFEST_VERSION:
        raise TDocZipValidationError(
            f"unsupported Markdown archive manifest version: {manifest['version']}"
        )

    ftp_url = manifest.get("ftp_url")
    if not isinstance(ftp_url, str) or not ftp_url.strip():
        raise TDocZipValidationError("Markdown archive manifest has no ftp_url")
    ftp_url = ftp_url.strip()
    main_source_file = _normalized_member_name(manifest.get("main_source_file", ""))
    entries = manifest.get("documents")
    if not isinstance(entries, list) or not entries:
        raise TDocZipValidationError("Markdown archive manifest has no document mappings")

    markdown_infos: dict[str, list[zipfile.ZipInfo]] = {}
    for info in infos:
        name_with_normalized_separators = info.filename.replace("\\", "/")
        if _is_directory(info, name_with_normalized_separators):
            continue
        if not name_with_normalized_separators.casefold().endswith(".md"):
            continue
        member_name = _normalized_member_name(name_with_normalized_separators)
        markdown_infos.setdefault(member_name, []).append(info)

    documents: list[TDocDocument] = []
    expected_markdown_files: set[str] = set()
    for order, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TDocZipValidationError("Markdown archive document mapping must be an object")
        source_file = _normalized_member_name(entry.get("source_file", ""))
        document_id = entry.get("document_id")
        markdown_file = _normalized_member_name(entry.get("markdown_file", ""))
        content_hash = entry.get("content_hash")
        file_order = entry.get("file_order")
        if not isinstance(document_id, str) or not isinstance(content_hash, str):
            raise TDocZipValidationError("Markdown archive document mapping has invalid hashes")
        if isinstance(file_order, bool) or not isinstance(file_order, int) or file_order != order:
            raise TDocZipValidationError("Markdown archive file_order values must be contiguous")
        if document_id != document_id_for(ftp_url, source_file):
            raise TDocZipValidationError(
                f"document_id does not match ftp_url/source_file for {source_file!r}"
            )
        if markdown_file != markdown_member_name(document_id, source_file):
            raise TDocZipValidationError(
                f"invalid Markdown member path for source {source_file!r}"
            )
        if markdown_file in expected_markdown_files:
            raise TDocZipValidationError(f"duplicate Markdown member mapping: {markdown_file!r}")
        expected_markdown_files.add(markdown_file)

        matching_infos = markdown_infos.get(markdown_file, [])
        if len(matching_infos) != 1:
            raise TDocZipValidationError(
                f"expected exactly one Markdown member for {markdown_file!r}"
            )
        try:
            markdown_text = archive.read(matching_infos[0]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TDocZipValidationError(
                f"Markdown member {markdown_file!r} is not valid UTF-8"
            ) from exc
        try:
            document = TDocDocument(
                document_id=document_id,
                source_file=source_file,
                markdown_file=markdown_file,
                file_order=file_order,
                markdown_text=markdown_text,
                content_hash=content_hash,
            )
        except ValueError as exc:
            raise TDocZipValidationError(
                f"invalid Markdown archive document {source_file!r}: {exc}"
            ) from exc
        documents.append(document)

    actual_markdown_files = set(markdown_infos)
    if actual_markdown_files != expected_markdown_files:
        raise TDocZipValidationError("Markdown archive members do not match the manifest")
    if documents[0].source_file != main_source_file:
        raise TDocZipValidationError("Markdown archive main source is not first in the manifest")
    if tuple(document.source_file for document in documents[1:]) != tuple(
        sorted((document.source_file for document in documents[1:]), key=source_sort_key)
    ):
        raise TDocZipValidationError("Markdown archive supporting sources are not path-sorted")

    try:
        return MarkdownArchive(
            ftp_url=ftp_url,
            main_source_file=main_source_file,
            documents=tuple(documents),
            manifest_version=_MANIFEST_VERSION,
        )
    except ValueError as exc:
        raise TDocZipValidationError(f"invalid Markdown archive metadata: {exc}") from exc


def _read_legacy_zip(archive: zipfile.ZipFile) -> MarkdownArchive:
    markdown_infos = [
        info
        for info in archive.infolist()
        if not _is_directory(info, info.filename.replace("\\", "/"))
        and info.filename.replace("\\", "/").casefold().endswith(".md")
    ]
    if len(markdown_infos) != 1:
        raise TDocZipValidationError(
            "legacy Markdown ZIP must contain exactly one .md member and no manifest"
        )
    try:
        markdown = archive.read(markdown_infos[0]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TDocZipValidationError("legacy Markdown ZIP member is not valid UTF-8") from exc
    return MarkdownArchive(legacy_markdown=markdown)


def read_markdown_archive(raw: bytes) -> MarkdownArchive:
    """Read a versioned multi-document ZIP or return a provenance-free legacy body.

    New ZIPs read Markdown members only through the JSON manifest. Legacy ZIPs
    must have one ``.md`` member. Plain UTF-8 and gzip blobs remain available in
    the separate ``legacy_markdown`` field; the manifest is never returned as
    Markdown.
    """
    if not raw:
        return MarkdownArchive(legacy_markdown="")
    if raw.startswith(_ZIP_MAGIC):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            manifest_infos = [
                info
                for info in infos
                if info.filename.replace("\\", "/") == _MANIFEST_MEMBER
                and not _is_directory(info, info.filename.replace("\\", "/"))
            ]
            if manifest_infos:
                if len(manifest_infos) != 1:
                    raise TDocZipValidationError("Markdown ZIP contains duplicate manifests")
                return _read_versioned_archive(archive, infos, manifest_infos[0])
            return _read_legacy_zip(archive)
    if raw.startswith(_GZIP_MAGIC):
        try:
            markdown = gzip.decompress(raw).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError) as exc:
            raise TDocZipValidationError("legacy gzip Markdown is unreadable") from exc
        return MarkdownArchive(legacy_markdown=markdown)
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TDocZipValidationError("legacy Markdown is not valid UTF-8") from exc
    return MarkdownArchive(legacy_markdown=markdown)


__all__ = [
    "TDocZipLimitError",
    "TDocZipValidationError",
    "build_markdown_archive",
    "document_id_for",
    "enumerate_docx_members",
    "ephemeral_document_id_for",
    "read_markdown_archive",
]
