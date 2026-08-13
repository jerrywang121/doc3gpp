"""Unit tests for the auto-sync orchestration helpers in ``cli_auto_sync.py``.

These tests mock the underlying services so they never hit the network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from doc3gpp.cli_auto_sync import (
    collect_tdoc_candidates_for_url,
    extract_tsg_from_tdoc_id_or_pattern,
    resolve_meeting_id_for_tdoc_id,
    resolve_meetings_for_name_pattern,
    sync_meeting_internal,
    sync_tsg_internal,
    trigger_auto_sync,
)
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.sync import SyncOutcome
from doc3gpp.parsers.direct_extractor import NotAFolderError
from doc3gpp.services.meetings_service import MeetingService
from doc3gpp.services.tdoc_cr_service import TDocCrService
from doc3gpp.services.tdoc_sync_coordinator import (
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

    def test_returns_false_on_known_sync_errors(self) -> None:
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.side_effect = MeetingNotFoundError(
            "Meeting not found with id 42"
        )

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


class TestCollectTdocCandidatesForUrl:
    def _service(self, **kwargs) -> MagicMock:
        service = MagicMock(spec=TDocCrService)
        service.collect_3gpp_file_urls.configure_mock(**kwargs)
        return service

    def test_non_3gpp_url_returns_empty(self) -> None:
        service = self._service()

        result = collect_tdoc_candidates_for_url(
            "https://example.com/foo.zip",
            tdoc_service=service,
        )

        assert result == set()
        service.collect_3gpp_file_urls.assert_not_called()

    def test_3gpp_file_url_returns_single_id_without_calling_service(self) -> None:
        service = self._service()

        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/R5s260009.zip",
            tdoc_service=service,
        )

        assert result == {"R5s260009"}
        service.collect_3gpp_file_urls.assert_not_called()

    def test_3gpp_file_url_with_no_service_still_returns_id(self) -> None:
        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/R5s260009.zip",
            tdoc_service=None,
        )

        assert result == {"R5s260009"}

    def test_3gpp_file_url_with_unparseable_basename_returns_empty(self) -> None:
        service = self._service()

        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/readme.zip",
            tdoc_service=service,
        )

        assert result == set()
        service.collect_3gpp_file_urls.assert_not_called()

    def test_3gpp_docx_file_url_returns_single_id(self) -> None:
        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/R5-260020.docx",
            tdoc_service=None,
        )

        assert result == {"R5-260020"}

    def test_3gpp_folder_url_with_service_returns_union(self) -> None:
        service = self._service(
            return_value=[
                "https://www.3gpp.org/ftp/Docs/R5s260001.zip",
                "https://www.3gpp.org/ftp/Docs/R5s260002.zip",
                "https://www.3gpp.org/ftp/Docs/R5s260001_r1.zip",
            ],
        )

        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/Docs/",
            tdoc_service=service,
            max_depth=2,
        )

        assert result == {"R5s260001", "R5s260002"}
        service.collect_3gpp_file_urls.assert_called_once_with(
            "https://www.3gpp.org/ftp/Docs/",
            max_depth=2,
        )

    def test_3gpp_folder_url_without_service_returns_empty(self) -> None:
        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/Docs/",
            tdoc_service=None,
            max_depth=0,
        )

        assert result == set()

    @pytest.mark.parametrize(
        "exc",
        [
            NotAFolderError("https://www.3gpp.org/ftp/R5s260009.zip"),
            RuntimeError("network down"),
            ConnectionError("socket closed"),
        ],
    )
    def test_3gpp_folder_url_service_failure_returns_empty(
        self, exc: Exception, caplog
    ) -> None:
        service = self._service(side_effect=exc)

        with caplog.at_level("WARNING"):
            result = collect_tdoc_candidates_for_url(
                "https://www.3gpp.org/ftp/Docs/",
                tdoc_service=service,
                max_depth=0,
            )

        assert result == set()
        assert any(record.levelname == "WARNING" for record in caplog.records)

    def test_3gpp_unknown_shape_url_uses_basename_best_effort(self) -> None:
        service = self._service()

        result = collect_tdoc_candidates_for_url(
            "https://www.3gpp.org/ftp/info",
            tdoc_service=service,
        )

        assert result == set()
        service.collect_3gpp_file_urls.assert_not_called()


class TestTriggerAutoSyncTdocIds:
    def test_disabled_short_circuits_with_tdoc_ids(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        assert trigger_auto_sync(
            auto_sync_enabled=False,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc_ids=["R5-260013", "R5-260014", "S2-123456"],
        ) == (0, 0)
        meeting_service.sync.assert_not_called()
        coordinator.sync_for_meeting_id.assert_not_called()

    def test_deduplicates_repeated_tdoc_ids(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="R5 synced",
            synced_count=1,
        )
        meeting_service.list_recent.return_value = [
            Meeting(
                meeting_id=42,
                name="R5-200",
                title="",
                location="",
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
            reason="TDoc list synced",
            synced_count=3,
        )

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc_ids=["R5-260013"] * 200,
        ) == (1, 1)

        meeting_service.sync.assert_called_once()
        sync_args, sync_kwargs = meeting_service.sync.call_args
        assert "Meetings-R5" in sync_args[0]
        assert sync_kwargs == {"tsg": "R5", "force": False}
        # Set-dedup collapses the *sync* candidate sets, not the per-id
        # meeting lookups — each yielded id still triggers a probe.
        assert meeting_service.list_recent.call_count == 200
        coordinator.sync_for_meeting_id.assert_called_once_with(42, force=False)

    def test_deduplicates_across_distinct_meetings(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="synced",
            synced_count=1,
        )

        def fake_list_recent(*, tdoc_id, limit):  # type: ignore[no-untyped-def]
            prefix, _ = tdoc_id
            return [
                Meeting(
                    meeting_id=42 if prefix == "R5-" else 99,
                    name=prefix + "200",
                    title="",
                    location="",
                    start_date=None,
                    end_date=None,
                    ftp_url=None,
                    start_doc=None,
                    end_doc=None,
                    tsg=prefix.rstrip("-"),
                ),
            ]

        meeting_service.list_recent.side_effect = fake_list_recent
        coordinator = MagicMock(spec=TDocSyncCoordinator)
        coordinator.sync_for_meeting_id.return_value = SyncOutcome(
            status="synced",
            reason="synced",
            synced_count=1,
        )

        tdoc_ids = [
            *[f"R5-{260010 + i}" for i in range(50)],
            *[f"S2-{100010 + i}" for i in range(50)],
            "R5-260013",
        ] * 4

        result = trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc_ids=tdoc_ids,
        )
        assert result == (2, 2)
        sync_urls = sorted(
            c.args[0] for c in meeting_service.sync.call_args_list
        )
        assert any("Meetings-R5" in url for url in sync_urls)
        assert any("Meetings-S2" in url for url in sync_urls)
        assert sorted(
            c.args[0] for c in coordinator.sync_for_meeting_id.call_args_list
        ) == [42, 99]

    def test_tdoc_ids_pattern_only_triggers_tsg_sync(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="R5 synced",
            synced_count=1,
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        assert trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc_ids=["R5%", "R5s%"],
        ) == (1, 0)
        coordinator.sync_for_meeting_id.assert_not_called()

    def test_tdoc_ids_generator_is_consumed(self) -> None:
        meeting_service = MagicMock(spec=MeetingService)
        meeting_service.sync.return_value = SyncOutcome(
            status="synced",
            reason="R5 synced",
            synced_count=1,
        )
        coordinator = MagicMock(spec=TDocSyncCoordinator)

        def gen():
            yield "R5-260013"
            yield "R5-260014"

        result = trigger_auto_sync(
            auto_sync_enabled=True,
            meeting_service=meeting_service,
            tdoc_sync_coordinator=coordinator,
            tdoc_ids=gen(),
        )
        assert meeting_service.list_recent.call_count == 2
        assert result == (1, 0)
