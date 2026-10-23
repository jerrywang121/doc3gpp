"""Content-store registry and completeness rules for eligible TDoc types.

Pure values only: no ORM, service, or network imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from doc3gpp.models.tdoc_identity import document_id_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from doc3gpp.models.tdoc_cr import TDocExtractMeta
    from doc3gpp.models.tdoc_document import TDocDocument


@dataclass(frozen=True, slots=True)
class TDocContentStore:
    """Storage target for one family of eligible ``tdocs.type`` values."""

    key: str
    table_name: str
    type_values: tuple[str, ...]
    includes_main: bool


TDOC_CONTENT_STORES: tuple[TDocContentStore, ...] = (
    TDocContentStore(
        key="cr",
        table_name="tdoc_cr_annexes",
        type_values=("CR",),
        includes_main=False,
    ),
    TDocContentStore(
        key="wid",
        table_name="tdoc_wid_contents",
        type_values=("WID new", "WID revised"),
        includes_main=True,
    ),
    TDocContentStore(
        key="ls",
        table_name="tdoc_ls_contents",
        type_values=("LS in", "LS out"),
        includes_main=True,
    ),
    TDocContentStore(
        key="discussion",
        table_name="tdoc_discussion_contents",
        type_values=("discussion",),
        includes_main=True,
    ),
)

FAMILY_STORES: tuple[TDocContentStore, ...] = tuple(
    store for store in TDOC_CONTENT_STORES if store.includes_main
)

_STORE_BY_TYPE: dict[str, TDocContentStore] = {
    value.casefold(): store
    for store in TDOC_CONTENT_STORES
    for value in store.type_values
}


def store_for_type(tdoc_type: str | None) -> TDocContentStore | None:
    if not tdoc_type:
        return None
    return _STORE_BY_TYPE.get(tdoc_type.strip().casefold())


def is_eligible_type(tdoc_type: str | None) -> bool:
    return store_for_type(tdoc_type) is not None


def eligible_type_values() -> tuple[str, ...]:
    return tuple(value for store in TDOC_CONTENT_STORES for value in store.type_values)


def is_complete_cr_extraction(
    meta: TDocExtractMeta,
    annexes: Sequence[TDocDocument],
    *,
    has_cover: bool,
) -> bool:
    if meta.extraction_version != 2 or meta.skipped_sources or not has_cover:
        return False
    if meta.document_count != len(annexes):
        return False
    return all(annex.source_file != meta.doc_filename for annex in annexes)


def is_complete_family_extraction(
    meta: TDocExtractMeta,
    documents: Sequence[TDocDocument],
) -> bool:
    if meta.extraction_version != 2 or meta.skipped_sources or not documents:
        return False
    if meta.document_count != len(documents):
        return False
    sources = [document.source_file for document in documents]
    if len(set(sources)) != len(sources):
        return False
    return sources.count(meta.doc_filename) == 1 and sources[0] == meta.doc_filename


def validate_content_bundle(
    store: TDocContentStore,
    meta: TDocExtractMeta,
    documents: Sequence[TDocDocument],
) -> None:
    """Reject a content bundle that cannot be written to ``store`` for ``meta``."""
    if store.includes_main:
        if not documents:
            raise ValueError(f"{store.key} bundle must contain a main document")
        if documents[0].source_file != meta.doc_filename:
            raise ValueError(
                "the first document must be the main document named by doc_filename"
            )
    elif any(document.source_file == meta.doc_filename for document in documents):
        raise ValueError("CR annexes must not include the main document")

    sources = [document.source_file for document in documents]
    if len(set(sources)) != len(sources):
        raise ValueError("content bundle contains duplicate source_file values")
    for document in documents:
        if document.document_id != document_id_for(meta.ftp_url, document.source_file):
            raise ValueError(
                "document_id does not match ftp_url and source_file for "
                f"{document.source_file!r}"
            )
    if meta.document_count != len(documents):
        raise ValueError("document_count must equal the number of documents")
