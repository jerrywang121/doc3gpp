"""Shared conversion of TDoc ZIP archives into ordered Markdown documents."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from doc3gpp.models.tdoc_document import TDocDocument, markdown_member_name, ordered_sources
from doc3gpp.models.tdoc_identity import document_id_for
from doc3gpp.parsers.docx_converter import convert_document_to_markdown
from doc3gpp.parsers.tdoc_family_main import member_names, select_main_member
from doc3gpp.parsers.tdoc_zip import enumerate_docx_members

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

    documents = []
    for file_order, (source_file, markdown_text) in enumerate(converted):
        document_id = document_id_for(identity, source_file)
        documents.append(
            TDocDocument(
                document_id=document_id,
                source_file=source_file,
                markdown_file=markdown_member_name(document_id, source_file),
                file_order=file_order,
                markdown_text=markdown_text,
                content_hash=hashlib.sha256(markdown_text.encode("utf-8")).hexdigest(),
            )
        )

    return TDocZipConversion(
        main_source_file=main,
        documents=tuple(documents),
        skipped_sources=tuple(skipped),
    )
