"""Unit tests for SpecService orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from doc3gpp.models.spec import Spec, SpecVersion  # noqa: F401  (spec contract)
from doc3gpp.models.sync import SyncOutcome  # noqa: F401  (spec contract)
from doc3gpp.services.spec_service import (
    SpecService,
    SpecUnknownOnUpstreamError,
)


class _StubSpecRepo:
    """In-memory stand-in for :class:`SQLAlchemySpecRepository`.

    Mirrors the real repository's "snapshot at write time" semantics:
    ``upsert`` and ``upsert_versions`` copy the inbound dataclass
    into a fresh instance so a later mutation of the caller's
    reference is not visible to readers. This matches the
    SQLAlchemy row mapping (which materialises a row into a domain
    object on read and writes the column values verbatim on upsert).
    """

    def __init__(self) -> None:
        self.specs: dict[str, Spec] = {}
        self.versions: dict[str, list[SpecVersion]] = {}
        self.upserted: list[Spec] = []

    def _snapshot_spec(self, spec: Spec) -> Spec:
        from dataclasses import replace

        return replace(spec)

    def _snapshot_version(self, v: SpecVersion) -> SpecVersion:
        from dataclasses import replace

        return replace(v)

    def upsert(self, spec: Spec) -> None:
        snap = self._snapshot_spec(spec)
        self.specs[spec.spec_id] = snap
        self.upserted.append(snap)

    def upsert_versions(self, versions: list[SpecVersion]) -> int:
        snap_list = [self._snapshot_version(v) for v in versions]
        key = versions[0].spec_id if versions else None
        if key is not None:
            self.versions.setdefault(key, []).extend(snap_list)
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


def test_service_list_versions_forwards_version_filter() -> None:
    """``SpecService.list_versions`` passes ``version`` through to the repo."""
    repo = _StubSpecRepo()
    svc = SpecService(repo)
    repo.list_versions = MagicMock(return_value=[])
    svc.list_versions("36.579-5", limit=10, offset=2, version="19.%")
    repo.list_versions.assert_called_once_with(
        "36.579-5", limit=10, offset=2, version="19.%"
    )


def test_sync_skips_etsi_fetch_when_pdf_url_already_persisted(monkeypatch) -> None:
    """On a re-sync, a version whose ``pdf_url`` is already stored in the
    DB must NOT re-fetch the ETSI PDF page.

    ``_sync_one_spec`` parses *fresh* ``SpecVersion`` objects from the
    detail page whose ``pdf_url`` is always ``None``, so without merging
    the persisted value in, the ETSI page would be re-fetched on every
    sync even though the value never changes.
    """
    etsi_calls: list[int] = []

    def _etsi(_wki, _client):
        etsi_calls.append(_wki)
        return "<html><a href='x.pdf'>d</a></html>"

    recent_upload = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    detail_html = DETAIL_HTML.replace("2025-06-01", recent_upload)

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: detail_html,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text", _etsi
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html><a id='wgTdocDetailsLink'>R5-1</a></html>",
    )

    repo = _StubSpecRepo()
    svc = SpecService(repo)

    first = svc.sync("R5")
    assert first.status == "synced"
    assert etsi_calls == [12345], "first sync should fetch the ETSI page once"

    persisted = repo.list_versions("36.579-5")
    assert len(persisted) == 1
    assert persisted[0].pdf_url == "x.pdf"

    etsi_calls.clear()
    second = svc.sync("R5")
    assert second.status == "synced"
    assert etsi_calls == [], (
        "second sync must not re-fetch the ETSI page — pdf_url is already persisted"
    )


def test_sync_skips_etsi_fetch_for_stale_versions(monkeypatch) -> None:
    """The ETSI PDF follow-up is skipped for versions uploaded more than
    ``_CR_RECENCY_WINDOW`` (90 days) ago, even when no ``pdf_url`` is
    persisted yet — the link is stable and re-fetching old versions on
    every sync wastes an HTTP request per spec.
    """
    old_upload = (datetime.now(timezone.utc).date() - timedelta(days=400)).isoformat()
    detail_html = DETAIL_HTML.replace("2025-06-01", old_upload)
    etsi_calls: list[int] = []

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: detail_html,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: (etsi_calls.append(wki) or "<html><a href='x.pdf'>d</a></html>"),
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html><a id='wgTdocDetailsLink'>R5-1</a></html>",
    )

    repo = _StubSpecRepo()
    svc = SpecService(repo)
    outcome = svc.sync("R5")

    assert outcome.status == "synced"
    assert etsi_calls == [], (
        "stale version (>90 days old) must not re-fetch the ETSI page"
    )
    assert repo.list_versions("36.579-5")[0].pdf_url is None


def test_sync_smoke(monkeypatch) -> None:
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
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
    svc = SpecService(repo)
    outcome = svc.sync("R5")
    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert outcome.version_count == 1


def test_sync_skips_within_interval(monkeypatch) -> None:
    """``sync`` honours the per-spec ``last_synced_at`` skip rule.

    A spec that was synced within the interval must be short-circuited
    by the worker — no detail-page fetch, no upsert. The
    ``synced_count`` must not count the skipped spec.
    """
    now = datetime.now(timezone.utc)
    repo = _StubSpecRepo()
    repo.upsert(
        Spec(
            spec_id="36.579-5",
            type="TS",
            title="NR conformance",
            tsg="R5",
            last_synced_at=now - timedelta(hours=1),
        )
    )
    svc = SpecService(repo, sync_interval=timedelta(hours=24))

    def _fail_detail(*args, **kwargs):
        raise AssertionError("detail page must not be fetched when skipped")

    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_spec_list", lambda t, **k: LIST_HTML)
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_spec_detail", _fail_detail)

    outcome = svc.sync("R5")

    assert outcome.status == "synced"
    assert outcome.synced_count == 0
    assert outcome.version_count == 0


def test_sync_force_bypasses_interval(monkeypatch) -> None:
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_spec_list", lambda t, **k: LIST_HTML)
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_spec_detail", lambda s, **k: DETAIL_HTML)
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_etsi_pdf_text", lambda w, c: "<html><a href='x.pdf'>d</a></html>")
    monkeypatch.setattr("doc3gpp.services.spec_service.fetch_cr_list", lambda v, c: "<html><a id='wgTdocDetailsLink'>R5-1</a></html>")
    svc = SpecService(_StubSpecRepo())
    outcome = svc.sync("R5", force=True)
    assert outcome.status == "synced"


def test_sync_stamps_last_synced_at_only_after_full_upsert(monkeypatch) -> None:
    """``Spec.last_synced_at`` must be set ONLY after the full per-spec
    sync (detail page + follow-ups + ``upsert`` + ``upsert_versions``)
    succeeds.

    Rationale: ``last_synced_at`` is the per-spec completion marker
    the next sync uses to decide whether to re-extract the detail page
    for this spec. Stamping it before the upserts means a partial
    sync (e.g. ETSI/CR follow-up crashed, or ``upsert_versions``
    raised) leaves a ``last_synced_at`` set on a half-written row —
    so the next sync would skip the detail page and the half-state
    would persist forever. We want a partial sync to be retryable.

    This test pins the happy-path invariant: after a successful
    full sync, ``last_synced_at`` is stamped. The companion test
    ``test_sync_does_not_stamp_last_synced_at_when_upsert_versions_fails``
    pins the failure-mode invariant.
    """
    repo = _StubSpecRepo()

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html><a href='x.pdf'>d</a></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html><a id='wgTdocDetailsLink'>R5-1</a></html>",
    )

    svc = SpecService(repo)
    svc.sync("R5", force=True)

    persisted = repo.specs["36.579-5"]
    assert persisted.last_synced_at is not None, (
        "happy-path invariant: last_synced_at must be stamped after a "
        "successful full sync (detail + follow-ups + upserts)"
    )


def test_sync_does_not_stamp_last_synced_at_when_upsert_versions_fails(
    monkeypatch,
) -> None:
    """If ``upsert_versions`` raises, the header has already been
    written but ``last_synced_at`` must NOT be set — the next sync
    should re-extract the detail page so the missing ``spec_versions``
    rows can be back-filled.

    We simulate the failure by monkeypatching ``upsert_versions`` on
    the service's repository to raise.
    """
    repo = _StubSpecRepo()

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html><a href='x.pdf'>d</a></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html></html>",
    )

    def _raise(*_args, **_kwargs):
        raise RuntimeError("simulated DB failure on upsert_versions")

    monkeypatch.setattr(repo, "upsert_versions", _raise)

    svc = SpecService(repo)
    # The outer loop catches the per-spec exception and continues —
    # the sync still completes (with synced_count=0).
    outcome = svc.sync("R5", force=True)
    assert outcome.status == "synced"
    assert outcome.synced_count == 0  # the failing spec was counted as failed

    persisted = repo.specs["36.579-5"]
    assert persisted.last_synced_at is None, (
        "last_synced_at must NOT be set when upsert_versions fails — "
        "otherwise the next sync would skip the detail page and the "
        "missing version rows would never be back-filled"
    )


def _list_html_for(n_specs: int) -> str:
    return (
        '<html><body><table class="dsptab adynspec dsp-tsgwg">'
        + "".join(
            "<tr>"
            f"<td><span>TS</span><a href='/DynaReport/{1000 + i}.htm'>{i}.0</a></td>"
            f"<td>spec {i}</td><td>r</td>"
            "</tr>"
            for i in range(n_specs)
        )
        + "</table></body></html>"
    )


def _detail_html_with(n_versions: int) -> str:
    return (
        "<html><body><table>"
        + "".join(
            "<tr>"
            f'<td><a id="lnkFtpDownload" href="u{j}.zip">{j}.0.0</a></td>'
            f'<td><a id="lnkMeetings" href="?m_id=108">RAN#108</a></td>'
            f'<td><a id="imgRelatedCRs" href="?versionId={1000 + j}"></a></td>'
            f'<td><a id="imgRelatedWI" href="?WKI_ID={2000 + j}"></a></td>'
            f"<td>2025-06-01</td><td><span class='lblRemarkText'>c</span></td>"
            "</tr>"
            for j in range(n_versions)
        )
        + "</table></body></html>"
    )


def test_sync_uses_one_shared_client_across_the_whole_sweep(monkeypatch) -> None:
    """A single ``ScraperClient`` must be opened for the entire sweep.

    Prior to the fix each spec's ``_sync_one_spec`` opened its own
    ``ScraperClient`` (and ``fetch_spec_list`` another), so a 94-spec
    sweep created ~95 separate httpx clients and paid a fresh
    TLS + DNS + connect handshake for every one. This test counts
    ``ScraperClient`` constructions during a multi-spec ``sync`` and
    pins it to exactly one shared instance.
    """
    import doc3gpp.scraping.client as client_mod

    created: list = []

    def counting_init(self, *args, **kwargs):
        created.append(self)
        return original_init(self, *args, **kwargs)

    original_init = client_mod.ScraperClient.__init__
    monkeypatch.setattr(client_mod.ScraperClient, "__init__", counting_init)

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: _list_html_for(3),
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html><a href='x.pdf'>d</a></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html></html>",
    )

    svc = SpecService(_StubSpecRepo(), max_workers=4)
    outcome = svc.sync("R5", force=True)

    assert outcome.status == "synced"
    assert outcome.synced_count == 3
    assert len(created) == 1, (
        f"expected exactly one shared ScraperClient for the whole sweep, "
        f"but {len(created)} were constructed"
    )


def test_sync_followups_do_not_starve_the_spec_pool(monkeypatch) -> None:
    """Follow-up fetches must not block the worker that owns the spec.

    Before the fix ``_fetch_followups_concurrently`` submitted the ETSI +
    CR follow-ups to the *same* ``ThreadPoolExecutor`` that was already
    running ``_sync_one_spec``, then blocked on ``future.result()``. With
    ``max_workers=1`` a single spec with even one version submits 2
    follow-ups the sole worker cannot run while it waits -> the sweep
    deadlocks.

    The fix fans the follow-ups out onto a *separate* executor, so a
    lone worker can always make progress. We assert the sweep completes
    under ``max_workers=1`` (the configuration that provably deadlocks
    the old design).
    """
    import concurrent.futures as _cf
    from concurrent.futures import ThreadPoolExecutor

    list_html = _list_html_for(2)
    detail_html = _detail_html_with(2)

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list", lambda tsg, **k: list_html
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: detail_html,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html><a href='x.pdf'>d</a></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html></html>",
    )

    svc = SpecService(_StubSpecRepo(), max_workers=1)

    result: dict = {}
    def _run():
        result["outcome"] = svc.sync("R5", force=True)

    with ThreadPoolExecutor(max_workers=1) as t:
        fut = t.submit(_run)
        finished = _cf.wait([fut], timeout=20.0)

    assert fut in finished.done, "spec sync deadlocked under max_workers=1"
    assert result["outcome"].status == "synced"
    assert result["outcome"].synced_count == 2


def test_sync_followups_are_fanned_out_across_the_thread_pool(monkeypatch) -> None:
    """``_sync_one_spec`` submits the ETSI + CR follow-ups to the
    *shared* ``ThreadPoolExecutor`` and waits for every future before
    upserting — so a single-spec sync doesn't serialise ~2N HTTP
    requests when the underlying network is the bottleneck.

    The proof uses a slow ``fetch_etsi_pdf_text`` that sleeps; if the
    follow-ups ran serially the call would take ``~2N * sleep``
    seconds, but with parallel fan-out it should finish in roughly
    ``sleep`` seconds (assuming at least one other worker is free).
    """
    import time

    from doc3gpp.services.spec_service import SpecService

    n_versions = 20
    sleep_per_call = 0.2

    versions_html = (
        "<html><body>"
        + "".join(
            f"""
            <tr>
              <td><a id="lnkFtpDownload" href="u{i}.zip">{i}.0.0</a></td>
              <td><a id="lnkMeetings" href="?m_id=108">RAN#108</a></td>
              <td><a id="imgRelatedCRs" href="?versionId={1000+i}"></a></td>
              <td><a id="imgRelatedWI" href="?WKI_ID={2000+i}"></a></td>
              <td>2025-06-01</td><td><span class="lblRemarkText">c</span></td>
            </tr>
            """
            for i in range(n_versions)
        )
        + "</body></html>"
    )

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: versions_html,
    )

    def _slow_etsi(_wki, _client):
        time.sleep(sleep_per_call)
        return "<html><a href='x.pdf'>d</a></html>"

    def _slow_cr(_version_id, _client):
        time.sleep(sleep_per_call)
        return "<html></html>"

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text", _slow_etsi
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list", _slow_cr
    )

    svc = SpecService(_StubSpecRepo())
    started = time.perf_counter()
    outcome = svc.sync("R5", force=True)
    elapsed = time.perf_counter() - started

    assert outcome.status == "synced"
    assert outcome.version_count == n_versions

    # Serial baseline: ~2N * sleep = 8s for N=20, sleep=0.2.
    # Parallel fan-out (workers >= 32) finishes in roughly sleep + jitter.
    # Generous ceiling: anything under 4s proves parallelism.
    assert elapsed < 4.0, (
        f"follow-ups appear to run serially: took {elapsed:.2f}s "
        f"for {n_versions} versions × 2 follow-ups × {sleep_per_call}s each "
        f"(serial baseline {n_versions * 2 * sleep_per_call:.1f}s)"
    )


def test_sync_spec_syncs_single_stored_spec(monkeypatch) -> None:
    """``sync_spec`` fetches only the detail page of one stored spec."""
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
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
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    svc = SpecService(repo)

    outcome = svc.sync_spec("36.579-5")

    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert repo.versions["36.579-5"]
    # Per-spec stamp: last_synced_at was advanced on the row the
    # worker upserted.
    assert repo.specs["36.579-5"].last_synced_at is not None


def test_sync_spec_honours_skip_rule() -> None:
    """``sync_spec`` skips when the spec's ``last_synced_at`` is within the interval."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo = _StubSpecRepo()
    repo.upsert(
        Spec(
            spec_id="36.579-5",
            type="TS",
            title="NR conformance",
            tsg="R5",
            last_synced_at=recent,
        )
    )
    svc = SpecService(repo, sync_interval=timedelta(hours=24))

    outcome = svc.sync_spec("36.579-5")

    assert outcome.status == "skipped"
    assert "Use --force to override" in outcome.reason


def test_list_distinct_tsgs_delegates_to_repo() -> None:
    """``SpecService.list_distinct_tsgs`` forwards to the repo."""
    repo = _StubSpecRepo()
    repo.list_distinct_tsgs = MagicMock(return_value=["R5", "S2"])
    svc = SpecService(repo)
    assert svc.list_distinct_tsgs() == ["R5", "S2"]
    repo.list_distinct_tsgs.assert_called_once_with()


DYNAREPORT_HEADER_HTML = """
<html><body>
<table>
  <tr>
    <td class="TabLineLeft">
      <span id="titleLbl">Title:</span>
    </td>
    <td class="TabLineRight">
      <span id="titleVal">NR conformance test (Bootstrap)</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="typeLbl">Type:</span>
    </td>
    <td class="TabLineRight">
      <span id="typeVal">Technical specification (TS)</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="PrimaryResponsibleGroupLbl">Primary responsible group:</span>
    </td>
    <td class="TabLineRight">
      <span>
        <span>RAN 5</span>
      </span>
    </td>
  </tr>
</table>
<table>
  <tr>
    <td><a id="lnkFtpDownload" href="https://www.3gpp.org/ftp/Specs/archive/38_series/38.523-1/38523-1-i30.zip">18.3.0</a></td>
    <td><a id="lnkMeetings" href="?m_id=108">RAN#108</a></td>
    <td><a id="imgRelatedCRs" href="imgRelatedCRs.aspx?versionId=92276"></a></td>
    <td></td>
    <td>2025-06-01</td>
  </tr>
</table>
</body></html>
"""


def test_sync_spec_falls_back_to_dynareport_when_missing(monkeypatch) -> None:
    repo = _StubSpecRepo()

    def fake_fetch(spec_id_dotted, client=None):
        assert spec_id_dotted == "38.523-1"
        return DYNAREPORT_HEADER_HTML

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        fake_fetch,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, client=None: DYNAREPORT_HEADER_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html></html>",
    )

    svc = SpecService(repository=repo)
    outcome = svc.sync_spec("38.523-1", force=True)

    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert "38.523-1" in repo.specs
    persisted = repo.specs["38.523-1"]
    assert persisted.title == "NR conformance test (Bootstrap)"
    assert persisted.type == "TS"
    assert persisted.tsg == "R5"
    assert len(repo.versions.get("38.523-1", [])) == 1


def test_sync_spec_raises_when_dynareport_body_empty(monkeypatch) -> None:
    repo = _StubSpecRepo()
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        lambda spec_id_dotted, client=None: "<html><body></body></html>",
    )

    svc = SpecService(repository=repo)
    with pytest.raises(SpecUnknownOnUpstreamError):
        svc.sync_spec("38.523-1", force=True)


def test_sync_spec_bootstrap_unaffected_by_tsg_skip_rule(monkeypatch) -> None:
    """A bootstrap (cache miss) must run even when the spec was synced
    inside the sync interval.

    Regression for the bug surfaced by `doc3gpp spec sync --spec-id
    38.321` returning a "skipped" outcome for a missing spec: the
    bootstrap fetches the DynaReport page, parses the header, and
    builds a fresh ``Spec``, but a per-TSG skip rule could fire and
    discard the result. The user is left with no cache entry and no
    error explaining why.

    The skip rule is a re-sync optimisation; it should not block a
    first-time fetch. With the per-spec skip rule, a bootstrapped
    spec has no ``last_synced_at`` to consult, so the rule is a
    no-op on the bootstrap path.
    """
    repo = _StubSpecRepo()  # no cached row for 38.523-1

    def fake_fetch(spec_id_dotted, client=None):
        assert spec_id_dotted == "38.523-1"
        return DYNAREPORT_HEADER_HTML

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        fake_fetch,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, client=None: DYNAREPORT_HEADER_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html></html>",
    )

    svc = SpecService(repository=repo)
    outcome = svc.sync_spec("38.523-1")  # force=False — skip rule must NOT apply

    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert "38.523-1" in repo.specs
    assert repo.specs["38.523-1"].tsg == "R5"


def test_sync_spec_stored_row_unchanged(monkeypatch) -> None:
    repo = _StubSpecRepo()
    repo.upsert(Spec(spec_id="38.523-1", type="TS", title="Cached", tsg="R5"))

    def fail_fetch(spec_id_dotted, client=None):
        raise AssertionError("fetch must not be called when the row is stored")

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        fail_fetch,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, client=None: "<html></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html></html>",
    )

    svc = SpecService(repository=repo)
    outcome = svc.sync_spec("38.523-1", force=True)
    assert outcome.status == "synced"
    assert repo.specs["38.523-1"].title == "Cached"


class _StubTsgRepo:
    """Minimal stub for the spec service — the per-spec skip rule no
    longer reads from the TSG repo, so this stub is empty."""

    def get_by_short_name(self, short_name: str):
        return None


def test_sync_spec_skips_when_last_synced_recently() -> None:
    repo = _StubSpecRepo()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    existing = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
        last_synced_at=now - timedelta(hours=1),
    )
    repo.upsert(existing)

    svc = SpecService(
        repository=repo,
        sync_interval=timedelta(hours=24),
    )
    out = svc.sync_spec("36.579-5")
    assert out.status == "skipped"
    assert "36.579-5" in out.reason
    # detail-page fetch must not have run; versions remain empty.
    assert repo.versions.get("36.579-5", []) == []


def test_sync_spec_force_overrides_recent_sync() -> None:
    repo = _StubSpecRepo()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    existing = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
        last_synced_at=now - timedelta(hours=1),
    )
    repo.upsert(existing)
    # Bootstrap returns a Spec that will be parsed via _sync_one_spec.
    # To keep this test simple, we monkey-patch _sync_one_spec to a no-op
    # that upserts a fresh Spec.
    svc = SpecService(
        repository=repo,
        sync_interval=timedelta(hours=24),
    )

    def _fake_sync_one(spec, canonical, executor, client):
        repo.upsert(
            Spec(
                spec_id=spec.spec_id,
                type=spec.type,
                title=spec.title,
                tsg=spec.tsg,
                last_synced_at=now,
            )
        )
        return 1

    svc._sync_one_spec = _fake_sync_one  # type: ignore[assignment]
    out = svc.sync_spec("36.579-5", force=True)
    assert out.status == "synced"
    # last_synced_at was advanced.
    assert repo.specs["36.579-5"].last_synced_at == now


def test_sync_spec_proceeds_when_no_last_synced_at() -> None:
    repo = _StubSpecRepo()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    existing = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
        last_synced_at=None,
    )
    repo.upsert(existing)
    svc = SpecService(
        repository=repo,
        sync_interval=timedelta(hours=24),
    )

    def _fake_sync_one(spec, canonical, executor, client):
        repo.upsert(
            Spec(
                spec_id=spec.spec_id,
                type=spec.type,
                title=spec.title,
                tsg=spec.tsg,
                last_synced_at=now,
            )
        )
        return 1

    svc._sync_one_spec = _fake_sync_one  # type: ignore[assignment]
    out = svc.sync_spec("36.579-5")
    assert out.status == "synced"
