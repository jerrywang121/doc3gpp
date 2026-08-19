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


def test_positive_detection_for_s_prefixed_tdoc_id():
    """``S2-2606762`` (LS in, SA WG2) — the meeting reference line omits
    the literal ``3GPP`` prefix and carries an ``S``-prefixed TDoc id,
    which the detector must still recognise."""
    docx_md = (
        "SA WG2 Meeting #S2-176    S2-2606762\n"
        "\n"
        "24 - 28 August, 2026, Prague, CZ\n"
        "\n"
        "Title:    Reply LS on IMS network behaviour for new Contact header parameters\n"
        "Response to:    LS S4-260901 on Reply LS on IMS network behaviour for new Contact header parameters from WG2\n"
        "Release:    Rel-20\n"
        "Work Item:    AvCall-MED, FS_Avatar_Ph2_MED\n"
        "Source:    SA WG4\n"
        "To:    SA WG2\n"
        "Cc:    CT WG1, CT WG3, CT WG4\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_c_prefixed_tdoc_id():
    """``C6-250418`` (LS in, CT WG6) — ``C``-prefixed TDoc id on the
    meeting reference line."""
    docx_md = (
        "3GPP TSG CT WG6 Meeting #123    C6-250418\n"
        "\n"
        "Title:    LS to 3GPP CT WG6 on changes of the Universal PIN support\n"
        "Source:    ETSI TC SET\n"
        "To:    CT WG6\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_reply_ls_title():
    """``S3-263149`` (LS out, SA3) — the title starts with ``Reply LS on``
    rather than the bare ``LS on`` prefix."""
    docx_md = (
        "3GPP TSG-SA3 Meeting #129    S3-263149\n"
        "\n"
        "Prague, Czech Republic 24 - 28 August 2026\n"
        "\n"
        "Title:    Reply LS on 6G study for secure SMS\n"
        "Response to:    LS on 6G study for secure SMS (C1-262557)\n"
        "Release:    Rel-20\n"
        "Source:    Qualcomm Incorporated To be SA3\n"
        "To:    CT1\n"
        "Cc:    SA2\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_draft_reply_ls_title():
    """``S3-263033`` (LS out, SA3) — bracketed draft marker before
    ``Reply LS on`` in the title."""
    docx_md = (
        "3GPP TSG-SA3 Meeting #129    S3-263033\n"
        "\n"
        "Title:    [draft] Reply LS on single paging mechanism for idle and inactive in 6G\n"
        "Source:    OPPO\n"
        "To:    RAN2\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_ls_to_title():
    """``C6-250664`` (LS in) — ``LS to ... on ...`` title shape where the
    ``on`` keyword does not immediately follow ``LS``."""
    docx_md = (
        "3GPP TSG CT WG6 Meeting #124    C6-250664\n"
        "\n"
        "Title:    LS to 3GPP regarding NG-eCall test URN\n"
        "Source:    GSMA NG/UPG\n"
        "To:    CT WG6\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_ls_with_questions_title():
    """``S1-263020`` (LS in) — title shape."""
    docx_md = (
        "3GPP TSG SA WG1 Meeting #115    S1-263020\n"
        "\n"
        "Title:    LS with questions on IMS voice over NB-IoT NTN\n"
        "Source:    RAN WG2\n"
        "To:    SA WG1\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_hash_prefixed_title():
    """``S2-2606763`` (LS in, SA WG4) — the docx converter emits the
    header cells as markdown headings (``# Title:``) when the source
    document styles them as heading paragraphs."""
    docx_md = (
        "SA WG2 Meeting #S2-176    S2-2606763\n"
        "\n"
        "24 - 28 August, 2026, Prague, CZ\n"
        "\n"
        "3GPP TSG- SA4 Meeting # 136    S4-261351\n"
        "\n"
        "Montreal, Canada 10th - 15th May 2026, Online\n"
        "\n"
        "# Title:    LS response on AI/ML for Media\n"
        "# Response to:    LS (S4-260888/S2-2510954) on AI/ML for Media\n"
        "# Release:    Release 20\n"
        "# Work Item:    AIML_IMS-MED\n"
        "Source:    TSG SA WG4\n"
        "To:    TSG SA WG2\n"
        "Cc:\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_for_lsout_title():
    """``S2-2606790`` (LS out, SA WG5) — ``LSout on`` (no space) title
    shape."""
    docx_md = (
        "SA WG2 Meeting #S2-176    S2-2606790\n"
        "\n"
        "24 - 28 August, 2026, Prague, CZ\n"
        "\n"
        "3GPP TSG-SA5 Meeting #167    S5-262805\n"
        "\n"
        "Dalian, China, 18-22 May 2026\n"
        "\n"
        "Title:    LSout on CHF info, Charging Groups and handling\n"
        "Response to:    -\n"
        "Release:    Rel-19\n"
        "Work Item:    CHFSeg\n"
        "Source:    SA5\n"
        "To:    SA2\n"
        "Cc:    CT4\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_positive_detection_without_tdoc_id_on_first_line():
    """The first line can be a bare meeting reference without any TDoc
    id (the id may live on a later line or be absent). Detection must
    not depend on the id token."""
    docx_md = (
        "SA WG2 Meeting #S2-176\n"
        "\n"
        "24 - 28 August, 2026, Prague, CZ\n"
        "\n"
        "Title:    Reply LS on IMS network behaviour\n"
        "Response to:    -\n"
        "Source:    SA WG4\n"
        "To:    SA WG2\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is True


def test_negative_when_title_has_no_ls_word():
    """A document whose title carries no ``LS`` token (e.g. an external
    organisation's liaison template) is not recognised as a 3GPP LS."""
    docx_md = (
        "3GPP TSG RAN WG1#126    R1-2605198\n"
        "\n"
        "Title:    LIAISON STATEMENT TO EXTERNAL ORGANIZATIONS - REQUEST FOR INPUTS\n"
        "Source:    ITU-R WP 5A\n"
        "To:    RAN WG1\n"
        "Cc:    -\n"
    )
    present, _blob = is_ls_header_present(docx_md)
    assert present is False
