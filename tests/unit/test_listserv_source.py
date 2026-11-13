"""Unit tests for restricted ETSI LISTSERV transport (:mod:`doc3gpp.scraping.listserv_source`)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import patch

import httpx
import pytest

from doc3gpp.scraping.listserv_source import (
    FetchedPage,
    ListservAuthRequiredError,
    ListservRedirectError,
    ListservSource,
    ListservSourceError,
    archive_index_url,
    validate_listserv_url,
)


@dataclass
class RecordingSource:
    """Wrapper around :class:`ListservSource` that records every transmitted request URL."""

    source: ListservSource
    requests: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> FetchedPage:
        return self.source.fetch(url)

    def close(self) -> None:
        self.source.close()


def _make_recording_source(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 2,
    retry_backoff: float = 0.0,
    max_redirects: int = 5,
) -> RecordingSource:
    recorded: list[str] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        recorded.append(str(request.url))
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    source = ListservSource(
        transport=transport,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        max_redirects=max_redirects,
    )
    return RecordingSource(source=source, requests=recorded)


@pytest.fixture
def recording_source() -> RecordingSource:
    rec = _make_recording_source(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html><body>OK</body></html>",
            request=req,
        )
    )
    yield rec
    rec.close()


# ---------------------------------------------------------------------------
# archive_index_url tests
# ---------------------------------------------------------------------------


def test_archive_index_url_targets_encoded_a0_parameter() -> None:
    url = archive_index_url("3gpp_tsg_ran_wg1")
    assert url == "https://list.etsi.org/scripts/wa.exe?A0=3GPP_TSG_RAN_WG1"


ITEM_URL = "https://list.etsi.org/scripts/wa.exe?A2=3GPP_TSG_RAN_WG5_SIG;5d45a78f.2611B&S="


def test_item_url_canonical_identity_and_aliases() -> None:
    from doc3gpp.scraping.listserv_source import (
        listserv_item_url_aliases,
        validate_listserv_item_url,
    )

    assert validate_listserv_item_url(ITEM_URL) == (ITEM_URL, "3GPP_TSG_RAN_WG5_SIG")
    assert validate_listserv_item_url(ITEM_URL.removesuffix("&S=")) == (
        ITEM_URL, "3GPP_TSG_RAN_WG5_SIG"
    )
    assert set(listserv_item_url_aliases(ITEM_URL)) == {ITEM_URL, ITEM_URL.removesuffix("&S=")}


@pytest.mark.parametrize(
    "bad_url",
    [
        ITEM_URL.replace("https://", "http://"),
        ITEM_URL.replace("list.etsi.org", "evil.example"),
        ITEM_URL.replace("list.etsi.org", "user@list.etsi.org"),
        ITEM_URL.replace("list.etsi.org", "list.etsi.org:444"),
        ITEM_URL.replace("/scripts/wa.exe", "/scripts/other"),
        ITEM_URL + "#fragment",
        ITEM_URL.replace("A2=", "A0="),
        ITEM_URL + "&A2=OTHER;abc.2611A",
        ITEM_URL.replace("3GPP_TSG_RAN_WG5_SIG", ""),
        ITEM_URL.replace("5d45a78f", ""),
        ITEM_URL.replace("2611B", ""),
        ITEM_URL.replace("2611B", "2613B"),
        ITEM_URL.replace("2611B", "2611F"),
        ITEM_URL.replace(";", ""),
        ITEM_URL + "&X=1",
        ITEM_URL + "value",
        ITEM_URL + "&S=",
        ITEM_URL.replace("/scripts/wa.exe", "/file.pdf"),
        " " + ITEM_URL,
        ITEM_URL + "\n",
        ITEM_URL.replace(";", "%3B"),
        ITEM_URL.replace("&S=", "%26S%3D"),
    ],
)
def test_item_url_rejects_unsafe_or_malformed_input(bad_url: str) -> None:
    from doc3gpp.scraping.listserv_source import validate_listserv_item_url

    with (
        patch.object(ListservSource, "fetch", side_effect=AssertionError("network used")),
        pytest.raises(ValueError),
    ):
        validate_listserv_item_url(bad_url)


def test_archive_index_url_preserves_unicode_without_allowlist() -> None:
    url = archive_index_url("  custom_list_ümlaut-1  ")
    assert url == "https://list.etsi.org/scripts/wa.exe?A0=CUSTOM_LIST_%C3%9CMLAUT-1"


@pytest.mark.parametrize(
    "bad_list_id",
    [
        "",
        "   ",
        "bad list",
        "bad\nlist",
        "bad\x00list",
        "LIST&A1=ind2610A",
        "LIST?A0=OTHER",
        "LIST/../../etc/passwd",
        "LIST#frag",
        "LIST=1",
        "https://list.etsi.org/scripts/wa.exe",
    ],
)
def test_archive_index_url_rejects_empty_or_invalid_list_id(bad_list_id: str) -> None:
    with pytest.raises(ValueError):
        archive_index_url(bad_list_id)


# ---------------------------------------------------------------------------
# validate_listserv_url tests & initial/iframe pre-flight rejection
# ---------------------------------------------------------------------------


def test_lookalike_host_rejected_before_fetch(recording_source: RecordingSource) -> None:
    with pytest.raises(ValueError):
        recording_source.fetch("https://list.etsi.org.evil.example/scripts/wa.exe")
    assert recording_source.requests == []


@pytest.mark.parametrize(
    "invalid_url",
    [
        "",
        "   ",
        "http://list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "ftp://list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "javascript:alert(1)",
        "/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org.evil.example/scripts/wa.exe?A0=EXAMPLE",
        "https://sub.list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "https://evil.example/scripts/wa.exe?A0=EXAMPLE",
        "https://user:pass@list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "https://user@list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org:8443/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org:80/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org:/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org/scripts/../etc/passwd",
        "https://list.etsi.org/scripts/%2e%2e/etc/passwd",
        # Attachment targets must be rejected before transmission
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&B=--part&T=application%2Fzip&N=R1-2610793.zip",
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&B=--part&N=spec.docx",
        "https://list.etsi.org/att/R1-2610793.docx",
        "https://list.etsi.org/files/archive.zip",
        "https://list.etsi.org/files/att%61chments/current",
    ],
)
def test_invalid_or_attachment_urls_rejected_without_request(
    recording_source: RecordingSource, invalid_url: str
) -> None:
    with pytest.raises(ValueError):
        validate_listserv_url(invalid_url)
    with pytest.raises(ValueError):
        recording_source.fetch(invalid_url)
    assert recording_source.requests == []


def test_validate_listserv_url_accepts_default_443_and_body_parts() -> None:
    assert (
        validate_listserv_url("https://list.etsi.org:443/scripts/wa.exe?A0=EXAMPLE")
        == "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"
    )
    iframe_html = (
        "https://list.etsi.org/scripts/wa.exe"
        "?A3=ind2610A&L=EXAMPLE&E=quoted-printable&P=1&B=--part&T=text%2Fhtml;%20charset=UTF-8"
    )
    assert validate_listserv_url(iframe_html) == iframe_html
    iframe_plain = (
        "https://list.etsi.org/scripts/wa.exe"
        "?A3=ind2610A&L=EXAMPLE&E=quoted-printable&P=1&B=--part&T=text%2Fplain;%20charset=UTF-8"
    )
    assert validate_listserv_url(iframe_plain) == iframe_plain


# ---------------------------------------------------------------------------
# Fetch decoding, content-type, and login HTML passthrough
# ---------------------------------------------------------------------------


def test_fetch_decodes_utf8_and_returns_content_type() -> None:
    html_text = "<html><body><h1>3GPP RAN1 — München αβγ</h1></body></html>"
    rec = _make_recording_source(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=html_text.encode("utf-8"),
            request=req,
        )
    )
    try:
        url = archive_index_url("3gpp_tsg_ran_wg1")
        page = rec.fetch(url)
        assert page == FetchedPage(
            text=html_text,
            content_type="text/html",
            final_url=url,
        )
        assert rec.requests == [url]
    finally:
        rec.close()


def test_fetch_decodes_declared_header_charset() -> None:
    html_text = "<html><body>Café résumé naïve</body></html>"
    rec = _make_recording_source(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=ISO-8859-1"},
            content=html_text.encode("iso-8859-1"),
            request=req,
        )
    )
    try:
        page = rec.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert page.text == html_text
        assert page.content_type == "text/html"
    finally:
        rec.close()


def test_fetch_decodes_declared_meta_charset_when_header_omits_charset() -> None:
    html_text = (
        '<html><head><meta charset="iso-8859-1"></head>'
        "<body>Größe &amp; Café</body></html>"
    )
    rec = _make_recording_source(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=html_text.encode("iso-8859-1"),
            request=req,
        )
    )
    try:
        page = rec.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert page.text == html_text
        assert page.content_type == "text/html"
    finally:
        rec.close()


def test_fetch_passes_login_html_through_for_parser_detection() -> None:
    login_html = (
        "<html><head><title>Login Required</title></head>"
        '<body><form><input type="password" name="p"></form></body></html>'
    )
    rec = _make_recording_source(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=login_html.encode("utf-8"),
            request=req,
        )
    )
    try:
        page = rec.fetch("https://list.etsi.org/scripts/wa.exe?A0=PRIVATE_LIST")
        assert "Login Required" in page.text
        assert page.content_type == "text/html"
    finally:
        rec.close()


def test_fetch_rejects_attachment_response_headers() -> None:
    rec = _make_recording_source(
        lambda req: httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain",
                "Content-Disposition": 'attachment; filename="R1-2610793.txt"',
            },
            content=b"secret attachment payload",
            request=req,
        )
    )
    try:
        with pytest.raises(ValueError, match="attachment"):
            rec.fetch("https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1")
    finally:
        rec.close()


# ---------------------------------------------------------------------------
# Manual redirect handling and hop validation
# ---------------------------------------------------------------------------


def test_fetch_follows_valid_relative_and_absolute_redirects() -> None:
    start_url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"
    hop1_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE"
    hop2_url = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE&O=D"

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == start_url:
            return httpx.Response(
                302,
                headers={"Location": "/scripts/wa.exe?A1=ind2610A&L=EXAMPLE"},
                request=req,
            )
        if url == hop1_url:
            return httpx.Response(
                301,
                headers={"Location": hop2_url},
                request=req,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            content=b"Plain body text",
            request=req,
        )

    rec = _make_recording_source(handler)
    try:
        page = rec.fetch(start_url)
        assert page == FetchedPage(
            text="Plain body text",
            content_type="text/plain",
            final_url=hop2_url,
        )
        assert rec.requests == [start_url, hop1_url, hop2_url]
    finally:
        rec.close()


@pytest.mark.parametrize(
    "bad_location",
    [
        "http://list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org.evil.example/scripts/wa.exe?A0=EXAMPLE",
        "https://user:pass@list.etsi.org/scripts/wa.exe?A0=EXAMPLE",
        "https://list.etsi.org:8443/scripts/wa.exe?A0=EXAMPLE",
        "https://evil.example/phish",
        "/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&T=application%2Fzip&N=R1-2610793.zip",
    ],
)
def test_redirect_to_invalid_target_rejected_before_second_request(
    bad_location: str,
) -> None:
    start_url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"
    rec = _make_recording_source(
        lambda req: httpx.Response(
            302,
            headers={"Location": bad_location},
            request=req,
        )
    )
    try:
        with pytest.raises(ValueError):
            rec.fetch(start_url)
        assert rec.requests == [start_url]
    finally:
        rec.close()


def test_redirect_loop_fails_cleanly_without_logging_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    url_a = "https://list.etsi.org/scripts/wa.exe?A1=ind2610A&L=EXAMPLE"
    url_b = "https://list.etsi.org/scripts/wa.exe?A1=ind2610B&L=EXAMPLE"
    sensitive_body = "SENSITIVE_REDIRECT_BODY_MARKER_999"

    def handler(req: httpx.Request) -> httpx.Response:
        target = url_b if str(req.url) == url_a else url_a
        return httpx.Response(
            302,
            headers={"Location": target},
            content=sensitive_body.encode("utf-8"),
            request=req,
        )

    rec = _make_recording_source(handler, max_redirects=4)
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(
            (ListservRedirectError, ValueError)
        ):
            rec.fetch(url_a)
        assert sensitive_body not in caplog.text
    finally:
        rec.close()


def test_redirect_hop_limit_exceeded_fails_cleanly() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        hop = int(req.url.params.get("hop", "0")) + 1
        return httpx.Response(
            302,
            headers={"Location": f"/scripts/wa.exe?A0=EXAMPLE&hop={hop}"},
            request=req,
        )

    rec = _make_recording_source(handler, max_redirects=3)
    try:
        with pytest.raises((ListservRedirectError, ValueError)):
            rec.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE&hop=0")
        assert len(rec.requests) == 4  # initial + 3 redirects before failing on 4th redirect
    finally:
        rec.close()


# ---------------------------------------------------------------------------
# Retries, 401/403 actionable errors, and log hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 408, 429])
def test_retryable_status_codes_follow_retry_limits_then_succeed(status_code: int) -> None:
    url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                status_code,
                content=b"SENSITIVE_ERROR_BODY_DO_NOT_LOG",
                request=req,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html>Recovered</html>",
            request=req,
        )

    sleeps: list[float] = []
    rec = _make_recording_source(handler, max_retries=2, retry_backoff=0.25)
    try:
        with patch.object(
            ListservSource, "_sleep", staticmethod(lambda s: sleeps.append(s))
        ):
            page = rec.fetch(url)
        assert page.text == "<html>Recovered</html>"
        assert len(rec.requests) == 3
        assert sleeps == [0.25, 0.5]
    finally:
        rec.close()


def test_retryable_transport_exception_exhausts_retries_without_logging_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out reading response", request=req)

    rec = _make_recording_source(handler, max_retries=2, retry_backoff=0.0)
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(httpx.ReadTimeout):
            rec.fetch(url)
        assert len(rec.requests) == 3
    finally:
        rec.close()


@pytest.mark.parametrize("status_code", [401, 403])
def test_401_and_403_raise_actionable_error_without_retrying_or_logging_body(
    status_code: int, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://list.etsi.org/scripts/wa.exe?A0=PRIVATE_WG"
    secret_body = "PRIVATE_LIST_BODY_CONTENT_12345"

    rec = _make_recording_source(
        lambda req: httpx.Response(
            status_code,
            headers={"Content-Type": "text/html"},
            content=secret_body.encode("utf-8"),
            request=req,
        ),
        max_retries=3,
    )
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(
            ListservAuthRequiredError, match=r"(?i)public|authentication|private"
        ):
            rec.fetch(url)
        assert len(rec.requests) == 1
        assert secret_body not in caplog.text
    finally:
        rec.close()


# ---------------------------------------------------------------------------
# Ownership, idempotent close, and exception-path cleanup
# ---------------------------------------------------------------------------


def test_owned_client_closed_idempotently() -> None:
    source = ListservSource(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"ok", request=req)
        )
    )
    with patch.object(source._client, "close", wraps=source._client.close) as close_spy:
        source.close()
        source.close()
        assert close_spy.call_count == 1


def test_context_manager_cleans_up_owned_client_on_exception() -> None:
    source = ListservSource(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"ok", request=req)
        )
    )
    with patch.object(source._client, "close", wraps=source._client.close) as close_spy:
        with pytest.raises(RuntimeError, match="boom"), source:
            raise RuntimeError("boom")
        assert close_spy.call_count == 1


def test_injected_client_remains_caller_owned_on_close_and_context_exit() -> None:
    injected_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html>Caller Owned</html>",
                request=req,
            )
        )
    )

    with ListservSource(client=injected_client) as source:
        page = source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert page.text == "<html>Caller Owned</html>"
    source.close()

    assert injected_client.is_closed is False
    injected_client.close()


def test_injected_redirect_following_client_does_not_bypass_hop_validation() -> None:
    start_url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"
    untrusted_url = "https://list.etsi.org.evil.example/scripts/wa.exe?A0=EXAMPLE"
    requests: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(str(req.url))
        if str(req.url) == start_url:
            return httpx.Response(
                302,
                headers={"Location": untrusted_url},
                request=req,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html>Should never be fetched</html>",
            request=req,
        )

    injected_client = httpx.Client(
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    try:
        source = ListservSource(client=injected_client)
        with pytest.raises(ValueError):
            source.fetch(start_url)
        assert requests == [start_url]
    finally:
        injected_client.close()


@pytest.mark.parametrize(
    "smuggled_url",
    [
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&T=application%2Fzip&t=text%2Fhtml",
        "https://list.etsi.org/scripts/wa.exe?a3=ind2610A&L=EXAMPLE&P=1&N=R1-2610793.zip&n=",
        "https://list.etsi.org/scripts/wa.exe?A3=ind2610A&L=EXAMPLE&P=1&disposition=attachment&DISPOSITION=inline",
    ],
)
def test_mixed_case_duplicate_query_keys_cannot_hide_attachment_parameters(
    recording_source: RecordingSource, smuggled_url: str
) -> None:
    with pytest.raises(ValueError):
        validate_listserv_url(smuggled_url)
    with pytest.raises(ValueError):
        recording_source.fetch(smuggled_url)
    assert recording_source.requests == []


def test_listserv_source_honors_explicit_settings_argument() -> None:
    from doc3gpp.settings.schema import Settings

    custom_settings = Settings(
        http_verify=False,
        http_max_retries=4,
        http_retry_backoff=0.125,
    )
    attempts = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 4:
            return httpx.Response(503, request=req)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<html>OK</html>",
            request=req,
        )

    sleeps: list[float] = []
    with (
        patch("doc3gpp.scraping.listserv_source.httpx.Client", wraps=httpx.Client) as client_spy,
        patch.object(ListservSource, "_sleep", staticmethod(lambda s: sleeps.append(s))),
    ):
        source = ListservSource(
            settings=custom_settings,
            transport=httpx.MockTransport(handler),
        )
        try:
            assert client_spy.call_args.kwargs["verify"] is False
            page = source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
            assert page.text == "<html>OK</html>"
            assert attempts == 5
            assert sleeps == [0.125, 0.25, 0.5, 1.0]
        finally:
            source.close()



def test_attachment_response_is_rejected_before_stream_iteration_and_closed() -> None:
    class ObservableStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.iterated = False
            self.closed = False

        def __iter__(self):
            self.iterated = True
            yield b"secret attachment payload"

        def close(self) -> None:
            self.closed = True

    stream = ObservableStream()
    responses: list[httpx.Response] = []

    def handler(req: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain",
                "Content-Disposition": 'attachment; filename="archive.txt"',
            },
            stream=stream,
            request=req,
        )
        responses.append(response)
        return response

    source = ListservSource(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ListservSourceError, match="attachment"):
            source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert stream.iterated is False
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


def test_fetch_rejects_response_over_decoded_size_limit_and_closes() -> None:
    import gzip

    limit = 16 * 1024 * 1024
    decoded_body = b"x" * (limit + 1)
    compressed_body = gzip.compress(decoded_body)

    class ObservableStream(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self):
            yield compressed_body

        def close(self) -> None:
            self.closed = True

    stream = ObservableStream()
    responses: list[httpx.Response] = []

    def handler(req: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
            stream=stream,
            request=req,
        )
        responses.append(response)
        return response

    source = ListservSource(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ListservSourceError, match="16 MiB"):
            source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


def test_fetch_bounded_decompression_allocation_in_fresh_process() -> None:
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    script = textwrap.dedent(
        """\
        import gzip
        import tracemalloc

        import httpx

        from doc3gpp.scraping.listserv_source import ListservSource, ListservSourceError

        compressed_body = gzip.compress(b"x" * (64 * 1024 * 1024))

        class ObservableStream(httpx.SyncByteStream):
            def __init__(self):
                self.closed = False

            def __iter__(self):
                yield compressed_body

            def close(self):
                self.closed = True

        stream = ObservableStream()
        responses = []

        def handler(request):
            response = httpx.Response(
                200,
                headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
                stream=stream,
                request=request,
            )
            responses.append(response)
            return response

        source = ListservSource(transport=httpx.MockTransport(handler), max_retries=0)
        tracemalloc.start()
        try:
            try:
                source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
            except ListservSourceError as exc:
                assert "16 MiB" in str(exc)
            else:
                raise AssertionError("oversized decoded body was accepted")
        finally:
            source.close()
        assert responses[0].is_closed
        assert stream.closed
        _, peak = tracemalloc.get_traced_memory()
        print(peak)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) < 48 * 1024 * 1024


def test_listserv_requests_override_accept_encoding_on_each_redirect_hop() -> None:
    start_url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE"
    next_url = "https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE&P=2"
    observed: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((str(request.url), request.headers.get("accept-encoding")))
        if str(request.url) == start_url:
            return httpx.Response(302, headers={"Location": next_url}, request=request)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"ok", request=request)

    client = httpx.Client(
        headers={"Accept-Encoding": "br, zstd"},
        follow_redirects=True,
        transport=httpx.MockTransport(handler),
    )
    source = ListservSource(client=client, max_retries=0)
    try:
        assert source.fetch(start_url).text == "ok"
        assert observed == [
            (start_url, "gzip, deflate, identity"),
            (next_url, "gzip, deflate, identity"),
        ]
    finally:
        source.close()
        client.close()


class _FragmentedResponseStream(httpx.SyncByteStream):
    def __init__(self, body: bytes, fragment_size: int) -> None:
        self.body = body
        self.fragment_size = fragment_size
        self.iterated = False
        self.closed = False

    def __iter__(self):
        self.iterated = True
        for start in range(0, len(self.body), self.fragment_size):
            yield self.body[start : start + self.fragment_size]

    def close(self) -> None:
        self.closed = True


def _make_encoded_source(
    wire_body: bytes, encoding: str | None, *, fragment_size: int = 64 * 1024
) -> tuple[ListservSource, list[httpx.Response], _FragmentedResponseStream]:
    stream = _FragmentedResponseStream(wire_body, fragment_size)
    responses: list[httpx.Response] = []
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if encoding is not None:
        headers["Content-Encoding"] = encoding

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(200, headers=headers, stream=stream, request=request)
        responses.append(response)
        return response

    source = ListservSource(transport=httpx.MockTransport(handler), max_retries=0)
    return source, responses, stream


def _encode_http_body(body: bytes, encoding: str) -> bytes:
    import gzip
    import zlib

    if encoding == "identity":
        return body
    if encoding == "gzip":
        return gzip.compress(body)
    if encoding == "deflate":
        return zlib.compress(body)
    raise AssertionError(f"unknown test encoding: {encoding}")


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.parametrize("decoded_body", [b"", b"LISTSERV response body"])
def test_fetch_decodes_supported_content_encodings(
    encoding: str, decoded_body: bytes
) -> None:
    source, responses, stream = _make_encoded_source(
        _encode_http_body(decoded_body, encoding), encoding, fragment_size=3
    )
    try:
        page = source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert page.text == decoded_body.decode("ascii")
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


def test_fetch_treats_missing_content_encoding_as_identity() -> None:
    source, _, _ = _make_encoded_source(b"plain body", None)
    try:
        assert source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE").text == "plain body"
    finally:
        source.close()


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_fetch_accepts_exact_decoded_body_limit_with_trailer(encoding: str) -> None:
    decoded_body = b"x" * (16 * 1024 * 1024)
    source, responses, stream = _make_encoded_source(
        _encode_http_body(decoded_body, encoding), encoding, fragment_size=8191
    )
    try:
        page = source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert len(page.text) == 16 * 1024 * 1024
        assert page.text[:1] == "x" and page.text[-1:] == "x"
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_fetch_rejects_one_byte_over_decoded_body_limit(encoding: str) -> None:
    decoded_body = b"x" * (16 * 1024 * 1024 + 1)
    source, responses, stream = _make_encoded_source(
        _encode_http_body(decoded_body, encoding), encoding, fragment_size=8191
    )
    try:
        with pytest.raises(ListservSourceError, match="16 MiB"):
            source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


@pytest.mark.parametrize("extra_byte", [0, 1])
def test_fetch_counts_concatenated_gzip_members_toward_one_limit(extra_byte: int) -> None:
    import gzip

    first_size = 8 * 1024 * 1024
    second_size = 8 * 1024 * 1024 + extra_byte
    wire_body = gzip.compress(b"a" * first_size) + gzip.compress(b"b" * second_size)
    source, _, _ = _make_encoded_source(wire_body, "gzip", fragment_size=7)
    try:
        if extra_byte:
            with pytest.raises(ListservSourceError, match="16 MiB"):
                source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        else:
            page = source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
            assert len(page.text) == 16 * 1024 * 1024
            assert page.text[:1] == "a" and page.text[-1:] == "b"
    finally:
        source.close()


@pytest.mark.parametrize("raw_deflate", [False, True])
def test_fetch_decodes_zlib_and_raw_deflate_with_one_byte_fragments(
    raw_deflate: bool
) -> None:
    import zlib

    body = b"A raw or zlib-wrapped deflate body with fragmented input."
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS if raw_deflate else zlib.MAX_WBITS)
    wire_body = compressor.compress(body) + compressor.flush()
    source, _, _ = _make_encoded_source(wire_body, "deflate", fragment_size=1)
    original_iter_raw = httpx.Response.iter_raw

    def one_byte_iter_raw(response: httpx.Response, chunk_size: int | None = None):
        return original_iter_raw(response, chunk_size=1)

    try:
        with patch.object(httpx.Response, "iter_raw", one_byte_iter_raw):
            page = source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert page.text == body.decode("ascii")
    finally:
        source.close()


@pytest.mark.parametrize(
    "malformed_kind",
    [
        "invalid_gzip",
        "truncated_gzip",
        "bad_gzip_checksum",
        "gzip_trailing_junk",
        "truncated_deflate",
        "invalid_deflate",
        "deflate_trailing_junk",
    ],
)
def test_fetch_rejects_malformed_content_encoding(malformed_kind: str) -> None:
    import gzip
    import zlib

    gzip_body = gzip.compress(b"valid gzip body")
    deflate_body = zlib.compress(b"valid deflate body")
    cases = {
        "invalid_gzip": ("gzip", b"not gzip"),
        "truncated_gzip": ("gzip", gzip_body[:-1]),
        "bad_gzip_checksum": ("gzip", gzip_body[:-8] + bytes([gzip_body[-8] ^ 1]) + gzip_body[-7:]),
        "gzip_trailing_junk": ("gzip", gzip_body + b"junk"),
        "truncated_deflate": ("deflate", deflate_body[:-1]),
        "invalid_deflate": ("deflate", b"not deflate"),
        "deflate_trailing_junk": ("deflate", deflate_body + b"junk"),
    }
    encoding, wire_body = cases[malformed_kind]
    source, responses, stream = _make_encoded_source(wire_body, encoding, fragment_size=1)
    try:
        with pytest.raises(ListservSourceError, match="(?i)(malformed|invalid|incomplete|encoding)"):
            source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


@pytest.mark.parametrize(
    "encoding", ["br", "zstd", "compress", "x-unknown", "gzip, deflate", "identity, gzip"]
)
def test_fetch_rejects_unsupported_or_stacked_encoding_before_iteration(encoding: str) -> None:
    source, responses, stream = _make_encoded_source(b"body", encoding, fragment_size=1)
    try:
        with pytest.raises(ListservSourceError, match="(?i)encoding"):
            source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert stream.iterated is False
        assert responses[0].is_closed
        assert stream.closed
    finally:
        source.close()


def test_fetch_rejects_preconsumed_compressed_response_without_redecoding() -> None:
    import gzip

    responses: list[httpx.Response] = []
    wire_body = gzip.compress(b"already buffered response")

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
            content=wire_body,
            request=request,
        )
        responses.append(response)
        return response

    source = ListservSource(transport=httpx.MockTransport(handler), max_retries=0)
    try:
        with pytest.raises(ListservSourceError, match="pre-consumed|already consumed"):
            source.fetch("https://list.etsi.org/scripts/wa.exe?A0=EXAMPLE")
        assert responses[0].is_closed
    finally:
        source.close()
