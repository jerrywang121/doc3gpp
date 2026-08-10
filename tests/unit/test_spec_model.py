from doc3gpp.models.spec import Spec, SpecVersion


def test_spec_fields() -> None:
    spec = Spec(
        spec_id="36.579-5", type="TS", title="T", status="Under change control",
        radio_tech="2G,3G,LTE", initial_release="Rel-20", tsg="R5", wis="A,B",
    )
    assert spec.spec_id == "36.579-5"
    assert spec.type == "TS"
    assert spec.tsg == "R5"


def test_spec_defaults() -> None:
    spec = Spec(spec_id="36.579-5", type="TS", title="T")
    assert spec.status is None
    assert spec.radio_tech is None
    assert spec.initial_release is None
    assert spec.tsg is None
    assert spec.wis is None
    assert spec.last_synced_at is None


def test_spec_version_fields() -> None:
    v = SpecVersion(
        spec_id="36.579-5", version="18.3.0",
        ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-i30.zip",
        release="Rel-18", meeting_id=108, meeting_name="RAN#108",
        upload_date=None, version_id=92276,
    )
    assert v.version == "18.3.0"
    assert v.meeting_id == 108


def test_spec_version_optional_fields() -> None:
    v = SpecVersion(spec_id="s", version="1.0.0", ftp_url="ftp://x")
    assert v.pdf_url is None
    assert v.crs is None
    assert v.comment is None
