"""Unit tests for the ``tdoc parse`` and ``tdoc show`` CLI commands.

The tests stub out :func:`doc3gpp.services.factory.build_tdoc_cr_service`
and :class:`SQLAlchemyTDocRepository` via ``monkeypatch`` so the CLI
runs without ever touching the network, the python-docx renderer, or
the ``tdocs`` table. Each test seeds the ``sqlite_env`` fixture and
resets the settings/engine caches via ``conftest.sqlite_env``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import (
    TDocCRDetails,
    TDocCRTTCNDetails,
    TDocExtractMeta,
)
from doc3gpp.models.tdoc_file import TDocFile
from doc3gpp.parsers.docx_converter import PythonDocxNotInstalledError
from doc3gpp.services.tdoc_cr_service import (
    BatchExtractResult,
    ExtractResult,
    TDocNotFoundError,
    TDocTypeUnsupportedError,
    TDocZipDownloadError,
)
from doc3gpp.settings.loader import get_settings
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_file_sql import SQLAlchemyTDocFileRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


@pytest.fixture(autouse=True)
def _disable_auto_sync_by_default(monkeypatch):
    """Force ``sync.auto_sync`` off for every test in this file.

    These tests stub out the network/DB paths, so a user-level
    ``~/.config/doc3gpp/config.toml`` with ``auto_sync = "true"`` would
    cause the CLI to attempt a sync against the (otherwise unused) tmp
    sqlite DB before the stubbed repo takes over, raising
    ``sqlite3.OperationalError: no such table: meetings``. Pinning the
    setting via the env (which beats the TOML in precedence) keeps the
    tests hermetic regardless of the operator's local config.
    """
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeExtractResult:
    """Minimal stand-in for :class:`ExtractResult` with `.details` / `.from_cache`.

    The CLI only reads ``result.details`` (for ``spec`` / ``cr_num`` /
    ``title``) and ``result.details.tdoc_id`` (to seed the success set).
    Constructing a full :class:`ExtractResult` would force a real
    :class:`TDocExtractMeta`, which is fine but heavier than this test
    surface needs.
    """

    details: TDocCRDetails
    from_cache: bool = False


class _FakeCrService:
    """In-memory :class:`TDocCrService` double that records extract_many calls."""

    def __init__(
        self,
        results: dict[str, ExtractResult] | None = None,
        failures: dict[str, str] | None = None,
        skipped: dict[str, str] | None = None,
        raise_from_many: Exception | None = None,
        raise_from_extract: Exception | None = None,
    ) -> None:
        self._results = results or {}
        self._failures = failures or {}
        self._skipped = skipped or {}
        self._raise_from_many = raise_from_many
        self._raise_from_extract = raise_from_extract
        self.many_calls: list[tuple[list[str], bool, bool]] = []
        self.extract_calls: list[str] = []

    def extract_many(
        self,
        tdoc_ids,
        *,
        force: bool = False,
        full: bool = False,
        on_progress=None,
        is_cancelled=None,
    ) -> BatchExtractResult:
        self.many_calls.append((list(tdoc_ids), force, full))
        if self._raise_from_many is not None:
            raise self._raise_from_many
        return BatchExtractResult(
            successes=dict(self._results),
            failures=dict(self._failures),
            skipped=dict(self._skipped),
        )

    def extract(self, tdoc_id, *, force: bool = False) -> ExtractResult:
        self.extract_calls.append(tdoc_id)
        if self._raise_from_extract is not None:
            raise self._raise_from_extract
        if tdoc_id not in self._results:
            raise TDocNotFoundError(tdoc_id)
        return self._results[tdoc_id]


class _FakeTDocRepoList:
    """In-memory :class:`TDocRepository` double exposing ``list()`` / ``list_with_meeting``.

    The ``--meeting-id`` / filter branch calls ``list_with_meeting``;
    older tests stubbed ``list`` directly, so we mirror both APIs on
    the same fixture. Records every call's kwargs so the test can
    assert the CLI asked for ``tdoc_type="CR"`` with the right
    ``meeting_id`` and the configured ``max_batch``.

    When ``parsed_ids`` is supplied, the fake models the contract the
    production SQL repo must satisfy once pending selection is pushed
    to SQL: passing ``exclude_parsed=True`` causes parsed rows to be
    filtered *before* ``limit`` is applied. Without the flag, ``limit``
    is applied to the raw match set (the current behaviour).
    """

    def __init__(
        self,
        list_tdocs: list[TDoc],
        meeting_names: dict[int, str] | None = None,
        parsed_ids: set[str] | None = None,
    ) -> None:
        self._list_tdocs = list_tdocs
        self._meeting_names = meeting_names or {}
        self._parsed_ids = parsed_ids or set()
        self.list_calls: list[dict] = []
        self.list_with_meeting_calls: list[dict] = []

    def get_by_id(self, tdoc_id: str) -> TDoc | None:
        return None

    def list(self, **kwargs) -> list[TDoc]:
        self.list_calls.append(kwargs)
        return list(self._list_tdocs)

    def list_with_meeting(self, **kwargs):
        from doc3gpp.models.tdoc import TDocWithMeeting

        self.list_with_meeting_calls.append(kwargs)
        rows = list(self._list_tdocs)
        if kwargs.get("exclude_parsed"):
            rows = [t for t in rows if t.tdoc_id not in self._parsed_ids]
        limit = kwargs.get("limit")
        if limit is not None:
            rows = rows[:limit]
        return [
            TDocWithMeeting(
                tdoc=tdoc,
                meeting_name=self._meeting_names.get(tdoc.meeting_id) if tdoc.meeting_id else None,
            )
            for tdoc in rows
        ]


class _FakeMeetingService:
    """In-memory :class:`MeetingService` double exposing ``get_by_id`` for --meeting-id."""

    def __init__(self, meetings: dict[int, Meeting | None]) -> None:
        self._meetings = meetings
        self.get_calls: list[int] = []

    def get_by_id(self, meeting_id: int) -> Meeting | None:
        self.get_calls.append(meeting_id)
        return self._meetings.get(meeting_id)


class _FakeCrDetailRepo:
    """In-memory :class:`TDocCrDetailRepository` double that answers ``get(tdoc_id)``.

    Mirrors the slim cover-page repo surface: ``get`` /
    ``get_by_url`` for reads, ``upsert`` for cover-row writes, and
    ``upsert_extract_meta`` for the extract-metadata sidecar. The
    ``tdoc parse`` filter branch only exercises ``get``; the
    ``tdoc show`` branch exercises ``get_by_url`` and
    ``get_extract_meta_by_url``.
    """

    def __init__(
        self,
        parsed_ids: set[str] | None = None,
        by_url: dict[str, TDocCRDetails] | None = None,
        extract_meta_by_url: dict[str, TDocExtractMeta] | None = None,
    ) -> None:
        self._parsed = parsed_ids or set()
        self._by_url = by_url or {}
        self._extract_meta_by_url = extract_meta_by_url or {}
        self.get_calls: list[str] = []
        self.get_by_url_calls: list[str] = []
        self.upsert_calls: list[TDocCRDetails] = []
        self.upsert_extract_meta_calls: list[TDocExtractMeta] = []

    def get(self, tdoc_id: str) -> list:
        self.get_calls.append(tdoc_id)
        return [] if tdoc_id not in self._parsed else [_SENTINEL_DETAIL]

    def get_by_url(self, url: str) -> TDocCRDetails | None:
        self.get_by_url_calls.append(url)
        return self._by_url.get(url)

    def upsert(self, details: TDocCRDetails) -> None:
        self.upsert_calls.append(details)

    def upsert_extract_meta(self, meta: TDocExtractMeta) -> None:
        self.upsert_extract_meta_calls.append(meta)

    def get_extract_meta(self, tdoc_id: str) -> list:
        return []

    def get_extract_meta_by_url(self, url: str) -> TDocExtractMeta | None:
        return self._extract_meta_by_url.get(url)

    def list_all(self) -> list:
        return []


class _FakeCrTtcnRepo:
    """In-memory :class:`TDocCrTTCNDetailRepository` double.

    Records every ``upsert`` / ``get_by_url`` call so tests can verify
    that ``tdoc show`` only touches the TTCN table for TTCN-shape ids.
    """

    def __init__(self, by_url: dict[str, TDocCRTTCNDetails] | None = None) -> None:
        self._by_url = by_url or {}
        self.upsert_calls: list[TDocCRTTCNDetails] = []
        self.get_by_url_calls: list[str] = []

    def upsert(self, details: TDocCRTTCNDetails) -> None:
        self.upsert_calls.append(details)

    def get_by_url(self, url: str) -> TDocCRTTCNDetails | None:
        self.get_by_url_calls.append(url)
        return self._by_url.get(url)

    def get(self, tdoc_id: str) -> list[TDocCRTTCNDetails]:
        return []

    def list_all(self) -> list[TDocCRTTCNDetails]:
        return []


_SENTINEL_DETAIL = object()


def _patch_service(monkeypatch, fake: _FakeCrService) -> None:
    """Stub ``build_tdoc_cr_service`` so the CLI picks up ``fake``.

    The CLI passes ``max_tdoc_size_bytes=`` through to the factory; the
    stub accepts and ignores any kwargs so dispatch-side wiring changes
    don't break unrelated tests.
    """
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: fake,
    )


def _patch_tdoc_repo_for_listing(
    monkeypatch,
    list_tdocs: list[TDoc],
    meeting_names: dict[int, str] | None = None,
    parsed_ids: set[str] | None = None,
) -> "_FakeTDocRepoList":
    """Stub ``build_tdoc_repository`` so ``tdoc parse`` can call ``list_with_meeting``.

    Records every ``list_with_meeting`` call so the test can assert
    the filter branch queried for ``tdoc_type="CR"`` with the expected
    ``meeting_id`` and ``limit=max_batch``. Only ``list_with_meeting``
    and ``list`` are exercised; ``get_by_id`` returns ``None`` so a
    stray lookup surfaces as a miss.

    When ``parsed_ids`` is supplied, the fake filters them out of
    ``list_with_meeting`` whenever the caller passes
    ``exclude_parsed=True`` (the SQL-level pending-selection contract
    under test).
    """
    fake = _FakeTDocRepoList(
        list_tdocs, meeting_names=meeting_names, parsed_ids=parsed_ids,
    )

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_repository",
        lambda: fake,
    )
    return fake


def _patch_meeting_service(
    monkeypatch, meetings: dict[int, Meeting | None]
) -> "_FakeMeetingService":
    """Stub ``build_meeting_service`` so ``--meeting-id`` validation has data.

    ``meetings`` maps meeting_id → :class:`Meeting` (or ``None`` to
    simulate an unknown id). Records every ``get_by_id`` call.
    """
    fake = _FakeMeetingService(meetings)

    monkeypatch.setattr(
        "doc3gpp.cli.build_meeting_service",
        lambda: fake,
    )
    return fake


def _patch_cr_repo(
    monkeypatch,
    parsed_ids: set[str] | None = None,
    *,
    by_url: dict[str, TDocCRDetails] | None = None,
    extract_meta_by_url: dict[str, TDocExtractMeta] | None = None,
) -> "_FakeCrDetailRepo":
    """Stub ``build_tdoc_cr_repository`` so ``tdoc parse`` can probe parsed status.

    ``parsed_ids`` is the set of TDoc ids considered already parsed
    (``get(tdoc_id)`` returns a non-empty list for these). Records
    every ``get`` call so the test can verify the CLI checked parsed
    status per row when ``force=False``.

    Pass ``by_url`` / ``extract_meta_by_url`` to seed the URL-keyed
    lookups used by ``tdoc show``.
    """
    fake = _FakeCrDetailRepo(
        parsed_ids=parsed_ids or set(),
        by_url=by_url,
        extract_meta_by_url=extract_meta_by_url,
    )

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_repository",
        lambda: fake,
    )
    return fake


def _patch_cr_ttcn_repo(
    monkeypatch,
    *,
    by_url: dict[str, TDocCRTTCNDetails] | None = None,
) -> "_FakeCrTtcnRepo":
    """Stub ``build_tdoc_cr_ttcn_repository`` so ``tdoc show`` can probe the sidecar.

    Pass ``by_url`` to seed TTCN detail rows keyed by ``ftp_url``.
    """
    fake = _FakeCrTtcnRepo(by_url=by_url)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_ttcn_repository",
        lambda: fake,
    )
    return fake


def _make_details(
    tdoc_id: str = "R5s260009",
    *,
    spec: str | None = "38.523-3",
    cr_num: str | None = "3790",
    title: str | None = "Example CR",
) -> TDocCRDetails:
    """Build a populated :class:`TDocCRDetails` with sensible defaults."""
    return TDocCRDetails(tdoc_id=tdoc_id, spec=spec, cr_num=cr_num, title=title)


def _make_result(
    tdoc_id: str = "R5s260009",
    *,
    spec: str | None = "38.523-3",
    cr_num: str | None = "3790",
    title: str | None = "Example CR",
) -> ExtractResult:
    """Build a fully-wired :class:`ExtractResult` for the fake service."""
    url = f"stored/{tdoc_id}.zip"
    meta = TDocExtractMeta(
        ftp_url=url,
        tdoc_id=tdoc_id,
        cache_file=f"{tdoc_id}.zip",
        doc_filename=f"{tdoc_id}.docx",
        extracted_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    return ExtractResult(
        details=_make_details(tdoc_id, spec=spec, cr_num=cr_num, title=title),
        extract_meta=meta,
        from_cache=False,
    )


def _pin_max_batch_via_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    max_batch: int,
) -> None:
    """Set ``tdoc_parse.max_batch`` through a TOML config file.

    Replaces the legacy ``DOC3GPP_TDOC_PARSE__MAX_BATCH`` env-var
    override, which is now outside the
    :data:`doc3gpp.settings.schema.ALLOWED_ENV_VARS` allowlist and
    therefore silently ignored.
    """
    config_path = tmp_path / "tdoc-parse-config.toml"
    config_path.write_text(
        f"[tdoc_parse]\nmax_batch = {max_batch}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# tdoc parse — flag surface
# ---------------------------------------------------------------------------


def test_tdoc_parse_rejects_no_filters(sqlite_env) -> None:
    """Invoking the command with no filter flag raises ``BadParameter``."""
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse"])
    assert result.exit_code != 0
    assert "Specify at least one filter" in result.output


def test_tdoc_parse_tdoc_id_flag_removed(sqlite_env) -> None:
    """``--tdoc-id`` no longer exists — Typer's usage error surfaces."""
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--tdoc-id", "1"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower() or "Unknown option" in result.output


def test_tdoc_parse_cat_flag_renamed_to_cr_cat(sqlite_env) -> None:
    """``--cat`` was renamed to ``--cr-cat`` — old flag now rejected."""
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--cat", "F"])
    assert result.exit_code != 0
    assert "no such option" in result.output.lower() or "Unknown option" in result.output


def test_tdoc_parse_tdoc_flag_singular_silently_keeps_last(sqlite_env, monkeypatch) -> None:
    """``--tdoc`` is singular — passing it twice silently keeps the last
    value (Click's standard non-multi behaviour). Operators wanting to
    match multiple ids should build a single LIKE pattern instead.
    """
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260010", type="CR")]
    repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260010": _make_result("R5s260010")})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--tdoc", "R5s260009",
            "--tdoc", "R5s260010",
            "--yes",
        ],
    )
    # Click keeps the last value; exit is 0 because the LIKE match
    # resolves a row.
    assert result.exit_code == 0, result.output
    # The repo saw only the LAST --tdoc value.
    assert repo.list_with_meeting_calls[0]["tdoc_id"] == "R5s260010"
    # And the dispatched id is the resolved one, not both.
    assert fake.many_calls == [(["R5s260010"], False, False)]


# ---------------------------------------------------------------------------
# tdoc parse — --tdoc LIKE pattern
# ---------------------------------------------------------------------------


def test_tdoc_parse_tdoc_like_pattern_happy_path(sqlite_env, monkeypatch) -> None:
    """``--tdoc R5s260009`` (no wildcards) resolves through the repo's
    LIKE filter and dispatches the match to ``extract_many``."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260009", "--yes"])
    assert result.exit_code == 0, result.output
    # The repo received the exact value as a LIKE pattern (not normalised).
    assert fake.many_calls == [(["R5s260009"], False, False)]
    # The to-parse group was printed, plus a "Newly parsed" summary.
    assert "To parse" in result.output
    assert "R5s260009" in result.output
    assert "Newly parsed:                              1" in result.output


def test_tdoc_parse_tdoc_wildcard_pattern(sqlite_env, monkeypatch) -> None:
    """``--tdoc 'R5s26%'`` is forwarded as a LIKE pattern verbatim;
    the repo filters to matching rows."""
    runner = CliRunner()
    cr_tdocs = [
        TDoc(tdoc_id="R5s260001", type="CR"),
        TDoc(tdoc_id="R5s260002", type="CR"),
    ]
    repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={
            "R5s260001": _make_result("R5s260001"),
            "R5s260002": _make_result("R5s260002"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s26%", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert repo.list_with_meeting_calls[0]["tdoc_id"] == "R5s26%"
    assert sorted(fake.many_calls[0][0]) == ["R5s260001", "R5s260002"]


# ---------------------------------------------------------------------------
# tdoc parse — partial / all failures
# ---------------------------------------------------------------------------


def test_tdoc_parse_partial_failure(sqlite_env, monkeypatch) -> None:
    """When ``extract_many`` reports a failure for one id via
    ``batch.failures``, the CLI prints ``FAILED - {reason}`` inline and
    still exits 0 (one success keeps the batch non-fatal)."""
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    runner = CliRunner()
    cr_tdocs = [
        TDoc(tdoc_id="R5s260009", type="CR"),
        TDoc(tdoc_id="R5s260010", type="CR"),
    ]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={"R5s260009": _make_result("R5s260009")},
        failures={"R5s260010": "TDocNotFoundError: TDoc 'R5s260010' is not stored"},
    )
    _patch_service(monkeypatch, fake)

    # LIKE pattern that matches both ids — singular flag, two matches
    # via the wildcard.
    result = runner.invoke(
        app,
        ["tdoc", "parse", "--tdoc", "R5s2600%", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "R5s260009: spec=" in result.output
    assert "R5s260010: FAILED - TDocNotFoundError: TDoc 'R5s260010' is not stored" in result.output
    assert "Newly parsed:                              1" in result.output
    assert "Failures:                                  1" in result.output


def test_tdoc_parse_all_failures(sqlite_env, monkeypatch) -> None:
    """``extract_many`` reporting every id as a failure (no successes)
    yields exit 1 and an all-failures summary that surfaces the per-id
    reason instead of the old generic "see logs" message."""
    runner = CliRunner()
    cr_tdocs = [
        TDoc(tdoc_id="R5s260009", type="CR"),
        TDoc(tdoc_id="R5s260010", type="CR"),
    ]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={},
        failures={
            "R5s260009": "TDocNotFoundError: TDoc 'R5s260009' is not stored",
            "R5s260010": "TDocTypeUnsupportedError: TDoc 'R5s260010' has type 'LS'",
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s26%", "--yes"])
    assert result.exit_code == 1
    assert "R5s260009: FAILED - TDocNotFoundError:" in result.output
    assert "R5s260010: FAILED - TDocTypeUnsupportedError:" in result.output
    assert "Newly parsed:                              0" in result.output


@pytest.mark.parametrize(
    ("exc_factory", "expected_class"),
    [
        (lambda: TDocNotFoundError("R5s260010"), "TDocNotFoundError"),
        (
            lambda: TDocTypeUnsupportedError("R5s260010", "LS"),
            "TDocTypeUnsupportedError",
        ),
        (
            lambda: TDocZipDownloadError(
                "https://www.3gpp.org/ftp/R5s260010.zip",
                RuntimeError("404 Not Found"),
            ),
            "TDocZipDownloadError",
        ),
        (
            lambda: ValueError("Invalid tdoc_id shape: 'X'"),
            "ValueError",
        ),
    ],
)
def test_tdoc_parse_failure_message_names_the_step(
    sqlite_env, monkeypatch, exc_factory, expected_class
) -> None:
    """The ``FAILED - {ExceptionClassName}: {message}`` format lets the
    operator see *which* step failed (type guard, DB lookup, network,
    shape check) without opening the log file."""
    exc = exc_factory()
    cr_tdocs = [TDoc(tdoc_id="R5s260010", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={},
        failures={"R5s260010": f"{type(exc).__name__}: {exc}"},
    )
    _patch_service(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260010", "--yes"])
    assert result.exit_code == 1
    assert f"R5s260010: FAILED - {expected_class}:" in result.output
    assert str(exc) in result.output


def test_tdoc_parse_force_passed_through(sqlite_env, monkeypatch) -> None:
    """``--force=True`` is forwarded to ``extract_many``."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--tdoc", "R5s260009", "--force", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260009"], True, False)]


def test_tdoc_parse_full_passed_through(sqlite_env, monkeypatch) -> None:
    """``--full`` reaches ``extract_many`` so TTCN ``before_change`` /
    ``after_change`` / ``new_change`` extraction fires for every id in
    the batch. Locks the wiring that previously silently dropped the
    flag in the DB-mode path.
    """
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--tdoc", "R5s260009", "--full", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260009"], False, True)]


def test_tdoc_parse_full_and_force_compose(sqlite_env, monkeypatch) -> None:
    """``--full`` and ``--force`` both reach ``extract_many`` when combined."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--tdoc", "R5s260009", "--full", "--force", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260009"], True, True)]


def test_tdoc_parse_python_docx_missing_friendly_error(sqlite_env, monkeypatch) -> None:
    """When ``extract_many`` raises :class:`PythonDocxNotInstalledError`,
    the CLI prints the install hint and exits 1 — the batch does not
    crash with a Python traceback."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(raise_from_many=PythonDocxNotInstalledError())
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s260009", "--yes"],
    )
    assert result.exit_code == 1
    assert "python-docx is not installed" in result.output
    assert "pip install doc3gpp[extract]" in result.output


def test_tdoc_parse_typeerror_in_batch_is_per_id_failure(
    sqlite_env, monkeypatch,
) -> None:
    """Regression: an internal ``TypeError`` from one TDoc (e.g. from a
    cache-key helper being called with ``None``) used to abort the
    whole batch via the outer CLI handler. ``extract_many`` now catches
    ``TypeError`` as a per-id failure so the rest of the batch makes
    progress and the CLI prints the failure inline.
    """
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")

    runner = CliRunner()
    cr_tdocs = [
        TDoc(tdoc_id="R5s263431", type="CR"),
        TDoc(tdoc_id="R5s263432", type="CR"),
    ]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={
            "R5s263432": _make_result("R5s263432"),
        },
        failures={
            "R5s263431": (
                "TypeError: argument should be a str or an os.PathLike "
                "object where __fspath__ returns a str, not 'NoneType'"
            ),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s2634%", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "R5s263431: FAILED - TypeError:" in result.output
    assert "R5s263432: spec=" in result.output
    assert "Newly parsed:                              1" in result.output
    assert "Failures:                                  1" in result.output


def test_tdoc_parse_ftp_url_null_lands_in_skip_bucket(
    sqlite_env, monkeypatch,
) -> None:
    """Rows whose ``ftp_url`` is NULL (3GPP upload pipeline hasn't
    propagated yet) are routed to the ``skipped`` dict, NOT
    ``failures`` — the CLI prints them under a dedicated
    ``Skipped (not yet on FTP)`` summary line and exits 0 when the
    batch has no real failures.
    """
    # Pin ``auto_sync=false`` so the test doesn't depend on the
    # ``DOC3GPP_SYNC__AUTO_SYNC`` value any prior test left in
    # the cached pydantic settings.
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")

    runner = CliRunner()
    cr_tdocs = [
        TDoc(tdoc_id="R5s263431", type="CR", ftp_url=None),
        TDoc(tdoc_id="R5s263432", type="CR", ftp_url=None),
    ]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={},
        skipped={
            "R5s263431": (
                "TDocNotYetOnFTPError: TDoc 'R5s263431' has no ftp_url "
                "yet — the 3GPP upload pipeline has not propagated a "
                "final URL; try again later or run `doc3gpp tdoc sync` "
                "to refresh the tdocs table"
            ),
            "R5s263432": (
                "TDocNotYetOnFTPError: TDoc 'R5s263432' has no ftp_url "
                "yet — the 3GPP upload pipeline has not propagated a "
                "final URL; try again later or run `doc3gpp tdoc sync` "
                "to refresh the tdocs table"
            ),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s2634%", "--yes"],
    )
    assert result.exit_code == 0, result.output
    # Per-row inline prefix for the skip bucket.
    assert "R5s263431: SKIPPED - TDocNotYetOnFTPError:" in result.output
    assert "R5s263432: SKIPPED - TDocNotYetOnFTPError:" in result.output
    # Dedicated summary line (matches the existing format: right-padded
    # to a column width with the bucket label + count).
    assert "Skipped (not yet on FTP):" in result.output
    assert "Failures:                                  0" in result.output


def test_tdoc_parse_unexpected_internal_error_exits_cleanly(
    sqlite_env, monkeypatch, caplog,
) -> None:
    """Regression: a non-``PythonDocxNotInstalledError`` exception escaping
    ``extract_many`` used to leak a raw ``rich`` traceback whose
    in-context source line confused users into reading the
    ``python-docx`` install hint as the actual error. The CLI now
    surfaces a short message and exits 1, with the full traceback in
    the log file.
    """
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")

    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(raise_from_many=RuntimeError("boom"))
    _patch_service(monkeypatch, fake)

    with caplog.at_level(logging.ERROR, logger="doc3gpp.cli"):
        result = runner.invoke(
            app, ["tdoc", "parse", "--tdoc", "R5s26%", "--yes"],
        )
    assert result.exit_code == 1, result.output
    assert "Unexpected error: RuntimeError: boom" in result.output
    # The full traceback lands in the log, not stdout.
    assert "Traceback" not in result.output
    assert any("Unexpected error" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# tdoc parse — --meeting-id + filter composition
# ---------------------------------------------------------------------------


def test_tdoc_parse_meeting_id_parses_new_only(
    sqlite_env, monkeypatch
) -> None:
    """``--meeting-id`` asks the repo to exclude already-parsed rows so
    only pending ones reach ``extract_many``; the parsed id is filtered
    at the SQL layer and is not surfaced in the confirmation preview."""
    runner = CliRunner()
    meeting_id = 42
    cr_tdocs = [
        TDoc(tdoc_id="R5s260009", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260010", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260011", type="CR", meeting_id=meeting_id),
    ]
    # R5s260010 is already parsed; the other two are new.
    parsed = {"R5s260010"}

    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    repo = _patch_tdoc_repo_for_listing(
        monkeypatch, cr_tdocs, parsed_ids=parsed,
    )
    _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(
        results={
            "R5s260009": _make_result("R5s260009"),
            "R5s260011": _make_result("R5s260011"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--meeting-id", str(meeting_id), "--yes"])
    assert result.exit_code == 0, result.output
    assert repo.list_with_meeting_calls[0] == {
        "limit": 100,  # default max_batch
        "offset": 0,
        "tdoc_id": None,
        "meeting_like": None,
        "meeting_id": meeting_id,
        "tdoc_type": "CR",
        "status": None,
        "cr_cat": None,
        "spec": None,
        "wi": None,
        "revision_of": None,
        "revised_to": None,
        "title": None,
        "ftp_url": None,
        "release": None,
        "version": None,
        "cr_num": None,
        "cr_pack": None,
        "source": None,
        "uploaded_date": None,
        "exclude_parsed": True,
    }
    assert fake.many_calls == [(["R5s260009", "R5s260011"], False, False)]
    assert "R5s260009" in result.output
    assert "R5s260011" in result.output
    # Normal mode never renders the already-parsed table.
    assert "Already parsed in tdoc_cr_cover_page" not in result.output
    assert "R5s260010" not in result.output
    assert "Newly parsed:                              2" in result.output


def test_tdoc_parse_meeting_id_force_re_extracts_parsed(
    sqlite_env, monkeypatch
) -> None:
    """With ``--force``, every CR-type TDoc under the meeting reaches
    ``extract_many`` regardless of parsed status — the parsed-status
    group is printed as informational only and labelled
    'with --force, these will be re-extracted'."""
    runner = CliRunner()
    meeting_id = 7
    cr_tdocs = [
        TDoc(tdoc_id="R5s260009", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260010", type="CR", meeting_id=meeting_id),
    ]
    # Both are "parsed" in the fake CR repo; --force must skip the check.
    parsed = {"R5s260009", "R5s260010"}

    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(
        results={
            "R5s260009": _make_result("R5s260009"),
            "R5s260010": _make_result("R5s260010"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(meeting_id), "--force", "--yes"],
    )
    assert result.exit_code == 0, result.output
    # force=True was forwarded to extract_many with every CR tdoc id.
    assert fake.many_calls == [(["R5s260009", "R5s260010"], True, False)]
    # The completion summary reports Re-parsed + Newly parsed correctly.
    assert "Re-parsed (with --force):                  2" in result.output
    assert "Newly parsed:                              0" in result.output
    assert "with --force, these will be re-extracted" in result.output


def test_tdoc_parse_meeting_id_unknown_meeting(sqlite_env, monkeypatch) -> None:
    """An unknown ``--meeting-id`` raises ``BadParameter`` with a
    pointer to ``meeting list`` and never reaches the TDoc repo."""
    runner = CliRunner()
    _patch_meeting_service(monkeypatch, meetings={})
    repo = _patch_tdoc_repo_for_listing(monkeypatch, list_tdocs=[])

    result = runner.invoke(app, ["tdoc", "parse", "--meeting-id", "9999"])
    assert result.exit_code != 0
    assert "Unknown meeting_id 9999" in result.output
    assert "doc3gpp meeting list" in result.output
    # The CLI bailed before fetching the TDoc list.
    assert repo.list_with_meeting_calls == []


def test_tdoc_parse_meeting_id_no_matches_exits_1(sqlite_env, monkeypatch) -> None:
    """When the filter set matches zero TDocs the CLI prints a friendly
    message and exits 1."""
    runner = CliRunner()
    meeting_id = 1
    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    _patch_tdoc_repo_for_listing(monkeypatch, list_tdocs=[])
    fake = _FakeCrService(results={})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(meeting_id), "--yes"],
    )
    assert result.exit_code == 1
    assert "No TDoc matched" in result.output
    assert fake.many_calls == []


def test_tdoc_parse_all_already_parsed_exits_0(sqlite_env, monkeypatch) -> None:
    """When every match is already parsed and ``--force`` is *not* set,
    the pending query returns nothing but a raw ``limit=1`` probe
    confirms the filters still match. The CLI prints
    "Nothing to extract", exits 0, and never dispatches
    ``extract_many``."""
    runner = CliRunner()
    meeting_id = 5
    cr_tdocs = [
        TDoc(tdoc_id="R5s260009", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260010", type="CR", meeting_id=meeting_id),
    ]
    parsed = {"R5s260009", "R5s260010"}

    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs, parsed_ids=parsed)
    _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(results={})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(meeting_id), "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "Nothing to extract" in result.output
    assert fake.many_calls == []
    # First call: pending query with exclude_parsed=True.
    # Second call: raw existence probe with exclude_parsed=False, limit=1.
    assert len(repo.list_with_meeting_calls) == 2
    assert repo.list_with_meeting_calls[0]["exclude_parsed"] is True
    assert repo.list_with_meeting_calls[0]["limit"] == 100  # default max_batch
    assert repo.list_with_meeting_calls[1]["exclude_parsed"] is False
    assert repo.list_with_meeting_calls[1]["limit"] == 1


# ---------------------------------------------------------------------------
# tdoc parse — --meeting (LIKE on meeting name)
# ---------------------------------------------------------------------------


def test_tdoc_parse_meeting_like_flows_to_repo(sqlite_env, monkeypatch) -> None:
    """``--meeting PATTERN`` is forwarded as ``meeting_like``."""
    runner = CliRunner()
    cr_tdocs = [
        TDoc(tdoc_id="R5s260001", type="CR", meeting_id=10),
        TDoc(tdoc_id="R5s260002", type="CR", meeting_id=10),
    ]
    repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={
            "R5s260001": _make_result("R5s260001"),
            "R5s260002": _make_result("R5s260002"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        ["tdoc", "parse", "--meeting", "%RAN5%", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert repo.list_with_meeting_calls[0]["meeting_like"] == "%RAN5%"
    assert repo.list_with_meeting_calls[0]["meeting_id"] is None


# ---------------------------------------------------------------------------
# tdoc parse — combined filters
# ---------------------------------------------------------------------------


@pytest.fixture
def _meeting_with_cr_tdocs(sqlite_env, monkeypatch):
    """Seed a meeting with a few CR TDocs and stub the supporting services.

    Returns a small namespace with the meeting id, the fake TDoc repo
    (so the test can assert the filter kwargs reached the repo), the
    fake CR service, and the ``invoke`` callable. The TDoc repo is
    seeded with three rows so a filter that excludes one of them can
    be observed downstream.
    """
    meeting_id = 99
    cr_tdocs = [
        TDoc(
            tdoc_id="R5s260009",
            type="CR",
            meeting_id=meeting_id,
            status="Agreed",
            source="Qualcomm",
        ),
        TDoc(
            tdoc_id="R5s260010",
            type="CR",
            meeting_id=meeting_id,
            status="Noted",
            source="Huawei",
        ),
        TDoc(
            tdoc_id="R5s260011",
            type="CR",
            meeting_id=meeting_id,
            status="Agreed",
            source="Ericsson",
        ),
    ]

    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    tdoc_repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={
            "R5s260009": _make_result("R5s260009"),
            "R5s260010": _make_result("R5s260010"),
            "R5s260011": _make_result("R5s260011"),
        },
    )
    _patch_service(monkeypatch, fake)

    class _Ns:
        pass

    ns = _Ns()
    ns.meeting_id = meeting_id
    ns.tdoc_repo = tdoc_repo
    ns.fake = fake
    ns.runner = CliRunner()
    return ns


def test_tdoc_parse_passes_status_filter(_meeting_with_cr_tdocs) -> None:
    """`--status` flows through to the repo's `list_with_meeting` call."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--status", "Agreed",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(ns.tdoc_repo.list_with_meeting_calls) == 1
    assert ns.tdoc_repo.list_with_meeting_calls[0]["status"] == "Agreed"
    assert ns.tdoc_repo.list_with_meeting_calls[0]["spec"] is None
    assert ns.tdoc_repo.list_with_meeting_calls[0]["uploaded_date"] is None


def test_tdoc_parse_passes_null_status(_meeting_with_cr_tdocs) -> None:
    """`--status null` is forwarded verbatim to the repo (which
    interprets the literal token as a NULL filter)."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--status", "null",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_with_meeting_calls[0]["status"] == "null"


def test_tdoc_parse_passes_date_filter(_meeting_with_cr_tdocs) -> None:
    """`--uploaded-date ">='2026-02-31'"` is forwarded verbatim."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--uploaded-date", ">= '2026-02-31'",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_with_meeting_calls[0]["uploaded_date"] == ">= '2026-02-31'"


def test_tdoc_parse_rejects_bad_date_filter(_meeting_with_cr_tdocs) -> None:
    """An invalid `--uploaded-date` is caught at the CLI boundary with
    a clear BadParameter and never reaches the repo."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--uploaded-date", "yesterday",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid date filter" in result.output
    assert "'yesterday'" in result.output
    assert ns.tdoc_repo.list_with_meeting_calls == []
    assert ns.fake.many_calls == []


def test_tdoc_parse_rejects_bad_date_operator(_meeting_with_cr_tdocs) -> None:
    """An unsupported operator (`==`) is rejected with the same
    BadParameter message before the repo is touched."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--uploaded-date", "== '2026-02-31'",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid date filter" in result.output
    assert ns.tdoc_repo.list_with_meeting_calls == []


@pytest.mark.parametrize(
    ("flag", "kwarg"),
    [
        ("--cr-cat", "cr_cat"),
        ("--spec", "spec"),
        ("--wi", "wi"),
        ("--revision-of", "revision_of"),
        ("--revised-to", "revised_to"),
        ("--title", "title"),
        ("--ftp-url", "ftp_url"),
        ("--source", "source"),
        ("--type", "tdoc_type"),
        ("--release", "release"),
        ("--version", "version"),
        ("--cr-num", "cr_num"),
        ("--cr-pack", "cr_pack"),
    ],
)
def test_tdoc_parse_passes_text_filters(
    _meeting_with_cr_tdocs, flag, kwarg
) -> None:
    """Every text-column filter is forwarded to the repo under its
    expected kwarg name."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(ns.meeting_id), flag, "X%Y", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_with_meeting_calls[0][kwarg] == "X%Y"


@pytest.mark.parametrize(
    ("flag", "kwarg"),
    [
        ("--title", "title"),
        ("--source", "source"),
        ("--cr-cat", "cr_cat"),
        ("--spec", "spec"),
        ("--wi", "wi"),
        ("--revision-of", "revision_of"),
        ("--revised-to", "revised_to"),
        ("--ftp-url", "ftp_url"),
        ("--release", "release"),
        ("--version", "version"),
        ("--cr-num", "cr_num"),
        ("--cr-pack", "cr_pack"),
    ],
)
def test_tdoc_parse_passes_not_like_prefix(
    _meeting_with_cr_tdocs, flag, kwarg
) -> None:
    """`-prefixed values flow through to the repo verbatim. The bang
    is consumed by ``_apply_text_filter`` to emit ``NOT LIKE``; the
    CLI layer must not interpret it."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(ns.meeting_id), flag, "!%X%", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_with_meeting_calls[0][kwarg] == "!%X%"


def test_tdoc_parse_combines_filters(_meeting_with_cr_tdocs) -> None:
    """Passing multiple filters at once forwards each to its own kwarg
    without any cross-contamination."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--status", "Agreed",
            "--spec", "38.%",
            "--uploaded-date", "< '2026-12-31'",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    call = ns.tdoc_repo.list_with_meeting_calls[0]
    assert call["status"] == "Agreed"
    assert call["spec"] == "38.%"
    assert call["uploaded_date"] == "< '2026-12-31'"


def test_tdoc_parse_passes_null_and_not_null_for_new_filters(
    _meeting_with_cr_tdocs,
) -> None:
    """`--release`, `--version`, `--cr-num`, `--cr-pack` all accept
    `null` / `not-null` literal tokens for column nullability,
    consistent with the rest of the text-column filter surface."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--release", "null",
            "--version", "not-null",
            "--cr-num", "null",
            "--cr-pack", "not-null",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    call = ns.tdoc_repo.list_with_meeting_calls[0]
    assert call["release"] == "null"
    assert call["version"] == "not-null"
    assert call["cr_num"] == "null"
    assert call["cr_pack"] == "not-null"


def test_tdoc_parse_tdoc_and_meeting_id_combine(sqlite_env, monkeypatch) -> None:
    """``--tdoc`` and ``--meeting-id`` now combine freely — both flow
    into the same ``list_with_meeting`` call as filters."""
    runner = CliRunner()
    meeting_id = 11
    cr_tdocs = [
        TDoc(tdoc_id="R5s260001", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260002", type="CR", meeting_id=meeting_id),
    ]
    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={
            "R5s260001": _make_result("R5s260001"),
            "R5s260002": _make_result("R5s260002"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--tdoc", "R5s260001",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    call = repo.list_with_meeting_calls[0]
    assert call["meeting_id"] == meeting_id
    assert call["tdoc_id"] == "R5s260001"


# ---------------------------------------------------------------------------
# tdoc parse — confirmation prompt and --yes skip
# ---------------------------------------------------------------------------


def test_tdoc_parse_yes_skips_confirmation_prompt(sqlite_env, monkeypatch) -> None:
    """``--yes`` short-circuits the ``typer.confirm`` call entirely."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    confirm_calls = {"count": 0}

    def fake_confirm(*args, **kwargs):
        confirm_calls["count"] += 1
        return True

    monkeypatch.setattr("doc3gpp.cli.typer.confirm", fake_confirm)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s260009", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert confirm_calls["count"] == 0


def test_tdoc_parse_declined_prompt_exits_0(sqlite_env, monkeypatch) -> None:
    """A declined ``typer.confirm`` aborts before any work and exits 0."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    monkeypatch.setattr("doc3gpp.cli.typer.confirm", lambda *a, **k: False)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260009"])
    assert result.exit_code == 0
    assert "Aborted." in result.output
    # No work was dispatched.
    assert fake.many_calls == []


def test_tdoc_parse_accepted_prompt_dispatches(sqlite_env, monkeypatch) -> None:
    """When ``typer.confirm`` returns True the batch runs normally."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    monkeypatch.setattr("doc3gpp.cli.typer.confirm", lambda *a, **k: True)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260009"])
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260009"], False, False)]


def test_tdoc_parse_yes_short_alias_works(sqlite_env, monkeypatch) -> None:
    """``-y`` is registered as the short alias for ``--yes``."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id="R5s260009", type="CR")]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    confirm_calls = {"count": 0}

    monkeypatch.setattr(
        "doc3gpp.cli.typer.confirm",
        lambda *a, **k: (confirm_calls.update(count=confirm_calls["count"] + 1) or True),
    )

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260009", "-y"])
    assert result.exit_code == 0, result.output
    assert confirm_calls["count"] == 0


# ---------------------------------------------------------------------------
# tdoc parse — two-group rendering + active-column selection
# ---------------------------------------------------------------------------


def test_tdoc_parse_renders_already_parsed_group(sqlite_env, monkeypatch) -> None:
    """Under ``--force`` the preview prints both groups: "To parse" plus
    "Already parsed ... (with --force, these will be re-extracted)"."""
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "false")
    runner = CliRunner()
    meeting_id = 33
    cr_tdocs = [
        TDoc(
            tdoc_id="R5s260009",
            type="CR",
            meeting_id=meeting_id,
            title="Example CR title",
            cr_cat="F",
            status="Agreed",
            spec="38.331",
        ),
        TDoc(
            tdoc_id="R5s260010",
            type="CR",
            meeting_id=meeting_id,
            title="Another CR title",
            cr_cat="F",
            status="Noted",
            spec="38.331",
        ),
    ]
    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, {"R5s260010"})
    fake = _FakeCrService(results={"R5s260009": _make_result("R5s260009")})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--spec", "38.331",
            "--force",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "To parse [count=2]:" in result.output
    assert (
        "Already parsed in tdoc_cr_cover_page "
        "(with --force, these will be re-extracted) [count=1]:"
    ) in result.output
    assert "spec" in result.output
    assert "R5s260009" in result.output
    assert "R5s260010" in result.output
    for header in ("tdoc_id", "title", "type", "cr_cat", "status"):
        assert header in result.output


def test_tdoc_parse_meeting_filter_adds_meeting_name_column(
    sqlite_env, monkeypatch
) -> None:
    """``--meeting`` and ``--meeting-id`` add the ``meeting_name``
    column to the rendered groups."""
    runner = CliRunner()
    meeting_id = 50
    cr_tdocs = [
        TDoc(
            tdoc_id="R5s260009",
            type="CR",
            meeting_id=meeting_id,
            title="Example CR",
            cr_cat="F",
            status="Agreed",
        ),
    ]
    _patch_meeting_service(
        monkeypatch,
        {
            meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            ),
        },
    )
    _patch_tdoc_repo_for_listing(
        monkeypatch, cr_tdocs, meeting_names={meeting_id: "RAN5#111"},
    )
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(results={"R5s260009": _make_result("R5s260009")})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "meeting_name" in result.output
    assert "RAN5#111" in result.output


# ---------------------------------------------------------------------------
# tdoc parse — completion summary counters
# ---------------------------------------------------------------------------


def test_tdoc_parse_summary_counts_split_correctly(sqlite_env, monkeypatch) -> None:
    """``--force`` re-parses already-parsed rows; ``--no-force``
    dispatches only new ones. The completion summary reflects both
    with separate counters."""
    runner = CliRunner()
    meeting_id = 77
    cr_tdocs = [
        TDoc(tdoc_id="R5s260001", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260002", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260003", type="CR", meeting_id=meeting_id),
    ]
    parsed = {"R5s260001"}  # only one pre-parsed
    _patch_meeting_service(
        monkeypatch,
        {meeting_id: Meeting(
            meeting_id=meeting_id,
            name="RAN5#111",
            title="RAN WG5 #111",
            location="Online",
        )},
    )
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(
        results={
            "R5s260001": _make_result("R5s260001"),
            "R5s260002": _make_result("R5s260002"),
            "R5s260003": _make_result("R5s260003"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--force",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    # With --force, all three are dispatched; R5s260001 was already
    # parsed (counted as Re-parsed), the other two are Newly parsed.
    assert fake.many_calls == [(["R5s260001", "R5s260002", "R5s260003"], True, False)]
    assert "Re-parsed (with --force):                  1" in result.output
    assert "Newly parsed:                              2" in result.output
    assert "Skipped (already parsed before this run): 0" in result.output
    assert "Failures:                                  0" in result.output


def test_tdoc_parse_summary_without_force_dispatches_only_new(
    sqlite_env, monkeypatch
) -> None:
    """Without ``--force`` only the pending id reaches ``extract_many``;
    the parsed id is dropped by SQL, so it is not counted as
    ``Skipped``."""
    runner = CliRunner()
    meeting_id = 78
    cr_tdocs = [
        TDoc(tdoc_id="R5s260001", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260002", type="CR", meeting_id=meeting_id),
    ]
    parsed = {"R5s260001"}
    _patch_meeting_service(
        monkeypatch,
        {meeting_id: Meeting(
            meeting_id=meeting_id,
            name="RAN5#111",
            title="RAN WG5 #111",
            location="Online",
        )},
    )
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs, parsed_ids=parsed)
    _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(
        results={"R5s260002": _make_result("R5s260002")},
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260002"], False, False)]
    assert "Skipped (already parsed before this run): 0" in result.output
    assert "Re-parsed (with --force):                  0" in result.output
    assert "Newly parsed:                              1" in result.output


def test_tdoc_parse_db_mode_summary_splits_size_skip_from_ftp_skip(
    sqlite_env, monkeypatch,
) -> None:
    """The DB-mode summary splits ``batch.skipped`` into a size-skip
    bucket and an FTP-skip bucket, distinguished by reason prefix.

    The service stores reasons as ``"TDocTooLargeError: ..."`` /
    ``"TDocNotYetOnFTPError: ..."`` (Task 5). The CLI must count
    each bucket separately so an operator can see whether the cap or
    a missing upload is responsible for the gap.
    """
    runner = CliRunner()
    meeting_id = 79
    cr_tdocs = [
        TDoc(tdoc_id="R5s260001", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260002", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260003", type="CR", meeting_id=meeting_id),
    ]
    _patch_meeting_service(
        monkeypatch,
        {meeting_id: Meeting(
            meeting_id=meeting_id,
            name="RAN5#111",
            title="RAN WG5 #111",
            location="Online",
        )},
    )
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs, parsed_ids=set())
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={},
        skipped={
            "R5s260001": "TDocTooLargeError: 5 MiB exceeds 1024 bytes",
            "R5s260002": "TDocNotYetOnFTPError: not on FTP",
            "R5s260003": "TDocTooLargeError: 9 MiB exceeds 1024 bytes",
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(meeting_id),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Skipped (exceeds max_tdoc_size_kb" in result.output
    assert "Skipped (not yet on FTP):" in result.output
    # Split counts: 2 size-skip, 1 FTP-skip.
    size_line = next(
        (ln for ln in result.output.splitlines()
         if ln.startswith("Skipped (exceeds max_tdoc_size_kb")),
    )
    assert size_line.endswith(" 2"), size_line
    assert "Skipped (not yet on FTP):                 1" in result.output


# ---------------------------------------------------------------------------
# tdoc parse — batch limit warning
# ---------------------------------------------------------------------------


def test_tdoc_parse_max_batch_default_is_100(sqlite_env) -> None:
    """The configured default is 100 — sanity check the schema wiring."""
    from doc3gpp.config import get_settings

    settings = get_settings()
    assert settings.tdoc_parse.max_batch == 100


def test_tdoc_parse_max_batch_toml_override(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """``tdoc_parse.max_batch`` is TOML-only since
    ``DOC3GPP_TDOC_PARSE__MAX_BATCH`` is outside the env-var allowlist.
    Pin a TOML config to override the default."""
    _pin_max_batch_via_toml(monkeypatch, tmp_path, max_batch=5)
    try:
        assert get_settings().tdoc_parse.max_batch == 5
    finally:
        get_settings.cache_clear()


def test_tdoc_parse_max_batch_env_var_is_ignored(
    sqlite_env, monkeypatch,
) -> None:
    """``DOC3GPP_TDOC_PARSE__MAX_BATCH`` is outside the env-var allowlist
    so setting it must have no effect — the default wins."""
    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_BATCH", "5")
    get_settings.cache_clear()
    try:
        assert get_settings().tdoc_parse.max_batch == 100
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# tdoc_parse.max_tdoc_size_kb — settings surface
# ---------------------------------------------------------------------------


def _pin_max_tdoc_size_kb_via_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    max_tdoc_size_kb: int,
) -> None:
    """Set ``tdoc_parse.max_tdoc_size_kb`` through a TOML config file.

    Mirrors :func:`_pin_max_batch_via_toml`; ``tdoc_parse.*`` knobs
    are TOML-only (the corresponding ``DOC3GPP_TDOC_PARSE__*`` env
    vars are outside the
    :data:`doc3gpp.settings.schema.ALLOWED_ENV_VARS` allowlist).
    """
    config_path = tmp_path / "tdoc-parse-size-config.toml"
    config_path.write_text(
        f"[tdoc_parse]\nmax_tdoc_size_kb = {max_tdoc_size_kb}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOC3GPP_CONFIG", str(config_path))
    get_settings.cache_clear()


def test_tdoc_parse_max_tdoc_size_kb_default_is_1000(sqlite_env) -> None:
    """``tdoc_parse.max_tdoc_size_kb`` defaults to 1000 KB (≈ 1 MiB)."""
    from doc3gpp.config import get_settings

    assert get_settings().tdoc_parse.max_tdoc_size_kb == 1000


def test_tdoc_parse_max_tdoc_size_kb_toml_override(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """TOML config can override ``tdoc_parse.max_tdoc_size_kb``."""
    _pin_max_tdoc_size_kb_via_toml(
        monkeypatch, tmp_path, max_tdoc_size_kb=500,
    )
    try:
        assert get_settings().tdoc_parse.max_tdoc_size_kb == 500
    finally:
        get_settings.cache_clear()


def test_tdoc_parse_max_tdoc_size_kb_zero_disables(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """``0`` is a valid value that disables the limit (per the field's ``ge=0``)."""
    _pin_max_tdoc_size_kb_via_toml(
        monkeypatch, tmp_path, max_tdoc_size_kb=0,
    )
    try:
        assert get_settings().tdoc_parse.max_tdoc_size_kb == 0
    finally:
        get_settings.cache_clear()


def test_tdoc_parse_batch_limit_warning_when_under_max(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """When the *actual work* count exceeds max_batch but the repo
    already capped the result, the warning is suppressed — the operator
    can only see what was returned. (We simulate this by returning 5
    rows with max_batch=5 so the warning never fires.)"""
    _pin_max_batch_via_toml(monkeypatch, tmp_path, max_batch=5)
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id=f"R5s26000{i}", type="CR") for i in range(1, 6)]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={tdoc.tdoc_id: _make_result(tdoc.tdoc_id) for tdoc in cr_tdocs},
    )
    _patch_service(monkeypatch, fake)

    try:
        result = runner.invoke(
            app, ["tdoc", "parse", "--tdoc", "R5s26%", "--yes"],
        )
        assert result.exit_code == 0, result.output
        # No warning text — the repo returned <= max_batch.
        assert "exceeds max_batch" not in result.output
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# tdoc parse — max_batch applied after excluding already-parsed rows
# ---------------------------------------------------------------------------


def test_tdoc_parse_max_batch_applies_after_excluding_parsed(
    sqlite_env, monkeypatch, tmp_path,
) -> None:
    """``max_batch`` must cap *pending* matches, not raw matches.

    Given 3 already-parsed + 5 pending CR TDocs (DESC order) and
    ``max_batch=3``, the CLI must dispatch exactly 3 pending ids to
    ``extract_many`` and the "To parse" preview must list only
    pending ids.

    Current code omits the ``exclude_parsed`` kwarg, so the
    repository returns the first 3 raw matches (all already-parsed)
    and the Python-level filter strips them — leaving 0 ids
    dispatched. Fixed code will pass ``exclude_parsed=True``, so
    the repository filters parsed rows before applying the limit
    and returns the first 3 pending ids.
    """
    _pin_max_batch_via_toml(monkeypatch, tmp_path, max_batch=3)
    from doc3gpp.config import get_settings
    get_settings.cache_clear()
    try:
        meeting_id = 21
        parsed_ids = {"R5s260008", "R5s260007", "R5s260006"}
        universe = [
            TDoc(tdoc_id=f"R5s26000{i}", type="CR", meeting_id=meeting_id)
            for i in (8, 7, 6, 5, 4, 3, 2, 1)
        ]
        _patch_meeting_service(
            monkeypatch,
            {meeting_id: Meeting(
                meeting_id=meeting_id,
                name="RAN5#111",
                title="RAN WG5 #111",
                location="Online",
            )},
        )
        _patch_tdoc_repo_for_listing(
            monkeypatch, universe, parsed_ids=parsed_ids,
        )
        _patch_cr_repo(monkeypatch, parsed_ids)
        fake = _FakeCrService(
            results={
                "R5s260005": _make_result("R5s260005"),
                "R5s260004": _make_result("R5s260004"),
                "R5s260003": _make_result("R5s260003"),
            },
        )
        _patch_service(monkeypatch, fake)

        result = CliRunner().invoke(
            app,
            ["tdoc", "parse", "--meeting-id", str(meeting_id), "--yes"],
        )

        # Given: 3 already-parsed + 5 pending, max_batch=3.
        # When: the operator runs `tdoc parse` without --force.
        # Then: extract_many receives exactly max_batch pending ids
        # and the "To parse" preview contains only pending ids.
        assert result.exit_code == 0, result.output
        assert fake.many_calls == [
            (["R5s260005", "R5s260004", "R5s260003"], False, False),
        ]
        to_parse_section = result.output.split("To parse [count=3]:", 1)[1]
        for parsed_id in parsed_ids:
            assert parsed_id not in to_parse_section, (
                f"Parsed id {parsed_id!r} appeared in the 'To parse' preview"
            )
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# tdoc show (unchanged)
# ---------------------------------------------------------------------------


def _seed_full_crdetail_row(tdoc_id: str, url: str | None = None) -> None:
    """Insert a parent TDoc + a populated CR detail row + extract metadata.

    Uses the SQL repositories directly so the test exercises the same
    write path the production CLI relies on. The CR detail row carries
    enough fields to verify the ``[Extracted Details]`` block output.
    The URL defaults to a unique-per-call value so multiple seeds in
    the same test produce distinct (URL-keyed) detail rows.

    The parent TDoc's ``ftp_url`` is also populated — the new
    URL-keyed ``tdoc show`` lookup requires the TDoc row to carry
    the same ``ftp_url`` the cover row was persisted under.
    """
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    resolved_url = url or f"stored/{tdoc_id}.zip"
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=resolved_url))
    details = TDocCRDetails(
        tdoc_id=tdoc_id,
        spec="38.523-3",
        cr_num="3790",
        rev="0",
        version="18.4.0",
        title="Example CR for tests",
        source="Qualcomm",
        tsg="R5",
        related_wis="NR_ext",
        date=date(2026, 6, 12),
        cr_cat="F",
        release="Rel-18",
        reason_for_change="Some long reason " * 20,
        consequences_if_not_approved="Consequence text " * 15,
        clauses_affected="5.3.4.2",
        ftp_url=resolved_url,
    )
    meta = TDocExtractMeta(
        ftp_url=resolved_url,
        tdoc_id=tdoc_id,
        cache_file="R5s260009.zip",
        doc_filename="R5s260009.docx",
    )
    cr_repo.upsert(details)
    cr_repo.upsert_extract_meta(meta)


def test_tdoc_show_happy_path(sqlite_env, monkeypatch) -> None:
    """A seeded TDoc + CR detail row renders both the ``[TDoc]`` and
    ``[Extracted Details]`` blocks."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260009"])
    assert result.exit_code == 0, result.output
    assert "[TDoc]" in result.output
    assert "tdoc_id: R5s260009" in result.output
    assert "[Extracted Details]" in result.output
    assert "spec: 38.523-3" in result.output
    assert "cr_num: 3790" in result.output
    assert "title: Example CR for tests" in result.output
    assert "reason_for_change:" in result.output
    assert "..." in result.output  # truncation ellipsis
    assert "extracted_at:" in result.output


def test_tdoc_show_no_extract_row(sqlite_env) -> None:
    """A TDoc without a matching ``tdoc_cr_cover_page`` row prints a
    friendly hint rather than the full block."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260010", type="CR"))

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260010"])
    assert result.exit_code == 0, result.output
    assert "[TDoc]" in result.output
    assert "tdoc_id: R5s260010" in result.output
    assert "No extracted details" in result.output
    assert "doc3gpp tdoc parse" in result.output
    assert "extracted_at: -" in result.output


def test_tdoc_show_unknown_tdoc_raises_bad_parameter(sqlite_env) -> None:
    """An unknown TDoc id exits non-zero with a friendly message."""
    create_schema()
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "bogus"])
    assert result.exit_code != 0
    assert "Unknown TDoc 'bogus'" in result.output


def test_tdoc_show_no_ftp_url_skips_cover_and_ttcn(sqlite_env) -> None:
    """A TDoc row without an ``ftp_url`` never touches the CR repos.

    The CR detail / TTCN sidecar lookups are URL-keyed, so a TDoc
    with no stored URL renders just the ``[TDoc]`` block plus the
    ``extracted_at: -`` placeholder — no extracted detail / TTCN
    section is emitted even when rows exist at other URLs.
    """
    create_schema()
    # Seed two stored detail rows so the DB has matches at the same
    # tdoc_id — the lookup still must not find them because there is
    # no URL to probe with.
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260011", type="CR", ftp_url=None),
    )
    cr_repo = SQLAlchemyTDocCrRepository()
    cr_repo.upsert(TDocCRDetails(
        tdoc_id="R5s260011",
        spec="38.523-3",
        cr_num="3790",
        ftp_url="stored/R5s260011_someremote.zip",
    ))

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260011"])
    assert result.exit_code == 0, result.output
    assert "[TDoc]" in result.output
    assert "No extracted details" in result.output
    assert "extracted_at: -" in result.output
    assert "[Extracted Details]" not in result.output
    assert "cr_num: 3790" not in result.output


# ---------------------------------------------------------------------------
# tdoc show - auxiliary files (tdoc_files)
# ---------------------------------------------------------------------------


def _seed_aux_files(
    tdoc_id: str,
    files: list[TDocFile],
    ftp_url: str | None = None,
) -> None:
    """Seed a parent TDoc (and optional CR row) plus auxiliary files.

    The parent row is what ``tdoc show`` keys off; the file rows live on
    ``tdoc_files`` and are matched by ``tdoc_id``. ``ftp_url`` on the
    parent row is populated so the URL-keyed cover lookup is exercised
    end-to-end when a CR row is supplied.
    """
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=ftp_url))
    SQLAlchemyTDocFileRepository().upsert_many(files)


def test_tdoc_show_table_includes_auxiliary_files_block(sqlite_env) -> None:
    """Two ``tdoc_files`` rows render an ``[Auxiliary Files]`` block
    with the four informative fields per file.

    The autoincrement ``id`` and the ``tdoc_id`` match key are dropped
    per D8 — they are noise in the show output and the parent ``[TDoc]``
    block already carries the match key.
    """
    create_schema()
    _seed_aux_files(
        "R5s260020",
        [
            TDocFile(
                tdoc_id="R5s260020",
                type="revision",
                file="R5s260020r1.zip",
                ftp_url="tsg_ran/WG5/TSGR5_128/Inbox/R5s260020r1.zip",
                uploaded_date=date(2026, 7, 4),
            ),
            TDocFile(
                tdoc_id="R5s260020",
                type="review",
                file="R5s260020_MCC160Comments.zip",
                ftp_url="tsg_ran/WG5/TSGR5_128/Review/R5s260020_MCC160Comments.zip",
                uploaded_date=date(2026, 7, 3),
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260020"])
    assert result.exit_code == 0, result.output
    assert "[Auxiliary Files]" in result.output
    assert "type: revision" in result.output
    assert "file: R5s260020r1.zip" in result.output
    assert "ftp_url: tsg_ran/WG5/TSGR5_128/Inbox/R5s260020r1.zip" in result.output
    assert "uploaded_date: 2026-07-04" in result.output
    assert "type: review" in result.output
    assert "file: R5s260020_MCC160Comments.zip" in result.output
    assert "id:" not in result.output.split("[Auxiliary Files]")[1]
    assert "tdoc_id:" not in result.output.split("[Auxiliary Files]")[1]


def test_tdoc_show_table_omits_auxiliary_files_header_when_empty(
    sqlite_env,
) -> None:
    """A TDoc with no ``tdoc_files`` rows emits no ``[Auxiliary Files]``
    header but the placeholder line is still rendered so the reader
    knows where the file table would appear once synced."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260021", type="CR"))

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260021"])
    assert result.exit_code == 0, result.output
    assert "[Auxiliary Files]" not in result.output
    assert "No auxiliary files" in result.output
    assert "doc3gpp tdoc sync" in result.output


def test_tdoc_show_json_payload_includes_files_array(sqlite_env) -> None:
    """The JSON payload gains a ``files`` array with one entry per
    auxiliary file, every dataclass field of ``TDocFile`` preserved."""
    create_schema()
    _seed_aux_files(
        "R5s260022",
        [
            TDocFile(
                tdoc_id="R5s260022",
                type="revision",
                file="R5s260022r1.zip",
                ftp_url="x/R5s260022r1.zip",
                uploaded_date=date(2026, 7, 4),
            ),
            TDocFile(
                tdoc_id="R5s260022",
                type="review",
                file="R5s260022_MCC.zip",
                ftp_url="x/R5s260022_MCC.zip",
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260022", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "files" in payload
    assert isinstance(payload["files"], list)
    assert len(payload["files"]) == 2
    assert payload["files"][0]["type"] == "review"
    assert payload["files"][0]["file"] == "R5s260022_MCC.zip"
    assert payload["files"][0]["tdoc_id"] == "R5s260022"
    assert payload["files"][0]["uploaded_date"] is None
    assert payload["files"][1]["type"] == "revision"
    assert payload["files"][1]["file"] == "R5s260022r1.zip"
    assert payload["files"][1]["uploaded_date"] == "2026-07-04"


def test_tdoc_show_json_files_key_omitted_when_empty(sqlite_env) -> None:
    """When no auxiliary files exist, the JSON top-level ``files`` key
    is **omitted** (matches the existing optional-key convention)."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260023", type="CR"))

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260023", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "files" not in payload


def test_tdoc_show_markdown_includes_auxiliary_files_section(
    sqlite_env,
) -> None:
    """The markdown output gains a ``## Auxiliary Files`` section with
    one nested bullet group per file (type / file / ftp_url /
    uploaded_date)."""
    create_schema()
    _seed_aux_files(
        "R5s260024",
        [
            TDocFile(
                tdoc_id="R5s260024",
                type="revision",
                file="R5s260024r1.zip",
                ftp_url="x/R5s260024r1.zip",
                uploaded_date=date(2026, 7, 4),
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260024", "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert "## Auxiliary Files" in result.output
    assert "- **type**: revision" in result.output
    assert "- **file**: R5s260024r1.zip" in result.output
    assert "- **ftp_url**: x/R5s260024r1.zip" in result.output
    assert "- **uploaded_date**: 2026-07-04" in result.output


def test_tdoc_show_markdown_files_placeholder_when_empty(sqlite_env) -> None:
    """Empty case: the section is still emitted (skeleton stability)
    but with a placeholder pointing the reader at ``tdoc sync``."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260025", type="CR"))

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260025", "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert "## Auxiliary Files" in result.output
    assert "_No auxiliary files" in result.output
    assert "doc3gpp tdoc sync" in result.output


# ---------------------------------------------------------------------------
# Case-insensitive --tdoc normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_input", "expected_canonical"),
    [
        # Lowercase / uppercase suffix -> canonical R5s###### form.
        ("r5s260213", "R5s260213"),
        ("R5S260213", "R5s260213"),
        ("r5w260213", "R5w260213"),
        ("r5-227476", "R5-227476"),
        ("c6-250028", "C6-250028"),
        # Already-canonical input is a no-op.
        ("R5s260009", "R5s260009"),
        # Surrounding whitespace is stripped before canonicalisation.
        ("  r5s260213  ", "R5s260213"),
    ],
)
def test_tdoc_parse_canonicalises_input(
    sqlite_env, monkeypatch, raw_input: str, expected_canonical: str
) -> None:
    """``--tdoc r5s260213`` flows into the repo as ``R5s260213`` (the
    LIKE pattern) so the DB lookup against the canonical PK matches."""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id=expected_canonical, type="CR")]
    repo = _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={expected_canonical: _make_result(expected_canonical)},
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", raw_input, "--yes"])
    assert result.exit_code == 0, result.output
    assert repo.list_with_meeting_calls[0]["tdoc_id"] == expected_canonical


def test_tdoc_show_lowercase_input_resolves_canonical_row(
    sqlite_env, monkeypatch
) -> None:
    """``tdoc show --tdoc r5s260213`` finds the canonical-form row stored
    in the ``tdocs`` table (R5s260213); the output is unchanged."""
    create_schema()
    _seed_full_crdetail_row("R5s260213")

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "r5s260213"])
    assert result.exit_code == 0, result.output
    assert "tdoc_id: R5s260213" in result.output
    assert "[Extracted Details]" in result.output


def test_tdoc_show_uppercase_suffix_input_resolves_canonical_row(
    sqlite_env,
) -> None:
    """All-uppercase suffix variant ``R5S260213`` resolves to the
    canonical row ``R5s260213`` in the DB."""
    create_schema()
    _seed_full_crdetail_row("R5s260213")

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5S260213"])
    assert result.exit_code == 0, result.output
    assert "tdoc_id: R5s260213" in result.output


def test_tdoc_show_missing_tdoc_option_is_required(sqlite_env) -> None:
    """Typer's own validation rejects a missing ``--tdoc`` value."""
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show"])
    assert result.exit_code != 0


def test_tdoc_show_renders_distinct_revisions(sqlite_env) -> None:
    """Two distinct URLs for the same TDoc id show up at the URL-keyed output.

    Under the slim URL-keyed lookup, ``tdoc show --tdoc R5s260009``
    (which has ``ftp_url = stored/R5s260009.zip``) renders only that
    specific revision's cover row. The second URL (``rev2``) is
    persisted but not displayed unless the operator points
    ``--tdoc`` at it specifically.
    """
    create_schema()
    # Seed two cover rows at distinct URLs, then pin the parent TDoc's
    # ``ftp_url`` to the first URL so the URL-keyed lookup selects it.
    _seed_full_crdetail_row("R5s260009")
    _seed_full_crdetail_row(
        "R5s260009",
        url="stored/R5s260009_rev2.zip",
    )
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url="stored/R5s260009.zip"),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260009"])
    assert result.exit_code == 0, result.output
    assert "R5s260009.zip" in result.output
    assert "R5s260009_rev2.zip" not in result.output
    assert result.output.count("[Extracted Details]") == 1


# ---------------------------------------------------------------------------
# tdoc show --format / --output
# ---------------------------------------------------------------------------


def test_tdoc_show_format_json_happy_path(sqlite_env) -> None:
    """``--format json`` emits one JSON object with ``tdoc`` + ``cover`` + ``extracted_at``."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tdoc"]["tdoc_id"] == "R5s260009"
    assert payload["tdoc"]["type"] == "CR"
    assert payload["tdoc"]["reservation_date"] in (None, "-") or isinstance(
        payload["tdoc"]["reservation_date"], str
    )
    assert payload["cover"]["tdoc_id"] == "R5s260009"
    assert payload["cover"]["spec"] == "38.523-3"
    assert payload["cover"]["cr_num"] == "3790"
    assert payload["cover"]["date"] == "2026-06-12"
    assert "details" not in payload["cover"]
    assert "parser_version" not in payload["cover"]
    assert "extracted_at" in payload
    assert isinstance(payload["extracted_at"], str)


def test_tdoc_show_format_json_omits_ttcn_for_non_ttcn_tdoc(sqlite_env) -> None:
    """A non-TTCN TDoc (e.g. ``R5-260020``) never emits a ``ttcn`` block even when rows exist for the same id."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260020", type="LS", ftp_url="stored/R5-260020.zip"),
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5-260020", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tdoc"]["tdoc_id"] == "R5-260020"
    assert "cover" not in payload
    assert "ttcn" not in payload


def test_tdoc_show_format_json_includes_ttcn_block(sqlite_env) -> None:
    """A TTCN TDoc (e.g. ``R5s260009``) emits a ``ttcn`` block when a sidecar row exists."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url="stored/R5s260009.zip"),
    )
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )

    SQLAlchemyTDocCrTtcnRepository().upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260009",
            ftp_url="stored/R5s260009.zip",
            testcase="7.1.3.5.3",
            ue="UE1",
            ss="SS_NR5G",
            ats_version="iwd-TTCN3-B2512-260-eng",
            ttcn_release="B2512",
            test_suite="NR5GC",
            required_changes=[{"function_name": "fl_TC_7_1_3_5_3_Body"}],
        ),
    )
    # Seed the extract metadata so the CLI can surface ``extracted_at``
    # in the JSON payload (sourced from the ``tdoc_extracts`` row).
    SQLAlchemyTDocCrRepository().upsert_extract_meta(
        TDocExtractMeta(
            ftp_url="stored/R5s260009.zip",
            tdoc_id="R5s260009",
            cache_file="R5s260009.zip",
            doc_filename="R5s260009.docx",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "ttcn" in payload
    assert payload["ttcn"]["tdoc_id"] == "R5s260009"
    assert payload["ttcn"]["testcase"] == "7.1.3.5.3"
    assert payload["ttcn"]["ats_version"] == "iwd-TTCN3-B2512-260-eng"
    assert payload["ttcn"]["required_changes"] == [
        {"function_name": "fl_TC_7_1_3_5_3_Body"},
    ]
    assert "extracted_at" in payload


def test_tdoc_show_format_json_skips_ttcn_for_non_ttcn_id(sqlite_env) -> None:
    """The TTCN gate is structural: ``R5-260020`` never reads the TTCN sidecar even when rows exist."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260020", type="CR", ftp_url="stored/R5-260020.zip"),
    )
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )

    # Persist a sidecar row at the same URL — it must not surface in
    # the CLI output because ``is_ttcn_tdoc(R5-260020)`` is False.
    SQLAlchemyTDocCrTtcnRepository().upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5-260020",
            ftp_url="stored/R5-260020.zip",
            testcase="placeholder",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5-260020", "--format", "json"]
    )
    payload = json.loads(result.output)
    assert "ttcn" not in payload


def test_tdoc_show_format_markdown_happy_path(sqlite_env) -> None:
    """``--format markdown`` emits a Markdown document with a cover section."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert "# TDoc `R5s260009`" in result.output
    assert "## Metadata" in result.output
    assert "## Extracted Cover Details" in result.output
    assert "38.523-3" in result.output
    assert "3790" in result.output
    assert "## TTCN Details" not in result.output


def test_tdoc_show_format_markdown_includes_ttcn_section_for_ttcn_tdoc(sqlite_env) -> None:
    """A TTCN TDoc with a sidecar row gets a ``## TTCN Details`` markdown section."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260009", type="CR", ftp_url="stored/R5s260009.zip"),
    )
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )

    SQLAlchemyTDocCrTtcnRepository().upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260009",
            ftp_url="stored/R5s260009.zip",
            testcase="7.1.3.5.3",
            ue="UE1",
            required_changes=[{"function_name": "fl_TC_7_1_3_5_3_Body"}],
        ),
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert "## TTCN Details" in result.output
    assert "**testcase**: 7.1.3.5.3" in result.output
    assert "```json" in result.output
    assert "fl_TC_7_1_3_5_3_Body" in result.output


def test_tdoc_show_format_markdown_no_extract_row(sqlite_env) -> None:
    """``--format markdown`` on a TDoc without extracted details emits a friendly hint."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260010", type="CR"))

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260010", "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert "# TDoc `R5s260010`" in result.output
    assert "No extracted details" in result.output


def test_tdoc_show_output_writes_to_file(sqlite_env, tmp_path) -> None:
    """``--output PATH`` redirects the default table output to a file."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    out_path = tmp_path / "show.txt"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "show",
            "--tdoc", "R5s260009",
            "--output", str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()
    contents = out_path.read_text()
    assert "[TDoc]" in contents
    assert "tdoc_id: R5s260009" in contents
    assert "[Extracted Details]" in contents


def test_tdoc_show_format_json_output_writes_to_file(sqlite_env, tmp_path) -> None:
    """``--format json --output PATH`` round-trips a parseable JSON file."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    out_path = tmp_path / "show.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "show",
            "--tdoc", "R5s260009",
            "--format", "json",
            "--output", str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(out_path.read_text())
    assert payload["tdoc"]["tdoc_id"] == "R5s260009"
    assert payload["cover"]["cr_num"] == "3790"
    assert "extracted_at" in payload


def test_tdoc_show_format_raw_emits_cached_markdown(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """``--format raw`` writes the converted markdown from the cache."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    cached_md = "# Heading\n\nbody paragraph\n"
    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path",
        lambda cache_file, cache_root: cached_md,
    )
    _patch_service(monkeypatch, _FakeCrService(results={
        "R5s260009": _make_result("R5s260009"),
    }))

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "raw"]
    )
    assert result.exit_code == 0, result.output
    assert result.output == cached_md


def test_tdoc_show_format_raw_output_writes_to_file(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """``--format raw --output PATH`` writes the converted markdown to a file."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    cached_md = "raw body\n"
    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path",
        lambda cache_file, cache_root: cached_md,
    )
    _patch_service(monkeypatch, _FakeCrService(results={
        "R5s260009": _make_result("R5s260009"),
    }))

    out_path = tmp_path / "raw.md"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "show",
            "--tdoc", "R5s260009",
            "--format", "raw",
            "--output", str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out_path.read_text() == cached_md


def test_tdoc_show_format_raw_non_cr_tdoc_raises_bad_parameter(
    sqlite_env, monkeypatch
) -> None:
    """``--format raw`` on a non-CR TDoc surfaces a friendly error."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5-260020", type="LS"))

    class _RaisingCrService:
        def extract(self, tdoc_id):
            raise TDocTypeUnsupportedError(tdoc_id, observed_type="LS")

    _patch_service(monkeypatch, _RaisingCrService())

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5-260020", "--format", "raw"]
    )
    assert result.exit_code != 0
    assert "R5-260020" in result.output
    assert "type" in result.output.lower()
    assert "CR-type" in result.output or "CR" in result.output


def test_tdoc_show_format_raw_python_docx_missing_raises_bad_parameter(
    sqlite_env, monkeypatch
) -> None:
    """A missing ``[extract]`` extra surfaces a friendly install hint."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    class _RaisingCrService:
        def extract(self, tdoc_id):
            raise PythonDocxNotInstalledError()

    _patch_service(monkeypatch, _RaisingCrService())

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "raw"]
    )
    assert result.exit_code != 0
    assert "python-docx" in result.output
    assert "doc3gpp[extract]" in result.output


def test_tdoc_show_format_raw_zip_download_failure_raises_bad_parameter(
    sqlite_env, monkeypatch
) -> None:
    """A TDocZipDownloadError becomes a friendly BadParameter on raw."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    class _RaisingCrService:
        def extract(self, tdoc_id):
            raise TDocZipDownloadError(
                url="https://example/missing.zip",
                original=RuntimeError("boom"),
            )

    _patch_service(monkeypatch, _RaisingCrService())

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "raw"]
    )
    assert result.exit_code != 0
    assert "Failed to download" in result.output


def test_tdoc_show_format_raw_empty_cache_raises_bad_parameter(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """A non-empty extract_meta path that reads to empty raises BadParameter."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path",
        lambda cache_file, cache_root: "",
    )
    _patch_service(monkeypatch, _FakeCrService(results={
        "R5s260009": _make_result("R5s260009"),
    }))

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "raw"]
    )
    assert result.exit_code != 0
    assert "empty or unreadable" in result.output


def test_tdoc_show_format_invalid_raises_bad_parameter(sqlite_env) -> None:
    """An unknown --format value exits non-zero with a helpful message."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(TDoc(tdoc_id="R5s260009", type="CR"))

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "show", "--tdoc", "R5s260009", "--format", "yaml"],
    )
    assert result.exit_code != 0
    assert "yaml" in result.output
    assert "table" in result.output and "json" in result.output
    assert "raw" in result.output


# ---------------------------------------------------------------------------
# tdoc show --ftp-url — URL-keyed read across tdocs / tdoc_cr_cover_page /
# tdoc_cr_ttcn_details / tdoc_files
# ---------------------------------------------------------------------------


_FTP_URL = "stored/R5s260100.zip"
_FTP_URL_ALT_FORM = "https://www.3gpp.org/ftp/stored/R5s260100.zip"


def _seed_ftp_url_target(
    tdoc_id: str,
    url: str,
    *,
    with_cover: bool = True,
    with_ttcn: bool = False,
    files: list[TDocFile] | None = None,
) -> None:
    """Seed a parent TDoc row + cover/TTCN rows + optional aux files
    all under the same ``ftp_url``.

    The TDoc row carries ``ftp_url`` because the URL-keyed lookup
    reads via :meth:`SQLAlchemyTDocRepository.get_by_ftp_url`; the
    cover/TTCN repos use URL as PK; aux files are matched by
    ``TDocFileORM.ftp_url``.
    """
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR", ftp_url=url))
    if with_cover:
        cr_repo.upsert(
            TDocCRDetails(
                tdoc_id=tdoc_id,
                spec="38.523-3",
                cr_num="3790",
                ftp_url=url,
            )
        )
        cr_repo.upsert_extract_meta(
            TDocExtractMeta(
                ftp_url=url,
                tdoc_id=tdoc_id,
                cache_file=f"{tdoc_id}-deadbeef.zip",
                doc_filename=f"{tdoc_id}.docx",
            )
        )
    if with_ttcn:
        from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
            SQLAlchemyTDocCrTtcnRepository,
        )
        SQLAlchemyTDocCrTtcnRepository().upsert(
            TDocCRTTCNDetails(
                tdoc_id=tdoc_id,
                ftp_url=url,
                testcase="7.1.3.5.3",
            )
        )
    if files:
        SQLAlchemyTDocFileRepository().upsert_many(files)


def test_tdoc_show_ftp_url_flag_mutex_neither_provided_raises(
    sqlite_env,
) -> None:
    """Calling ``tdoc show`` with neither selector raises BadParameter."""
    create_schema()
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show"])
    assert result.exit_code != 0
    assert "exactly one" in result.output
    assert "--tdoc" in result.output
    assert "--ftp-url" in result.output


def test_tdoc_show_ftp_url_flag_mutex_both_provided_raises(
    sqlite_env,
) -> None:
    """Calling ``tdoc show`` with both selectors raises BadParameter."""
    create_schema()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "show", "--tdoc", "R5s260009", "--ftp-url", _FTP_URL],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_tdoc_show_ftp_url_normalises_full_url_to_bare_path(
    sqlite_env,
) -> None:
    """A full ``https://www.3gpp.org/ftp/...`` URL is normalised to the
    canonical bare-path form the DB stores, so either spelling matches
    the same row."""
    create_schema()
    _seed_ftp_url_target("R5s260100", _FTP_URL)

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", _FTP_URL_ALT_FORM]
    )
    assert result.exit_code == 0, result.output
    assert "[FTP URL]" in result.output
    assert f"ftp_url: {_FTP_URL}" in result.output
    assert "[TDoc]" in result.output
    assert "tdoc_id: R5s260100" in result.output


def test_tdoc_show_ftp_url_empty_raises_bad_parameter(sqlite_env) -> None:
    """An empty URL value is rejected at the boundary with a friendly message."""
    create_schema()
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--ftp-url", ""])
    assert result.exit_code != 0
    assert "Empty FTP URL" in result.output or "empty" in result.output.lower()


def test_tdoc_show_ftp_url_unknown_raises_bad_parameter(sqlite_env) -> None:
    """A URL that matches no row in any of the four tables exits non-zero."""
    create_schema()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "show", "--ftp-url", "stored/does-not-exist.zip"],
    )
    assert result.exit_code != 0
    assert "No row" in result.output or "matches" in result.output.lower()


def test_tdoc_show_ftp_url_table_happy_path(sqlite_env) -> None:
    """A URL that hits every table emits ``[FTP URL]``, ``[TDoc]``,
    ``[Extracted Details]``, and ``[Auxiliary Files]`` blocks."""
    create_schema()
    _seed_ftp_url_target(
        "R5s260101",
        _FTP_URL,
        files=[
            TDocFile(
                tdoc_id="R5s260101",
                type="revision",
                file="R5s260101r1.zip",
                ftp_url=_FTP_URL,
                uploaded_date=date(2026, 7, 4),
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--ftp-url", _FTP_URL])
    assert result.exit_code == 0, result.output
    assert "[FTP URL]" in result.output
    assert f"ftp_url: {_FTP_URL}" in result.output
    assert "[TDoc]" in result.output
    assert "tdoc_id: R5s260101" in result.output
    assert "[Extracted Details]" in result.output
    assert "spec: 38.523-3" in result.output
    assert "cr_num: 3790" in result.output
    assert "extracted_at:" in result.output
    assert "[Auxiliary Files]" in result.output
    assert "type: revision" in result.output
    assert "file: R5s260101r1.zip" in result.output


def test_tdoc_show_ftp_url_table_omits_cover_when_null(sqlite_env) -> None:
    """No ``tdoc_cr_cover_page`` row at the URL → the ``[Extracted Details]``
    block is absent (omitted, not null) per the optional-key convention."""
    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260102", type="CR", ftp_url=_FTP_URL)
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--ftp-url", _FTP_URL])
    assert result.exit_code == 0, result.output
    assert "[TDoc]" in result.output
    assert "[Extracted Details]" not in result.output
    assert "extracted_at: -" in result.output


def test_tdoc_show_ftp_url_json_payload_shape(sqlite_env) -> None:
    """The JSON payload always carries ``ftp_url``; ``tdoc`` is present
    when a TDoc row matches; optional keys are omitted when null."""
    create_schema()
    _seed_ftp_url_target("R5s260103", _FTP_URL)

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ftp_url"] == _FTP_URL
    assert payload["tdoc"]["tdoc_id"] == "R5s260103"
    assert payload["cover"]["spec"] == "38.523-3"
    assert "extracted_at" in payload
    assert "ttcn" not in payload
    assert "files" not in payload


def test_tdoc_show_ftp_url_json_payload_includes_ttcn_and_files(
    sqlite_env,
) -> None:
    """The TTCN block and ``files`` array both surface when seeded."""
    create_schema()
    _seed_ftp_url_target(
        "R5s260104",
        _FTP_URL,
        with_ttcn=True,
        files=[
            TDocFile(
                tdoc_id="R5s260104",
                type="review",
                file="R5s260104_MCC.zip",
                ftp_url=_FTP_URL,
            ),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ttcn"]["testcase"] == "7.1.3.5.3"
    assert isinstance(payload["files"], list)
    assert len(payload["files"]) == 1
    assert payload["files"][0]["type"] == "review"
    assert payload["files"][0]["ftp_url"] == _FTP_URL


def test_tdoc_show_ftp_url_json_tdoc_omitted_when_no_match(sqlite_env) -> None:
    """A URL that has ``tdoc_cr_cover_page`` / ``files`` but no ``tdocs``
    row surfaces those keys but ``tdoc`` is omitted.

    The TDoc row is seeded at a *different* URL than the cover row so
    the URL-keyed TDoc lookup misses while the URL-keyed cover lookup
    still hits. The CR row's ``tdoc_id`` FK targets the seeded TDoc.
    """
    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    other_url = "stored/different-tdoc.zip"
    tdoc_repo.upsert(TDoc(tdoc_id="R5s260999", type="CR", ftp_url=other_url))
    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id="R5s260999",
            spec="38.523-3",
            cr_num="3790",
            ftp_url=_FTP_URL,
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "tdoc" not in payload
    assert payload["cover"]["spec"] == "38.523-3"


def test_tdoc_show_ftp_url_markdown_anchors_on_ftp_url(sqlite_env) -> None:
    """Markdown output is anchored on ``# FTP URL`` and renders every
    populated section beneath it."""
    create_schema()
    _seed_ftp_url_target("R5s260105", _FTP_URL)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "markdown"],
    )
    assert result.exit_code == 0, result.output
    assert f"# FTP URL `{_FTP_URL}`" in result.output
    assert "## TDoc" in result.output
    assert "- **tdoc_id**: R5s260105" in result.output
    assert "## Extracted Cover Details" in result.output


def test_tdoc_show_ftp_url_markdown_omits_tdoc_section_when_no_match(
    sqlite_env,
) -> None:
    """No ``tdocs`` row → the ``## TDoc`` section is absent (omitted)."""
    create_schema()
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    other_url = "stored/different-tdoc.zip"
    tdoc_repo.upsert(TDoc(tdoc_id="R5s260998", type="CR", ftp_url=other_url))
    cr_repo.upsert(
        TDocCRDetails(
            tdoc_id="R5s260998",
            spec="38.523-3",
            cr_num="3790",
            ftp_url=_FTP_URL,
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "markdown"],
    )
    assert result.exit_code == 0, result.output
    assert "## TDoc" not in result.output
    assert "## Extracted Cover Details" in result.output


def test_tdoc_show_ftp_url_raw_emits_cached_markdown(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """``--ftp-url --format raw`` reads the cache file derived from the
    URL directly and writes its content to stdout."""
    create_schema()
    _seed_ftp_url_target("R5s260106", _FTP_URL)

    cached_md = "# Heading\n\nbody from URL-keyed cache\n"
    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path",
        lambda cache_file, cache_root: cached_md,
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "raw"]
    )
    assert result.exit_code == 0, result.output
    assert result.output == cached_md


def test_tdoc_show_ftp_url_raw_cache_miss_raises_bad_parameter(
    sqlite_env, monkeypatch
) -> None:
    """``--ftp-url --format raw`` with no cached markdown raises
    ``BadParameter`` pointing at the parse command."""
    create_schema()
    _seed_ftp_url_target("R5s260107", _FTP_URL)

    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path",
        lambda cache_file, cache_root: "",
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--ftp-url", _FTP_URL, "--format", "raw"]
    )
    assert result.exit_code != 0
    assert "cached markdown" in result.output.lower() or "cache" in result.output
    assert "tdoc parse" in result.output


def test_tdoc_show_ftp_url_does_not_trigger_auto_sync(
    sqlite_env, monkeypatch
) -> None:
    """The ``--ftp-url`` path bypasses ``trigger_auto_sync`` — there's
    no parent meeting sync meaningful for an arbitrary URL."""
    create_schema()
    _seed_ftp_url_target("R5s260108", _FTP_URL)

    mock_auto_sync = MagicMock(return_value=(0, 0))
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", mock_auto_sync)

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--ftp-url", _FTP_URL])
    assert result.exit_code == 0, result.output
    mock_auto_sync.assert_not_called()


# ---------------------------------------------------------------------------
# tdoc parse --from-url — auto-sync wiring (3GPP URL only)
# ---------------------------------------------------------------------------


class _RecordingCrService:
    """Stand-in for ``TDocCrService`` that records which public methods
    are touched during ``tdoc parse --from-url``.

    The auto-sync tests replace ``build_tdoc_cr_service`` with this and
    verify that ``collect_3gpp_file_urls`` and ``extract_from_url_batch``
    are (or aren't) invoked — the parse-step content is irrelevant for
    the auto-sync plumbing check.
    """

    def __init__(self) -> None:
        self.collect_calls: list[tuple[str, int]] = []
        self.extract_batch_calls: list[tuple[str, int]] = []
        self.extract_url_calls: list[str] = []

    def collect_3gpp_file_urls(self, url: str, *, max_depth: int) -> list[str]:
        self.collect_calls.append((url, max_depth))
        return []

    def extract_from_url_batch(
        self, url: str, *, max_depth: int, force: bool, full: bool,
        max_tdoc_size_bytes: int = 0,
    ) -> "_RecordingBatchResult":
        self.extract_batch_calls.append((url, max_depth))
        return _RecordingBatchResult(results=[], failures={})

    def extract_from_url(
        self, url: str, *, force: bool, full: bool,
        max_tdoc_size_bytes: int = 0,
    ) -> "_RecordingSingleResult":
        self.extract_url_calls.append(url)
        return _RecordingSingleResult(
            source_kind="url-3gpp",
            markdown="",
            details=None,
            extract_meta=None,
            from_cache=False,
            persisted=False,
            tdoc_id="R5s260009",
            tdoc_id_in_tdocs=True,
        )


@dataclass
class _RecordingBatchResult:
    results: list
    failures: dict


@dataclass
class _RecordingSingleResult:
    source_kind: str
    markdown: str
    details: object
    extract_meta: object
    from_cache: bool
    persisted: bool
    tdoc_id: str
    tdoc_id_in_tdocs: bool


def _patch_direct_parse_to_noop(monkeypatch) -> MagicMock:
    """Stub ``_tdoc_parse_direct`` so it does nothing observable.

    The auto-sync tests focus on whether the URL derived the right
    candidates and whether ``trigger_auto_sync`` fired — the actual
    parse-step output is irrelevant.
    """
    mock = MagicMock()
    monkeypatch.setattr("doc3gpp.cli._tdoc_parse_direct", mock)
    return mock


def _patch_url_batch_to_noop(monkeypatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("doc3gpp.cli._tdoc_parse_url_batch", mock)
    return mock


def _enable_auto_sync(monkeypatch) -> None:
    monkeypatch.setenv("DOC3GPP_SYNC__AUTO_SYNC", "true")
    get_settings.cache_clear()


def test_tdoc_parse_from_url_3gpp_file_triggers_auto_sync_with_candidates(
    sqlite_env, monkeypatch,
) -> None:
    """3GPP file URL + auto_sync on → trigger_auto_sync fires once with
    the basename-derived tdoc_id set BEFORE the parse runs."""
    _enable_auto_sync(monkeypatch)
    service = _RecordingCrService()
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: service)
    _patch_direct_parse_to_noop(monkeypatch)

    candidates_mock = MagicMock(return_value={"R5s260009"})
    monkeypatch.setattr(
        "doc3gpp.cli.collect_tdoc_candidates_for_url", candidates_mock,
    )

    sync_mock = MagicMock(return_value=(1, 0))
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", sync_mock)

    meeting_mock = MagicMock()
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: meeting_mock)
    coordinator_mock = MagicMock()
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_sync_coordinator", lambda: coordinator_mock,
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/R5s260009.zip"],
    )

    assert result.exit_code == 0, result.output
    candidates_mock.assert_called_once()
    call_args, call_kwargs = candidates_mock.call_args
    assert call_args == ("https://www.3gpp.org/ftp/R5s260009.zip",)
    assert call_kwargs["tdoc_service"] is service
    assert call_kwargs["max_depth"] >= 0

    sync_mock.assert_called_once()
    sync_kwargs = sync_mock.call_args.kwargs
    assert sync_kwargs["auto_sync_enabled"] is True
    assert sync_kwargs["tdoc_ids"] == {"R5s260009"}
    assert sync_kwargs["meeting_service"] is meeting_mock
    assert sync_kwargs["tdoc_sync_coordinator"] is coordinator_mock


def test_tdoc_parse_from_url_3gpp_folder_skips_sync_when_no_candidates(
    sqlite_env, monkeypatch,
) -> None:
    """3GPP folder URL where the BFS yields nothing → candidates empty →
    trigger_auto_sync is NOT called, but the parse still proceeds via
    the batch dispatcher."""
    _enable_auto_sync(monkeypatch)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: _RecordingCrService(),
    )
    batch_mock = _patch_url_batch_to_noop(monkeypatch)

    candidates_mock = MagicMock(return_value=set())
    monkeypatch.setattr(
        "doc3gpp.cli.collect_tdoc_candidates_for_url", candidates_mock,
    )

    sync_mock = MagicMock(return_value=(0, 0))
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", sync_mock)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "parse", "--from-url", "https://www.3gpp.org/ftp/Docs/"],
    )

    assert result.exit_code == 0, result.output
    candidates_mock.assert_called_once()
    sync_mock.assert_not_called()
    batch_mock.assert_called_once()
    assert batch_mock.call_args.kwargs["from_url"] == "https://www.3gpp.org/ftp/Docs/"


def test_tdoc_parse_from_url_non_3gpp_skips_auto_sync(
    sqlite_env, monkeypatch,
) -> None:
    """Non-3GPP URLs never trigger auto-sync."""
    _enable_auto_sync(monkeypatch)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: _RecordingCrService(),
    )
    direct_mock = _patch_direct_parse_to_noop(monkeypatch)

    candidates_mock = MagicMock(return_value=set())
    monkeypatch.setattr(
        "doc3gpp.cli.collect_tdoc_candidates_for_url", candidates_mock,
    )

    sync_mock = MagicMock(return_value=(0, 0))
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", sync_mock)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url", "https://example.com/R5s260009.zip",
        ],
    )

    assert result.exit_code == 0, result.output
    # Non-3GPP URL short-circuits the ``is_3gpp_ftp_url`` gate in the
    # CLI before the collect helper is invoked — so ``collect`` is NOT
    # called and the sync is skipped, but the direct-mode parse still
    # dispatches.
    candidates_mock.assert_not_called()
    sync_mock.assert_not_called()
    direct_mock.assert_called_once()


def test_tdoc_parse_from_url_passes_max_depth_into_collect(
    sqlite_env, monkeypatch,
) -> None:
    """``--max-depth`` and ``--recursive`` are forwarded through the
    ``_resolve_url_batch_depth`` helper into ``collect_tdoc_candidates_for_url``."""
    _enable_auto_sync(monkeypatch)
    service = _RecordingCrService()
    monkeypatch.setattr("doc3gpp.cli.build_tdoc_cr_service", lambda *args, **kwargs: service)
    _patch_url_batch_to_noop(monkeypatch)

    candidates_mock = MagicMock(return_value=set())
    monkeypatch.setattr(
        "doc3gpp.cli.collect_tdoc_candidates_for_url", candidates_mock,
    )
    sync_mock = MagicMock(return_value=(0, 0))
    monkeypatch.setattr("doc3gpp.cli.trigger_auto_sync", sync_mock)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url", "https://www.3gpp.org/ftp/Docs/",
            "--max-depth", "3",
        ],
    )

    assert result.exit_code == 0, result.output
    _, collect_kwargs = candidates_mock.call_args
    assert collect_kwargs["max_depth"] == 3
    sync_mock.assert_not_called()


def test_tdoc_parse_from_url_services_only_built_once(
    sqlite_env, monkeypatch,
) -> None:
    """``build_meeting_service`` and ``build_tdoc_sync_coordinator`` are
    only invoked when there are candidates to sync — avoid spinning up
    unneeded service instances for non-3GPP URLs."""
    _enable_auto_sync(monkeypatch)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _RecordingCrService(),
    )
    _patch_direct_parse_to_noop(monkeypatch)
    monkeypatch.setattr(
        "doc3gpp.cli.collect_tdoc_candidates_for_url",
        MagicMock(return_value=set()),
    )

    meeting_factory = MagicMock(side_effect=RuntimeError("not built"))
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", meeting_factory)
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_sync_coordinator", meeting_factory,
    )
    monkeypatch.setattr(
        "doc3gpp.cli.trigger_auto_sync", MagicMock(return_value=(0, 0))
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tdoc", "parse", "--from-url", "https://example.com/x.zip"],
    )
    assert result.exit_code == 0, result.output
    meeting_factory.assert_not_called()


# ---------------------------------------------------------------------------
# tdoc parse --compact (direct-mode)
# ---------------------------------------------------------------------------


def test_tdoc_parse_compact_flag_accepted_on_from_url(sqlite_env, monkeypatch) -> None:
    """``tdoc parse --from-url URL --format json --compact`` is a valid
    CLI surface combination. Verifies the Typer command accepts the
    ``--compact`` flag without raising an ``Unknown option`` error and
    reaches the noop direct-parse stub. The actual compact JSON output
    contract is covered by ``tests/unit/test_tdoc_parse_direct.py``."""
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda *args, **kwargs: _RecordingCrService(),
    )
    _patch_direct_parse_to_noop(monkeypatch)
    monkeypatch.setattr(
        "doc3gpp.cli.collect_tdoc_candidates_for_url",
        MagicMock(return_value=set()),
    )

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--from-url", "https://example.com/x.zip",
            "--format", "json",
            "--compact",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no such option" not in (result.output or "").lower()
    assert "Unknown option" not in (result.output or "")