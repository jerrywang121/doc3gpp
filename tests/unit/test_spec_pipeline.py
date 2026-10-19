"""Preparation/persistence boundary tests for specification sync."""

from datetime import datetime, timezone

from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.services.spec_service import PreparedSpecSync, SpecService


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def upsert(self, spec) -> None:
        self.calls.append("header")

    def upsert_versions(self, versions) -> int:
        self.calls.append("versions")
        return len(versions)


def test_persist_prepared_spec_stamps_timestamp_after_versions() -> None:
    repo = _Repo()
    header = Spec(spec_id="38.331", type="TS", title="RRC")
    version = SpecVersion(spec_id="38.331", version="19.0.0", ftp_url="")
    prepared = PreparedSpecSync(
        header=header,
        versions=(version,),
        canonical="RAN",
    )

    count = SpecService(repo).persist_prepared_spec(prepared)

    assert count == 1
    assert repo.calls == ["header", "versions", "header"]
    assert header.last_synced_at is not None
    assert header.last_synced_at.tzinfo is not None
    assert header.last_synced_at <= datetime.now(timezone.utc)
