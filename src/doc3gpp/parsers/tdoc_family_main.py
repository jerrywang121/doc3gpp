"""Main-document selection for multi-file TDoc ZIP archives."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

from doc3gpp.parsers.tdoc_zip import _is_directory


class TDocMainDocumentError(ValueError):
    """A TDoc archive has no single unambiguous main .docx member."""


def _basename(member: str) -> str:
    return member.rsplit("/", 1)[-1]


def is_tdoc_member(member: str, prefix: str) -> bool:
    """True when the casefolded basename starts with the TDoc number and no digit follows."""
    basename = _basename(member).casefold()
    if not basename.startswith(prefix):
        return False
    return not basename[len(prefix) : len(prefix) + 1].isdigit()


def select_main_member(
    tdoc_id: str,
    docx_members: Sequence[str],
    other_members: Sequence[str] = (),
) -> str:
    """Return the .docx member that holds the TDoc's main document.

    Candidates are .docx members whose basename starts with ``tdoc_id`` and is
    not continued by a digit, so ``R5-1234567`` is a different TDoc. A lone
    candidate is the main; among several, only the member named exactly
    ``<tdoc_id>.docx`` qualifies. Basenames are compared casefolded.
    """
    if not tdoc_id.strip():
        raise ValueError("tdoc_id must be a non-blank TDoc identifier")
    prefix = tdoc_id.casefold()
    exact_name = f"{prefix}.docx"
    candidates = [member for member in docx_members if is_tdoc_member(member, prefix)]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        exact = [member for member in candidates if _basename(member).casefold() == exact_name]
        if len(exact) == 1:
            return exact[0]
        raise TDocMainDocumentError(
            f"ambiguous main document for {tdoc_id}: {', '.join(candidates)}"
        )
    for member in other_members:
        if is_tdoc_member(member, prefix):
            raise TDocMainDocumentError(f"main document for {tdoc_id} is not .docx: {member}")
    raise TDocMainDocumentError(f"no main .docx for {tdoc_id}")


def member_names(zip_bytes: bytes) -> tuple[str, ...]:
    """Return every non-directory member path, skipping ``__MACOSX`` entries.

    Separators are normalized to ``/`` so basenames are read consistently.
    """
    names: list[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if _is_directory(info, name):
                continue
            if any(part.casefold() == "__macosx" for part in name.split("/")):
                continue
            names.append(name)
    return tuple(names)
