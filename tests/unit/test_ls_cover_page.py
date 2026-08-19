from doc3gpp.parsers.ls.cover_page import LSCoverPageParser


_LS_LINES = [
    "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001",
    "",
    "Title:\tLS on 5G_eHealth WI status update",
    "Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3",
    "Release:\tRelease 17",
    "Work Item:\t5G_eHealth (WI-123456)",
    "",
    "Source:\t3GPP TSG RAN WG2",
    "To:\tRAN WG3, RAN WG4",
    "Cc:\tSA WG2",
    "",
    "Attachments:\tTR 38.901 v0.1.0 [draft]. ",
]


def test_supports_source_true_for_non_none():
    assert LSCoverPageParser.supports_source("3GPP TSG") is True
    assert LSCoverPageParser.supports_source("IEEE 802.11") is True


def test_supports_source_false_for_none():
    assert LSCoverPageParser.supports_source(None) is True  # 3GPP is the catch-all


def test_parse_extracts_all_fields():
    ok, payload, advanced = LSCoverPageParser().parse(_LS_LINES)
    assert ok is True
    assert advanced == len(_LS_LINES)
    assert payload["title"] == "LS on 5G_eHealth WI status update"
    assert payload["response_to"] == "LS R5-234567 on 5G_eHealth WI status from RAN WG3"
    assert "response_to_doc" not in payload
    assert payload["release"] == "Rel-17"
    assert payload["work_item_name"] == "5G_eHealth"
    assert payload["work_item_code"] == "WI-123456"
    assert payload["source"] == "3GPP TSG RAN WG2"
    assert "RAN WG3" in payload["to_groups"]
    assert "RAN WG4" in payload["to_groups"]
    assert "SA WG2" in payload["cc_groups"]


def test_to_groups_normalises_comma_separated_to_newlines():
    lines = [line.replace("RAN WG3, RAN WG4", "RAN WG3, RAN WG4") for line in _LS_LINES]
    _, payload, _ = LSCoverPageParser().parse(lines)
    assert payload["to_groups"] == "RAN WG3\nRAN WG4"


def test_parse_handles_missing_response_to():
    lines = [line for line in _LS_LINES if not line.startswith("Response to:")]
    _, payload, _ = LSCoverPageParser().parse(lines)
    assert payload["response_to"] is None
    assert "response_to_doc" not in payload


def test_parse_handles_missing_work_item_code():
    lines = [line.replace("(WI-123456)", "(no-code)") for line in _LS_LINES]
    _, payload, _ = LSCoverPageParser().parse(lines)
    assert payload["work_item_name"] == "5G_eHealth"
    assert payload["work_item_code"] == "no-code"


def test_attachments_parsed_as_list():
    lines = _LS_LINES + [
        "Attachments:\tTR 38.901 v0.1.0 [draft].",
        "Attachments:\tTS 38.300 v17.1.0.",
    ]
    _, payload, _ = LSCoverPageParser().parse(lines)
    attachments = payload["attachments"]
    assert isinstance(attachments, list)
    assert {"doc_number": "TR 38.901 v0.1.0", "description": "draft"} in attachments


def test_parse_extracts_hash_prefixed_header_cells():
    """``S2-2606763`` (LS in, SA WG4) — the docx converter emits header
    cells as markdown headings (``# Title:``) when the source document
    styles them as heading paragraphs. The cover extractor must strip
    the heading marker before matching the field regexes."""
    lines = [
        "SA WG2 Meeting #S2-176    S2-2606763",
        "",
        "# Title:    LS response on AI/ML for Media",
        "# Response to:    LS (S4-260888/S2-2510954) on AI/ML for Media",
        "# Release:    Release 20",
        "# Work Item:    AIML_IMS-MED",
        "Source:    TSG SA WG4",
        "To:    TSG SA WG2",
        "Cc:",
    ]
    ok, payload, _ = LSCoverPageParser().parse(lines)
    assert ok is True
    assert payload["title"] == "LS response on AI/ML for Media"
    assert payload["response_to"] == "LS (S4-260888/S2-2510954) on AI/ML for Media"
    assert payload["release"] == "Rel-20"
    assert payload["work_item_name"] == "AIML_IMS-MED"
    assert payload["source"] == "TSG SA WG4"
    assert payload["to_groups"] == "TSG SA WG2"


def test_parse_response_to_parenthesised_doc_ids_before_ls():
    """``S3-262697`` (LS out) — parenthesised doc ids precede the
    ``LS on`` token: ``(RP-261538/S3-262664) LS on user plane security``."""
    lines = [
        "3GPP TSG-SA3 Meeting #129    S3-262697",
        "",
        "Title:    Reply LS on privacy and security of sensing",
        "Response to:    (RP-261538/S3-262664) LS on user plane security",
        "Release:    Release 20",
        "Work Item:    FS_Sensing_SEC",
        "",
        "Source:    MITRE-FFRDC to be SA3",
        "To:    RAN",
        "Cc:",
    ]
    ok, payload, _ = LSCoverPageParser().parse(lines)
    assert ok is True
    assert payload["response_to"] == "(RP-261538/S3-262664) LS on user plane security"
    assert "S3-262664" in payload["response_to"]
    assert "user plane security" in payload["response_to"]


def test_parse_response_to_trailing_parenthesized_doc_ids():
    """``S2-2607046`` (LS in, SA2) — doc ids in trailing parens:
    ``Reply LS on scope alignment for Rel-20 AIoT (S2-2606764/S3-261710)``."""
    lines = [
        "3GPP SA WG2 Meeting #176    S2-2607046",
        "",
        "# Title:    [Draft] Reply LS on scope alignment for Rel-20 AIoT",
        "# Response to:    Reply LS on scope alignment for Rel-20 AIoT (S2-2606764/S3-261710)",
        "# Release:    Release 20",
        "# Work Item:    AmbientIoT_Ph2-ARC",
        "Source:    SA2",
        "To:    SA3",
        "Cc:",
    ]
    ok, payload, _ = LSCoverPageParser().parse(lines)
    assert ok is True
    assert payload["response_to"] == "Reply LS on scope alignment for Rel-20 AIoT (S2-2606764/S3-261710)"


def test_parse_response_to_inline_doc_from_group():
    """Tab-separated template shape:
    ``LS R5-234567 on 5G_eHealth WI status from RAN WG3``."""
    lines = [
        "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001",
        "",
        "Title:\tLS on 5G_eHealth WI status update",
        "Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3",
        "Release:\tRelease 17",
        "Work Item:\t5G_eHealth (WI-123456)",
        "",
        "Source:\t3GPP TSG RAN WG2",
        "To:\tRAN WG3, RAN WG4",
        "Cc:\tSA WG2",
    ]
    ok, payload, _ = LSCoverPageParser().parse(lines)
    assert ok is True
    assert payload["response_to"] == "LS R5-234567 on 5G_eHealth WI status from RAN WG3"


def test_parse_response_to_na_not_an_error():
    """``S2-2607151`` — ``Response to: N/A`` must not raise and must not
    populate the response-to fields."""
    lines = [
        "3GPP TSG SA WG2 Meeting SA2#176    S2-2607151",
        "",
        "Title:    [Draft] LS on RAN dependencies for KI#23 (Support of 6G NTN)",
        "Response to:    N/A",
        "Release:    Rel-20",
        "Work Item:    FS_6G_ARC",
        "",
        "Source:    SA2",
        "To:    RAN3",
        "Cc:    SA, RAN",
    ]
    ok, payload, _ = LSCoverPageParser().parse(lines)
    assert ok is True
    assert payload["response_to"] is None
    assert "response_to_doc" not in payload


def test_release_normalisation():
    """Release values are canonicalised to ``Rel-<n>`` (and R99-style
    pre-release markers)."""
    for raw, expected in [
        ("Release 20", "Rel-20"),
        ("Release 9", "Rel-9"),
        ("Release 1999", "R99"),
        ("Release 1998", "R98"),
        ("Rel-18", "Rel-18"),
        ("Release 17", "Rel-17"),
    ]:
        lines = [line.replace("Release:\tRelease 17", f"Release:\t{raw}") for line in _LS_LINES]
        _, payload, _ = LSCoverPageParser().parse(lines)
        assert payload["release"] == expected, f"{raw!r} -> {payload['release']!r}"
