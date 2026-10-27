"""Selection rules shared by the CLI, web, and MCP ``tdoc parse`` surfaces.

Pure helpers: no SQL, network, or ORM imports.
"""

from __future__ import annotations

from doc3gpp.models.tdoc_content_families import eligible_type_values


def exact_tdoc_id(value: str) -> bool:
    """Return ``True`` when ``value`` names one TDoc rather than a pattern."""
    return not value.startswith("!") and not any(char in value for char in "%_*")


def parse_selection_kwargs(
    tdoc_type: str | None,
    *,
    exact_tdoc_id: bool = False,
) -> dict[str, object]:
    """Return the list kwargs that select TDocs for ``tdoc parse``.

    An explicit ``tdoc_type`` is kept as a rich filter and intersected with
    the eligible types. With no type, an exact TDoc id skips the eligibility
    filter so the pipeline can report an ineligible TDoc as skipped.
    """
    if not tdoc_type:
        tdoc_type = None
    eligible_types = None if tdoc_type is None and exact_tdoc_id else eligible_type_values()
    return {"tdoc_type": tdoc_type, "eligible_types": eligible_types}
