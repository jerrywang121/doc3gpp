"""Unit tests for the ``tdoc parse`` and ``tdoc show`` CLI commands.

The tests stub out :func:`doc3gpp.services.factory.build_tdoc_cr_service`
and :class:`SQLAlchemyTDocRepository` via ``monkeypatch`` so the CLI
runs without ever touching the network, the python-docx renderer, or
the ``tdocs`` table. Each test seeds the ``sqlite_env`` fixture and
resets the settings/engine caches via ``conftest.sqlite_env``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone

import pytest
from typer.testing import CliRunner

from doc3gpp.cli import app
from doc3gpp.models.meeting import Meeting
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.parsers.docx_converter import PythonDocxNotInstalledError
from doc3gpp.services.tdoc_cr_service import (
    BatchExtractResult,
    ExtractResult,
    TDocNotFoundError,
    TDocTypeUnsupportedError,
    TDocZipDownloadError,
)
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tdoc_cr_sql import SQLAlchemyTDocCrRepository
from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository


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
        raise_from_many: Exception | None = None,
        raise_from_extract: Exception | None = None,
    ) -> None:
        self._results = results or {}
        self._failures = failures or {}
        self._raise_from_many = raise_from_many
        self._raise_from_extract = raise_from_extract
        self.many_calls: list[tuple[list[str], bool, bool]] = []
        self.extract_calls: list[str] = []

    def extract_many(
        self, tdoc_ids, *, force: bool = False, full: bool = False,
    ) -> BatchExtractResult:
        self.many_calls.append((list(tdoc_ids), force, full))
        if self._raise_from_many is not None:
            raise self._raise_from_many
        return BatchExtractResult(
            successes=dict(self._results),
            failures=dict(self._failures),
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

    Only ``get`` is exercised by the ``tdoc parse`` filter branch; an
    empty return list means "not parsed" and a one-element list means
    "at least one detail row exists" (mirroring the real repo contract).
    """

    def __init__(self, parsed_ids: set[str]) -> None:
        self._parsed = parsed_ids
        self.get_calls: list[str] = []

    def get(self, tdoc_id: str) -> list:
        self.get_calls.append(tdoc_id)
        return [] if tdoc_id not in self._parsed else [_SENTINEL_DETAIL]


_SENTINEL_DETAIL = object()


def _patch_service(monkeypatch, fake: _FakeCrService) -> None:
    """Stub ``build_tdoc_cr_service`` so the CLI picks up ``fake``."""
    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_service",
        lambda: fake,
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
    monkeypatch, parsed_ids: set[str]
) -> "_FakeCrDetailRepo":
    """Stub ``build_tdoc_cr_repository`` so ``tdoc parse`` can probe parsed status.

    ``parsed_ids`` is the set of TDoc ids considered already parsed
    (``get(tdoc_id)`` returns a non-empty list for these). Records
    every ``get`` call so the test can verify the CLI checked parsed
    status per row when ``force=False``.
    """
    fake = _FakeCrDetailRepo(parsed_ids)

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_cr_repository",
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
        zip_path=f"/tmp/cache/zips/{tdoc_id}",
        markdown_path=f"/tmp/cache/markdown/{tdoc_id}.md",
        doc_filename=f"{tdoc_id}.docx",
        extracted_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
    )
    return ExtractResult(
        details=_make_details(tdoc_id, spec=spec, cr_num=cr_num, title=title),
        extract_meta=meta,
        from_cache=False,
    )


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
    assert "Already parsed in tdoc_cr_details" not in result.output
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
        "Already parsed in tdoc_cr_details "
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


# ---------------------------------------------------------------------------
# tdoc parse — batch limit warning
# ---------------------------------------------------------------------------


def test_tdoc_parse_max_batch_default_is_100(sqlite_env) -> None:
    """The configured default is 100 — sanity check the env wiring."""
    from doc3gpp.config import get_settings

    settings = get_settings()
    assert settings.tdoc_parse.max_batch == 100


def test_tdoc_parse_max_batch_env_override(sqlite_env, monkeypatch) -> None:
    """``DOC3GPP_TDOC_PARSE__MAX_BATCH`` overrides the default."""
    from doc3gpp.config import get_settings

    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_BATCH", "5")
    get_settings.cache_clear()
    try:
        assert get_settings().tdoc_parse.max_batch == 5
    finally:
        get_settings.cache_clear()


def test_tdoc_parse_batch_limit_warning_when_under_max(sqlite_env, monkeypatch) -> None:
    """When the *actual work* count exceeds max_batch but the repo
    already capped the result, the warning is suppressed — the operator
    can only see what was returned. (We simulate this by returning 5
    rows with max_batch=5 so the warning never fires.)"""
    runner = CliRunner()
    cr_tdocs = [TDoc(tdoc_id=f"R5s26000{i}", type="CR") for i in range(1, 6)]
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, set())
    fake = _FakeCrService(
        results={tdoc.tdoc_id: _make_result(tdoc.tdoc_id) for tdoc in cr_tdocs},
    )
    _patch_service(monkeypatch, fake)

    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_BATCH", "5")
    from doc3gpp.config import get_settings
    get_settings.cache_clear()
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
    sqlite_env, monkeypatch,
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
    monkeypatch.setenv("DOC3GPP_TDOC_PARSE__MAX_BATCH", "3")
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
    """Insert a parent TDoc + a populated CR detail row via the real repo.

    Uses the SQL repositories directly so the test exercises the same
    write path the production CLI relies on. The CR detail row carries
    enough fields to verify the ``[Extracted Details]`` block output.
    The URL defaults to a unique-per-call value so multiple seeds in
    the same test produce distinct (URL-keyed) detail rows.
    """
    tdoc_repo = SQLAlchemyTDocRepository()
    cr_repo = SQLAlchemyTDocCrRepository()
    tdoc_repo.upsert(TDoc(tdoc_id=tdoc_id, type="CR"))
    resolved_url = url or f"stored/{tdoc_id}.zip"
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
        details={
            "overview": {
                "ats_version": "iwd-TTCN3-B2512-260-eng",
                "ttcn_release": "B2512",
                "testcase": "7.1.3.5.3",
                "test_suite": "NR5GC",
                "ue": "UE1",
                "ss": "SS_NR5G",
            },
            "corrections": [
                {"function_name": "fl_TC_7_1_3_5_3_Body"},
            ],
        },
        ftp_url=resolved_url,
        parser_version="1.0.0",
    )
    meta = TDocExtractMeta(
        ftp_url=resolved_url,
        tdoc_id=tdoc_id,
        zip_path="/tmp/cache/zips/R5s260009",
        markdown_path="/tmp/cache/markdown/R5s260009.md",
        doc_filename="R5s260009.docx",
    )
    cr_repo.upsert(details, meta)


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
    assert "details:" in result.output
    assert "reason_for_change:" in result.output
    assert "..." in result.output  # truncation ellipsis


def test_tdoc_show_no_extract_row(sqlite_env) -> None:
    """A TDoc without a matching ``tdoc_cr_details`` row prints a
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


def test_tdoc_show_unknown_tdoc_raises_bad_parameter(sqlite_env) -> None:
    """An unknown TDoc id exits non-zero with a friendly message."""
    create_schema()
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "bogus"])
    assert result.exit_code != 0
    assert "Unknown TDoc 'bogus'" in result.output


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


def test_tdoc_show_renders_multiple_revisions(sqlite_env) -> None:
    """Two distinct URLs for the same TDoc id render as separate
    ``[Extracted Details]`` blocks (most recent first)."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")
    _seed_full_crdetail_row(
        "R5s260009",
        url="stored/R5s260009_rev2.zip",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "show", "--tdoc", "R5s260009"])
    assert result.exit_code == 0, result.output
    # Two distinct URLs surface as two ``[Extracted Details]`` blocks.
    assert "R5s260009.zip" in result.output
    assert "R5s260009_rev2.zip" in result.output
    assert result.output.count("[Extracted Details]") == 2


# ---------------------------------------------------------------------------
# tdoc show --format / --output
# ---------------------------------------------------------------------------


def test_tdoc_show_format_json_happy_path(sqlite_env) -> None:
    """``--format json`` emits one JSON object with ``tdoc`` and ``details``."""
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
    assert isinstance(payload["details"], list)
    assert len(payload["details"]) == 1
    detail = payload["details"][0]
    assert detail["tdoc_id"] == "R5s260009"
    assert detail["spec"] == "38.523-3"
    assert detail["cr_num"] == "3790"
    assert detail["date"] == "2026-06-12"
    assert detail["details"]["overview"]["ats_version"] == "iwd-TTCN3-B2512-260-eng"
    assert detail["extract_meta"] is not None
    assert detail["extract_meta"]["tdoc_id"] == "R5s260009"


def test_tdoc_show_format_markdown_happy_path(sqlite_env) -> None:
    """``--format markdown`` emits a Markdown document with per-revision sections."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    runner = CliRunner()
    result = runner.invoke(
        app, ["tdoc", "show", "--tdoc", "R5s260009", "--format", "markdown"]
    )
    assert result.exit_code == 0, result.output
    assert "# TDoc `R5s260009`" in result.output
    assert "## Metadata" in result.output
    assert "## Extracted Details #1" in result.output
    assert "```json" in result.output
    assert "iwd-TTCN3-B2512-260-eng" in result.output
    assert "38.523-3" in result.output
    assert "3790" in result.output


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
    assert payload["details"][0]["cr_num"] == "3790"


def test_tdoc_show_format_raw_emits_cached_markdown(
    sqlite_env, tmp_path, monkeypatch
) -> None:
    """``--format raw`` writes the converted markdown from the cache."""
    create_schema()
    _seed_full_crdetail_row("R5s260009")

    cached_md = "# Heading\n\nbody paragraph\n"
    monkeypatch.setattr(
        "doc3gpp.cli._read_cached_markdown_path",
        lambda path: cached_md,
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
        lambda path: cached_md,
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
        lambda path: "",
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