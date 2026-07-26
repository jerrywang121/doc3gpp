"""Unit tests for :mod:`doc3gpp.parsers.cr.ttcn_functions`.

The two regexes that drive the ``changed_functions`` aggregate column
on ``tdoc_cr_ttcn_details`` are user-supplied verbatim — see
`.omo/plans/ttcn-changed-functions.md` v2 §Goal. These tests pin the
regex contract on the public surface:

* :func:`extract_module_basename` — strips path separators and the
  optional ``.ttcn`` extension from a ``ttcn_module`` value.
* :func:`extract_function_name` — recognises the canonical TTCN
  function-name prefixes (``f_``, ``fl_``, ``fx_``, ``a_``, ``tsc_``,
  ``cs_``, ``cr_``, ``crs_``, ``cas_``, ``car_``, ``cds_``, ``cdr_``,
  ``cms_``, ``cmr_``, ``cads_``) plus a ``*_type`` fallback.
* :func:`extract_changed_functions` — composes the two helpers into a
  sorted, deduplicated ``list[str]`` of ``"<module>.<function>"``
  entries; skips degraded records (missing ``ttcn_module`` or
  ``function_name``).
"""

from __future__ import annotations

import pytest

from doc3gpp.parsers.cr.ttcn_functions import (
    extract_changed_functions,
    extract_function_name,
    extract_module_basename,
)


# ---------------------------------------------------------------------------
# extract_module_basename
# ---------------------------------------------------------------------------


def test_extract_module_basename_bare_name() -> None:
    """A bare basename without separators strips the ``.ttcn`` extension."""
    assert extract_module_basename("NR5GC_Test.ttcn") == "NR5GC_Test"


def test_extract_module_basename_unix_path() -> None:
    """Forward-slash paths extract the rightmost alphanumeric run."""
    assert (
        extract_module_basename("ttcn/develop/POS/NR5GC/NR5GC_Positioning_Functions.ttcn")
        == "NR5GC_Positioning_Functions"
    )


def test_extract_module_basename_windows_path() -> None:
    """Backslash paths extract the rightmost alphanumeric run.

    The regex itself is separator-agnostic — backslashes are ordinary
    characters inside ``[a-z_0-9]+``'s complement; what matters is the
    rightmost alphanumeric run ending the string.
    """
    assert (
        extract_module_basename("ttcn\\develop\\POS\\NR5GC\\NR5GC_Positioning_Functions.ttcn")
        == "NR5GC_Positioning_Functions"
    )


def test_extract_module_basename_without_extension() -> None:
    """The ``(?:\.ttcn)?`` segment is optional — bare basenames without
    an extension still resolve to the rightmost alphanumeric run."""
    assert extract_module_basename("POS/NR5GC_Functions") == "NR5GC_Functions"


def test_extract_module_basename_returns_none_for_blank() -> None:
    """Blank inputs (empty string, ``None``, whitespace-only) yield ``None``."""
    assert extract_module_basename("") is None
    assert extract_module_basename(None) is None
    assert extract_module_basename("   ") is None


def test_extract_module_basename_garbage_input() -> None:
    """The regex is intentionally permissive (Risk #3 in the plan).

    * ``"!!!"`` → ``None`` (no alphanumeric characters at all)
    * ``"functions with spaces and no slashes"`` → ``"slashes"`` (the
      rightmost alphanumeric run). The aggregator
      :func:`extract_changed_functions` skips entries whose function
      name also fails to extract, so a garbage module basename alone
      never produces a bogus ``changed_functions`` row.
    """
    assert extract_module_basename("!!!") is None
    assert (
        extract_module_basename("functions with spaces and no slashes")
        == "slashes"
    )


# ---------------------------------------------------------------------------
# extract_function_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prefix",
    [
        "f_",
        "fl_",
        "fx_",
        "a_",
        "tsc_",
        "cs_",
        "cr_",
        "crs_",
        "cas_",
        "car_",
        "cds_",
        "cdr_",
        "cms_",
        "cmr_",
        "cads_",
    ],
)
def test_extract_function_name_known_prefix(prefix: str) -> None:
    """Every canonical TTCN function prefix produces an exact match."""
    raw = f"{prefix}POS_NR_StoreReportedUTCModelSupport"
    assert extract_function_name(raw) == raw


def test_extract_function_name_with_trailing_signature() -> None:
    """A trailing non-alphanumeric fragment (e.g., ``" (NR)"``) is consumed
    but not returned — the boundary ``(?:[^a-z_0-9])?`` swallows it."""
    assert (
        extract_function_name("f_POS_SelectTemplateReq_GenericAssistData (NR)")
        == "f_POS_SelectTemplateReq_GenericAssistData"
    )


def test_extract_function_name_type_fallback() -> None:
    """A bare identifier ending in ``_type`` matches group 2 of the regex
    (the ``*_type`` fallback for type definitions)."""
    assert extract_function_name("some_template_type") == "some_template_type"


def test_extract_function_name_returns_none_for_unknown_prefix() -> None:
    """An identifier without a known prefix and without a ``_type`` suffix
    yields ``None``."""
    assert extract_function_name("Random_Identifier") is None


def test_extract_function_name_returns_none_for_blank() -> None:
    """Blank inputs (empty string, ``None``, whitespace-only) yield ``None``."""
    assert extract_function_name("") is None
    assert extract_function_name(None) is None
    assert extract_function_name("   ") is None


# ---------------------------------------------------------------------------
# extract_changed_functions — aggregation
# ---------------------------------------------------------------------------


def test_extract_changed_functions_dedupes_and_sorts() -> None:
    """Two corrections in the same module collapse to one entry; entries
    across distinct modules sort lexicographically."""
    corrections = [
        {
            "function_name": "fl_TC_Body",
            "ttcn_module": "Beta_Test.ttcn",
        },
        {
            "function_name": "fl_TC_Body",
            "ttcn_module": "Beta_Test.ttcn",
        },
        {
            "function_name": "fl_TC_Body",
            "ttcn_module": "Alpha_Test.ttcn",
        },
    ]
    result = extract_changed_functions(corrections)
    assert result == [
        "Alpha_Test.fl_TC_Body",
        "Beta_Test.fl_TC_Body",
    ]


def test_extract_changed_functions_partial_extraction_includes_extracted_piece() -> None:
    """Partial extraction emits the available piece with a dot sentinel:
    ``"<module>."`` when only the module basename extracted,
    ``".<function>"`` when only the function name extracted. The
    sentinel is unambiguous in SQL (``LIKE '%.'`` finds module-only,
    ``LIKE '.%'`` finds function-only) and stays inert under the
    ``\\n`` column delimiter.
    """
    corrections = [
        {
            "function_name": "fl_TC_Body",
            "ttcn_module": "NR5GC_Test.ttcn",
        },
        {
            "function_name": "fl_TC_Other",
            "summary_of_change": "missing module",
        },
        {
            "ttcn_module": "NR5GC_Test.ttcn",
            "reason_for_change": "missing function",
        },
    ]
    result = extract_changed_functions(corrections)
    assert result == [
        ".fl_TC_Other",
        "NR5GC_Test.",
        "NR5GC_Test.fl_TC_Body",
    ]


def test_extract_changed_functions_drops_only_when_both_fail() -> None:
    corrections = [
        {
            "function_name": "Random_Identifier",
            "ttcn_module": "!!!",
        },
        {
            "function_name": "fl_TC_Body",
            "ttcn_module": "NR5GC_Test.ttcn",
        },
    ]
    result = extract_changed_functions(corrections)
    assert result == ["NR5GC_Test.fl_TC_Body"]


def test_extract_changed_functions_handles_windows_paths() -> None:
    """End-to-end: Windows-style backslash paths and trailing function
    signatures produce the user's spec example output (sorted,
    deduplicated, ``"<module>.<function>"``)."""
    corrections = [
        {
            "function_name": "f_POS_NR_StoreReportedUTCModelSupport",
            "ttcn_module": "ttcn\\develop\\POS\\NR5GC\\NR5GC_Positioning_Functions.ttcn",
        },
        {
            "function_name": "f_POS_SelectTemplateReq_GenericAssistData (NR)",
            "ttcn_module": "ttcn\\develop\\POS\\NR5GC\\NR5GC_Positioning_Functions.ttcn",
        },
    ]
    result = extract_changed_functions(corrections)
    assert result == [
        "NR5GC_Positioning_Functions.f_POS_NR_StoreReportedUTCModelSupport",
        "NR5GC_Positioning_Functions.f_POS_SelectTemplateReq_GenericAssistData",
    ]
