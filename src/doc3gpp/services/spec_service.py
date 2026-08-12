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
        max_workers: int | None = None,
    ) -> None:
        self._repository = repository
        self._tsg_repository = tsg_repository
        self._sync_interval = sync_interval
        self._max_workers = max_workers

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

        synced = 0
        version_total = 0
        workers = self._max_workers or min(6, (os.cpu_count() or 4) + 4)
        # One shared client for the entire sweep: opening a fresh
        # ScraperClient per spec cost ~95 separate httpx clients (one per
        # spec + the list page), each paying a TLS + DNS + connect
        # handshake. A single client is thread-safe for concurrent GETs,
        # so every worker reuses the same connection pool.
        with ScraperClient() as client:
            list_html = fetch_spec_list(canonical, client=client)
            specs = parse_spec_list(list_html, canonical)
            logger.info(
                "Parsed %s specs from list page for TSG %s", len(specs), canonical
            )

            if on_progress is not None:
                on_progress("list_parsed", {"total": len(specs)})

            # The ETSI + CR follow-ups must run on a *separate* executor
            # from the spec workers. Fanning them out onto the same pool
            # that was already running ``_sync_one_spec`` starved the
            # pool: a worker blocked on ``future.result()`` held its slot
            # while the follow-up futures it was waiting on could never
            # get a free worker, deadlocking the sweep past ~15 specs.
            with ThreadPoolExecutor(max_workers=workers) as executor:
                with ThreadPoolExecutor(max_workers=workers) as followup_executor:
                    futures = {
                        executor.submit(
                            self._sync_one_spec,
                            spec,
                            canonical,
                            followup_executor,
                            client,
                        ): spec
                        for spec in specs
                    }
                    for future in futures:
                        spec = futures[future]
                        try:
                            version_count = future.result()
                        except Exception:  # noqa: BLE001 - one spec failure must not abort the sweep
                            logger.exception(
                                "Spec sync failed for %s", spec.spec_id
                            )
                            if on_progress is not None:
                                on_progress(
                                    "spec_done", {"spec_id": spec.spec_id}
                                )
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

    def sync_spec(
        self,
        spec_id: str,
        *,
        force: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
        """Refresh a single stored spec's detail page + versions.

        Looks up ``spec_id`` in the DB to recover its TSG (required for
        the FK and for ``parse_spec_detail``). A spec that is not
        stored cannot be synced on its own and raises ``ValueError``.
        Honours the per-TSG ``tsgs.spec_last_sync`` skip rule unless
        ``force``, and stamps it again on success — identical to
        :meth:`sync`.
        """
        spec = self._repository.get(spec_id)
        if spec is None:
            raise ValueError(
                f"spec {spec_id!r} is not in the database; run 'doc3gpp spec sync --tsg <tsg>' first"
            )
        canonical = spec.tsg.upper() if spec.tsg else ""
        if not force and self._tsg_repository is not None:
            tsg_record = self._tsg_repository.get_by_short_name(canonical)
            last_sync = tsg_record.spec_last_sync if tsg_record is not None else None
            now = datetime.now(timezone.utc)
            if last_sync is not None and (now - last_sync) < self._sync_interval:
                ago = now - last_sync
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"Spec sync skipped for {spec.spec_id} (TSG {canonical}): "
                        f"last sync {_format_duration(ago)} ago "
                        f"(sync interval {_format_duration(self._sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

        logger.info("Syncing spec %s", spec.spec_id)
        with ScraperClient() as client:
            with ThreadPoolExecutor(max_workers=1) as followup_executor:
                version_count = self._sync_one_spec(
                    spec, canonical, followup_executor, client
                )
            if on_progress is not None:
                on_progress("spec_done", {"spec_id": spec.spec_id})

        if self._tsg_repository is not None:
            self._tsg_repository.update_spec_last_sync(
                canonical, datetime.now(timezone.utc)
            )

        return SyncOutcome(
            status="synced",
            reason=f"Spec sync complete for {spec.spec_id}: 1 spec, {version_count} versions stored",
            synced_count=1,
            version_count=version_count,
        )

    def list_distinct_tsgs(self) -> list[str]:
        """Return distinct TSG short names currently stored in specs."""
        return self._repository.list_distinct_tsgs()

    def _sync_one_spec(
        self,
        spec: Spec,
        canonical: str,
        followup_executor: ThreadPoolExecutor,
        client: ScraperClient,
    ) -> int:
        slug = spec.spec_id.replace(".", "")
        detail_html = fetch_spec_detail(slug, client=client)
        header, versions = parse_spec_detail(detail_html, spec.spec_id, canonical)
        header.type = spec.type
        header.title = spec.title

        # The detail page does not carry the ETSI PDF link, so freshly
        # parsed versions always arrive with ``pdf_url`` unset. Back-fill
        # the persisted value so ``_maybe_fetch_etsi_pdf`` skips the
        # upstream fetch for versions we have already resolved — the
        # link is stable for a version and re-fetching it on every sync
        # wastes an HTTP request per spec.
        self._backfill_pdf_urls(versions)

        # The ETSI + CR follow-ups are independent HTTP requests, fanned
        # out across the dedicated follow-up executor so they overlap
        # with the detail-page fetches of the other spec workers, then
        # waited on before upserting so the in-place mutations on
        # ``versions`` (pdf_url / crs) are captured in the row write.
        self._fetch_followups_concurrently(versions, followup_executor, client)

        # Write the header first WITHOUT ``last_synced_at`` so a failure
        # in ``upsert_versions`` below leaves the timestamp unset on the
        # persisted header row. Set it only after both upserts succeed,
        # then re-upsert to stamp the column. Re-upserting with only
        # ``last_synced_at`` set is cheap (the repo's update branch
        # touches that one field) and lets a partial sync retry the
        # detail page on the next run instead of skipping it.
        self._repository.upsert(header)
        self._repository.upsert_versions(versions)
        header.last_synced_at = datetime.now(timezone.utc)
        self._repository.upsert(header)
        return len(versions)

    def _backfill_pdf_urls(self, versions: list[SpecVersion]) -> None:
        """Copy the persisted ``pdf_url`` onto freshly parsed versions.

        ``versions`` come from the detail page, which has no ETSI PDF
        link, so each ``pdf_url`` is ``None``. For any version we have
        already resolved (``pdf_url`` stored), load the stored value so
        the ETSI follow-up is skipped instead of re-fetched.
        """
        if not versions:
            return
        spec_id = versions[0].spec_id
        persisted = self._repository.list_versions(spec_id)
        by_version = {v.version: v.pdf_url for v in persisted if v.pdf_url}
        for v in versions:
            if v.pdf_url is None:
                v.pdf_url = by_version.get(v.version)

    def _fetch_followups_concurrently(
        self,
        versions: list[SpecVersion],
        executor: ThreadPoolExecutor,
        client: ScraperClient,
    ) -> None:
        """Submit the ETSI + CR follow-ups for ``versions`` to the
        dedicated ``executor`` (separate from the spec-worker pool) and
        wait for every future to complete.

        ``executor`` is the *follow-up* executor, not the pool running
        ``_sync_one_spec``: submitting to the same pool would starve it
        (a worker blocked on ``result()`` holds a slot the follow-ups it
        is waiting on can never get), deadlocking the sweep.

        Each follow-up is wrapped in its own try/except so a single
        failure does not cancel the others (``FIRST_EXCEPTION`` would
        otherwise leave later futures pending until the wait times
        out). The submission cost is bounded by ``len(versions) * 2``,
        which is in the low tens even for the largest specs.
        """
        followup_futures = []
        for v in versions:
            followup_futures.append(
                executor.submit(self._safe_fetch_etsi_pdf, v, client)
            )
            followup_futures.append(
                executor.submit(self._safe_fetch_crs, v, client)
            )
        for future in followup_futures:
            try:
                future.result()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Follow-up task raised unexpectedly", exc_info=True
                )

    def _safe_fetch_etsi_pdf(self, v: SpecVersion, client: ScraperClient) -> None:
        try:
            self._maybe_fetch_etsi_pdf(v, client)
        except Exception:  # noqa: BLE001
            logger.debug("ETSI PDF fetch failed for %s", v.version, exc_info=True)

    def _safe_fetch_crs(self, v: SpecVersion, client: ScraperClient) -> None:
        try:
            self._maybe_fetch_crs(v, client)
        except Exception:  # noqa: BLE001
            logger.debug("CR list fetch failed for %s", v.version, exc_info=True)

    def _maybe_fetch_etsi_pdf(self, v: SpecVersion, client: ScraperClient) -> None:
        if v.wki_id is None:
            return
        now = datetime.now(timezone.utc)
        upload_recent = (
            v.upload_date is not None
            and (now - datetime.combine(v.upload_date, datetime.min.time(), tzinfo=timezone.utc))
            < _CR_RECENCY_WINDOW
        )
        if not upload_recent or v.pdf_url:
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
        rapporteurs: str | None = None,
    ) -> list[Spec]:
        return self._repository.list(
            limit=limit, offset=offset, tsg=tsg, type=type, spec_id=spec_id,
            title=title, status=status, radio_tech=radio_tech,
            initial_release=initial_release, wis=wis, rapporteurs=rapporteurs,
        )

    def get(self, spec_id: str) -> Spec | None:
        return self._repository.get(spec_id)

    def list_versions(
        self,
        spec_id: str,
        limit: int = 200,
        offset: int = 0,
        version: str | None = None,
    ) -> list[SpecVersion]:
        return self._repository.list_versions(
            spec_id, limit=limit, offset=offset, version=version
        )


def _format_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        return f"{total // 3600}h"
    return f"{total // 86400}d"
