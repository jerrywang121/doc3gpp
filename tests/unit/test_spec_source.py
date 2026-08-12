from doc3gpp.scraping.spec_source import (
    build_spec_detail_url,
    build_spec_list_url,
    fetch_dynareport_detail,
    fetch_spec_list,
)


def test_build_spec_list_url() -> None:
    assert (
        build_spec_list_url("r5")
        == "https://www.3gpp.org/dynareport?code=TSG-WG--R5.htm"
    )


def test_build_spec_detail_url() -> None:
    assert build_spec_detail_url("36579-5") == "https://www.3gpp.org/DynaReport/36579-5.htm"


def test_fetch_spec_list_uses_scraper(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def __init__(self):
            self.get_text = lambda url: calls.append(url) or "<html></html>"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr("doc3gpp.scraping.spec_source.ScraperClient", FakeClient)
    body = fetch_spec_list("R5")
    assert body == "<html></html>"
    assert calls[0].startswith("https://www.3gpp.org/dynareport?code=TSG-WG--R5")


def test_fetch_dynareport_detail_uses_dotless_slug(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def get_text(self, url: str) -> str:
            calls.append(url)
            return "<html></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("doc3gpp.scraping.spec_source.ScraperClient", FakeClient)
    body = fetch_dynareport_detail("38.523-1")
    assert body == "<html></html>"
    assert calls == ["https://www.3gpp.org/DynaReport/38523-1.htm"]
