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


def test_positive_detection_for_docx_converter_output():
    """Real 3GPP LS docx (e.g. R5-260017, R5-261602) round-tripped
    through :func:`convert_document_to_markdown` produces a markdown
    body whose first line uses spaces (not tabs) and omits the literal
    word ``TDoc`` — only the bare TDoc id appears. The detector must
    still recognise the LS header shape because the ``Title:`` cell,
    the ``Source:`` / ``To:`` / ``Cc:`` cells, and the meeting
    reference line are all present."""
    docx_md = (
        "3GPP TSG RAN WG5 Meeting #110        R5-260017\n"
        "\n"
        "Gothenburg, Sweden\n"
        "\n"
        "9th – 13th February 2026\n"
        "\n"
        "3GPP TSG-RAN WG4 Meeting #117    R4-2522058\n"
        "\n"
        "Dallas (TX), USA, November 17-21, 2025\n"
        "\n"
        "Title:    LS on frequency separation for Type 4b UE NR-CA PDSCH demodulation requirements\n"
        "Response to:    -\n"
        "Release:    Release 19\n"
        "Work Item:    NonCol_intraB_ENDC_NR_CA_Ph2\n"
        "Source:    TSG RAN WG4\n"
        "To:    TSG RAN WG5\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_ls_out_docx():
    """``R5-261602`` (LS out, TSG-RAN5) — same docx-converter emit shape,
    no ``TDoc`` word, spaces not tabs on the first line."""
    docx_md = (
        "3GPP TSG-RAN5 Meeting #110            R5-261602\n"
        "\n"
        "Gothenburg, SE, 9th Feb 2026 - 13th Feb 2026\n"
        "\n"
        "Title:    LS on A-IoT OTA Anechoic chamber method\n"
        "Response to:    -\n"
        "Release:    Rel-18\n"
        "Work Item:    Ambient_IoT_Solutions_plus_CT1_SA3-ConTest\n"
        "Source:    TSG WG RAN5\n"
        "To:    TSG WG CT1, TSG SA WG3\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True
