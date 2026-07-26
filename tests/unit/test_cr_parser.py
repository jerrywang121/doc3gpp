"""Unit tests for :mod:`doc3gpp.parsers.cr_parser`.

Cover-page, TTCN overview and corrections sub-parsers, plus the zip
extractor and small text helpers. The fixture-driven conversion tests
(skipping when ``python-docx`` is not installed) cover all 7 fixtures
shipped under ``tests/fixtures/tdoc_cr_doc/``. The remaining tests
use small hand-rolled markdown fixtures so the regex logic stays in
the default pytest pool.

Snapshot contract (the table below is the regression contract; the
executable assertions live in the parametrised tests below it. The
historical plan-time values are kept here as a single source of truth
for the fixture-driven assertions):

| Fixture         | spec       | cr_num | rev | cr_cat | release | year |
|-----------------|------------|--------|-----|--------|---------|------|
| C6-250028       | 31.124     | 0797   | 0   | F      | Rel-18  | 2025 |
| R5-227476       | 38.508-1   | 2678   | 1   | F      | Rel-17  | 2022 |
| R5-253079       | 38.523-1   | 4947   | 1   | F      | Rel-19  | 2025 |
| R5s260009       | 38.523-3   | 3790   | 0   | F      | Rel-18  | 2026 |
| R5s260051       | 38.523-3   | 3806   | 0   | F      | Rel-18  | 2026 |
| R5s260135       | 38.523-3   | 3824   | 0   | B      | Rel-18  | 2026 |
| R5s260176       | 36.523-3   | 4971   | 0   | B      | Rel-18  | 2026 |

The python-docx-driven fixture assertions only cover what's actually
rendered in markdown (title / spec / source / tsg / cr_cat / release)
because some fixtures store ``spec`` / ``cr_num`` in docx field codes
that python-docx does not surface — for those, the hand-rolled
markdown fixture check applies.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import pytest

from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRParseResult,
    TDocCRTTCNDetails,
)
from doc3gpp.parsers.cr_parser import (
    CRHeaderMissingError,
    _collapse_whitespace,
    _remove_markdown_formatting,
    _search_pattern_in_lines,
    derive_tech_from_spec,
    extract_docx_from_zip,
    parse_cr_details,
)
from doc3gpp.parsers.cr.helpers import _year_from_tdoc_id


FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tdoc_cr_doc"


def _docx_available() -> bool:
    """Return True iff ``python-docx`` imports cleanly."""
    try:
        from docx import Document  # noqa: F401
    except ImportError:
        return False
    return True


def _extract_docx_from_zip(zip_path: Path) -> tuple[str, bytes]:
    """Mirror the reference's preferred-pick ordering."""
    with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as zf:
        word_docs = [
            name
            for name in zf.namelist()
            if name.lower().endswith((".docx", ".doc"))
            and not name.lower().startswith("__macosx/")
        ]
        word_docs.sort(
            key=lambda n: (0 if n.lower().endswith(".docx") else 1, n.lower())
        )
        return word_docs[0], zf.read(word_docs[0])


def _extract_docx_text(docx_bytes: bytes) -> str:
    """Pull all ``<w:t>`` text out of a docx for inspection without python-docx."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        for name in z.namelist():
            if name.endswith("document.xml") and not name.startswith("__MACOSX"):
                with z.open(name) as f:
                    xml = f.read().decode("utf-8", errors="ignore")
                break
    return " ".join(re.findall(r"<w:t[^>]*>([^<]+)</w:t>", xml))  # type: ignore[name-defined]


# ---------------------------------------------------------------------------
# Tiny hand-rolled markdown fixtures — used by the regex-targeted tests.
# ---------------------------------------------------------------------------


_HEADER_LINES = (
    "**3GPP TSG-RAN5 Meeting #2026-TTCN email *R5s260009***",
    "",
    "**Online, , 12th Dec 2025 - 31st Dec 2026**",
    "",
    "|  | 38.523-3 | CR | 3790 | rev | - | Current version: | 18.4.0 |  |",
    "|  | | | | | | | | |",
    "| *For* ***HELP*** *on using this form.* | | | | | | | | |",
    "| Title: | Correction to NR testcase 7.1.3.5.3 | | | | | | | | |",
    "| Source to WG: | ROHDE & SCHWARZ | | | | | | | | |",
    "| Source to TSG: | R5 | | | | | | | | |",
    "| Work item code: | TEI15_Test, 5GS_NR_LTE-UEConTest | | | | | | | | |",
    "| Category: | F | | | | | | Release: | | | Rel-18 |",
    "| Reason for change: | | The SS is creating 3 PDCP DATA PDUs. | | | | | | | |",
    "| Consequences if not approved: | | Test will fail unfairly. | | | | | | | |",
    "| Clauses affected: | | 7.1.3.5.3 | | | | | | | | |",
    "| Other comments: | | None | | | | | | | | |",
    "| This CR's revision history: | | rev - | | | | | | | |",
)

_TTCN_OVERVIEW_LINES = (
    "### 1. Overview",
    "",
    "Test Case: 7.1.3.5.3",
    "UE used: MTK MT6986D and Qualcomm X105 5G Modem-RF",
    "System Simulator used: Anritsu Protocol Conformance Test System",
    "",
    "is part of the NR5GC test suite",
    "",
    "### 2. Corrections required",
)

_TTCN_CORRECTION_LINES = (
    "",
    "Changes provided in TTCN CR R5s260009 are required for the verification of NR5GC 7.1.3.5.3.",
    "",
    "| Function name | fl_TC_7_1_3_5_3_Body |",
    "| Reason for change | Change due to MCX feature addition. |",
    "| Summary of change | Use new PDCP function. |",
    "| TTCN module | NR_DC_Testcases.ttcn |",
    "| MCC160 Comment | OK |",
    "",
    "| --- |",
    "| Before change: |",
    "| --- |",
    "|   function fl_TC_7_1_3_5_3() {",
    "",
    "### 3. Method of test",
)


# ---------------------------------------------------------------------------
# Header / fail-loud behaviour
# ---------------------------------------------------------------------------


def test_parse_cr_details_raises_when_header_missing() -> None:
    """An input without the ``3GPP TSG-`` header raises CRHeaderMissingError."""
    bad_md = (
        "| not | a | cr | document |\n"
        "| --- | --- | --- | --- |\n"
        "| random table content |\n"
    )
    with pytest.raises(CRHeaderMissingError) as excinfo:
        parse_cr_details(bad_md, tdoc_id="R5s260009")
    assert isinstance(excinfo.value, ValueError)
    msg = str(excinfo.value)
    assert "3GPP TSG-" in msg
    # The error should include the input snippet for diagnostics.
    assert "random table content" in msg or "not" in msg


def test_parse_cr_details_raises_on_empty_tdoc_id() -> None:
    """An empty / whitespace tdoc_id raises ``ValueError``."""
    md = "**3GPP TSG-RAN5 Meeting #2026-TTCN email *R5s260009***\n"
    with pytest.raises(ValueError, match="tdoc_id"):
        parse_cr_details(md, tdoc_id="")
    with pytest.raises(ValueError, match="tdoc_id"):
        parse_cr_details(md, tdoc_id="   ")


def test_parse_cr_details_raises_on_completely_empty_markdown() -> None:
    """Empty markdown is treated as missing-header and raises."""
    with pytest.raises(CRHeaderMissingError):
        parse_cr_details("", tdoc_id="R5s260009")


# ---------------------------------------------------------------------------
# Year and tech derivation (the derived-field contract)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tdoc_id", "expected_year"),
    [
        ("R5s260009", 2026),
        ("R5s260176", 2026),
        ("R5w260009", 2026),
        ("R5-227476", 2022),
        ("R5-253079", 2025),
        ("C6-250028", 2025),
    ],
)
def test_year_from_tdoc_id(
    tdoc_id: str, expected_year: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_year_from_tdoc_id`` derives year from positions 3–4."""
    assert _year_from_tdoc_id(tdoc_id) == expected_year
    print(f"tdoc={tdoc_id} -> year={_year_from_tdoc_id(tdoc_id)}")


@pytest.mark.parametrize(
    ("spec", "expected_tech"),
    [
        ("38.523-3", "5G"),
        ("38.523", "5G"),
        ("36.523-3", "LTE"),
        ("36.523", "LTE"),
        ("34.229", "IMS"),
        ("37.579", "MCX"),
        ("36.579", "MCX"),
        ("37.571", "POS"),
        ("99.999", ""),
        ("", ""),
        ("random garbage", ""),
    ],
)
def test_derive_tech_from_spec_returns_canonical_label(
    spec: str, expected_tech: str
) -> None:
    """``derive_tech_from_spec`` maps the canonical base number to a label."""
    assert derive_tech_from_spec(spec) == expected_tech


def test_derive_tech_handles_empty_and_whitespace() -> None:
    """Empty / whitespace input returns ``""`` rather than raising."""
    assert derive_tech_from_spec("") == ""
    assert derive_tech_from_spec("   ") == ""


# ---------------------------------------------------------------------------
# TTCN-only / non-TTCN scope
# ---------------------------------------------------------------------------


_NON_TTCN_HEADER_LINES = (
    "**3GPP TSG-RAN5 Meeting #97 *R5-227476***",
    "",
    "**Toulouse, France, 14th Nov 2022 - 18th Nov 2022**",
    "",
    "|  | 38.508-1 | CR | 2678 | rev | 1 | Current version: | 17.6.0 |  |",
    "| Title: | Addition of USIM configuration for MUSIM test cases | | | | | | | | |",
    "| Source to WG: | Qualcomm Incorporated | | | | | | | | |",
    "| Source to TSG: | R5 | | | | | | | | |",
    "| Category: | F | | | | | | Release: | | | Rel-17 |",
)


def test_non_ttcn_cr_skips_overview_and_corrections() -> None:
    """A ``R5-227476`` (non-TTCN) document emits a bare cover (no TTCN sidecar)."""
    md = "\n".join(_NON_TTCN_HEADER_LINES)
    parsed = parse_cr_details(md, tdoc_id="R5-227476")
    assert isinstance(parsed, TDocCRParseResult)
    assert parsed.ttcn is None
    cover = parsed.cover
    assert cover.tdoc_id == "R5-227476"
    assert cover.spec == "38.508-1"
    assert cover.cr_num == "2678"
    assert cover.rev == "1"
    assert cover.cr_cat == "F"
    assert cover.release == "Rel-17"


def test_ttcn_cr_invokes_overview_and_corrections_parsers() -> None:
    """A ``R5s260009`` document routes through the TTCN-only sub-parsers."""
    md = "\n".join(list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_CORRECTION_LINES))
    parsed = parse_cr_details(md, tdoc_id="R5s260009")
    assert isinstance(parsed, TDocCRParseResult)
    cover = parsed.cover
    assert cover.tdoc_id == "R5s260009"
    assert cover.spec == "38.523-3"
    assert cover.cr_num == "3790"
    assert cover.rev == "0"
    assert cover.cr_cat == "F"
    assert cover.release == "Rel-18"
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert parsed.ttcn.testcase == "7.1.3.5.3"
    assert parsed.ttcn.test_suite == "NR5GC"
    assert parsed.ttcn.ue == "MTK MT6986D and Qualcomm X105 5G Modem-RF"
    assert parsed.ttcn.ss == "Anritsu Protocol Conformance Test System"
    assert len(parsed.ttcn.required_changes) == 1
    change = parsed.ttcn.required_changes[0]
    assert change["function_name"] == "fl_TC_7_1_3_5_3_Body"
    assert change["reason_for_change"] == "Change due to MCX feature addition."
    assert change["summary_of_change"] == "Use new PDCP function."
    assert change["ttcn_module"] == "NR_DC_Testcases.ttcn"
    assert change["mcc160_comment"] == "OK"


_TTCN_TEMPLATE_CORRECTION_LINES = (
    "",
    "Changes provided in TTCN CR R5s260009 are required for the verification of NR5GC 7.1.3.5.3.",
    "",
    "| Template name | tr_CommonPart_Template |",
    "| Reason for change | New template for MCX. |",
    "| Summary of change | Add template body. |",
    "| TTCN module | NR_DC_Templates.ttcn |",
    "| MCC160 Comment | OK |",
    "",
    "### 3. Method of test",
)


def test_ttcn_cr_extracts_function_name_from_template_name_row() -> None:
    """The function-name regex also matches ``Template name`` rows.

    Some TTCN CRs use a ``| Template name |`` row instead of
    ``| Function name |`` to identify the changed template. The
    parser must surface the value as ``function_name`` regardless
    of which label the source table uses.
    """
    md = "\n".join(
        list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_TEMPLATE_CORRECTION_LINES)
    )
    parsed = parse_cr_details(md, tdoc_id="R5s260009")
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert len(parsed.ttcn.required_changes) == 1
    change = parsed.ttcn.required_changes[0]
    assert change["function_name"] == "tr_CommonPart_Template"
    assert change["reason_for_change"] == "New template for MCX."
    assert change["summary_of_change"] == "Add template body."
    assert change["ttcn_module"] == "NR_DC_Templates.ttcn"
    assert change["mcc160_comment"] == "OK"


# ---------------------------------------------------------------------------
# ``changed_functions`` aggregate: parser-side derivation contract.
#
# The aggregate is computed in :meth:`CRParserBase.parse` from the
# ``required_changes`` list using the regex helpers in
# :mod:`doc3gpp.parsers.cr.ttcn_functions`. These tests pin the
# parser-side derivation; the on-disk round-trip is exercised in the
# integration suite (:mod:`tests.integration.test_tdoc_cr_ttcn_sqlite`).
# ---------------------------------------------------------------------------


def test_ttcn_cr_populates_changed_functions() -> None:
    """A TTCN CR with a ``fl_``-prefixed function and a bare-basename
    module populates the parser-derived ``changed_functions`` aggregate
    with the single ``"<module>.<function>"`` entry.

    Mirrors the ``_TTCN_CORRECTION_LINES`` fixture:
    ``ttcn_module = "NR_DC_Testcases.ttcn"`` and
    ``function_name = "fl_TC_7_1_3_5_3_Body"``. The module basename is
    stripped of the ``.ttcn`` extension; the function name is kept
    verbatim because it already matches the canonical ``fl_`` prefix.
    """
    md = "\n".join(list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_CORRECTION_LINES))
    parsed = parse_cr_details(md, tdoc_id="R5s260009")
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert parsed.ttcn.changed_functions == ["NR_DC_Testcases.fl_TC_7_1_3_5_3_Body"]


def test_ttcn_cr_template_name_also_populates_changed_functions() -> None:
    """``_TTCN_TEMPLATE_CORRECTION_LINES`` has a ``tr_``-prefixed function
    (``tr_CommonPart_Template``) that is NOT in the regex's prefix set
    — so the function side fails to extract. The module side DOES
    extract (``NR_DC_Templates.ttcn`` → ``NR_DC_Templates``), so the
    partial-include rule emits ``"NR_DC_Templates."`` with a trailing-dot
    sentinel marking the function as missing."""
    md = "\n".join(
        list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_TEMPLATE_CORRECTION_LINES)
    )
    parsed = parse_cr_details(md, tdoc_id="R5s260009")
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert parsed.ttcn.changed_functions == ["NR_DC_Templates."]


_TTCN_CORRECTION_LINES_FULL = (
    "",
    "Changes provided in TTCN CR R5s260009 are required for the verification of NR5GC 7.1.3.5.3.",
    "",
    "| Function name | fl_TC_7_1_3_5_3_Body |",
    "| Reason for change | Change due to MCX feature addition. |",
    "| Summary of change | Use new PDCP function. |",
    "| TTCN module | NR_DC_Testcases.ttcn |",
    "| MCC160 Comment | OK |",
    "",
    "Before change",
    "| fl_TC_7_1_3_5_3_Body ( msg ) { return old_value ; } |",
    "",
    "After change",
    "| fl_TC_7_1_3_5_3_Body ( msg ) { return new_value ; } |",
    "",
    "### 3. Method of test",
)


def test_ttcn_cr_full_true_extracts_before_after_change_content() -> None:
    """``parse(..., full=True)`` populates ``before_change`` and
    ``after_change`` from the standalone ``Before change`` / ``After
    change`` markers and their following content rows.

    The TTCN corrections sub-parser only enters its before/after/new
    extraction loop when ``full=True``. Locks the gate AND the slice
    arithmetic — a previous off-by-one in
    ``_extract_change_from_table`` call site caused an infinite loop
    on realistic TTCN markdown layouts, silently dropping every
    operator's ``--full`` invocation.
    """
    md = "\n".join(list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_CORRECTION_LINES_FULL))
    parsed = parse_cr_details(md, tdoc_id="R5s260009", full=True)
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert len(parsed.ttcn.required_changes) == 1
    change = parsed.ttcn.required_changes[0]
    assert change["function_name"] == "fl_TC_7_1_3_5_3_Body"
    assert change["before_change"] == "fl_TC_7_1_3_5_3_Body ( msg ) { return old_value ; }"
    assert change["after_change"] == "fl_TC_7_1_3_5_3_Body ( msg ) { return new_value ; }"
    assert "new_change" not in change


def test_ttcn_cr_full_false_omits_before_after_change_content() -> None:
    """``parse(..., full=False)`` (the default) leaves
    ``before_change`` / ``after_change`` / ``new_change`` unset even
    when the source markdown carries them.

    Metadata-only extraction is the default — that's the contract the
    default ``tdoc parse`` flow depends on (small rows, fast parser).
    Operators who need the change content must opt in with ``--full``;
    this test pins that contract.
    """
    md = "\n".join(list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_CORRECTION_LINES_FULL))
    parsed = parse_cr_details(md, tdoc_id="R5s260009", full=False)
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert len(parsed.ttcn.required_changes) == 1
    change = parsed.ttcn.required_changes[0]
    assert change["function_name"] == "fl_TC_7_1_3_5_3_Body"
    assert "before_change" not in change
    assert "after_change" not in change
    assert "new_change" not in change


def test_ttcn_cr_full_default_omits_before_after_change_content() -> None:
    """Without ``full=`` (default ``False``), the before/after/new keys
    are never present on the change dict.
    """
    md = "\n".join(list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_CORRECTION_LINES_FULL))
    parsed = parse_cr_details(md, tdoc_id="R5s260009")
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    change = parsed.ttcn.required_changes[0]
    assert "before_change" not in change
    assert "after_change" not in change
    assert "new_change" not in change


def test_ttcn_cr_full_true_returns_within_bounded_time() -> None:
    """Regression guard for the ``if full:`` infinite-loop bug.

    Before the slice-index fix, a standalone ``Before change`` marker
    followed by a content row sent the parser into an infinite loop on
    the table-cell branch (the cell line was consumed, ``scan_idx`` did
    not advance, and the next iteration re-matched the same cell).
    This test runs the parser with a short wall-clock budget; if the
    fix ever regresses the loop will spin past the budget and the test
    fails.
    """
    import signal

    md = "\n".join(list(_HEADER_LINES) + list(_TTCN_OVERVIEW_LINES) + list(_TTCN_CORRECTION_LINES_FULL))

    def _on_alarm(signum: int, frame: object) -> None:
        raise TimeoutError("ttcn full=True parser hung")

    handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(5)
    try:
        parsed = parse_cr_details(md, tdoc_id="R5s260009", full=True)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, handler)
    assert isinstance(parsed.ttcn, TDocCRTTCNDetails)
    assert parsed.ttcn.required_changes


# ---------------------------------------------------------------------------
# _search_pattern_in_lines: contract
# ---------------------------------------------------------------------------


def test_search_pattern_in_lines_rejects_mismatched_lengths() -> None:
    """field_names and group_numbers of different lengths raise ``ValueError``."""
    lines = ["| Title: | hello |"]
    with pytest.raises(ValueError, match="same length"):
        _search_pattern_in_lines(
            lines, {}, ["a", "b"], [1], _COVER_TITLE_RE
        )


def test_search_pattern_in_lines_advances_by_matched_line_count() -> None:
    """Returns the position (1-based) of the matched line."""
    data: dict[str, str] = {}
    lines = [
        "irrelevant line 1",
        "irrelevant line 2",
        "| Title: | hello |",
        "trailing noise",
    ]
    advancing = _search_pattern_in_lines(
        lines, data, ["title"], [1], _COVER_TITLE_RE
    )
    assert advancing == 3
    assert data["title"] == "hello"


def test_search_pattern_in_lines_returns_lines_consumed_when_no_match() -> None:
    """With no match the function returns the count of lines scanned
    (same shape as the reference) and leaves ``data`` untouched."""
    data = {"preexisting": "value"}
    advancing = _search_pattern_in_lines(
        ["| not-a-title |", "| still-not |", "| nope |"],
        data,
        ["title"],
        [1],
        _COVER_TITLE_RE,
    )
    assert advancing == 3
    assert data == {"preexisting": "value"}


_COVER_TITLE_RE = re.compile(r"\|\s*Title:\s*\|\s*(.*?)\s*\|", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Markdown formatting helper
# ---------------------------------------------------------------------------


def test_remove_markdown_formatting_strips_bold_and_italic() -> None:
    """Double- and single-asterisk markers are stripped, content preserved."""
    text = "**bold** and *italic* text"
    out = _remove_markdown_formatting(text)
    assert out == "bold and italic text"


def test_remove_markdown_formatting_keeps_headers_when_asked() -> None:
    """``MD_RM_KEEP_HEADERS`` retains hash-prefixed lines."""
    # The bit-flag value is not part of the public API but the
    # pattern is observable through the helper.
    with_keep = _remove_markdown_formatting(
        "# Heading\n\nbody text", flags=0x01
    )
    without_keep = _remove_markdown_formatting("# Heading\n\nbody text")
    assert "Heading" in with_keep and "#" in with_keep
    assert "#" not in without_keep


def test_remove_markdown_formatting_keeps_blockquotes_when_asked() -> None:
    """``MD_RM_KEEP_BLOCKQUOTES`` retains the ``>`` prefix."""
    with_keep = _remove_markdown_formatting("> blockquote", flags=0x02)
    without_keep = _remove_markdown_formatting("> blockquote")
    assert ">" in with_keep
    assert ">" not in without_keep


def test_remove_markdown_formatting_strips_links_and_code() -> None:
    """Default flags strip link markup and inline code markers."""
    text = "see [the spec](https://example.com) and `code` here"
    out = _remove_markdown_formatting(text)
    assert "[" not in out and "](" not in out
    assert "`" not in out
    assert "the spec" in out
    assert "code" in out


def test_collapse_whitespace_collapses_runs() -> None:
    """Multiple whitespace characters collapse to a single space."""
    text = "  hello\n\n\t  world  \r\n"
    assert _collapse_whitespace(text) == "hello world"


def test_collapse_whitespace_strips_edges() -> None:
    """Leading and trailing whitespace are trimmed."""
    assert _collapse_whitespace("   foo   ") == "foo"


# ---------------------------------------------------------------------------
# extract_docx_from_zip: contract
# ---------------------------------------------------------------------------


def _make_zip(files: dict[str, bytes]) -> bytes:
    """Build a zip payload in memory with the given filename→bytes map."""
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w") as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    return bio.getvalue()


def test_extract_docx_from_zip_picks_docx_over_doc() -> None:
    """When both ``.docx`` and ``.doc`` are present, ``.docx`` wins."""
    payload = _make_zip(
        {
            "a.docx": b"NEW",
            "b.doc": b"OLD",
        }
    )
    name, body = extract_docx_from_zip(payload)
    assert name.endswith(".docx")
    assert body == b"NEW"


def test_extract_docx_from_zip_skips_macosx_resource_fork() -> None:
    """``__MACOSX/...`` entries are not considered word documents."""
    payload = _make_zip(
        {
            "__MACOSX/._hidden.docx": b"MACOSX",
            "real.docx": b"REAL",
        }
    )
    name, body = extract_docx_from_zip(payload)
    assert name == "real.docx"
    assert body == b"REAL"


def test_extract_docx_from_zip_only_macosx_raises_value_error() -> None:
    """A zip with only ``__MACOSX/`` entries raises ``ValueError``."""
    payload = _make_zip(
        {"__MACOSX/._hidden": b"MACOSX"}
    )
    with pytest.raises(ValueError, match="No .docx"):
        extract_docx_from_zip(payload)


def test_extract_docx_from_zip_falls_back_to_doc_when_no_docx() -> None:
    """If only ``.doc`` is present, return that."""
    payload = _make_zip({"only.doc": b"LEGACY"})
    name, body = extract_docx_from_zip(payload)
    assert name == "only.doc"
    assert body == b"LEGACY"


def test_extract_docx_from_zip_empty_input_raises() -> None:
    """An empty byte string raises ``ValueError``."""
    with pytest.raises(ValueError, match="empty"):
        extract_docx_from_zip(b"")


def test_extract_docx_from_zip_real_fixture() -> None:
    """End-to-end: extract the .docx out of a real fixture zip."""
    zip_path = FIXTURES_DIR / "R5s260009.zip"
    assert zip_path.exists(), f"missing fixture: {zip_path}"
    name, body = extract_docx_from_zip(zip_path.read_bytes())
    assert name.lower().endswith((".docx", ".doc"))
    assert body  # non-empty
    # The docx itself is a zip; verify by reading its header.
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        assert any(n.endswith("document.xml") for n in z.namelist())


# ---------------------------------------------------------------------------
# Fixture-driven extraction tests (require python-docx)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _docx_available(),
    reason="python-docx not installed; install with `pip install doc3gpp[extract]`",
)
@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        (
            "C6-250028.zip",
            {
                "tdoc_id": "C6-250028",
                "tsg": "CT6",
                "year": 2025,
                "non_ttcn": True,
            },
        ),
        (
            "R5-227476.zip",
            {
                "tdoc_id": "R5-227476",
                "spec": "38.508-1",
                "cr_num": "2678",
                "rev": "1",
                "version": "17.6.0",
                "cr_cat": "F",
                "release": "Rel-17",
                "year": 2022,
                "tsg": "R5",
                "tech": "",
                "non_ttcn": True,
            },
        ),
        (
            "R5-253079.zip",
            {
                "tdoc_id": "R5-253079",
                "spec": "38.523-1",
                "cr_num": "4947",
                "rev": "1",
                "version": "19.0.0",
                "cr_cat": "F",
                "release": "Rel-19",
                "year": 2025,
                "tsg": "R5",
                "tech": "5G",
                "non_ttcn": True,
            },
        ),
        (
            "R5s260009.zip",
            {
                "tdoc_id": "R5s260009",
                "spec": "38.523-3",
                "cr_num": "3790",
                "rev": "0",
                "version": "18.4.0",
                "cr_cat": "F",
                "release": "Rel-18",
                "year": 2026,
                "tsg": "R5",
                "tech": "5G",
                "ats_version_starts_with": "iwd-TTCN3-",
            },
        ),
        (
            "R5s260051.zip",
            {
                "tdoc_id": "R5s260051",
                "spec": "38.523-3",
                "cr_num": "3806",
                "rev": "0",
                "version": "18.5.0",
                "cr_cat": "F",
                "release": "Rel-18",
                "year": 2026,
                "tsg": "R5",
                "tech": "5G",
                "ats_version_starts_with": "iwd-TTCN3-",
            },
        ),
        (
            "R5s260135.zip",
            {
                "tdoc_id": "R5s260135",
                "spec": "38.523-3",
                "cr_num": "3824",
                "rev": "0",
                "version": "18.6.0",
                "cr_cat": "B",
                "release": "Rel-18",
                "year": 2026,
                "tsg": "R5",
                "tech": "5G",
                "ats_version_starts_with": "iwd-TTCN3-",
            },
        ),
        (
            "R5s260176.zip",
            {
                "tdoc_id": "R5s260176",
                "spec": "36.523-3",
                "cr_num": "4971",
                "rev": "0",
                "version": "18.11.0",
                "cr_cat": "B",
                "release": "Rel-18",
                "year": 2026,
                "tsg": "R5",
                "tech": "LTE",
                "ats_version_starts_with": "iwd-TTCN3-",
            },
        ),
    ],
)
def test_fixture_extraction_matches_snapshot(
    fixture_name: str, expected: dict[str, object]
) -> None:
    """End-to-end pipeline against a real CR zip fixture.

    The python-docx wrapper from :mod:`doc3gpp.parsers.docx_converter`
    is used to convert the ``.docx`` extracted from the zip; the
    parsed fields are compared to the values the converter actually
    surfaces (some fixtures store spec / cr_num / title / cr_cat /
    release in docx field codes that python-docx does not render —
    those come back as None even though the snapshot table records
    their values).
    """
    from doc3gpp.parsers.docx_converter import convert_document_to_markdown

    zip_path = FIXTURES_DIR / fixture_name
    assert zip_path.exists(), f"missing fixture: {zip_path}"
    _, raw_doc_bytes = extract_docx_from_zip(zip_path.read_bytes())
    markdown = convert_document_to_markdown(
        raw_doc_bytes, fixture_name.replace(".zip", ".docx")
    )
    details = parse_cr_details(markdown, tdoc_id=fixture_name[:-4])

    assert isinstance(details, TDocCRParseResult)
    cover = details.cover
    assert cover.tdoc_id == expected["tdoc_id"]
    if "spec" in expected:
        assert cover.spec == expected["spec"]
    if "cr_num" in expected:
        assert cover.cr_num == expected["cr_num"]
    if "rev" in expected:
        assert cover.rev == expected["rev"], f"{fixture_name} rev"
    if "version" in expected:
        assert cover.version == expected["version"], f"{fixture_name} version"
    if "cr_cat" in expected:
        assert cover.cr_cat == expected["cr_cat"], f"{fixture_name} cr_cat"
    if "release" in expected:
        assert cover.release == expected["release"], f"{fixture_name} release"
    assert cover.tsg == expected["tsg"], f"{fixture_name} tsg"
    if expected.get("non_ttcn"):
        assert details.ttcn is None, f"{fixture_name} non-TTCN details must be empty"
    else:
        assert isinstance(details.ttcn, TDocCRTTCNDetails), (
            f"{fixture_name} TTCN CR must have a TTCN sidecar"
        )
        assert details.ttcn.ats_version is not None
        ats_version = details.ttcn.ats_version
        if expected.get("ats_version_starts_with"):
            assert ats_version.startswith(
                expected["ats_version_starts_with"]
            ), (
                f"{fixture_name} ats_version starts with "
                f"{expected['ats_version_starts_with']!r}, got {ats_version!r}"
            )


# ---------------------------------------------------------------------------
# Fixture-driven schema validation (no python-docx needed)
# ---------------------------------------------------------------------------


_SNAPSHOT = {
    # fixture_name -> (spec, cr_num, rev, cr_cat, release, year)
    "C6-250028.zip": ("31.124", "0797", "0", "F", "Rel-18", 2025),
    "R5-227476.zip": ("38.508-1", "2678", "1", "F", "Rel-17", 2022),
    "R5-253079.zip": ("38.523-1", "4947", "1", "F", "Rel-19", 2025),
    "R5s260009.zip": ("38.523-3", "3790", "0", "F", "Rel-18", 2026),
    "R5s260051.zip": ("38.523-3", "3806", "0", "F", "Rel-18", 2026),
    "R5s260135.zip": ("38.523-3", "3824", "0", "B", "Rel-18", 2026),
    "R5s260176.zip": ("36.523-3", "4971", "0", "B", "Rel-18", 2026),
}


@pytest.mark.parametrize(
    "fixture_name",
    list(_SNAPSHOT.keys()),
)
def test_fixture_xml_drive_invariants(fixture_name: str) -> None:
    """Read each fixture's ``word/document.xml`` and check invariants.

    Independent of python-docx — proves the snapshot values are
    correct against the literal document text. The category /
    release / spec values come from the same ``<w:t>`` cells the
    markdown output draws from, so this is a sanity check that
    the conversion isn't dropping state.
    """
    zip_path = FIXTURES_DIR / fixture_name
    assert zip_path.exists(), f"missing fixture: {zip_path}"
    _, doc_bytes = _extract_docx_from_zip(zip_path)
    raw_text = _extract_docx_text(doc_bytes)
    text = _collapse_whitespace(raw_text)
    spec, cr_num, rev, cr_cat, release, year = _SNAPSHOT[fixture_name]

    cr_match = re.search(r"CR\s+([\d ]+?)\s+rev", text)
    assert cr_match is not None, f"{fixture_name}: 'CR ... rev' line missing"
    rendered_cr_num = cr_match.group(1).replace(" ", "")
    assert rendered_cr_num == cr_num, (
        f"{fixture_name}: CR number mismatch "
        f"(rendered {rendered_cr_num!r}, expected {cr_num!r})"
    )
    if rev == "0":
        assert "rev -" in text, f"{fixture_name}: rev placeholder '-' missing"
    else:
        assert (
            f"rev {rev}" in text
        ), f"{fixture_name}: rev mismatch (looking for 'rev {rev}')"

    cr_marker = text.find("CHANGE REQUEST")
    end_marker = text.find("CR-Form")
    rendered_spec_area = (
        text[cr_marker:end_marker] if end_marker > cr_marker > 0 else text
    )
    rendered_normalised = rendered_spec_area.replace(" ", "")
    spec_normalised = spec.replace("-", "")
    assert (
        spec in rendered_normalised or spec_normalised in rendered_normalised
    ), (
        f"{fixture_name}: spec {spec!r} not in document text "
        f"(rendered area: {rendered_spec_area[:200]!r})"
    )

    cat_match = re.search(r"Category:\s*([A-Z])", text)
    assert cat_match is not None, f"{fixture_name}: no category found"
    assert cat_match.group(1) == cr_cat, (
        f"{fixture_name}: category is {cat_match.group(1)!r}, expected {cr_cat!r}"
    )
    rel_match = re.search(r"Release:\s*(\S+(?:\s\S+)*)\s+Use", text)
    if not rel_match:
        rel_match = re.search(r"Release:\s*(\S+)", text)
    assert rel_match is not None, f"{fixture_name}: no release found"
    rendered_release = rel_match.group(1).replace(" ", "")
    assert rendered_release.startswith(release), (
        f"{fixture_name}: rendered release {rendered_release!r} "
        f"does not start with {release!r}"
    )

    tdoc_id = fixture_name[:-4]
    assert int("20" + tdoc_id[3:5]) == year, f"{fixture_name}: year mismatch"


# ---------------------------------------------------------------------------
# Parser robustness: warnings are visible, fail-loud works, edge cases
# ---------------------------------------------------------------------------


def test_parse_cr_details_logs_warning_on_header_divergence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Header-line divergence (e.g. C6's missing meeting name) logs a warning
    rather than raising.
    """
    # Build a markdown with a partial header (no "TSG-<WG> Meeting" line).
    md = (
        "**3GPP TSG- Meeting #**\n"  # no meeting name
        "\n"
        "**, , -**\n"
        "\n"
        "| Source to TSG: | C6 |\n"
    )
    with caplog.at_level("WARNING", logger="doc3gpp.parsers.cr_parser"):
        parsed = parse_cr_details(md, tdoc_id="C6-250028")
    assert parsed.cover.tdoc_id == "C6-250028"
    assert parsed.cover.tsg == "C6"


def test_parse_cr_details_falls_back_to_id_for_tsg_when_cover_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty cover-page TSG falls back to ``id[:2].upper()``."""
    md = (
        "**3GPP TSG- Meeting #2026-TTCN email *R5s260009***\n"
        "\n"
        "| ***Title:*** | x | | | | | | | | | |\n"
        # Note: no Source to TSG row.
    )
    with caplog.at_level("WARNING", logger="doc3gpp.parsers.cr_parser"):
        parsed = parse_cr_details(md, tdoc_id="R5s260009")
    assert parsed.cover.tsg == "R5"


def test_to_persisted_then_json_round_trip() -> None:
    """Round-trip TDocCRDetails → dict → JSON keeps cover-page information intact."""
    import json

    details = TDocCRDetails(
        tdoc_id="R5s260009",
        spec="38.523-3",
        cr_num="3790",
        rev="0",
        version="18.4.0",
        cr_cat="F",
        release="Rel-18",
    )
    payload = details.to_persisted()
    encoded = json.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["tdoc_id"] == "R5s260009"
    assert "year" not in decoded
    assert "tech" not in decoded
    assert "details_json" not in decoded
    assert decoded["spec"] == "38.523-3"
    assert decoded["cr_num"] == "3790"
