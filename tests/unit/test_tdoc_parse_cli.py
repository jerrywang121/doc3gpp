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
from doc3gpp.models.tdoc import TDoc
from doc3gpp.models.tdoc_cr import TDocCRDetails, TDocExtractMeta
from doc3gpp.parsers.docx_converter import PythonDocxNotInstalledError
from doc3gpp.services.tdoc_cr_service import ExtractResult
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
        raise_from_many: Exception | None = None,
    ) -> None:
        self._results = results or {}
        self._raise_from_many = raise_from_many
        self.many_calls: list[tuple[list[str], bool, bool]] = []

    def extract_many(
        self, tdoc_ids, *, force: bool = False, full: bool = False,
    ) -> dict[str, ExtractResult]:
        self.many_calls.append((list(tdoc_ids), force, full))
        if self._raise_from_many is not None:
            raise self._raise_from_many
        return dict(self._results)


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
    """When ``extract_many`` returns only some ids, the CLI prints
    ``FAILED - extract error`` for the missing ones and exits 0."""
    runner = CliRunner()
    fake = _FakeCrService(
        results={"R5s260009": _make_result("R5s260009")},
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
    assert "R5s260010: FAILED - extract error" in result.output
    assert "Extracted 1/2 TDocs (1 failures)" in result.output


def test_tdoc_parse_all_failures(sqlite_env, monkeypatch) -> None:
    """``extract_many`` returning an empty dict (every id skipped by the
    service) yields exit 1 and an all-failures summary."""
    runner = CliRunner()
    fake = _FakeCrService(results={})
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
    assert "R5s260009: FAILED - extract error" in result.output
    assert "R5s260010: FAILED - extract error" in result.output
    assert "Extracted 0/2 TDocs (2 failures)" in result.output


def test_tdoc_parse_no_tdocs_specified(sqlite_env) -> None:
    """Invoking the command with no selector raises ``BadParameter``."""
    runner = CliRunner()
    result = runner.invoke(app, ["tdoc", "parse"])
    assert result.exit_code != 0
    assert "Specify at least one --tdoc or --tdoc-id" in result.output


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
