"""Unit tests for ``doc3gpp.parsers.direct_extractor.list_3gpp_directory``."""

from __future__ import annotations

import httpx
import pytest

from doc3gpp.parsers.direct_extractor import (
    NotAFolderError,
    list_3gpp_directory,
)


class _FakeClient:
    """In-memory scraper that maps URLs to response bodies or exceptions."""

    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses
        self.fetched: list[str] = []

    def get_text(self, url: str) -> str:
        self.fetched.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _folder_html(*entries: str) -> str:
    return f"<html><body>{''.join(entries)}</body></html>"


def test_list_3gpp_directory_rejects_non_3gpp_url() -> None:
    client = _FakeClient({})
    with pytest.raises(ValueError, match="not a 3GPP FTP URL"):
        list_3gpp_directory("https://example.com/files/", client=client)


def test_list_3gpp_directory_raises_when_url_is_binary_file() -> None:
    client = _FakeClient(
        {"https://www.3gpp.org/ftp/R5s260043.zip": httpx.HTTPError("not html")}
    )
    with pytest.raises(NotAFolderError):
        list_3gpp_directory(
            "https://www.3gpp.org/ftp/R5s260043.zip", client=client
        )


def test_list_3gpp_directory_raises_when_response_is_not_html() -> None:
    client = _FakeClient(
        {"https://www.3gpp.org/ftp/docs/": "PK\x03\x04 zip bytes"}
    )
    with pytest.raises(NotAFolderError):
        list_3gpp_directory(
            "https://www.3gpp.org/ftp/docs/", client=client
        )


def test_list_3gpp_directory_raises_when_no_anchors() -> None:
    client = _FakeClient(
        {"https://www.3gpp.org/ftp/empty/": "<html><body></body></html>"}
    )
    with pytest.raises(NotAFolderError):
        list_3gpp_directory(
            "https://www.3gpp.org/ftp/empty/", client=client
        )


def test_list_3gpp_directory_classifies_files_and_subfolders() -> None:
    base = "https://www.3gpp.org/ftp/tsg_ran/WG5/Test_2026/Docs/"
    html = _folder_html(
        '<a href="../">Parent Directory</a>',
        '<a href="Inbox/">Inbox/</a>',
        '<a href="review/">review/</a>',
        '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
        '<a class="file" href="R5s260002_MCC160Comments.zip">R5s260002_MCC160Comments.zip</a>',
        '<a class="file" href="R5s260003.docx">R5s260003.docx</a>',
        '<a class="file" href="agenda.pdf">agenda.pdf</a>',
        '<a class="file" href="R5s260004.txt">R5s260004.txt</a>',
        '<a class="file" href="noid.zip">noid.zip</a>',
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.folder_url == base
    assert listing.file_urls == (
        f"{base}R5s260001.zip",
        f"{base}R5s260002_MCC160Comments.zip",
        f"{base}R5s260003.docx",
    )
    assert listing.subfolder_urls == (
        f"{base}Inbox/",
        f"{base}review/",
    )


def test_list_3gpp_directory_resolves_relative_hrefs() -> None:
    base = "https://www.3gpp.org/ftp/tsg_ran/WG5/Test_2026/Docs/"
    html = _folder_html(
        '<a class="file" href="sub/R5s260001.zip">sub/R5s260001.zip</a>',
        '<a href="sub/">sub/</a>',
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.file_urls == (f"{base}sub/R5s260001.zip",)
    assert listing.subfolder_urls == (f"{base}sub/",)


def test_list_3gpp_directory_deduplicates_urls() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = _folder_html(
        '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
        '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.file_urls == (f"{base}R5s260001.zip",)


def test_list_3gpp_directory_returns_empty_for_no_matching_files() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = _folder_html(
        '<a href="Inbox/">Inbox/</a>',
        '<a class="file" href="agenda.pdf">agenda.pdf</a>',
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.file_urls == ()
    assert listing.subfolder_urls == (f"{base}Inbox/",)


def test_list_3gpp_directory_classifies_subfolder_without_trailing_slash() -> None:
    """Real 3GPP FTP listings omit the trailing slash on content subfolders."""
    base = "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_2026/"
    html = _folder_html(
        '<a href="Docs">Docs</a>',
        '<a href="Review">Review</a>',
        '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.file_urls == (f"{base}R5s260001.zip",)
    assert listing.subfolder_urls == (
        f"{base}Docs",
        f"{base}Review",
    )


def test_list_3gpp_directory_skips_breadcrumb_ancestors_and_sort_links() -> None:
    base = "https://www.3gpp.org/ftp/tsg_ran/WG5_Test_2026/Docs/"
    html = _folder_html(
        '<a href="https://www.3gpp.org/ftp/tsg_ran/">tsg_ran</a>',
        '<a href="https://www.3gpp.org/ftp/tsg_ran/WG5_Test_2026/">WG5_Test_2026</a>',
        '<a href="?sortby=name">sort by name</a>',
        '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.file_urls == (f"{base}R5s260001.zip",)
    assert listing.subfolder_urls == ()


def test_list_3gpp_directory_uses_folder_icon_when_present() -> None:
    base = "https://www.3gpp.org/ftp/Docs/"
    html = (
        '<html><body><table>'
        '<tr><td><img class="icon" src="/ftp/geticon.axd?file="></td>'
        '<td><a href="sub">sub</a></td></tr>'
        '<tr><td><img class="icon" src="/ftp/geticon.axd?file=.zip"></td>'
        '<td><a class="file" href="R5s260001.zip">R5s260001.zip</a></td></tr>'
        '</table></body></html>'
    )
    client = _FakeClient({base: html})
    listing = list_3gpp_directory(base, client=client)

    assert listing.file_urls == (f"{base}R5s260001.zip",)
    assert listing.subfolder_urls == (f"{base}sub",)
