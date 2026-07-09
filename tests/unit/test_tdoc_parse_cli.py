"""Unit tests for the ``tdoc parse`` and ``tdoc show`` CLI commands.

The tests stub out :func:`doc3gpp.services.factory.build_tdoc_cr_service`
and :class:`SQLAlchemyTDocRepository` via ``monkeypatch`` so the CLI
runs without ever touching the network, the python-docx renderer, or
the ``tdocs`` table. Each test seeds the ``sqlite_env`` fixture and
resets the settings/engine caches via ``conftest.sqlite_env``.
"""

from __future__ import annotations

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
    ) -> None:
        self._results = results or {}
        self._failures = failures or {}
        self._raise_from_many = raise_from_many
        self.many_calls: list[tuple[list[str], bool, bool]] = []

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


class _FakeTDocRepoList:
    """In-memory :class:`TDocRepository` double exposing ``list()`` for meeting-id tests.

    ``get_by_id`` returns ``None`` so a stray ``--tdoc-id`` lookup
    surfaces as a miss; ``list`` returns the pre-seeded ``_list_tdocs``
    list verbatim and records every call's kwargs so the test can
    assert the CLI asked for ``tdoc_type="CR"`` with the right
    ``meeting_id``.
    """

    def __init__(self, list_tdocs: list[TDoc]) -> None:
        self._list_tdocs = list_tdocs
        self.list_calls: list[dict] = []

    def get_by_id(self, tdoc_id: str) -> TDoc | None:
        return None

    def list(self, **kwargs) -> list[TDoc]:
        self.list_calls.append(kwargs)
        return list(self._list_tdocs)


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

    Only ``get`` is exercised by the ``--meeting-id`` branch; an empty
    return list means "not parsed" and a one-element list means "at
    least one detail row exists" (mirroring the real repo contract).
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


def _patch_tdoc_repo(monkeypatch, by_int: dict[str, TDoc | None]) -> None:
    """Stub ``build_tdoc_repository`` so ``--tdoc-id`` lookups return fixtures.

    ``by_int`` maps the stringified integer arg to a TDoc (or None to
    simulate a miss). Only ``get_by_id`` is exercised by the CLI.
    """

    class _Repo:
        def get_by_id(self, tdoc_id: str) -> TDoc | None:
            return by_int.get(tdoc_id)

    monkeypatch.setattr(
        "doc3gpp.cli.build_tdoc_repository",
        lambda: _Repo(),
    )


def _patch_tdoc_repo_for_listing(
    monkeypatch, list_tdocs: list[TDoc]
) -> "_FakeTDocRepoList":
    """Stub ``build_tdoc_repository`` so ``--meeting-id`` can call ``list()``.

    Records every ``list`` call so the test can assert the meeting-id
    branch queried for ``tdoc_type="CR"`` with the expected ``meeting_id``
    and a positive ``limit``. Only ``list`` is exercised; ``get_by_id``
    returns ``None`` so a stray ``--tdoc-id`` lookup surfaces as a miss.
    """
    fake = _FakeTDocRepoList(list_tdocs)

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
    """Stub ``build_tdoc_cr_repository`` so ``--meeting-id`` can probe parsed status.

    ``parsed_ids`` is the set of TDoc ids considered already parsed
    (``get(tdoc_id)`` returns a non-empty list for these). Records every
    ``get`` call so the test can verify the CLI checked parsed status
    per row when ``force=False``.
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
# tdoc parse
# ---------------------------------------------------------------------------


def test_tdoc_parse_happy_path(sqlite_env, monkeypatch) -> None:
    """A single successful ``--tdoc`` prints spec/cr_num/title and exits 0."""
    runner = CliRunner()
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260009"])
    assert result.exit_code == 0, result.output
    assert "spec=38.523-3" in result.output
    assert "cr_num=3790" in result.output
    assert "title=Example CR" in result.output
    assert "Extracted 1/1 TDocs (0 failures)" in result.output
    # extract_many was called with the resolved id only.
    assert fake.many_calls == [(["R5s260009"], False, False)]


def test_tdoc_parse_partial_failure(sqlite_env, monkeypatch) -> None:
    """When ``extract_many`` reports a failure for one id via
    ``batch.failures``, the CLI prints ``FAILED - {reason}`` inline and
    still exits 0 (one success keeps the batch non-fatal)."""
    runner = CliRunner()
    fake = _FakeCrService(
        results={"R5s260009": _make_result("R5s260009")},
        failures={"R5s260010": "TDocNotFoundError: TDoc 'R5s260010' is not stored"},
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--tdoc", "R5s260009",
            "--tdoc", "R5s260010",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "R5s260009: spec=" in result.output
    assert "R5s260010: FAILED - TDocNotFoundError: TDoc 'R5s260010' is not stored" in result.output
    assert "Extracted 1/2 TDocs (1 failures)" in result.output


def test_tdoc_parse_all_failures(sqlite_env, monkeypatch) -> None:
    """``extract_many`` reporting every id as a failure (no successes)
    yields exit 1 and an all-failures summary that surfaces the per-id
    reason instead of the old generic "see logs" message."""
    runner = CliRunner()
    fake = _FakeCrService(
        results={},
        failures={
            "R5s260009": "TDocNotFoundError: TDoc 'R5s260009' is not stored",
            "R5s260010": "TDocTypeUnsupportedError: TDoc 'R5s260010' has type 'LS'",
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--tdoc", "R5s260009",
            "--tdoc", "R5s260010",
        ],
    )
    assert result.exit_code == 1
    assert "R5s260009: FAILED - TDocNotFoundError:" in result.output
    assert "R5s260010: FAILED - TDocTypeUnsupportedError:" in result.output
    assert "Extracted 0/2 TDocs (2 failures)" in result.output
    assert "extract error (see logs)" not in result.output


@pytest.mark.parametrize(
    ("exc_factory", "expected_class"),
    [
        # The exception class names are exactly what the user reads on
        # the FAILED line — they map 1:1 to a step in the extract
        # pipeline, so the operator can tell *where* the failure was
        # without tailing the log file.
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
    fake = _FakeCrService(
        results={},
        failures={"R5s260010": f"{type(exc).__name__}: {exc}"},
    )
    _patch_service(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", "R5s260010"])
    assert result.exit_code == 1
    assert f"R5s260010: FAILED - {expected_class}:" in result.output
    # The original exception message (which carries the actionable
    # detail — e.g. "run `doc3gpp tdoc sync` first" for the not-found
    # case) is included on the same line.
    assert str(exc) in result.output


def test_tdoc_parse_no_tdocs_specified(sqlite_env) -> None:
    """Invoking the command with no selector raises ``BadParameter``."""
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse"])
    assert result.exit_code != 0
    assert "Specify at least one --tdoc, --tdoc-id, or --meeting-id" in result.output


def test_tdoc_parse_tdoc_id_resolves(sqlite_env, monkeypatch) -> None:
    """``--tdoc-id`` is looked up via the repository; the resolved id
    string flows into ``extract_many``."""
    runner = CliRunner()
    fake = _FakeCrService(results={"R5s260009": _make_result("R5s260009")})
    _patch_service(monkeypatch, fake)
    _patch_tdoc_repo(
        monkeypatch,
        by_int={"1": TDoc(tdoc_id="R5s260009", type="CR")},
    )

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc-id", "1"])
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260009"], False, False)]
    assert "R5s260009: spec=38.523-3" in result.output


def test_tdoc_parse_force_passed_through(sqlite_env, monkeypatch) -> None:
    """``--force=True`` is forwarded to ``extract_many``."""
    runner = CliRunner()
    fake = _FakeCrService(results={"R5s260009": _make_result()})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s260009", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["R5s260009"], True, False)]


def test_tdoc_parse_python_docx_missing_friendly_error(sqlite_env, monkeypatch) -> None:
    """When ``extract_many`` raises :class:`PythonDocxNotInstalledError`,
    the CLI prints the install hint and exits 1 — the batch does not
    crash with a Python traceback."""
    runner = CliRunner()
    fake = _FakeCrService(
        raise_from_many=PythonDocxNotInstalledError(),
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "R5s260009"],
    )
    assert result.exit_code == 1
    assert "python-docx is not installed" in result.output
    assert "pip install doc3gpp[extract]" in result.output


# ---------------------------------------------------------------------------
# tdoc parse --meeting-id
# ---------------------------------------------------------------------------


def test_tdoc_parse_meeting_id_parses_new_only(
    sqlite_env, monkeypatch
) -> None:
    """``--meeting-id`` queries for CR-type TDocs under the meeting,
    filters out any that already have a ``tdoc_cr_details`` row, and
    dispatches the rest to ``extract_many``."""
    runner = CliRunner()
    meeting_id = 42
    cr_tdocs = [
        TDoc(tdoc_id="R5s260009", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260010", type="CR", meeting_id=meeting_id),
        TDoc(tdoc_id="R5s260011", type="CR", meeting_id=meeting_id),
    ]
    # R5s260010 is already parsed; the other two are new.
    parsed = {"R5s260010"}

    meeting_svc = _patch_meeting_service(
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
    cr_repo = _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(
        results={
            "R5s260009": _make_result("R5s260009"),
            "R5s260011": _make_result("R5s260011"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--meeting-id", str(meeting_id)])
    assert result.exit_code == 0, result.output
    # The CLI asked for CR-type TDocs under the meeting with a positive limit.
    assert tdoc_repo.list_calls == [
        {
            "meeting_id": meeting_id,
            "tdoc_type": "CR",
            "limit": 10_000,
            "status": None,
            "cr_cat": None,
            "spec": None,
            "wi": None,
            "revision_of": None,
            "revised_to": None,
            "title": None,
            "ftp_url": None,
            "source": None,
            "uploaded_date": None,
        },
    ]
    # The CLI probed parsed status for every row in the meeting.
    assert sorted(cr_repo.get_calls) == ["R5s260009", "R5s260010", "R5s260011"]
    assert fake.many_calls == [(["R5s260009", "R5s260011"], False, False)]
    assert "R5s260009: spec=" in result.output
    assert "R5s260011: spec=" in result.output
    assert "R5s260010" not in result.output  # parsed → skipped from output
    assert "Extracted 2/2 TDocs (0 failures)" in result.output
    assert meeting_svc.get_calls == [meeting_id]


def test_tdoc_parse_meeting_id_force_parses_all(
    sqlite_env, monkeypatch
) -> None:
    """With ``--force``, every CR-type TDoc under the meeting reaches
    ``extract_many`` regardless of parsed status — the CLI must not
    even probe the CR repo in this branch."""
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
    cr_repo = _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(
        results={
            "R5s260009": _make_result("R5s260009"),
            "R5s260010": _make_result("R5s260010"),
        },
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--meeting-id", str(meeting_id), "--force"],
    )
    assert result.exit_code == 0, result.output
    # force=True was forwarded to extract_many with every CR tdoc id.
    assert fake.many_calls == [(["R5s260009", "R5s260010"], True, False)]
    # The CLI never asked the CR repo which rows were parsed under --force.
    assert cr_repo.get_calls == []


def test_tdoc_parse_meeting_id_unknown_meeting(sqlite_env, monkeypatch) -> None:
    """An unknown ``--meeting-id`` raises ``BadParameter`` with a
    pointer to ``meeting list`` and never reaches the TDoc repo."""
    runner = CliRunner()
    _patch_meeting_service(monkeypatch, meetings={})
    tdoc_repo = _patch_tdoc_repo_for_listing(monkeypatch, list_tdocs=[])

    result = runner.invoke(app, ["tdoc", "parse", "--meeting-id", "9999"])
    assert result.exit_code != 0
    assert "Unknown meeting_id 9999" in result.output
    assert "doc3gpp meeting list" in result.output
    # The CLI bailed before fetching the TDoc list.
    assert tdoc_repo.list_calls == []


def test_tdoc_parse_meeting_id_no_cr_tdocs(sqlite_env, monkeypatch) -> None:
    """A meeting with zero CR-type TDocs (e.g. only LS / DRAFT rows) is
    a clear "nothing to do" case — friendly message, exit 1, and no
    extract_many call."""
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

    result = runner.invoke(app, ["tdoc", "parse", "--meeting-id", str(meeting_id)])
    assert result.exit_code == 1
    assert f"No CR-type TDocs found for meeting_id {meeting_id}" in result.output
    assert fake.many_calls == []


def test_tdoc_parse_meeting_id_all_parsed(sqlite_env, monkeypatch) -> None:
    """When every CR-type TDoc under the meeting is already parsed and
    ``--force`` is *not* set, the CLI prints a "use --force" hint and
    exits 0 — the user's intent (parse new ones) was satisfied."""
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
    _patch_tdoc_repo_for_listing(monkeypatch, cr_tdocs)
    _patch_cr_repo(monkeypatch, parsed)
    fake = _FakeCrService(results={})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--meeting-id", str(meeting_id)])
    assert result.exit_code == 0
    assert "All 2 CR-type TDocs for meeting_id 5 are already parsed" in result.output
    assert "use --force to re-parse" in result.output
    # Nothing reached extract_many — the CLI exited before dispatching.
    assert fake.many_calls == []


def test_tdoc_parse_meeting_id_mutually_exclusive(
    sqlite_env, monkeypatch
) -> None:
    """``--meeting-id`` cannot be combined with the per-id selectors."""
    runner = CliRunner()
    fake = _FakeCrService(results={})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", "1",
            "--tdoc", "R5s260009",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    # extract_many was never reached.
    assert fake.many_calls == []


def test_tdoc_parse_meeting_id_with_tdoc_id_mutually_exclusive(
    sqlite_env, monkeypatch
) -> None:
    """``--meeting-id`` is also mutually exclusive with ``--tdoc-id``."""
    runner = CliRunner()
    fake = _FakeCrService(results={})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", "1",
            "--tdoc-id", "2",
        ],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
    assert fake.many_calls == []


# ---------------------------------------------------------------------------
# tdoc parse --meeting-id field filters
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


def test_tdoc_parse_meeting_id_passes_status_filter(_meeting_with_cr_tdocs) -> None:
    """`--status` flows through to the repo's `list` call."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--status", "Agreed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(ns.tdoc_repo.list_calls) == 1
    assert ns.tdoc_repo.list_calls[0]["status"] == "Agreed"
    assert ns.tdoc_repo.list_calls[0]["spec"] is None
    assert ns.tdoc_repo.list_calls[0]["uploaded_date"] is None


def test_tdoc_parse_meeting_id_passes_null_status(_meeting_with_cr_tdocs) -> None:
    """`--status null` is forwarded verbatim to the repo (which
    interprets the literal token as a NULL filter)."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--status", "null",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_calls[0]["status"] == "null"


def test_tdoc_parse_meeting_id_passes_date_filter(_meeting_with_cr_tdocs) -> None:
    """`--uploaded-date ">='2026-02-31'"` is forwarded verbatim."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--uploaded-date", ">= '2026-02-31'",
        ],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_calls[0]["uploaded_date"] == ">= '2026-02-31'"


def test_tdoc_parse_meeting_id_rejects_bad_date_filter(_meeting_with_cr_tdocs) -> None:
    """An invalid `--uploaded-date` is caught at the CLI boundary with
    a clear BadParameter and never reaches the repo."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--uploaded-date", "yesterday",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid date filter" in result.output
    assert "'yesterday'" in result.output
    assert ns.tdoc_repo.list_calls == []
    assert ns.fake.many_calls == []


def test_tdoc_parse_meeting_id_rejects_bad_date_operator(_meeting_with_cr_tdocs) -> None:
    """An unsupported operator (`==`) is rejected with the same
    BadParameter message before the repo is touched."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        [
            "tdoc", "parse",
            "--meeting-id", str(ns.meeting_id),
            "--uploaded-date", "== '2026-02-31'",
        ],
    )
    assert result.exit_code != 0
    assert "Invalid date filter" in result.output
    assert ns.tdoc_repo.list_calls == []


@pytest.mark.parametrize(
    ("flag", "kwarg"),
    [
        ("--cat", "cr_cat"),
        ("--spec", "spec"),
        ("--wi", "wi"),
        ("--revision-of", "revision_of"),
        ("--revised-to", "revised_to"),
        ("--title", "title"),
        ("--ftp-url", "ftp_url"),
        ("--source", "source"),
        ("--type", "tdoc_type"),
    ],
)
def test_tdoc_parse_meeting_id_passes_text_filters(
    _meeting_with_cr_tdocs, flag, kwarg
) -> None:
    """Every text-column filter is forwarded to the repo under its
    expected kwarg name."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(ns.meeting_id), flag, "X%Y"],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_calls[0][kwarg] == "X%Y"


@pytest.mark.parametrize(
    ("flag", "kwarg"),
    [
        ("--title", "title"),
        ("--source", "source"),
        ("--cat", "cr_cat"),
        ("--spec", "spec"),
        ("--wi", "wi"),
        ("--revision-of", "revision_of"),
        ("--revised-to", "revised_to"),
        ("--ftp-url", "ftp_url"),
    ],
)
def test_tdoc_parse_meeting_id_passes_not_like_prefix(
    _meeting_with_cr_tdocs, flag, kwarg
) -> None:
    """`-prefixed values flow through to the repo verbatim. The bang
    is consumed by ``_apply_text_filter`` to emit ``NOT LIKE``; the
    CLI layer must not interpret it."""
    ns = _meeting_with_cr_tdocs
    result = ns.runner.invoke(
        app,
        ["tdoc", "parse", "--meeting-id", str(ns.meeting_id), flag, "!%X%"],
    )
    assert result.exit_code == 0, result.output
    assert ns.tdoc_repo.list_calls[0][kwarg] == "!%X%"


def test_tdoc_parse_meeting_id_combines_filters(_meeting_with_cr_tdocs) -> None:
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
        ],
    )
    assert result.exit_code == 0, result.output
    call = ns.tdoc_repo.list_calls[0]
    assert call["status"] == "Agreed"
    assert call["spec"] == "38.%"
    assert call["uploaded_date"] == "< '2026-12-31'"


# ---------------------------------------------------------------------------
# tdoc show
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
        ats_version="iwd-TTCN3-B2512-260-eng",
        ttcn_release="B2512",
        test_case="7.1.3.5.3",
        test_suite="NR5GC",
        ue="UE1",
        ss="SS_NR5G",
        year=2026,
        tech="5G",
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
    assert "corrections:" in result.output
    # The 200-char truncation helper kicks in on these long fields.
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
    """``--tdoc r5s260213`` must flow into ``extract_many`` as ``R5s260213``
    so the DB lookup against the canonical PK succeeds."""
    runner = CliRunner()
    fake = _FakeCrService(
        results={expected_canonical: _make_result(expected_canonical)},
    )
    _patch_service(monkeypatch, fake)

    result = runner.invoke(app, ["tdoc", "parse", "--tdoc", raw_input])
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [([expected_canonical], False, False)]
    assert f"{expected_canonical}: spec=" in result.output


def test_tdoc_parse_non_cr_shape_passes_through(sqlite_env, monkeypatch) -> None:
    """Non-CR shapes (e.g. LS) have no canonical mapping; the input is
    stripped of whitespace and forwarded verbatim — the DB lookup
    succeeds iff the user typed it exactly as stored."""
    runner = CliRunner()
    fake = _FakeCrService(results={"LS-260001": _make_result("LS-260001")})
    _patch_service(monkeypatch, fake)

    result = runner.invoke(
        app, ["tdoc", "parse", "--tdoc", "  LS-260001  "],
    )
    assert result.exit_code == 0, result.output
    assert fake.many_calls == [(["LS-260001"], False, False)]


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
