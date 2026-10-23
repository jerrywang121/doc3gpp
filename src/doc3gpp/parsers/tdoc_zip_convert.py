"""Shared conversion of TDoc ZIP archives into ordered Markdown documents."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlparse

from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name, ordered_sources
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.parsers.docx_converter import convert_document_to_markdown
from doc3gpp.parsers.tdoc_family_main import member_names, select_main_member
from doc3gpp.parsers.tdoc_zip import TDocZipLimitError, enumerate_docx_members

logger = logging.getLogger(__name__)

# Raised by convert_document_to_markdown when a document has no extractable text.
_EMPTY_TEXT_PREFIX = "python-docx returned empty result for "


def is_empty_text_error(exc: Exception) -> bool:
    """Return whether ``exc`` reports a document with no extractable text."""
    return isinstance(exc, RuntimeError) and str(exc).startswith(_EMPTY_TEXT_PREFIX)


def _convert_member(
    convert: Callable[[bytes, str], str],
    payload: bytes,
    source_file: str,
) -> str:
    """Return a member's Markdown, storing a document with no text as ``""``."""
    try:
        return convert(payload, source_file)
    except RuntimeError as exc:
        if is_empty_text_error(exc):
            return ""
        raise


def _document(
    identity: str,
    source_file: str,
    markdown_text: str,
    file_order: int,
) -> TDocDocument:
    document_id = document_id_for(identity, source_file)
    return TDocDocument(
        document_id=document_id,
        source_file=source_file,
        markdown_file=markdown_member_name(document_id, source_file),
        file_order=file_order,
        markdown_text=markdown_text,
        content_hash=hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class TDocZipConversion:
    """Converted documents from one TDoc archive, main first.

    ``documents`` holds every converted ``.docx`` member: the main document,
    then the annexes in source-path order. ``skipped_sources`` names annexes
    whose conversion failed; those are logged and not returned as documents.
    """

    main_source_file: str
    documents: tuple[TDocDocument, ...]
    skipped_sources: tuple[str, ...]


def _select_zip_main(
    zip_bytes: bytes,
    *,
    tdoc_id: str,
    max_documents: int,
    max_uncompressed_bytes: int,
) -> tuple[str, dict[str, bytes]]:
    """Return the main member's name and the payload of every ``.docx`` member."""
    payloads = dict(
        enumerate_docx_members(
            zip_bytes,
            max_documents=max_documents,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
    )
    other = tuple(
        name.rsplit("/", 1)[-1]
        for name in member_names(zip_bytes)
        if not name.casefold().endswith(".docx")
    )
    return select_main_member(tdoc_id, tuple(payloads), other), payloads


def convert_tdoc_zip(
    zip_bytes: bytes,
    *,
    tdoc_id: str,
    identity: str,
    max_documents: int,
    max_uncompressed_bytes: int,
    convert: Callable[[bytes, str], str] = convert_document_to_markdown,
) -> TDocZipConversion:
    """Convert a TDoc ZIP's ``.docx`` members without touching disk or storage.

    A main conversion failure propagates. An annex conversion failure is logged
    as a warning and recorded in ``skipped_sources``.
    """
    main, payloads = _select_zip_main(
        zip_bytes,
        tdoc_id=tdoc_id,
        max_documents=max_documents,
        max_uncompressed_bytes=max_uncompressed_bytes,
    )

    converted: list[tuple[str, str]] = [(main, _convert_member(convert, payloads[main], main))]
    skipped: list[str] = []
    for source_file in ordered_sources(tuple(payloads), main)[1:]:
        try:
            markdown_text = _convert_member(convert, payloads[source_file], source_file)
        except Exception as exc:  # noqa: BLE001 - one unreadable annex must not fail the archive
            logger.warning("Skipping TDoc annex %r: conversion failed: %s", source_file, exc)
            skipped.append(source_file)
            continue
        converted.append((source_file, markdown_text))

    documents = [
        _document(identity, source_file, markdown_text, file_order)
        for file_order, (source_file, markdown_text) in enumerate(converted)
    ]

    return TDocZipConversion(
        main_source_file=main,
        documents=tuple(documents),
        skipped_sources=tuple(skipped),
    )


def _source_name(filename: str) -> str:
    parsed = urlparse(filename)
    path = parsed.path if parsed.scheme else filename
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _direct_source(filename: str) -> tuple[str, str]:
    """Return the source basename and its lower-case extension, rejecting other types."""
    source = _source_name(filename)
    suffix = PurePosixPath(source).suffix.lower()
    if suffix not in (".zip", ".docx"):
        raise ValueError(
            f"Unsupported extension {suffix!r} for direct-parse; expected .docx or .zip"
        )
    return source, suffix


def _check_docx_limit(payload: bytes, max_uncompressed_bytes: int) -> None:
    if max_uncompressed_bytes and len(payload) > max_uncompressed_bytes:
        raise TDocZipLimitError(
            "TDoc ZIP aggregate uncompressed byte limit exceeded by actual bytes: "
            f"{len(payload)} bytes > {max_uncompressed_bytes}"
        )


def convert_direct_payload(
    payload: bytes,
    *,
    filename: str,
    tdoc_id: str,
    identity: str,
    max_documents: int,
    max_uncompressed_bytes: int,
    convert: Callable[[bytes, str], str] = convert_document_to_markdown,
) -> TDocZipConversion:
    """Convert a direct-mode ``.zip`` or standalone ``.docx`` payload without I/O.

    A ZIP follows :func:`convert_tdoc_zip`. A standalone ``.docx`` is its own
    main document and has no annexes.
    """
    source, suffix = _direct_source(filename)
    if suffix == ".zip":
        return convert_tdoc_zip(
            payload,
            tdoc_id=tdoc_id,
            identity=identity,
            max_documents=max_documents,
            max_uncompressed_bytes=max_uncompressed_bytes,
            convert=convert,
        )
    _check_docx_limit(payload, max_uncompressed_bytes)
    markdown_text = _convert_member(convert, payload, source)
    return TDocZipConversion(
        main_source_file=source,
        documents=(_document(identity, source, markdown_text, 0),),
        skipped_sources=(),
    )


def convert_direct_main_markdown(
    payload: bytes,
    *,
    filename: str,
    tdoc_id: str,
    max_documents: int,
    max_uncompressed_bytes: int,
    convert: Callable[[bytes, str], str] = convert_document_to_markdown,
) -> str:
    """Return only the main document's Markdown, for TDoc type sniffing.

    Annexes are never converted here. A ZIP's main is selected by name exactly
    as in :func:`convert_tdoc_zip`; a standalone ``.docx`` is its own main.
    """
    source, suffix = _direct_source(filename)
    if suffix == ".zip":
        main, payloads = _select_zip_main(
            payload,
            tdoc_id=tdoc_id,
            max_documents=max_documents,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        return _convert_member(convert, payloads[main], main)
    _check_docx_limit(payload, max_uncompressed_bytes)
    return _convert_member(convert, payload, source)
