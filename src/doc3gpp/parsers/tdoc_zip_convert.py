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


def _convert_member(
    convert: Callable[[bytes, str], str],
    payload: bytes,
    source_file: str,
) -> str:
    """Return a member's Markdown, storing a document with no text as ``""``."""
    try:
        return convert(payload, source_file)
    except RuntimeError as exc:
        if str(exc).startswith(_EMPTY_TEXT_PREFIX):
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
    main = select_main_member(tdoc_id, tuple(payloads), other)

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
    source = _source_name(filename)
    suffix = PurePosixPath(source).suffix.lower()
    if suffix == ".zip":
        return convert_tdoc_zip(
            payload,
            tdoc_id=tdoc_id,
            identity=identity,
            max_documents=max_documents,
            max_uncompressed_bytes=max_uncompressed_bytes,
            convert=convert,
        )
    if suffix != ".docx":
        raise ValueError(
            f"Unsupported extension {suffix!r} for direct-parse; expected .docx or .zip"
        )
    if max_uncompressed_bytes and len(payload) > max_uncompressed_bytes:
        raise TDocZipLimitError(
            "TDoc ZIP aggregate uncompressed byte limit exceeded by actual bytes: "
            f"{len(payload)} bytes > {max_uncompressed_bytes}"
        )
    markdown_text = _convert_member(convert, payload, source)
    return TDocZipConversion(
        main_source_file=source,
        documents=(_document(identity, source, markdown_text, 0),),
        skipped_sources=(),
    )
