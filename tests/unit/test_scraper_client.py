"""Unit tests for :class:`ScraperClient` retry/backoff and User-Agent.

Covers:
- #5 retry transient errors with exponential backoff
- #5 raise immediately on non-retryable errors
- #5 retry on retryable HTTP status codes (5xx, 408, 429)
- #5 give up after ``max_retries`` exhausted
- #18 correct User-Agent header is set on every request
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from doc3gpp.scraping.client import ScraperClient, _RETRYABLE_STATUS_CODES


def _build_response(status_code: int) -> httpx.Response:
    """Build a minimal httpx.Response with the given status code."""
    request = httpx.Request("GET", "https://example.com/")
    return httpx.Response(status_code, request=request)


def _client_with_http_client(http_client: MagicMock) -> ScraperClient:
    """Wrap a ScraperClient around a mocked httpx.Client."""
    client = ScraperClient.__new__(ScraperClient)
    client._client = http_client
    client._max_retries = 3
    client._backoff = 0.0  # avoid real sleeping in tests
    return client


# ---------------------------------------------------------------------------
# #5 retry-on-transient: connection errors are retried until success
# ---------------------------------------------------------------------------


def test_get_text_retries_on_connect_error_then_succeeds() -> None:
    http_client = MagicMock()
    http_client.get.side_effect = [
        httpx.ConnectError("conn refused"),
        httpx.ConnectError("conn refused"),
        _build_response(200),
    ]

    client = _client_with_http_client(http_client)
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with patch.object(ScraperClient, "_sleep", staticmethod(fake_sleep)):
        body = client.get_text("https://example.com/x")

    assert body == ""
    assert http_client.get.call_count == 3
    # Backoff doubled between retries (0 * 2^0, 0 * 2^1) since we set factor 0.
    assert sleep_calls == [0.0, 0.0]


def test_get_bytes_retries_on_timeout() -> None:
    success_response = httpx.Response(
        200, content=b"hello", request=httpx.Request("GET", "https://example.com/x")
    )
    http_client = MagicMock()
    http_client.get.side_effect = [
        httpx.ReadTimeout("slow"),
        success_response,
    ]

    client = _client_with_http_client(http_client)

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        body = client.get_bytes("https://example.com/x")

    assert body == b"hello"
    assert http_client.get.call_count == 2


def test_head_uses_retry_policy_and_returns_response() -> None:
    request = httpx.Request("HEAD", "https://example.com/x")
    success_response = httpx.Response(200, request=request)
    http_client = MagicMock()
    http_client.head.side_effect = [
        httpx.ConnectError("conn refused"),
        success_response,
    ]

    client = _client_with_http_client(http_client)

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        response = client.head("https://example.com/x")

    assert response is success_response
    assert http_client.head.call_count == 2


# ---------------------------------------------------------------------------
# #5 give up after max_retries and re-raise
# ---------------------------------------------------------------------------


def test_get_text_gives_up_after_max_retries() -> None:
    http_client = MagicMock()
    http_client.get.side_effect = httpx.ConnectError("conn refused")

    client = _client_with_http_client(http_client)
    client._max_retries = 2  # 1 initial + 2 retries = 3 attempts

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        with pytest.raises(httpx.HTTPError, match="conn refused"):
            client.get_text("https://example.com/x")

    assert http_client.get.call_count == 3


# ---------------------------------------------------------------------------
# #5 non-retryable errors raise immediately
# ---------------------------------------------------------------------------


def test_get_text_raises_immediately_on_non_retryable_error() -> None:
    http_client = MagicMock()
    http_client.get.side_effect = httpx.InvalidURL("not a url")

    client = _client_with_http_client(http_client)

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        with pytest.raises(httpx.InvalidURL):
            client.get_text("https://example.com/x")

    # No retries on programming-error class exceptions.
    assert http_client.get.call_count == 1


# ---------------------------------------------------------------------------
# #5 retryable HTTP status codes (5xx, 408, 429) are retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(_RETRYABLE_STATUS_CODES))
def test_get_text_retries_on_retryable_status(status: int) -> None:
    http_client = MagicMock()
    http_client.get.side_effect = [
        _build_response(status),
        _build_response(200),
    ]

    client = _client_with_http_client(http_client)

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        body = client.get_text("https://example.com/x")

    assert body == ""
    assert http_client.get.call_count == 2


def test_get_text_does_not_retry_on_404() -> None:
    http_client = MagicMock()
    http_client.get.return_value = _build_response(404)

    client = _client_with_http_client(http_client)

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        with pytest.raises(httpx.HTTPStatusError):
            client.get_text("https://example.com/missing")

    assert http_client.get.call_count == 1


# ---------------------------------------------------------------------------
# #5 exponential backoff doubles the delay between attempts
# ---------------------------------------------------------------------------


def test_backoff_doubles_between_attempts() -> None:
    http_client = MagicMock()
    http_client.get.side_effect = [
        httpx.ConnectError("e1"),
        httpx.ConnectError("e2"),
        httpx.ConnectError("e3"),
        _build_response(200),
    ]

    client = _client_with_http_client(http_client)
    client._backoff = 0.5
    sleeps: list[float] = []

    with patch.object(
        ScraperClient, "_sleep", staticmethod(lambda s: sleeps.append(s))
    ):
        client.get_text("https://example.com/x")

    # base 0.5, then doubled for each retry: 0.5, 1.0, 2.0
    assert sleeps == [0.5, 1.0, 2.0]


# ---------------------------------------------------------------------------
# #18 User-Agent header is set on every request
# ---------------------------------------------------------------------------


def test_user_agent_header_is_set() -> None:
    """The ScraperClient must set a non-placeholder User-Agent on init."""
    with patch("doc3gpp.scraping.client.get_settings") as get_settings:
        get_settings.return_value = MagicMock(
            http_verify=True, http_max_retries=0, http_retry_backoff=0.0
        )
        client = ScraperClient()

    user_agent = client._client.headers.get("User-Agent")
    assert user_agent is not None
    assert "https://github.com" not in user_agent or "jerrywang121" in user_agent
    assert "jerrywang121" in user_agent


def test_user_agent_includes_project_repo_url() -> None:
    """The User-Agent must identify the doc3gpp project (no placeholder)."""
    with patch("doc3gpp.scraping.client.get_settings") as get_settings:
        get_settings.return_value = MagicMock(
            http_verify=True, http_max_retries=0, http_retry_backoff=0.0
        )
        client = ScraperClient()

    ua = client._client.headers["User-Agent"]
    # Old placeholder: ``https://github.com`` with no project path.
    assert not ua.endswith("(+https://github.com)")
    assert "doc3gpp" in ua


# ---------------------------------------------------------------------------
# Regression — timeout must reach the underlying httpx client
# ---------------------------------------------------------------------------
# httpx 1.0 dropped ``timeout`` from ``Client.__init__``; we set it as a
# post-construct attribute so the same code works on 0.27+ and 1.0+.


def test_timeout_is_propagated_to_underlying_client() -> None:
    """The user-supplied timeout must reach the underlying httpx.Client."""
    with patch("doc3gpp.scraping.client.get_settings") as get_settings:
        get_settings.return_value = MagicMock(
            http_verify=True, http_max_retries=0, http_retry_backoff=0.0
        )
        client = ScraperClient(timeout_seconds=12.5)

    assert client._client.timeout == httpx.Timeout(12.5)


def test_default_timeout_is_applied_when_not_overridden() -> None:
    """Omitting ``timeout_seconds`` must use the documented default."""
    with patch("doc3gpp.scraping.client.get_settings") as get_settings:
        get_settings.return_value = MagicMock(
            http_verify=True, http_max_retries=0, http_retry_backoff=0.0
        )
        client = ScraperClient()

    assert client._client.timeout == httpx.Timeout(ScraperClient.DEFAULT_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Coverage for the "give up after max retries on 5xx" path
# ---------------------------------------------------------------------------


def test_give_up_after_max_retries_on_5xx() -> None:
    http_client = MagicMock()
    http_client.get.return_value = _build_response(503)
    client = _client_with_http_client(http_client)
    client._max_retries = 1  # 1 initial + 1 retry = 2 attempts

    with patch.object(ScraperClient, "_sleep", staticmethod(lambda _: None)):
        with pytest.raises(httpx.HTTPStatusError, match="503"):
            client.get_text("https://example.com/x")

    assert http_client.get.call_count == 2


# ---------------------------------------------------------------------------
# Coverage for the real _sleep helper (> 0 branch)
# ---------------------------------------------------------------------------


def test_sleep_helper_actually_sleeps_when_seconds_positive() -> None:
    """The non-mocked path of ``_sleep`` must honour positive durations."""
    with patch("doc3gpp.scraping.client.time.sleep") as mock_sleep:
        ScraperClient._sleep(0.25)
    mock_sleep.assert_called_once_with(0.25)


def test_sleep_helper_skips_zero_seconds() -> None:
    with patch("doc3gpp.scraping.client.time.sleep") as mock_sleep:
        ScraperClient._sleep(0.0)
    mock_sleep.assert_not_called()