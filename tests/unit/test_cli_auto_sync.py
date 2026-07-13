"""Unit tests for the auto-sync orchestration helpers in ``cli_auto_sync.py``.

These tests mock the underlying services so they never hit the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from doc3gpp.cli_auto_sync import (
    extract_tsg_from_tdoc_id_or_pattern,
    resolve_meeting_id_for_tdoc_id,
    resolve_meetings_for_name_pattern,
    sync_meeting_internal,
    sync_tsg_internal,
    trigger_auto_sync,
)
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_sync_coordinator import (
    MeetingMissingFtpUrlError,
    MeetingNotFoundError,
    TDocSyncCoordinator,
)


class TestExtractTsgFromTdocIdOrPattern:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("R5-260013", "R5"),
            ("R5s260009", "R5"),
            ("R5w260013", "R5"),
            ("r5-260013", "R5"),
            ("S2-123456", "S2"),
            ("C6-654321", "C6"),
            ("R5%", "R5"),
            ("R5-%", "R5"),
            ("R5s%", "R5"),
            ("R5w%", "R5"),
            ("S2_%", "S2"),
        ],
    )
    def test_extracts_tsg(self, value: str, expected: str) -> None:
        assert extract_tsg_from_tdoc_id_or_pattern(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not-a-tdoc",
            "%R5%",
            "260013",
            "XX-260013",
        ],
    )
    def test_returns_none_for_unrecognised(self, value: str) -> None:
        assert extract_tsg_from_tdoc_id_or_pattern(value) is None


class TestResolveMeetingIdForTdocId:
    def test_returns_first_matching_meeting_id(self) -> None:
        service = MagicMock(spec=MeetingService)
        service.list_recent.return_value = [
            Meeting(
                meeting_id=42,
                name="R5-200",
                title="Title",
                location="City",
                start_date=None,
                end_date=None,
                ftp_url=None,
                start_doc=None,
                end_doc=None,
                tsg="R5",
            ),
        ]

        assert resolve_meeting_id_for_tdoc_id("R5-260013", service) == 42
        service.list_recent.assert_called_once_with(tdoc_id=("R5-", 260013), limit=1)

    def test_returns_none_when_no_meeting_matches(self) -> None:
        service = MagicMock(spec=MeetingService)
        service.list_recent.return_value = []

        assert resolve_meeting_id_for_tdoc_id("R5-260013", service) is None

    def test_returns_none_for_malformed_tdoc_id(self) -> None:
        service = MagicMock(spec=MeetingService)

        assert resolve_meeting_id_for_tdoc_id("not-an-id", service) is None
        service.list_recent.assert_not_called()


class TestResolveMeetingsForNamePattern:
    def test_delegates_to_list_recent(self) -> None:
        service = MagicMock(spec=MeetingService)
        service.list_recent.return_value = []

        assert resolve_meetings_for_name_pattern("R5#%", service) == []
        service.list_recent.assert_called_once_with(name_like="R5#%")


class TestSyncTsgInternal:
    def test_returns_true_when_synced(self, capsys) -> None:
        service = MagicMock(spec=MeetingService)
        service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete: 5 meeting rows stored",
            synced_count=5,
        )

        assert sync_tsg_internal("r5", service) is True
        service.sync.assert_called_once()
        called_url = service.sync.call_args.args[0]
        assert called_url == "https://www.3gpp.org/dynareport?code=Meetings-R5.htm"
        assert service.sync.call_args.kwargs == {"tsg": "R5", "force": False}
        captured = capsys.readouterr()
        assert "[auto-sync] Meeting sync complete: 5 meeting rows stored" in captured.out

    def test_returns_false_when_skipped(self, capsys) -> None:
        service = MagicMock(spec=MeetingService)
        service.sync.return_value = SyncOutcome(
            status="skipped",
            reason="Meeting sync skipped for TSG R5",
        )

        assert sync_tsg_internal("R5", service) is False
        captured = capsys.readouterr()
        assert "[auto-sync] Meeting sync skipped for TSG R5" in captured.out

    def test_returns_false_and_logs_warning_on_exception(self, capsys) -> None:
        service = MagicMock(spec=MeetingService)
        service.sync.side_effect = ConnectionError("network down")

        assert sync_tsg_internal("R5", service) is False
        captured = capsys.readouterr()
        assert captured.out == ""


class TestSyncMeetingInternal:
    def test_returns_true_when_synced(self, capsys) -> None:
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="TDoc sync complete: 3 TDoc row(s) and 2 auxiliary TDoc file(s) stored",
            synced_count=3,
            file_count=2,
        )

        assert sync_meeting_internal(42, coordinator) is True
        coordinator.sync_for_meeting_id.assert_called_once_with(42, force=False)
        captured = capsys.readouterr()
        assert "[auto-sync] TDoc sync complete: 3 TDoc row(s)" in captured.out

    def test_returns_false_when_skipped(self, capsys) -> None:
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="skipped",
            reason="TDoc sync skipped for meeting 42",
        )

        assert sync_meeting_internal(42, coordinator) is False
        captured = capsys.readouterr()
        assert "[auto-sync] TDoc sync skipped for meeting 42" in captured.out

    @pytest.mark.parametrize(
        "error",
        [
            MeetingNotFoundError("Meeting not found with id 42"),
            MeetingMissingFtpUrlError("Meeting 42 has no FTP URL stored"),
        ],
    )
    def test_returns_false_on_known_sync_errors(self, error: Exception) -> None:
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.side_effect = error

        assert sync_meeting_internal(42, coordinator) is False

    def test_returns_false_on_unexpected_exception(self) -> None:
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.side_effect = ConnectionError("network down")

        assert sync_meeting_internal(42, coordinator) is False


class TestTriggerAutoSync:
    def test_disabled_returns_zero_counts(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        assert trigger_auto_sync(
            auto_sync_enabled=False,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tsg="R5",
        ) == (0, 0)
        meeting_service.sync.assert_not_called()
        coordinator.sync_for_meeting_id.assert_not_called()

    def test_tsg_only_triggers_meeting_sync(self, capsys) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete",
            synced_count=1,
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tsg="R5",
        ) == (1, 0)
        coordinator.sync_for_meeting_id.assert_not_called()

    def test_tdoc_pattern_triggers_tsg_sync(self, capsys) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete",
            synced_count=1,
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc="R5%",
        ) == (1, 0)

    def test_full_tdoc_id_triggers_tsg_and_meeting_sync(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete",
            synced_count=1,
        )
        meeting_service.list_recent.return_value = [
            Meeting(
                meeting_id=42,
                name="R5-200",
                title="Title",
                location="City",
                start_date=None,
                end_date=None,
                ftp_url=None,
                start_doc=None,
                end_doc=None,
                tsg="R5",
            ),
        ]
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="TDoc sync complete",
            synced_count=3,
        )

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc="R5-260013",
        ) == (1, 1)
        meeting_service.list_recent.assert_called_once_with(tdoc_id=("R5-", 260013), limit=1)
        coordinator.sync_for_meeting_id.assert_called_once_with(42, force=False)

    def test_meeting_id_triggers_both_syncs(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete",
            synced_count=1,
        )
        meeting_service.get_by_id.return_value = Meeting(
            meeting_id=42,
            name="R5-200",
            title="Title",
            location="City",
            start_date=None,
            end_date=None,
            ftp_url=None,
            start_doc=None,
            end_doc=None,
            tsg="R5",
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="TDoc sync complete",
            synced_count=3,
        )

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            meeting_id=42,
        ) == (1, 1)

    def test_meeting_name_triggers_sync_for_matching_meetings(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete",
            synced_count=1,
        )
        meeting_service.list_recent.return_value = [
            Meeting(
                meeting_id=42,
                name="R5-200",
                title="Title",
                location="City",
                start_date=None,
                end_date=None,
                ftp_url=None,
                start_doc=None,
                end_doc=None,
                tsg="R5",
            ),
        ]
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="TDoc sync complete",
            synced_count=3,
        )

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            meeting_name="R5#%",
        ) == (1, 1)

    def test_meeting_name_with_no_match_triggers_nothing(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.list_recent.return_value = []
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            meeting_name="R5#%",
        ) == (0, 0)
        meeting_service.sync.assert_not_called()
        coordinator.sync_for_meeting_id.assert_not_called()

    def test_deduplicates_candidates(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="Meeting sync complete",
            synced_count=1,
        )
        meeting_service.list_recent.return_value = [
            Meeting(
                meeting_id=42,
                name="R5-200",
                title="Title",
                location="City",
                start_date=None,
                end_date=None,
                ftp_url=None,
                start_doc=None,
                end_doc=None,
                tsg="R5",
            ),
        ]
        meeting_service.get_by_id.return_value = Meeting(
            meeting_id=42,
            name="R5-200",
            title="Title",
            location="City",
            start_date=None,
            end_date=None,
            ftp_url=None,
            start_doc=None,
            end_doc=None,
            tsg="R5",
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="TDoc sync complete",
            synced_count=3,
        )

        # tsg, tdoc pattern and meeting_id all point to R5/42; sync should fire once each.
        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tsg="R5",
            tdoc="R5-260013",
            meeting_id=42,
        ) == (1, 1)
        meeting_service.sync.assert_called_once()
        coordinator.sync_for_meeting_id.assert_called_once()

    def test_partial_failure_continues_and_counts_successes(self, capsys) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.side_effect = [
            SyncOutcome(status="synced", reason="R5 synced", synced_count=1),
            ConnectionError("network down"),
        ]
        meeting_service.get_by_id.return_value = Meeting(
            meeting_id=42,
            name="R5-200",
            title="Title",
            location="City",
            start_date=None,
            end_date=None,
            ftp_url=None,
            start_doc=None,
            end_doc=None,
            tsg="R5",
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="TDoc sync complete",
            synced_count=3,
        )

        # Two TSG candidates: R5 succeeds, S2 fails; the single meeting still syncs.
        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tsg="R5",
            tdoc="S2%",
            meeting_id=42,
        ) == (1, 1)
        assert meeting_service.sync.call_count == 2
