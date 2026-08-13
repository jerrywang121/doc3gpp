"""Service layer for 3GPP specifications (TSs / TRs)."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from doc3gpp.models.spec import Spec, SpecVersion
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.repository.protocols import SpecRepository
from doc3gpp.scraping.client import ScraperClient
from doc3gpp.services._duration import format_duration as _format_duration
from doc3gpp.scraping.spec_source import (
    fetch_cr_list,
    fetch_dynareport_detail,
    fetch_etsi_pdf_text,
    fetch_spec_detail,
    fetch_spec_list,
)
from doc3gpp.parsers.spec_parser import (
    extract_cr_tdocs,
    extract_etsi_pdf_url,
    normalise_tsg_long_name,
    parse_dynareport_header,
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


class SpecUnknownOnUpstreamError(LookupError):
    """Raised when the DynaReport detail page does not carry a usable spec.

    Triggered by a 404, an empty / unknown-spec body, an unparseable
    ``#titleVal`` / ``#typeVal`` / ``#PrimaryResponsibleGroupLbl``, or
    a responsible-group label that cannot be normalised to a seeded
    ``tsgs.short_name`` (e.g. ``RAN AH1``).
    """

    def __init__(self, spec_id: str, reason: str) -> None:
        super().__init__(
            f"spec {spec_id!r} is unknown on the 3GPP DynaReport upstream "
            f"({reason}); nothing to sync"
        )
        self.spec_id = spec_id
        self.reason = reason


class SpecService:
    """Sync and query 3GPP specification records."""

    def __init__(
        self,
        repository: SpecRepository,
        sync_interval: timedelta = timedelta(hours=24),
        max_workers: int | None = None,
    ) -> None:
        self._repository = repository
        self._sync_interval = sync_interval
        self._max_workers = max_workers

    def sync(
        self,
        tsg: str,
        *,
        force: bool = False,
        per_version_details: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
        """Fetch list page -> parallel detail pages -> upsert.

        Resolves the TSG, fetches the list once, then fetches each
        detail page in a thread pool, running the per-spec skip
        check, and the conditional ETSI / CR follow-ups (skipped when
        ``per_version_details`` is ``False``), and the upsert
        inside each worker. The per-spec skip rule
        (``spec.last_synced_at``) is enforced at the worker, so
        ``force`` is only consulted by ``sync_spec`` (single-spec
        path); this method always walks the list, but specs that are
        within their interval are short-circuited before any HTTP
        request is made.

        When ``on_progress`` is supplied it is invoked once per
        successful sync sweep with ``"list_parsed"`` (carrying
        ``{"total": N}`` after the list page is parsed) and once per
        spec with ``"spec_done"`` (carrying ``{"spec_id": ...}``),
        including specs that were skipped at the worker — the progress
        bar advances in lockstep with the worker fan-out.
        """
        canonical = tsg.upper()
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
                            per_version_details=per_version_details,
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
                        if version_count > 0:
                            synced += 1
                        if on_progress is not None:
                            on_progress("spec_done", {"spec_id": spec.spec_id})

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
        """Refresh a single spec's detail page + versions.

        Looks ``spec_id`` up in the DB. When the row is missing,
        fetches the DynaReport detail page directly, parses the
        bootstrap header (``title`` / ``type`` / primary responsible
        group), normalises the group label, and funnels the
        freshly-built :class:`Spec` through the same
        ``_sync_one_spec`` pipeline as the stored-row path.

        Honours the per-spec ``spec.last_synced_at`` skip rule
        unless ``force`` — a freshly-bootstrapped spec with no
        ``last_synced_at`` is never skipped. A spec that is unknown
        on the upstream raises :class:`SpecUnknownOnUpstreamError`.
        """
        spec = self._repository.get(spec_id)
        if spec is None:
            spec = self._bootstrap_spec_from_dynareport(spec_id)
        elif not force and spec.last_synced_at is not None:
            now = datetime.now(timezone.utc)
            if (now - spec.last_synced_at) < self._sync_interval:
                ago = now - spec.last_synced_at
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"Spec sync skipped for {spec.spec_id}: "
                        f"last sync {_format_duration(ago)} ago "
                        f"(sync interval {_format_duration(self._sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

        canonical = spec.tsg.upper() if spec.tsg else ""
        logger.info("Syncing spec %s", spec.spec_id)
        with ScraperClient() as client:
            with ThreadPoolExecutor(max_workers=1) as followup_executor:
                version_count = self._sync_one_spec(
                    spec, canonical, followup_executor, client
                )
            if on_progress is not None:
                on_progress("spec_done", {"spec_id": spec.spec_id})

        return SyncOutcome(
            status="synced",
            reason=f"Spec sync complete for {spec.spec_id}: 1 spec, {version_count} versions stored",
            synced_count=1,
            version_count=version_count,
        )

    def _bootstrap_spec_from_dynareport(self, spec_id: str) -> Spec:
        """Fetch a missing spec's DynaReport detail page and build a ``Spec``.

        Used by :meth:`sync_spec` when the local ``specs`` table has
        no row for ``spec_id``. Returns a ``Spec`` carrying only the
        three bootstrap fields (``spec_id`` / ``type`` / ``title`` /
        ``tsg``) — the other header fields and the version rows are
        filled by the existing ``_sync_one_spec`` pipeline.

        Raises :class:`SpecUnknownOnUpstreamError` when the upstream
        body is unusable (404, missing fields, unrecognised group
        label).
        """
        html = fetch_dynareport_detail(spec_id)
        header = parse_dynareport_header(html)
        if header.title is None or header.type is None or header.tsg_long_name is None:
            missing = [
                name
                for name, value in (
                    ("title", header.title),
                    ("type", header.type),
                    ("tsg_long_name", header.tsg_long_name),
                )
                if value is None
            ]
            raise SpecUnknownOnUpstreamError(
                spec_id, f"missing fields: {', '.join(missing)}"
            )

        short_name = normalise_tsg_long_name(header.tsg_long_name)
        if short_name is None:
            raise SpecUnknownOnUpstreamError(
                spec_id,
                f"unrecognised primary responsible group {header.tsg_long_name!r}",
            )

        return Spec(
            spec_id=spec_id,
            type=header.type,
            title=header.title,
            tsg=short_name,
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
        per_version_details: bool = False,
    ) -> int:
        # Per-spec skip rule: a sync interval throttles each spec
        # independently. Read from the *incoming* spec (which already
        # carries the persisted ``last_synced_at`` thanks to
        # ``SpecRepository.upsert`` round-tripping the column) so the
        # list sweep stays at one HTTP fetch per fresh spec and walks
        # the per-spec skip at the worker.
        now = datetime.now(timezone.utc)
        if spec.last_synced_at is not None and (now - spec.last_synced_at) < self._sync_interval:
            logger.debug(
                "Skipping spec %s: last_synced_at=%s within interval %s",
                spec.spec_id,
                spec.last_synced_at.isoformat(),
                self._sync_interval,
            )
            return 0

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
        self._backfill_followup_fields(versions)

        # The ETSI + CR follow-ups are independent HTTP requests, fanned
        # out across the dedicated follow-up executor so they overlap
        # with the detail-page fetches of the other spec workers, then
        # waited on before upserting so the in-place mutations on
        # ``versions`` (pdf_url / crs) are captured in the row write.
        self._fetch_followups_concurrently(
            versions, followup_executor, client, per_version_details=per_version_details
        )

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

    def _backfill_followup_fields(self, versions: list[SpecVersion]) -> None:
        """Copy the persisted ``pdf_url`` and ``crs`` onto freshly parsed versions.

        ``versions`` come from the detail page, which carries neither
        the ETSI PDF link nor the CR list, so each ``pdf_url`` and
        ``crs`` is ``None``. For every version we have already resolved
        (either column stored), load the stored value so the upsert
        below writes the original value back instead of clobbering it
        with ``None``. Runs on every sync, regardless of whether
        ``per_version_details`` is on, so a flag-OFF re-sync preserves
        previously-fetched follow-up data.
        """
        if not versions:
            return
        spec_id = versions[0].spec_id
        persisted = self._repository.list_versions(spec_id)
        by_version: dict[str, SpecVersion] = {}
        for v in persisted:
            if v.pdf_url or v.crs:
                by_version[v.version] = v
        for v in versions:
            stored = by_version.get(v.version)
            if stored is None:
                continue
            if v.pdf_url is None and stored.pdf_url:
                v.pdf_url = stored.pdf_url
            if v.crs is None and stored.crs:
                v.crs = stored.crs

    def _fetch_followups_concurrently(
        self,
        versions: list[SpecVersion],
        executor: ThreadPoolExecutor,
        client: ScraperClient,
        per_version_details: bool = False,
    ) -> None:
        """Submit the ETSI + CR follow-ups for ``versions`` to the
        dedicated ``executor`` (separate from the spec-worker pool) and
        wait for every future to complete.

        ``executor`` is the *follow-up* executor, not the pool running
        ``_sync_one_spec``: submitting to the same pool would starve it
        (a worker blocked on ``result()`` holds a slot the follow-ups it
        is waiting on can never get), deadlocking the sweep.

        When ``per_version_details`` is ``False`` the follow-up HTTP
        fetches are skipped entirely — the caller only wants the
        detail-page data. ``_backfill_followup_fields`` still runs in
        ``_sync_one_spec`` so previously-persisted ``pdf_url`` /
        ``crs`` are not clobbered on a flag-OFF re-sync.

        Each follow-up is wrapped in its own try/except so a single
        failure does not cancel the others (``FIRST_EXCEPTION`` would
        otherwise leave later futures pending until the wait times
        out). The submission cost is bounded by ``len(versions) * 2``,
        which is in the low tens even for the largest specs.
        """
        if not per_version_details:
            return
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
