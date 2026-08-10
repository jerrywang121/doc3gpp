"""Service layer for 3GPP specifications (TSs / TRs)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.repository.protocols import SpecRepository, TsgRepository
from doc3gpp.scraping.client import ScraperClient
from doc3gpp.scraping.spec_source import (
    fetch_cr_list,
    fetch_etsi_pdf_text,
    fetch_spec_detail,
    fetch_spec_list,
)
from doc3gpp.parsers.spec_parser import (
    extract_cr_tdocs,
    extract_etsi_pdf_url,
    parse_spec_detail,
    parse_spec_list,
)

logger = logging.getLogger(__name__)

# The 3GPP CR list page is a snapshot gated on recency (3 months) OR the
# stored crs being empty; the ETSI PDF is fetched once (pdf_url NULL).
_CR_RECENCY_WINDOW = timedelta(days=90)

SpecProgressFn = Callable[[str, dict], None]
"""Signature of the optional progress callback for :meth:`SpecService.sync`.

Events:
    ``"list_parsed"`` — fired after the spec list page is parsed and
        before the detail-page thread pool is started. ``data`` is
        ``{"total": <int>}`` with the number of specs to process.
    ``"spec_done"`` — fired once per spec whose detail page has been
        fetched and upserted (or whose failure was logged and
        swallowed). ``data`` is ``{"spec_id": <str>}``.
"""


class SpecService:
    """Sync and query 3GPP specification records."""

    def __init__(
        self,
        repository: SpecRepository,
        tsg_repository: TsgRepository | None = None,
        sync_interval: timedelta = timedelta(hours=24),
    ) -> None:
        self._repository = repository
        self._tsg_repository = tsg_repository
        self._sync_interval = sync_interval

    def sync(
        self,
        tsg: str,
        *,
        force: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
        """Fetch list page -> parallel detail pages -> upsert.

        Resolves the TSG, honours ``tsgs.spec_last_sync`` skip rule
        (unless ``force``), fetches the list once, then fetches each
        detail page in a thread pool, running the conditional ETSI / CR
        follow-ups inside each worker, and upserts per-spec in one
        transaction.

        When ``on_progress`` is supplied it is invoked twice per
        successful sync sweep: once with ``"list_parsed"`` (carrying
        ``{"total": N}`` after the list page is parsed) and once per
        spec with ``"spec_done"`` (carrying ``{"spec_id": ...}``).
        Skipped syncs (interval not elapsed) do not invoke the
        callback because there are no specs to process.
        """
        canonical = tsg.upper()
        if not force and self._tsg_repository is not None:
            tsg_record = self._tsg_repository.get_by_short_name(canonical)
            last_sync = tsg_record.spec_last_sync if tsg_record is not None else None
            now = datetime.now(timezone.utc)
            if last_sync is not None and (now - last_sync) < self._sync_interval:
                ago = now - last_sync
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"Spec sync skipped for TSG {canonical}: "
                        f"last sync {_format_duration(ago)} ago "
                        f"(sync interval {_format_duration(self._sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

        logger.info("Syncing specs for TSG %s", canonical)
        list_html = fetch_spec_list(canonical)
        specs = parse_spec_list(list_html, canonical)
        logger.info("Parsed %s specs from list page for TSG %s", len(specs), canonical)

        if on_progress is not None:
            on_progress("list_parsed", {"total": len(specs)})

        synced = 0
        version_total = 0
        workers = min(32, (os.cpu_count() or 4) + 4)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._sync_one_spec, spec, canonical): spec
                for spec in specs
            }
            for future in futures:
                spec = futures[future]
                try:
                    version_count = future.result()
                except Exception:  # noqa: BLE001 - one spec failure must not abort the sweep
                    logger.exception("Spec sync failed for %s", spec.spec_id)
                    if on_progress is not None:
                        on_progress("spec_done", {"spec_id": spec.spec_id})
                    continue
                version_total += version_count
                synced += 1
                if on_progress is not None:
                    on_progress("spec_done", {"spec_id": spec.spec_id})

        if self._tsg_repository is not None:
            self._tsg_repository.update_spec_last_sync(canonical, datetime.now(timezone.utc))

        return SyncOutcome(
            status="synced",
            reason=f"Spec sync complete for TSG {canonical}: {synced} specs, {version_total} versions stored",
            synced_count=synced,
            version_count=version_total,
        )

    def _sync_one_spec(self, spec: Spec, canonical: str) -> int:
        slug = spec.spec_id.replace(".", "")
        detail_html = fetch_spec_detail(slug)
        header, versions = parse_spec_detail(detail_html, spec.spec_id, canonical)
        header.type = spec.type
        header.title = spec.title
        header.last_synced_at = datetime.now(timezone.utc)

        with ScraperClient() as client:
            for v in versions:
                self._maybe_fetch_etsi_pdf(v, client)
                self._maybe_fetch_crs(v, client)

        self._repository.upsert(header)
        self._repository.upsert_versions(versions)
        return len(versions)

    def _maybe_fetch_etsi_pdf(self, v: SpecVersion, client: ScraperClient) -> None:
        if v.wki_id is None:
            return
        if v.pdf_url is not None:
            return
        try:
            html = fetch_etsi_pdf_text(v.wki_id, client)
            url = extract_etsi_pdf_url(html)
            if url:
                v.pdf_url = url
            else:
                logger.debug("No ETSI PDF link for version %s (WKI %s)", v.version, v.wki_id)
        except Exception:  # noqa: BLE001
            logger.debug("ETSI PDF fetch failed for version %s", v.version, exc_info=True)

    def _maybe_fetch_crs(self, v: SpecVersion, client: ScraperClient) -> None:
        if v.version_id is None:
            return
        now = datetime.now(timezone.utc)
        upload_recent = (
            v.upload_date is not None
            and (now - datetime.combine(v.upload_date, datetime.min.time(), tzinfo=timezone.utc))
            < _CR_RECENCY_WINDOW
        )
        if not upload_recent and v.crs:
            return
        try:
            html = fetch_cr_list(v.version_id, client)
            ids = extract_cr_tdocs(html)
            v.crs = ",".join(ids)
        except Exception:  # noqa: BLE001
            logger.debug("CR list fetch failed for version %s", v.version, exc_info=True)

    def list_recent(
        self,
        limit: int = 50,
        offset: int = 0,
        tsg: str | None = None,
        type: str | None = None,
        spec_id: str | None = None,
        title: str | None = None,
        status: str | None = None,
        radio_tech: str | None = None,
        initial_release: str | None = None,
        wis: str | None = None,
    ) -> list[Spec]:
        return self._repository.list(
            limit=limit, offset=offset, tsg=tsg, type=type, spec_id=spec_id,
            title=title, status=status, radio_tech=radio_tech,
            initial_release=initial_release, wis=wis,
        )

    def get(self, spec_id: str) -> Spec | None:
        return self._repository.get(spec_id)

    def list_versions(
        self, spec_id: str, limit: int = 200, offset: int = 0
    ) -> list[SpecVersion]:
        return self._repository.list_versions(spec_id, limit=limit, offset=offset)


def _format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"
