"""Unit tests for ``TDocCrService.extract_from_url_batch``."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import DirectParseBatchResult, DirectParseResult
from doc3gpp.parsers.direct_extractor import NotAFolderError
from doc3gpp.services.tdoc_cr_service import TDocCrService


class _FakeCache:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path

    def put_bytes(self, key: str, payload: bytes, subdir: str) -> Path:
        path = self.path_for(key, subdir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def get_bytes(self, key: str, subdir: str) -> bytes | None:
        path = self.path_for(key, subdir)
        if path.exists():
            return path.read_bytes()
        return None

    def path_for(self, key: str, subdir: str) -> Path:
        return self.root / subdir / key


class _FakeScraper:
    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses
        self.fetched: list[str] = []

    def get_text(self, url: str) -> str:
        self.fetched.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def get_bytes(self, url: str) -> bytes:
        return b"PK"


class _FakeCrRepo:
    def __init__(self) -> None:
        self.by_url: dict[str, Any] = {}
        self.upsert_extract_meta_calls: list[Any] = []

    def get(self, tdoc_id: str) -> list[Any]:
        return []

    def get_by_url(self, url: str) -> Any | None:
        return self.by_url.get(url)

    def upsert(self, details: Any) -> None:
        self.by_url[details.ftp_url] = details

    def upsert_extract_meta(self, extract_meta: Any) -> None:
        self.upsert_extract_meta_calls.append(extract_meta)

    def get_extract_meta(self, tdoc_id: str) -> list[Any]:
        return []

    def get_extract_meta_by_url(self, url: str) -> Any | None:
        return None

    def list_all(self) -> list[Any]:
        return []


class _FakeCrTtcnRepo:
    """In-memory :class:`TDocCrTTCNDetailRepository` double."""

    def __init__(self) -> None:
        self.by_url: dict[str, Any] = {}

    def upsert(self, details: Any) -> None:
        self.by_url[details.ftp_url] = details

    def get_by_url(self, url: str) -> Any | None:
        return self.by_url.get(url)

    def get(self, tdoc_id: str) -> list[Any]:
        return []

    def list_all(self) -> list[Any]:
        return []


class _FakeTDocRepo:
    def __init__(self, ids: set[str] | None = None) -> None:
        self.ids = ids or set()

    def get_by_id(self, tdoc_id: str) -> TDoc | None:
        if tdoc_id in self.ids:
            return TDoc(tdoc_id=tdoc_id, type="CR")
        return None

    def upsert(self, tdoc: TDoc) -> None:
        pass

    def upsert_many(self, tdocs: list[TDoc]) -> int:
        return len(tdocs)

    def list(self, **_: Any) -> list[TDoc]:
        return []

    def list_tdoc_ids_for_meeting(self, meeting_id: int) -> list[str]:
        return []

    def list_with_meeting(self, **_: Any) -> list[Any]:
        return []


@pytest.fixture()
def service(tmp_path: Path) -> TDocCrService:
    return TDocCrService(
        cache=_FakeCache(tmp_path),
        scraper_client=_FakeScraper({}),
        cr_repository=_FakeCrRepo(),
        cr_ttcn_repository=_FakeCrTtcnRepo(),
        tdoc_repository=_FakeTDocRepo(),
    )


def _folder_html(*entries: str) -> str:
    return f"<html><body>{''.join(entries)}</body></html>"


def _make_result(tdoc_id: str, url: str) -> DirectParseResult:
    return DirectParseResult(
        source_kind="url-3gpp",
        markdown="",
        details=None,
        extract_meta=None,
        from_cache=False,
        persisted=False,
        tdoc_id=tdoc_id,
        tdoc_id_in_tdocs=True,
    )


def test_collect_3gpp_file_urls_root_only_when_max_depth_zero(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/Docs/"
    service._scraper = _FakeScraper(
        {
            root: _folder_html(
                '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
                '<a href="sub/">sub/</a>',
            ),
            f"{root}sub/": _folder_html(
                '<a class="file" href="R5s260002.zip">R5s260002.zip</a>',
            ),
        }
    )

    urls = service._collect_3gpp_file_urls(root, max_depth=0)
    assert urls == [f"{root}R5s260001.zip"]


def test_collect_3gpp_file_urls_recurses_to_max_depth(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/Docs/"
    sub = f"{root}sub/"
    subsub = f"{sub}subsub/"
    service._scraper = _FakeScraper(
        {
            root: _folder_html(
                '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
                '<a href="sub/">sub/</a>',
            ),
            sub: _folder_html(
                '<a class="file" href="R5s260002.zip">R5s260002.zip</a>',
                '<a href="subsub/">subsub/</a>',
            ),
            subsub: _folder_html(
                '<a class="file" href="R5s260003.zip">R5s260003.zip</a>',
            ),
        }
    )

    urls = service._collect_3gpp_file_urls(root, max_depth=2)
    assert urls == [
        f"{root}R5s260001.zip",
        f"{sub}R5s260002.zip",
        f"{subsub}R5s260003.zip",
    ]


def test_collect_3gpp_file_urls_stops_at_max_depth(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/Docs/"
    sub = f"{root}sub/"
    subsub = f"{sub}subsub/"
    service._scraper = _FakeScraper(
        {
            root: _folder_html('<a href="sub/">sub/</a>'),
            sub: _folder_html('<a href="subsub/">subsub/</a>'),
            subsub: _folder_html(
                '<a class="file" href="R5s260003.zip">R5s260003.zip</a>',
            ),
        }
    )

    urls = service._collect_3gpp_file_urls(root, max_depth=1)
    assert urls == []
    assert subsub not in service._scraper.fetched


def test_collect_3gpp_file_urls_skips_failed_subfolder(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/Docs/"
    sub = f"{root}sub/"
    service._scraper = _FakeScraper(
        {
            root: _folder_html(
                '<a class="file" href="R5s260001.zip">R5s260001.zip</a>',
                '<a href="sub/">sub/</a>',
            ),
            sub: httpx.HTTPError("boom"),
        }
    )

    urls = service._collect_3gpp_file_urls(root, max_depth=2)
    assert urls == [f"{root}R5s260001.zip"]


def test_collect_3gpp_file_urls_raises_when_root_is_file(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/R5s260001.zip"
    service._scraper = _FakeScraper({root: httpx.HTTPError("not html")})

    with pytest.raises(NotAFolderError):
        service._collect_3gpp_file_urls(root, max_depth=2)


def test_extract_from_url_batch_delegates_per_file(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/Docs/"
    file1 = f"{root}R5s260001.zip"
    file2 = f"{root}R5s260002.zip"
    service._scraper = _FakeScraper(
        {
            root: _folder_html(
                f'<a class="file" href="{file1}">R5s260001.zip</a>',
                f'<a class="file" href="{file2}">R5s260002.zip</a>',
            ),
        }
    )

    service.extract_from_url = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda url, **_: _make_result(
            "R5s260001" if "R5s260001" in url else "R5s260002", url
        )
    )

    batch = service.extract_from_url_batch(root, max_depth=0)

    assert isinstance(batch, DirectParseBatchResult)
    assert len(batch.results) == 2
    assert batch.failures == {}
    service.extract_from_url.assert_any_call(file1, force=False, full=False)
    service.extract_from_url.assert_any_call(file2, force=False, full=False)


def test_extract_from_url_batch_records_per_file_failures(service: TDocCrService) -> None:
    root = "https://www.3gpp.org/ftp/Docs/"
    file1 = f"{root}R5s260001.zip"
    file2 = f"{root}R5s260002.zip"
    service._scraper = _FakeScraper(
        {
            root: _folder_html(
                f'<a class="file" href="{file1}">R5s260001.zip</a>',
                f'<a class="file" href="{file2}">R5s260002.zip</a>',
            ),
        }
    )

    def side_effect(url: str, **_: Any) -> DirectParseResult:
        if "R5s260002" in url:
            raise ValueError("bad file")
        return _make_result("R5s260001", url)

    service.extract_from_url = MagicMock(side_effect=side_effect)  # type: ignore[method-assign]

    batch = service.extract_from_url_batch(root, max_depth=0)

    assert len(batch.results) == 1
    assert {r.tdoc_id for r in batch.results} == {"R5s260001"}
    assert batch.failures == {file2: "ValueError: bad file"}


def test_extract_from_url_batch_rejects_non_3gpp_url(service: TDocCrService) -> None:
    with pytest.raises(ValueError, match="not a 3GPP FTP URL"):
        service.extract_from_url_batch("https://example.com/", max_depth=0)
