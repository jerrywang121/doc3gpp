"""Auto-sync orchestration helpers for read-only CLI commands.

When ``Settings.sync.auto_sync`` is enabled, the listing/show/parse commands
internally trigger meeting-calendar and TDoc-list syncs before querying the
database. The existing skip rules (sync intervals, closed window, upstream
mtime) are always respected; auto-sync never bypasses them.

All helpers log warnings and continue on failure so that a transient network
error does not break a read command.
"""

from __future__ import annotations

import logging
import re

from doc3gpp.cli_filters import parse_tdoc_id
from doc3gpp.models.meeting import Meeting
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
    TDocSyncCoordinator,
)

logger = logging.getLogger(__name__)

_TSG_PREFIX_RE = re.compile(r"^([RSC][1-9])", re.IGNORECASE)


def _build_meeting_url(tsg: str, ext: str = "htm") -> str:
    """Compose the 3GPP DynaReport meeting-calendar URL for ``tsg``.

    The default ``ext="htm"`` matches the canonical 3GPP filename. Pass
    ``"html"`` if the upstream ever switches the suffix.
    """
    if ext not in ("htm", "html"):
        raise ValueError(f"Unsupported meeting URL extension: {ext!r}")
    return f"https://www.3gpp.org/dynareport?code=Meetings-{tsg.upper()}.{ext}"


def extract_tsg_from_tdoc_id_or_pattern(tdoc: str) -> str | None:
    """Return the TSG short name (e.g. ``R5``) from a TDoc id or pattern.

    Accepts:
    - full CR-shape ids: ``R5-260013``, ``R5s260009``, ``R5w260013``
    - SQL ``LIKE`` patterns starting with a TSG prefix: ``R5%``, ``R5-%``,
      ``R5s%``, ``R%``

    Returns ``None`` when no recognisable TSG prefix is present.
    """
    stripped = tdoc.strip()
    if not stripped:
        return None

    try:
        prefix, _ = parse_tdoc_id(stripped)
        return prefix[:2].upper()
    except ValueError:
        pass

    match = _TSG_PREFIX_RE.match(stripped)
    if match is not None:
        return match.group(1).upper()
    return None


def resolve_meeting_id_for_tdoc_id(
    tdoc_id: str,
    meeting_service: MeetingService,
) -> int | None:
    """Find the numeric meeting_id that owns a full CR-shape TDoc id.

    Uses the existing ``meeting list --tdoc`` range logic via
    ``MeetingService.list_recent``. Returns ``None`` when the id is not
    bracketed by any stored meeting.
    """
    try:
        parsed = parse_tdoc_id(tdoc_id)
    except ValueError:
        return None

    meetings = meeting_service.list_recent(tdoc_id=parsed, limit=1)
    if meetings:
        return meetings[0].meeting_id
    return None


def resolve_meetings_for_name_pattern(
    name_pattern: str,
    meeting_service: MeetingService,
) -> list[Meeting]:
    """Return meetings whose name matches the SQL LIKE pattern.

    This helper intentionally only looks at the local database. If no
    meetings match, the caller gets an empty list and no auto-sync is
    triggered for the name pattern.
    """
    return meeting_service.list_recent(name_like=name_pattern)


def sync_tsg_internal(tsg: str, meeting_service: MeetingService) -> bool:
    """Trigger an internal meeting-calendar sync for ``tsg``.

    Returns ``True`` only when the sync actually ran (``status == "synced"``).
    Skipped syncs and failures both return ``False``. The reason is echoed
    with an ``[auto-sync]`` prefix so the operator can see what happened.
    """
    canonical_tsg = tsg.upper()
    url = _build_meeting_url(canonical_tsg)
    try:
        outcome = meeting_service.sync(url, tsg=canonical_tsg, force=False)
    except Exception as exc:  # noqa: BLE001 - sync failure must not break read commands
        logger.warning("Auto-sync failed for TSG %s: %s", canonical_tsg, exc)
        return False

    prefix = "[auto-sync]"
    if outcome.status == "synced":
        print(f"{prefix} {outcome.reason}")
        return True
    print(f"{prefix} {outcome.reason}")
    return False


def sync_meeting_internal(meeting_id: int, coordinator: TDocSyncCoordinator) -> bool:
    """Trigger an internal TDoc-list sync for ``meeting_id``.

    Returns ``True`` only when the sync actually ran (``status == "synced"``).
    Skipped syncs and failures both return ``False``. The reason is echoed
    with an ``[auto-sync]`` prefix.
    """
    try:
        outcome = coordinator.sync_for_meeting_id(meeting_id, force=False)
    except (MeetingNotFoundError, MeetingMissingFtpUrlError) as exc:
        logger.warning("Auto-sync skipped for meeting %s: %s", meeting_id, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - sync failure must not break read commands
        logger.warning("Auto-sync failed for meeting %s: %s", meeting_id, exc)
        return False

    prefix = "[auto-sync]"
    if outcome.status == "synced":
        print(f"{prefix} {outcome.reason}")
        return True
    print(f"{prefix} {outcome.reason}")
    return False


def trigger_auto_sync(
    *,
    auto_sync_enabled: bool,
    meeting_service: MeetingService,
    tdoc_sync_coordinator: TDocSyncCoordinator,
    tsg: str | None = None,
    meeting_id: int | None = None,
    meeting_name: str | None = None,
    tdoc: str | None = None,
) -> tuple[int, int]:
    """Fire internal syncs based on the CLI filters supplied.

    Returns ``(meeting_syncs_done, tdoc_syncs_done)`` — counts of syncs that
    actually ran, not skipped or failed.

    Candidate extraction:
    - ``tsg`` is added directly to the TSG candidate list.
    - ``tdoc`` contributes a TSG when it starts with a recognisable prefix,
      and contributes a meeting_id when it is a full CR-shape id whose
      owning meeting is already stored.
    - ``meeting_id`` contributes itself and its owning TSG (looked up from
      the stored meeting row).
    - ``meeting_name`` contributes matching stored meetings' ids and TSGs.
    """
    if not auto_sync_enabled:
        return 0, 0

    tsg_candidates: set[str] = set()
    meeting_candidates: set[int] = set()

    if tsg is not None:
        tsg_candidates.add(tsg.upper())

    if tdoc is not None:
        extracted_tsg = extract_tsg_from_tdoc_id_or_pattern(tdoc)
        if extracted_tsg is not None:
            tsg_candidates.add(extracted_tsg)
        resolved_meeting_id = resolve_meeting_id_for_tdoc_id(tdoc, meeting_service)
        if resolved_meeting_id is not None:
            meeting_candidates.add(resolved_meeting_id)

    if meeting_id is not None:
        meeting_candidates.add(meeting_id)
        meeting = meeting_service.get_by_id(meeting_id)
        if meeting is not None and meeting.tsg is not None:
            tsg_candidates.add(meeting.tsg.upper())

    if meeting_name is not None:
        meetings = resolve_meetings_for_name_pattern(meeting_name, meeting_service)
        for meeting in meetings:
            meeting_candidates.add(meeting.meeting_id)
            if meeting.tsg is not None:
                tsg_candidates.add(meeting.tsg.upper())

    meeting_syncs_done = 0
    for candidate_tsg in sorted(tsg_candidates):
        if sync_tsg_internal(candidate_tsg, meeting_service):
            meeting_syncs_done += 1

    tdoc_syncs_done = 0
    for candidate_meeting_id in sorted(meeting_candidates):
        if sync_meeting_internal(candidate_meeting_id, tdoc_sync_coordinator):
            tdoc_syncs_done += 1

    return meeting_syncs_done, tdoc_syncs_done
