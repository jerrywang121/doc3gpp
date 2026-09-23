from doc3gpp.models.spec import SpecVersion
from doc3gpp.scraping.spec_doc_source import fetch_spec_doc_zip, resolve_spec_doc_version
from doc3gpp.models.spec_doc import SpecDocUnknownVersionError
import pytest


class _FakeClient:
    def __init__(self, payload: bytes = b"PK fake-zip"):
        self.payload = payload
        self.urls: list[str] = []
        self.closed = False

    def get_bytes(self, url: str) -> bytes:
        self.urls.append(url)
        return self.payload

    def close(self) -> None:
        self.closed = True


def _v(ver, rel="Rel-18"):
    return SpecVersion(spec_id="38.331", version=ver, ftp_url=f"https://www.3gpp.org/ftp/x/{ver}.zip", release=rel)


def test_default_newest_numeric():
    vs = [_v("18.2.1"), _v("18.10.1"), _v("18.5.0")]
    assert resolve_spec_doc_version(vs).version == "18.10.1"


def test_exact_release_version():
    vs = [_v("18.5.0", "Rel-18"), _v("17.3.0", "Rel-17")]
    assert resolve_spec_doc_version(vs, release="Rel-17", version="17.3.0").version == "17.3.0"


def test_miss_lists_available():
    vs = [_v("18.5.0"), _v("18.10.1")]
    with pytest.raises(SpecDocUnknownVersionError) as e:
        resolve_spec_doc_version(vs, version="99.0.0")
    assert "18.10.1" in str(e.value)


def test_fetch_uses_injected_client_no_close():
    client = _FakeClient()
    data = fetch_spec_doc_zip("https://www.3gpp.org/ftp/x/18.10.1.zip", client=client)
    assert data == b"PK fake-zip"
    assert client.urls == ["https://www.3gpp.org/ftp/x/18.10.1.zip"]
    assert client.closed is False


def test_empty_versions_raises_with_empty_available():
    with pytest.raises(SpecDocUnknownVersionError) as e:
        resolve_spec_doc_version([])
    assert e.value.available == []


def test_release_only_picks_newest_in_release():
    vs = [_v("18.2.1", "Rel-18"), _v("18.10.1", "Rel-18"), _v("17.3.0", "Rel-17")]
    assert resolve_spec_doc_version(vs, release="Rel-17").version == "17.3.0"


def test_non_numeric_segment_sorts_as_zero():
    vs = [_v("18.2.1"), _v("18.x.9")]
    assert resolve_spec_doc_version(vs).version == "18.2.1"
