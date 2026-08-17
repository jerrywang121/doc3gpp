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


def test_parse_extracts_all_eleven_fields():
    ok, payload, advanced = LSCoverPageParser().parse(_LS_LINES)
    assert ok is True
    assert advanced == len(_LS_LINES)
    assert payload["title"] == "LS on 5G_eHealth WI status update"
    assert payload["response_to_doc"] == "R5-234567"
    assert payload["response_to_title"] == "5G_eHealth WI status"
    assert payload["response_to_group"] == "RAN WG3"
    assert payload["release"] == "Release 17"
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
    assert payload["response_to_doc"] is None
    assert payload["response_to_title"] is None
    assert payload["response_to_group"] is None


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
