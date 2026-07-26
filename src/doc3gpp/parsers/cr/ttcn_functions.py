"""TTCN module + function-name extraction helpers for the
`changed_functions` aggregate.

Public surface:
- extract_module_basename(ttcn_module) -> str | None
- extract_function_name(function_name) -> str | None
- extract_changed_functions(required_changes) -> list[str]

The two regexes are user-supplied verbatim. See
`.omo/plans/ttcn-changed-functions.md` v2 §Goal for the rationale
on why the simple greedy pattern handles Unix paths, Windows paths,
and bare basenames uniformly.

Invariant: extractors MUST produce ASCII [A-Za-z0-9_.]+ (case-
insensitive). No whitespace or special characters. The serializer
in storage/repositories/tdoc_cr_ttcn_sql.py uses '\n' as the
column delimiter and relies on this invariant to avoid escaping.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

# Accepts both forward-slash and backslash separators because the
# user-supplied example (Windows-style) only carries backslashes.
_TTCN_MODULE_RE = re.compile(r"(?i)([a-z_0-9]+)(?:\.ttcn)?(?:<br>)?$")
_FUNCTION_NAME_RE = re.compile(
    r"(?i)((?:f_|fl_|fx_|a_|tsc_|cs_|cr_|crs_|cas_|car_|cds_|cdr_|cms_|cmr_|cads_)[a-z_0-9]+)(?:[^a-z_0-9])?"
    r"|"
    r"([a-z_0-9]+_type)(?:[^a-z_0-9])?"
)


def extract_module_basename(ttcn_module: str | None) -> str | None:
    """Return the basename of the TTCN module (without `.ttcn`).

    Accepts:
    - bare basenames: 'NR_DC_Testcases.ttcn' -> 'NR_DC_Testcases'
    - Unix paths: 'ttcn/develop/POS/NR5GC/Foo.ttcn' -> 'Foo'
    - Windows paths: 'ttcn\\develop\\POS\\NR5GC\\Foo.ttcn' -> 'Foo'
    - paths without `.ttcn` extension: 'POS/NR5GC_Functions' -> 'NR5GC_Functions'

    Returns None for blank input or input with no alphanumeric characters
    (e.g., '!!!'). For inputs that contain alphanumeric runs but are not
    module paths (e.g., 'functions with spaces'), returns the rightmost
    alphanumeric run (the regex is intentionally permissive). See Risk #3
    in the plan.
    """
    if not ttcn_module or not ttcn_module.strip():
        return None
    match = _TTCN_MODULE_RE.search(ttcn_module)
    if match is None:
        return None
    return match.group(1)


def extract_function_name(function_name: str | None) -> str | None:
    """Return the function name extracted from the raw input.

    Group 1 captures identifiers with one of the well-known TTCN prefixes
    (f_, fl_, fx_, a_, tsc_, cs_, cr_, crs_, cas_, car_, cds_, cdr_,
    cms_, cmr_, cads_).

    Group 2 is a generic *`_type` fallback for type definitions.

    A trailing non-alphanumeric character (e.g., ' (NR)') is consumed
    by the boundary `(?:[^a-z_0-9])?` but not returned.

    Returns None when neither group matches (e.g., 'Random_Identifier'
    with no known prefix and no `_type` suffix) or input is blank.
    """
    if not function_name or not function_name.strip():
        return None
    match = _FUNCTION_NAME_RE.search(function_name)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def extract_changed_functions(
    required_changes: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Aggregate the sorted, deduplicated set of changed-function entries.

    Aggregation rules per item:

    - Both module and function extract: emit ``"<module>.<function>"``.
    - Only module extracts: emit ``"<module>."`` (trailing-dot sentinel
      marking the function side as missing).
    - Only function extracts: emit ``".<function>"`` (leading-dot sentinel
      marking the module side as missing).
    - Neither extracts: drop the item entirely.

    The dot sentinels are unambiguous in SQL: ``LIKE '%.'`` finds
    module-only entries, ``LIKE '.%'`` finds function-only entries, and
    the dot character inside the joined ``"<module>.<function>"`` form
    remains inert. They are also safe under the column's ``\\n``
    delimiter — the regex extractors produce ``[A-Za-z0-9_.]+`` only,
    so no entry can ever contain ``\\n``.

    The output is sorted lexicographically (Python's default string
    sort, case-sensitive) and deduplicated.
    """
    entries: set[str] = set()
    for item in required_changes:
        module = extract_module_basename(item.get("ttcn_module"))
        function = extract_function_name(item.get("function_name"))
        if module is not None and function is not None:
            entries.add(f"{module}.{function}")
        else:
            if module is not None:
                entries.add(f"{module}.")
            if function is not None:
                entries.add(f".{function}")
    return sorted(entries)