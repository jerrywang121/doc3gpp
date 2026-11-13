"""Offline unit tests for ETSI LISTSERV detail, body, attachment, and TDoc parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc3gpp.models.listserv import ListservAttachment

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "listserv"
BASE_DETAIL_URL = "https://list.etsi.org/scripts/wa.exe?A2=ind2610A&L=EXAMPLE&P=1"


@pytest.fixture
def detail_html() -> str:
    return (FIXTURES_DIR / "detail.html").read_text(encoding="utf-8")


@pytest.fixture
def body_html() -> str:
    return (FIXTURES_DIR / "body.html").read_text(encoding="utf-8")


@pytest.fixture
def body_txt() -> str:
    return (FIXTURES_DIR / "body.txt").read_text(encoding="utf-8")


def test_extract_tdocs_deduplicates_case_insensitively() -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs

    ids = extract_tdocs("R1-2610793 r1-2610793 RP-2601234 R5s260009")
    assert {item.upper() for item in ids} == {"R1-2610793", "RP-2601234", "R5S260009"}
    assert len(ids) == 3
    assert ids == ("R1-2610793", "RP-2601234", "R5S260009")


def test_parse_detail_page_prefers_html_body_part_and_filters_alternatives_from_attachments(
    detail_html: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import parse_detail_page

    body_url, attachments, inline_body = parse_detail_page(detail_html, BASE_DETAIL_URL)

    assert (
        body_url
        == "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&E=quoted-printable&P=102&B=--boundary&T=text%2Fhtml;%20charset=UTF-8"
    )
    assert inline_body is None
    assert attachments == (
        ListservAttachment(
            name="R1-2610793_MIMO_Summary.docx",
            url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&E=base64&P=104&B=--boundary&T=application%2Fvnd.openxmlformats-officedocument.wordprocessingml.document&N=R1-2610793_MIMO_Summary.docx",
        ),
        ListservAttachment(
            name="Attachment_Archive.zip",
            url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&E=base64&P=105&B=--boundary&T=application%2Fzip&N=R4-2607922_slides.zip",
        ),
    )


def test_parse_detail_page_prefers_html_part_when_iframe_points_to_plain_text() -> None:
    from doc3gpp.parsers.listserv_detail import parse_detail_page

    html = """
    <html><body>
      <table>
        <tr>
          <th>Parts/Attachments:</th>
          <td>
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=1&amp;T=text%2Fplain;%20charset=ISO-8859-1">text/plain</a> ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=2&amp;T=text%2Fhtml;%20charset=UTF-8">text/html</a> ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=3&amp;T=multipart%2Frelated">multipart/related</a>
          </td>
        </tr>
      </table>
      <iframe src="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=1&amp;T=text%2Fplain;%20charset=ISO-8859-1"></iframe>
    </body></html>
    """
    body_url, attachments, inline_body = parse_detail_page(html, BASE_DETAIL_URL)
    assert (
        body_url
        == "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=2&T=text%2Fhtml;%20charset=UTF-8"
    )
    assert attachments == ()
    assert inline_body is None


def test_parse_detail_page_falls_back_to_plain_text_part_when_no_html_part_exists() -> None:
    from doc3gpp.parsers.listserv_detail import parse_detail_page

    html = """
    <html><body>
      <table>
        <tr>
          <th>Parts/Attachments:</th>
          <td>
            <a href="wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=1&amp;T=text%2Fplain;%20charset=us-ascii">text/plain</a> (12 lines) ,
            <a href="wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=2&amp;T=multipart%2Frelated">multipart/related</a> ,
            <a href="wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=3&amp;T=text%2Fplain&amp;N=notes_R1-2600001.txt">notes_R1-2600001.txt</a>
          </td>
        </tr>
      </table>
    </body></html>
    """
    body_url, attachments, inline_body = parse_detail_page(html, BASE_DETAIL_URL)
    assert (
        body_url
        == "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&T=text%2Fplain;%20charset=us-ascii"
    )
    assert inline_body is None
    assert attachments == (
        ListservAttachment(
            name="notes_R1-2600001.txt",
            url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=3&T=text%2Fplain&N=notes_R1-2600001.txt",
        ),
    )


def test_parse_detail_page_inline_body_fallback_when_no_body_part_url_exists() -> None:
    from doc3gpp.parsers.listserv_detail import parse_detail_page

    html = """
    <html><body>
      <table class="message-header">
        <tr><th>Subject:</th><td>Inline notice</td></tr>
        <tr><th>From:</th><td>[log in to unmask]</td></tr>
        <tr><th>Date:</th><td>2026-10-01</td></tr>
      </table>
      <div id="message-body">
        <p>Inline body content for R1-2610793 with no iframe or A3 part link.</p>
        <blockquote><p>Quoted R1-111111 in inline fallback should be removed.</p></blockquote>
      </div>
    </body></html>
    """
    body_url, attachments, inline_body = parse_detail_page(html, BASE_DETAIL_URL)
    assert body_url is None
    assert attachments == ()
    assert inline_body is not None
    assert "R1-2610793" in inline_body
    assert "R1-111111" not in inline_body


def test_parse_message_body_html_removes_quotes_retains_uncertain_prose_and_sanitizes(
    body_html: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    markdown = parse_message_body(body_html, is_html=True)

    # Current message content and formatting survive
    assert "RAN1#126 MIMO Summary" in markdown
    assert "R1-2610793" in markdown
    assert "[RP-2601234](https://www.3gpp.org/ftp/tsg_ran/RP-2601234.zip)" in markdown
    assert "R5s260009" in markdown
    # Uncertain prose with "wrote:" and ">" in math comparison is retained
    assert "We wrote: the comparison in section 4" in markdown
    assert "threshold 3 dB is uncertain prose" in markdown

    # Blockquote and Gmail quote container with old TDocs are removed
    assert "R1-111111" not in markdown
    assert "R2-222222" not in markdown

    # Executable / unsafe HTML and javascript: links are neutralized
    assert "<script" not in markdown.lower()
    assert "executed-script" not in markdown
    assert "onerror" not in markdown.lower()
    assert "javascript:" not in markdown.lower()

    # TDoc extraction on cleaned markdown excludes quoted R1-111111 / R2-222222
    ids = extract_tdocs(markdown)
    assert ids == ("R1-2610793", "RP-2601234", "R5S260009")


@pytest.mark.parametrize(
    "quoted_headers",
    [
        (
            '<p><b>From:</b>Kuba &lt;<a href="/scripts/wa.exe?LOGON=abc">'
            "[log in to unmask]</a>&gt;</p>"
            '<p><b>To:</b><a href="/scripts/wa.exe?LOGON=abc">'
            "[log in to unmask]</a></p>"
            "<p><b>Date:</b>2026年11月09日 19:47</p>"
            "<p><b>Subject:</b><b>Old R1-111111</b></p>"
        ),
        (
            '<p><b>From:</b> Pradeep &lt;<a href="/scripts/wa.exe?LOGON=abc">'
            "[log in to unmask]</a>&gt;<br>"
            "<b>Sent:</b> Thursday, October 15, 2026 4:01 PM<br>"
            '<b>To:</b> <a href="/scripts/wa.exe?LOGON=abc">'
            "[log in to unmask]</a><br>"
            "<b>Subject:</b> Old R1-111111</p>"
        ),
    ],
    ids=["separate-paragraphs-date", "line-breaks-sent"],
)
def test_parse_message_body_removes_bold_html_reply_headers_and_history(
    quoted_headers: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    html = "<p>Current message R1-2610793.</p>" + quoted_headers + "<p>Old message R2-222222.</p>"

    markdown = parse_message_body(html, is_html=True)

    assert markdown == "Current message R1-2610793."
    assert extract_tdocs(markdown) == ("R1-2610793",)


def test_parse_message_body_removes_bold_plain_reply_headers_with_blank_lines() -> None:
    from doc3gpp.parsers.listserv_detail import parse_message_body

    body = (
        "Current discussion.\n\n"
        "**From:** Author\n\n"
        "**To:** List\n\n"
        "**Date:** 2026年11月09日 19:47\n\n"
        "**Subject:****Old topic**\n\n"
        "Old discussion."
    )

    assert parse_message_body(body, is_html=False) == "Current discussion."


@pytest.mark.parametrize(
    "body",
    [
        "Current message.\n\n**From:** Alice\n\nMore current prose.",
        "Current message.\n\n**From:** Alice\n**Date:** Monday\nMore current prose.",
        "Current message.\n\n**From:**\n**Date:** Monday\n**To:** Team\nMore prose.",
        (
            "Current message.\n\n**From:** Alice\n\nMore current prose.\n\n"
            "**Date:** Monday\n\n**To:** Team\n\nFinal paragraph."
        ),
    ],
    ids=["single-header", "missing-recipient-or-subject", "empty-from", "separated-headers"],
)
def test_parse_message_body_preserves_incomplete_or_separated_bold_headers(
    body: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import parse_message_body

    assert parse_message_body(body, is_html=False) == body


def test_parse_message_body_plain_removes_reply_and_forward_blocks_conservatively(
    body_txt: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    markdown = parse_message_body(body_txt, is_html=False)

    assert "R1-2610793" in markdown
    assert "RP-2601234" in markdown
    assert "R5s260009" in markdown
    assert "C6-250028" in markdown
    assert "power > 0 dBm in table 2 is regular technical prose" in markdown

    # Quoted reply and forwarded blocks are stripped
    assert "R1-111111" not in markdown
    assert "R3-333333" not in markdown

    # Unsafe inline HTML or javascript markdown links in plain text are sanitized
    assert "<script>" not in markdown.lower()
    assert "javascript:" not in markdown.lower()

    ids = extract_tdocs(markdown)
    assert ids == ("R1-2610793", "RP-2601234", "R5S260009", "C6-250028")


def test_extract_tdocs_combines_long_body_tail_and_attachment_names_and_urls(
    detail_html: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import (
        extract_tdocs,
        parse_detail_page,
        parse_message_body,
    )

    _, attachments, _ = parse_detail_page(detail_html, BASE_DETAIL_URL)
    # Construct a body larger than the 10,000-char storage limit with TDocs at the very end
    long_plain_body = (
        "Header intro with RP-2601234 and R5s260009.\n\n"
        + ("Filler paragraph text without identifiers. " * 300)
        + "\nFinal tail references: R1-2610793 and r1-2610793\n"
        + "\nOn Mon, Oct 1, 2026 at 08:00 AM Someone wrote:\n> Old quoted R1-111111\n"
    )
    cleaned_body = parse_message_body(long_plain_body, is_html=False)
    assert len(cleaned_body) > 10_000

    # Extract before any storage truncation, including attachment names and URLs
    attachment_inputs = [
        item for att in attachments for item in (att.name, att.url)
    ]
    ids = extract_tdocs(cleaned_body, *attachment_inputs)

    # Includes RP-2601234, R5S260009, tail R1-2610793 (deduplicated), and R4-2607922 from attachment URL
    assert ids == ("RP-2601234", "R5S260009", "R1-2610793", "R4-2607922")
    assert "R1-111111" not in ids


def test_parse_detail_page_rejects_empty_or_login_html(
    detail_html: str,
) -> None:
    from doc3gpp.parsers.listserv_detail import (
        ListservDetailParseError,
        parse_detail_page,
    )

    malformed_html = (FIXTURES_DIR / "malformed.html").read_text(encoding="utf-8")
    assert issubclass(ListservDetailParseError, ValueError)
    with pytest.raises(ListservDetailParseError):
        parse_detail_page("", BASE_DETAIL_URL)
    with pytest.raises(ListservDetailParseError):
        parse_detail_page(malformed_html, BASE_DETAIL_URL)


def _metadata_html(subject: str = "Topic R1-2610793", sender: str = "Alice &lt;[log in to unmask]&gt;", date: str = "2026-11-13 12:00:00 +0000") -> str:
    return (
        '<html><body><table class="message-header">'
        f'<tr><th>Subject:</th><td>{subject}</td></tr>'
        f'<tr><th>From:</th><td>{sender}</td></tr>'
        f'<tr><th>Date:</th><td>{date}</td></tr>'
        '</table><p>secret body contents</p></body></html>'
    )


def test_detail_metadata_preserves_displayed_date_and_headers() -> None:
    from datetime import datetime, timedelta, timezone

    from doc3gpp.parsers.listserv_detail import parse_detail_metadata

    item = parse_detail_metadata(
        _metadata_html(subject=" Topic &amp; R1-2610793 ", date=" 2026-11-13 12:00:00 +0530 "),
        url=BASE_DETAIL_URL,
        list_id="3gpp_tsg_ran_wg5_sig",
    )
    assert item.url == BASE_DETAIL_URL
    assert item.list_id == "3GPP_TSG_RAN_WG5_SIG"
    assert item.subject == "Topic & R1-2610793"
    assert item.sender == "Alice <[log in to unmask]>"
    assert item.date == datetime(2026, 11, 13, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert item.date_precision == "datetime"
    assert item.subject_tdocs == ("R1-2610793",)
    assert item.week == "2026-11-W2"


def test_detail_metadata_date_only() -> None:
    from datetime import datetime

    from doc3gpp.parsers.listserv_detail import parse_detail_metadata

    item = parse_detail_metadata(_metadata_html(date="2026-11-22"), url=BASE_DETAIL_URL, list_id="example")
    assert item.date == datetime(2026, 11, 22)
    assert item.date_precision == "date"
    assert item.week == "2026-11-W4"


def test_detail_metadata_accepts_public_etsi_td_header_shape() -> None:
    from doc3gpp.parsers.listserv_detail import parse_detail_metadata

    html = '''<table><tr><td scope="row"><b>Subject:</b></td><td><div><a href="#">Topic R1-2610793</a></div></td></tr>
    <tr><td scope="row"><b>From:</b></td><td><div>Alice &lt;<a href="#">[log in to unmask]</a>&gt;</div></td></tr>
    <tr><td scope="row"><b>Date:</b></td><td><div>Fri, 13 Nov 2026 13:55:46 +0000</div></td></tr></table>'''
    item = parse_detail_metadata(html, url=BASE_DETAIL_URL, list_id="example")
    assert item.subject == "Topic R1-2610793"
    assert item.sender == "Alice <[log in to unmask]>"
    assert item.date.isoformat() == "2026-11-13T13:55:46+00:00"
    assert item.week == "2026-11-W2"


@pytest.mark.parametrize("html", [
    "",
    '<html><title>Login required</title><body>secret body contents</body></html>',
    _metadata_html().replace('<tr><th>Subject:</th><td>Topic R1-2610793</td></tr>', ''),
    _metadata_html(subject=""),
    _metadata_html(sender=""),
    _metadata_html(date=""),
    _metadata_html(date="not a date"),
    _metadata_html().replace('</table>', '<tr><th>From:</th><td>Duplicate</td></tr></table>'),
    _metadata_html().replace(
        "<tr><th>Subject:</th><td>Topic R1-2610793</td></tr>",
        "<tr><th>Subject:</th><td>Topic R1-2610793</td><th>Subject:</th><td>Duplicate</td></tr>",
    ),
])
def test_detail_metadata_rejects_incomplete_or_ambiguous_headers_safely(html: str) -> None:
    from doc3gpp.parsers.listserv_detail import ListservDetailParseError, parse_detail_metadata

    with pytest.raises(ListservDetailParseError) as caught:
        parse_detail_metadata(html, url=BASE_DETAIL_URL, list_id="example")
    assert "secret body contents" not in str(caught.value)


def test_parse_message_body_escapes_arbitrary_html_tags_and_outlook_reply_blocks() -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    html = """
    <html><body>
      <p>Current contribution S2-2605184 with &lt;details ontoggle=alert(1)&gt; and <a href="data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;">data link</a>.</p>
      <div id="divRplyFwdMsg">
        <p><b>From:</b> Prior Author<br><b>Sent:</b> Monday<br><b>To:</b> WG<br><b>Subject:</b> Old R1-111111</p>
      </div>
      <div><p>Trailing sibling paragraph after Outlook divider mentioning R1-111111.</p></div>
    </body></html>
    """
    markdown = parse_message_body(html, is_html=True)
    assert "S2-2605184" in markdown
    assert "<details" not in markdown.lower()
    assert "&lt;details" in markdown.lower()
    assert "data:text/html" not in markdown.lower()
    assert "R1-111111" not in markdown
    assert extract_tdocs(markdown) == ("S2-2605184",)


def test_parse_message_body_preserves_wordsection1_current_content() -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    html = """
    <html><body>
      <div class="WordSection1">
        <p class="MsoNormal">Dear RAN1, please find the updated text for R1-2610793 and RP-2601234.</p>
        <p class="MsoNormal">Best regards, Author</p>
      </div>
    </body></html>
    """
    markdown = parse_message_body(html, is_html=True)
    assert "Dear RAN1, please find the updated text for R1-2610793 and RP-2601234." in markdown
    assert "Best regards, Author" in markdown
    assert extract_tdocs(markdown) == ("R1-2610793", "RP-2601234")


def test_parse_message_body_removes_trailing_siblings_after_outlook_dividers_and_header_wrappers() -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    # Case 1: WordSection1 with border-top Outlook divider on a header-only wrapper div
    html_border_divider = """
    <html><body>
      <div class="WordSection1">
        <p class="MsoNormal">Current message discussing R1-2610793.</p>
        <div>
          <div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">
            <p class="MsoNormal"><b>From:</b> Old Sender<br><b>Sent:</b> Monday<br><b>To:</b> List<br><b>Subject:</b> Old R1-111111</p>
          </div>
        </div>
        <p class="MsoNormal">Quoted sibling paragraph after border-top wrapper mentioning R1-111111 and R2-222222.</p>
      </div>
    </body></html>
    """
    md_border = parse_message_body(html_border_divider, is_html=True)
    assert "Current message discussing R1-2610793." in md_border
    assert "R1-111111" not in md_border
    assert "R2-222222" not in md_border
    assert extract_tdocs(md_border) == ("R1-2610793",)

    # Case 2: OutlookMessageHeader class followed by sibling quoted paragraphs
    html_outlook_class = """
    <html><body>
      <div class="WordSection1">
        <p>Current contribution R5s260009 is ready.</p>
        <div class="OutlookMessageHeader">
          <p>From: Someone<br>Sent: Yesterday<br>To: WG5<br>Subject: Re: R1-111111</p>
        </div>
        <p>Trailing quoted paragraph after OutlookMessageHeader with R1-111111.</p>
      </div>
    </body></html>
    """
    md_outlook = parse_message_body(html_outlook_class, is_html=True)
    assert "Current contribution R5s260009 is ready." in md_outlook
    assert "R1-111111" not in md_outlook
    assert extract_tdocs(md_outlook) == ("R5S260009",)


def test_parse_message_body_html_strips_angle_bracket_quoted_lines_and_on_wrote_blocks() -> None:
    from doc3gpp.parsers.listserv_detail import extract_tdocs, parse_message_body

    html = """
    <html><body>
      <p>Current discussion on R1-2610793 with inline comparison gain &gt; 3 dB.</p>
      <p>&gt; Standalone quoted line referencing previous R1-111111</p>
      <p>On Mon, Oct 1, 2026 at 08:00 AM Someone wrote:<br>
      &gt; Previous mail body mentioning R2-222222<br>
      &gt; Another quoted line with R3-333333</p>
    </body></html>
    """
    markdown = parse_message_body(html, is_html=True)
    assert "Current discussion on R1-2610793" in markdown
    assert "3 dB" in markdown
    assert "R1-111111" not in markdown
    assert "R2-222222" not in markdown
    assert "R3-333333" not in markdown
    assert extract_tdocs(markdown) == ("R1-2610793",)



def test_parse_detail_page_preserves_named_text_plain_and_text_html_attachments() -> None:
    from doc3gpp.parsers.listserv_detail import parse_detail_page

    html = """
    <html><body>
      <table>
        <tr>
          <th>Parts/Attachments:</th>
          <td>
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=1&amp;T=text%2Fplain;%20charset=UTF-8">text/plain</a> (10 lines) ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=2&amp;T=text%2Fhtml;%20charset=UTF-8">text/html</a> (15 lines) ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=3&amp;T=multipart%2Frelated">multipart/related</a> ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=4&amp;T=text%2Fplain;%20charset=UTF-8">R1-2610793_notes.txt</a> (4 kB) ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=5&amp;T=text%2Fhtml;%20charset=UTF-8&amp;N=RP-2601234_table.html">RP-2601234_table.html</a> (8 kB) ,
            <a href="/scripts/wa.exe?A3=ind2610A&amp;L=EXAMPLE&amp;P=6&amp;T=application%2Fxhtml%2Bxml">R5s260009_report.xhtml</a> (6 kB)
          </td>
        </tr>
      </table>
    </body></html>
    """
    body_url, attachments, inline_body = parse_detail_page(html, BASE_DETAIL_URL)

    # Generic text/html alternative is chosen as the body representation, not the named HTML attachment
    assert (
        body_url
        == "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=2&T=text%2Fhtml;%20charset=UTF-8"
    )
    assert inline_body is None
    assert attachments == (
        ListservAttachment(
            name="R1-2610793_notes.txt",
            url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=4&T=text%2Fplain;%20charset=UTF-8",
        ),
        ListservAttachment(
            name="RP-2601234_table.html",
            url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=5&T=text%2Fhtml;%20charset=UTF-8&N=RP-2601234_table.html",
        ),
        ListservAttachment(
            name="R5s260009_report.xhtml",
            url="https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=6&T=application%2Fxhtml%2Bxml",
        ),
    )
