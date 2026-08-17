from doc3gpp.parsers.ls.header import is_ls_header_present


HEADER_LINES = [
    "3GPP TSG RAN WG2 Meeting #104\tTDoc R5-240001",
    "",
    "Title:\tLS on 5G_eHealth WI status update",
    "Response to:\tLS R5-234567 on 5G_eHealth WI status from RAN WG3",
    "Release:\tRelease 17",
    "Work Item:\t5G_eHealth (WI-123456)",
    "",
    "Source:\t3GPP TSG RAN WG2",
    "To:\tRAN WG3",
    "Cc:\tSA WG2, CT WG1",
    "",
]


def test_positive_detection_for_3gpp_template_shape():
    md = "\n".join(HEADER_LINES) + "\n\n1\tOverall description\n…"
    present, blob = is_ls_header_present(md)
    assert present is True
    assert "LS on 5G_eHealth" in blob


def test_negative_when_no_title_line():
    lines = [line for line in HEADER_LINES if not line.startswith("Title:")]
    md = "\n".join(lines) + "\n"
    present, _ = is_ls_header_present(md)
    assert present is False


def test_negative_when_title_does_not_start_with_ls_on():
    lines = [line.replace("LS on", "Update on") for line in HEADER_LINES]
    md = "\n".join(lines) + "\n"
    present, _ = is_ls_header_present(md)
    assert present is False


def test_negative_for_cr_shaped_document():
    cr = "\n".join([
        "| CHANGE REQUEST |",
        "|---|---|---|---|",
        "| 38.300 | CR | 1234 | rev | 1 | Current version: 17.1.0 |",
    ])
    present, _ = is_ls_header_present(cr)
    assert present is False


def test_negative_for_empty_markdown():
    present, _ = is_ls_header_present("")
    assert present is False


def test_detection_requires_one_of_source_to_cc():
    lines = [
        line
        for line in HEADER_LINES
        if not (line.startswith("Source:") or line.startswith("To:") or line.startswith("Cc:"))
    ]
    md = "\n".join(lines) + "\n"
    present, _ = is_ls_header_present(md)
    assert present is False
