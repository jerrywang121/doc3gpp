from doc3gpp.models.spec import Spec, SpecVersion, spec_version_sort_key


def test_spec_version_sort_key_is_numeric() -> None:
    versions = ["18.2.1", "19.1.0", "18.10.1"]
    assert sorted(versions, key=spec_version_sort_key, reverse=True) == [
        "19.1.0",
        "18.10.1",
        "18.2.1",
    ]


def test_spec_fields() -> None:
    spec = Spec(
        spec_id="36.579-5", type="TS", title="T", status="Under change control",
        radio_tech="2G,3G,LTE", initial_release="Rel-20", tsg="R5", wis="A,B",
        rapporteurs="Ericsson LM",
    )
    assert spec.spec_id == "36.579-5"
    assert spec.type == "TS"
    assert spec.tsg == "R5"
    assert spec.rapporteurs == "Ericsson LM"


def test_spec_defaults() -> None:
    spec = Spec(spec_id="36.579-5", type="TS", title="T")
    assert spec.status is None
    assert spec.radio_tech is None
    assert spec.initial_release is None
    assert spec.tsg is None
    assert spec.wis is None
    assert spec.rapporteurs is None
    assert spec.last_synced_at is None
    assert spec.parsed is None


def test_spec_version_optional_fields() -> None:
    v = SpecVersion(spec_id="s", version="1.0.0", ftp_url="ftp://x")
    assert v.pdf_url is None
    assert v.crs is None
    assert v.parsed is False
