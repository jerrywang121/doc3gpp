"""Unit tests for SpecService orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from doc3gpp.models.spec import Spec, SpecVersion  # noqa: F401  (spec contract)
from doc3gpp.models.sync import SyncOutcome  # noqa: F401  (spec contract)
from doc3gpp.services.spec_service import SpecService


class _StubTsgRepo:
    def __init__(self, last_spec_sync=None):
        self._last = last_spec_sync
        self.spec_sync_calls = []

    def get_by_short_name(self, short_name):
        return MagicMock(spec_last_sync=self._last)

    def update_spec_last_sync(self, short_name, synced_at):
        self.spec_sync_calls.append(synced_at)
        return True


class _StubSpecRepo:
    def __init__(self):
        self.specs = {}
        self.versions = {}
        self.upserted = []

    def upsert(self, spec):
        self.specs[spec.spec_id] = spec
        self.upserted.append(spec)

    def upsert_versions(self, versions):
        self.versions.setdefault(versions[0].spec_id if versions else None, []).extend(versions)
        return len(versions)

    def list(self, **kw):
        return list(self.specs.values())

    def get(self, spec_id):
        return self.specs.get(spec_id)

    def list_versions(self, spec_id, limit=200, offset=0):
        return self.versions.get(spec_id, [])


LIST_HTML = """
<html><body><table class="dsptab adynspec dsp-tsgwg">
<tr><td><span>TS</span><a href="/DynaReport/36579-5.htm">36.579-5</a></td><td>NR conformance</td><td>r</td></tr>
</table></body></html>
"""

DETAIL_HTML = """
<html><body>
<div id="statusVal">Under change control</div>
<div id="initialPlannedReleaseVal">Release 20</div>
<table>
<tr>
  <td><a id="lnkFtpDownload" href="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-i30.zip">18.3.0</a></td>
  <td><a id="lnkMeetings" href="?m_id=108">RAN#108</a></td>
  <td><a id="imgRelatedCRs" href="?versionId=92276"></a></td>
  <td><a id="imgRelatedWI" href="?WKI_ID=12345"></a></td>
  <td>2025-06-01</td><td><span class="lblRemarkText">c</span></td>
</tr>
</table>
</body></html>
"""


def test_sync_smoke(monkeypatch) -> None:
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html><a href='x.pdf'>d</a></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html><a id='wgTdocDetailsLink'>R5-1</a></html>",
    )
    repo = _StubSpecRepo()
    tsg = _StubTsgRepo()
    svc = SpecService(repo, tsg)
    outcome = svc.sync("R5")
    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert outcome.version_count == 1
    assert tsg.spec_sync_calls, "spec_last_sync not stamped"


def test_sync_skips_within_interval(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    tsg = _StubTsgRepo(last_spec_sync=now)
    svc = SpecService(_StubSpecRepo(), tsg, sync_interval=timedelta(hours=24))
    outcome = svc.sync("R5")
    assert outcome.status == "skipped"
    assert not tsg.spec_sync_calls


def test_sync_force_bypasses_interval(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    tsg = _StubTsgRepo(last_spec_sync=now)
    svc = SpecService(_StubSpecRepo(), tsg, sync_interval=timedelta(hours=24))
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_spec_list", lambda t: LIST_HTML)
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_spec_detail", lambda s: DETAIL_HTML)
    outcome = svc.sync("R5", force=True)
    assert outcome.status == "synced"
