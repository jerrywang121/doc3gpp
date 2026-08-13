# Spec sync DynaReport direct fetch on `--spec-id` miss — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `doc3gpp spec sync --spec-id <id>` fetch a missing spec directly from the 3GPP DynaReport detail page instead of refusing the sync.

**Architecture:** Service-level fallback in `SpecService.sync_spec`. When the DB lookup misses, fetch `https://www.3gpp.org/DynaReport/{no_dot}.htm`, parse the `#titleVal` / `#typeVal` / `#PrimaryResponsibleGroupLbl` fields, normalise the group to a seeded TSG short name, build an in-memory `Spec`, and hand off to the existing `_sync_one_spec` pipeline. The web and MCP paths inherit the new behaviour automatically. The CLI's pre-flight lookup block is removed; two new error types surface as `typer.BadParameter` and as the matching HTTP 404 / 400 in `web/errors.py`.

**Tech Stack:** Python 3.10+, BeautifulSoup4 + lxml, SQLAlchemy 2.0, httpx, Typer, FastAPI, pytest.

## Global Constraints

- Ruff is the only linter; line length 100; target py310. Run `ruff check .` after each task.
- No comments unless non-obvious.
- Stay in `parsers/spec_parser.py` for the new pure parser functions (per the brainstorming decision); do not add a new parser module.
- The `SpecHeaderFields` intermediate is a `NamedTuple`, not a `@dataclass` — `parsers/spec_parser.py` does not currently declare dataclasses and this DTO does not flow past `SpecService.sync_spec`.
- New exception types live in `services/spec_service.py` (alongside `SpecService`) — match the existing `ValueError` pattern used by the service layer.
- Existing `_sync_one_spec`, `_backfill_pdf_urls`, `_fetch_followups_concurrently`, `_is_sync_skipped` are reused unchanged.
- Update `AGENTS.md` if the CLI surface or behaviour changes (documentation-sync convention).
- Test command for the full suite: `./scripts/test_sqlite.sh`. Per-file: `pytest <file> -v`.

---

### Task 1: Parser — `parse_dynareport_header` + `normalise_tsg_long_name`

**Files:**
- Modify: `src/doc3gpp/parsers/spec_parser.py:1-20` (imports + module docstring), `src/doc3gpp/parsers/spec_parser.py:278-286` (`__all__`)
- Test: `tests/unit/test_spec_parser.py`

**Interfaces:**
- Consumes: `BeautifulSoup` (already imported), `_normalize` (existing helper at `parsers/spec_parser.py:20`), `_extract_type_token` (existing at `parsers/spec_parser.py:61`).
- Produces:
  - `parse_dynareport_header(html: str) -> SpecHeaderFields` where `SpecHeaderFields = NamedTuple("SpecHeaderFields", [("title", str | None), ("type", str | None), ("tsg_long_name", str | None)])`.
  - `normalise_tsg_long_name(long_name: str) -> str | None`.
  - Both are exported via `__all__`.

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/test_spec_parser.py`:

```python
from doc3gpp.parsers.spec_parser import (
    normalise_tsg_long_name,
    parse_dynareport_header,
)


HEADER_HTML = """
<html><body>
<table>
  <tr>
    <td class="TabLineLeft">
      <span id="titleLbl">Title:</span>
    </td>
    <td class="TabLineRight">
      <span id="titleVal">Presence service using the IP Multimedia (IM) Core Network (CN) subsystem; Stage 3</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="typeLbl">Type:</span>
    </td>
    <td class="TabLineRight">
      <span id="typeVal">Technical specification (TS)</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="PrimaryResponsibleGroupLbl">Primary responsible group:</span>
    </td>
    <td class="TabLineRight">
      <span>
        <span>CT 1</span>
      </span>
    </td>
  </tr>
</table>
</body></html>
"""


def test_parse_dynareport_header_extracts_all_three_fields() -> None:
    fields = parse_dynareport_header(HEADER_HTML)
    assert fields.title == (
        "Presence service using the IP Multimedia (IM) Core Network "
        "(CN) subsystem; Stage 3"
    )
    assert fields.type == "TS"
    assert fields.tsg_long_name == "CT 1"


def test_parse_dynareport_header_type_tr_token() -> None:
    html = HEADER_HTML.replace(
        '<span id="typeVal">Technical specification (TS)</span>',
        '<span id="typeVal">Technical report (TR)</span>',
    )
    assert parse_dynareport_header(html).type == "TR"


def test_parse_dynareport_header_missing_fields_are_none() -> None:
    html = "<html><body><table></table></body></html>"
    fields = parse_dynareport_header(html)
    assert fields == (None, None, None)


def test_normalise_tsg_long_name_ran_with_number() -> None:
    assert normalise_tsg_long_name("RAN 1") == "R1"
    assert normalise_tsg_long_name("RAN WG1") == "R1"
    assert normalise_tsg_long_name("RAN5") == "R5"
    assert normalise_tsg_long_name("ran 5") == "R5"


def test_normalise_tsg_long_name_ct_and_sa() -> None:
    assert normalise_tsg_long_name("CT 1") == "C1"
    assert normalise_tsg_long_name("CT 3") == "C3"
    assert normalise_tsg_long_name("SA 2") == "S2"
    assert normalise_tsg_long_name("SA WG6") == "S6"


def test_normalise_tsg_long_name_plenary() -> None:
    assert normalise_tsg_long_name("RT") == "RT"
    assert normalise_tsg_long_name("RP") == "RP"
    assert normalise_tsg_long_name("CP") == "CP"
    assert normalise_tsg_long_name("SP") == "SP"


def test_normalise_tsg_long_name_unknown_returns_none() -> None:
    assert normalise_tsg_long_name("RAN AH1") is None
    assert normalise_tsg_long_name("RAN") is None
    assert normalise_tsg_long_name("") is None
    assert normalise_tsg_long_name("bogus") is None
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/unit/test_spec_parser.py -v -k "parse_dynareport_header or normalise_tsg_long_name"`
Expected: FAIL — `ImportError: cannot import name 'parse_dynareport_header'`.

- [ ] **Step 3: Add `parse_dynareport_header` and `normalise_tsg_long_name` to `parsers/spec_parser.py`**

In `src/doc3gpp/parsers/spec_parser.py`, add `NamedTuple` to the import block (top of file):

```python
from typing import NamedTuple
```

Append the two new functions + `NamedTuple` after the existing `_spec_id_no_dot` helper (currently at `parsers/spec_parser.py:249-251`) and before `extract_etsi_pdf_url`:

```python
class SpecHeaderFields(NamedTuple):
    """Header fields extracted from the DynaReport detail page.

    Parser-private DTO; does not flow past :class:`doc3gpp.services.spec_service.SpecService`.
    """

    title: str | None
    type: str | None
    tsg_long_name: str | None


def parse_dynareport_header(html: str) -> SpecHeaderFields:
    """Extract the three bootstrap header fields from a DynaReport detail page.

    The DynaReport detail page at
    ``https://www.3gpp.org/DynaReport/{spec_id_no_dot}.htm`` carries
    the spec's title (``#titleVal``), its type (``#typeVal``), and
    the primary responsible group (the cell containing
    ``#PrimaryResponsibleGroupLbl``). This is a slim parser used by
    :meth:`doc3gpp.services.spec_service.SpecService.sync_spec` when
    the spec is not yet in the local DB — the rest of the detail
    page is parsed by :func:`parse_spec_detail` inside ``_sync_one_spec``.

    Returns a :class:`SpecHeaderFields` NamedTuple with all three
    fields set to ``None`` when the upstream body does not contain
    any of the expected spans.
    """
    soup = BeautifulSoup(html, "lxml")
    title = _text_of_id(soup, "titleVal")
    type_text = _text_of_id(soup, "typeVal")
    type_token: str | None = None
    if type_text is not None:
        m = re.search(r"\b(TS|TR)\b", type_text, flags=re.IGNORECASE)
        if m is not None:
            type_token = m.group(1).upper()

    tsg_long_name: str | None = None
    label = soup.find(id="PrimaryResponsibleGroupLbl")
    if label is not None:
        row = label.find_parent("tr")
        if row is not None:
            cells = row.find_all("td")
            if len(cells) >= 2:
                tsg_long_name = _normalize(cells[1].get_text()) or None

    return SpecHeaderFields(title=title, type=type_token, tsg_long_name=tsg_long_name)


def normalise_tsg_long_name(long_name: str) -> str | None:
    """Collapse a DynaReport responsible-group label to a ``tsgs.short_name`` row.

    Examples: ``RAN 1`` / ``RAN WG1`` → ``R1``, ``CT 3`` → ``C3``,
    ``SA WG2`` → ``S2``, ``RT`` / ``RP`` / ``CP`` / ``SP`` → as-is.
    Returns ``None`` for unrecognised labels (e.g. ``RAN AH1``,
    ``RAN`` with no number, free text, empty input) so the caller
    can refuse the sync with a clear message.
    """
    if not long_name:
        return None
    upper = re.sub(r"\s+", " ", long_name).strip().upper()
    m = re.match(r"^(RAN|CT|SA)\s*(?:WG)?\s*(\d+)$", upper)
    if m is not None:
        return f"{m.group(1)[0]}{m.group(2)}"
    if re.fullmatch(r"(RT|RP|CP|SP)", upper):
        return upper
    return None
```

- [ ] **Step 4: Update `__all__` to export the new names**

In `src/doc3gpp/parsers/spec_parser.py`, replace the existing `__all__` (line 279):

```python
__all__ = [
    "SpecHeaderFields",
    "normalise_tsg_long_name",
    "parse_dynareport_header",
    "parse_spec_list",
    "parse_spec_detail",
    "extract_etsi_pdf_url",
    "extract_cr_tdocs",
    "normalise_release",
    "release_from_version",
]
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `pytest tests/unit/test_spec_parser.py -v`
Expected: PASS (all tests in the file, including the new ones).

- [ ] **Step 6: Run ruff + commit**

Run: `ruff check src/doc3gpp/parsers/spec_parser.py tests/unit/test_spec_parser.py`
Expected: clean.

```bash
git add src/doc3gpp/parsers/spec_parser.py tests/unit/test_spec_parser.py
git commit -m "feat(spec): add parse_dynareport_header and normalise_tsg_long_name"
```

---

### Task 2: Scraper — `fetch_dynareport_detail`

**Files:**
- Modify: `src/doc3gpp/scraping/spec_source.py:1-60` (add the helper alongside the existing `fetch_spec_list` / `fetch_spec_detail`)
- Test: `tests/unit/test_spec_source.py`

**Interfaces:**
- Consumes: `ScraperClient` (already imported), `build_spec_detail_url` (existing at `scraping/spec_source.py:26`).
- Produces: `fetch_dynareport_detail(spec_id_dotted: str, client: ScraperClient | None = None) -> str`.

- [ ] **Step 1: Add the failing test**

Append to `tests/unit/test_spec_source.py`:

```python
from doc3gpp.scraping.spec_source import fetch_dynareport_detail


def test_fetch_dynareport_detail_uses_dotless_slug(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def get_text(self, url: str) -> str:
            calls.append(url)
            return "<html></html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("doc3gpp.scraping.spec_source.ScraperClient", FakeClient)
    body = fetch_dynareport_detail("38.523-1")
    assert body == "<html></html>"
    assert calls == ["https://www.3gpp.org/DynaReport/38523-1.htm"]
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `pytest tests/unit/test_spec_source.py::test_fetch_dynareport_detail_uses_dotless_slug -v`
Expected: FAIL — `ImportError: cannot import name 'fetch_dynareport_detail'`.

- [ ] **Step 3: Add `fetch_dynareport_detail` in `scraping/spec_source.py`**

In `src/doc3gpp/scraping/spec_source.py`, append after `fetch_spec_detail` (currently at lines 46-60):

```python
def fetch_dynareport_detail(
    spec_id_dotted: str, client: ScraperClient | None = None
) -> str:
    """Fetch the raw HTML body of a spec's DynaReport detail page by dotted id.

    Convenience wrapper around :func:`fetch_spec_detail` that takes
    the dotted ``spec_id`` (``38.523-1``) and strips the dot before
    composing the URL. ``client`` is reused when supplied (same
    pattern as :func:`fetch_spec_list`).
    """
    slug = spec_id_dotted.replace(".", "")
    return fetch_spec_detail(slug, client=client)
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `pytest tests/unit/test_spec_source.py -v`
Expected: PASS.

- [ ] **Step 5: Run ruff + commit**

Run: `ruff check src/doc3gpp/scraping/spec_source.py tests/unit/test_spec_source.py`
Expected: clean.

```bash
git add src/doc3gpp/scraping/spec_source.py tests/unit/test_spec_source.py
git commit -m "feat(spec): add fetch_dynareport_detail helper"
```

---

### Task 3: Service — fallback in `SpecService.sync_spec` + new exception types

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:1-46` (imports + new exception classes), `src/doc3gpp/services/spec_service.py:156-204` (`sync_spec`)
- Test: `tests/unit/test_spec_service.py`

**Interfaces:**
- Consumes:
  - `fetch_dynareport_detail` from Task 2.
  - `parse_dynareport_header`, `normalise_tsg_long_name` from Task 1.
  - `Spec` from `doc3gpp.models.spec` (already imported in `spec_service.py`).
  - `self._repository.get(spec_id)` (existing).
  - `self._tsg_repository.get_by_short_name` (existing optional dependency).
- Produces:
  - `SpecUnknownOnUpstreamError(LookupError)` and `UnknownTsgError(ValueError)` exported alongside `SpecService` in `services.spec_service`.
  - `SpecService.sync_spec(spec_id, *, force=False, on_progress=None)` — when the DB lookup misses, fetches the DynaReport detail page, parses the header, normalises the group, validates the short name against the TSG repo, and hands off to `_sync_one_spec`. The existing happy path (DB lookup hit) is unchanged.

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/test_spec_service.py`:

```python
import pytest

from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
    UnknownTsgError,
)


DYNAREPORT_HEADER_HTML = """
<html><body>
<table>
  <tr>
    <td class="TabLineLeft">
      <span id="titleLbl">Title:</span>
    </td>
    <td class="TabLineRight">
      <span id="titleVal">NR conformance test (Bootstrap)</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="typeLbl">Type:</span>
    </td>
    <td class="TabLineRight">
      <span id="typeVal">Technical specification (TS)</span>
    </td>
  </tr>
  <tr>
    <td class="TabLineLeft">
      <span id="PrimaryResponsibleGroupLbl">Primary responsible group:</span>
    </td>
    <td class="TabLineRight">
      <span>
        <span>RAN 5</span>
      </span>
    </td>
  </tr>
</table>
<table>
  <tr>
    <td><a id="lnkFtpDownload" href="https://www.3gpp.org/ftp/Specs/archive/38_series/38.523-1/38523-1-i30.zip">18.3.0</a></td>
    <td><a id="lnkMeetings" href="?m_id=108">RAN#108</a></td>
    <td><a id="imgRelatedCRs" href="imgRelatedCRs.aspx?versionId=92276"></a></td>
    <td></td>
    <td>2025-06-01</td>
  </tr>
</table>
</body></html>
"""


def test_sync_spec_falls_back_to_dynareport_when_missing(monkeypatch) -> None:
    repo = _StubSpecRepo()
    tsg_repo = _StubTsgRepo(short_names={"R5"})

    def fake_fetch(spec_id_dotted, client=None):
        assert spec_id_dotted == "38.523-1"
        return DYNAREPORT_HEADER_HTML

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        fake_fetch,
    )

    svc = SpecService(repository=repo, tsg_repository=tsg_repo)
    outcome = svc.sync_spec("38.523-1", force=True)

    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert "38.523-1" in repo.specs
    persisted = repo.specs["38.523-1"]
    assert persisted.title == "NR conformance test (Bootstrap)"
    assert persisted.type == "TS"
    assert persisted.tsg == "R5"
    assert len(repo.versions.get("38.523-1", [])) == 1


def test_sync_spec_raises_when_dynareport_body_empty(monkeypatch) -> None:
    repo = _StubSpecRepo()
    tsg_repo = _StubTsgRepo(short_names={"R5"})
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        lambda spec_id_dotted, client=None: "<html><body></body></html>",
    )

    svc = SpecService(repository=repo, tsg_repository=tsg_repo)
    with pytest.raises(SpecUnknownOnUpstreamError):
        svc.sync_spec("38.523-1", force=True)


def test_sync_spec_raises_when_tsg_unknown(monkeypatch) -> None:
    repo = _StubSpecRepo()
    tsg_repo = _StubTsgRepo(short_names=set())  # empty reference table
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        lambda spec_id_dotted, client=None: DYNAREPORT_HEADER_HTML,
    )

    svc = SpecService(repository=repo, tsg_repository=tsg_repo)
    with pytest.raises(UnknownTsgError):
        svc.sync_spec("38.523-1", force=True)


def test_sync_spec_stored_row_unchanged(monkeypatch) -> None:
    repo = _StubSpecRepo()
    repo.upsert(Spec(spec_id="38.523-1", type="TS", title="Cached", tsg="R5"))

    def fail_fetch(spec_id_dotted, client=None):
        raise AssertionError("fetch must not be called when the row is stored")

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_dynareport_detail",
        fail_fetch,
    )

    tsg_repo = _StubTsgRepo(short_names={"R5"})
    svc = SpecService(repository=repo, tsg_repository=tsg_repo)
    outcome = svc.sync_spec("38.523-1", force=True)
    assert outcome.status == "synced"
    assert repo.specs["38.523-1"].title == "Cached"
```

Replace the existing `_StubTsgRepo` at `tests/unit/test_spec_service.py:13-25` with a backward-compatible version that:

1. Keeps the existing `last_spec_sync=...` constructor (used by 11 existing call sites that read the skip rule).
2. Adds a new `short_names: set[str] | None = None` kwarg so the new tests can express "empty reference table" / "known short names".
3. `get_by_short_name` returns `None` when the name is not in the configured set, so `SpecService.sync_spec`'s new validation step surfaces `UnknownTsgError` exactly as production does.

```python
from doc3gpp.models.tsg import Tsg


class _StubTsgRepo:
    def __init__(
        self,
        last_spec_sync: datetime | None = None,
        short_names: set[str] | None = None,
    ) -> None:
        self._last = last_spec_sync
        self._known = {n.upper() for n in (short_names or set())}
        self.spec_sync_calls: list = []

    def get_by_short_name(self, short_name: str):
        if short_name.upper() not in self._known:
            return None
        return Tsg(
            tsg_name=short_name.upper(),
            short_name=short_name.upper(),
            description="stub",
            url=None,
            meeting_last_sync=None,
            spec_last_sync=self._last,
        )

    def update_spec_last_sync(self, short_name: str, synced_at) -> bool:
        self.spec_sync_calls.append(synced_at)
        return True
    # Superseded — the `_StubTsgRepo.spec_last_sync` /
    # `update_spec_last_sync` plumbing was removed in the per-spec
    # skip rule plan
    # ([docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md](2026-08-13-per-spec-skip-rule.md));
    # the new per-spec skip uses `Spec.last_synced_at` and the TSG
    # stub no longer carries a spec stamp.
```

`test_sync_spec_stored_row_unchanged` (in this task's test list) uses `tsg_repo = _StubTsgRepo(short_names={"R5"})` — the new `short_names` kwarg is additive. All 11 existing call sites that pass `last_spec_sync=...` keep working because the new parameter is keyword-only and defaults to `None`.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/unit/test_spec_service.py -v -k "falls_back or empty or unknown or stored_row_unchanged"`
Expected: FAIL — `ImportError: cannot import name 'SpecUnknownOnUpstreamError'`.

- [ ] **Step 3: Add the new exception classes in `services/spec_service.py`**

In `src/doc3gpp/services/spec_service.py`, append after the existing module-level helper `_format_duration` and before the existing `SyncOutcome` / `SpecProgressFn` block (search for the last import line and the class definition; the exact location depends on the current file head). Add:

```python
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


class UnknownTsgError(ValueError):
    """Raised when a freshly fetched spec's normalised TSG is not in ``tsgs``."""

    def __init__(self, spec_id: str, short_name: str, long_name: str) -> None:
        super().__init__(
            f"spec {spec_id!r} has unknown TSG short name {short_name!r} "
            f"(normalised from {long_name!r}); run 'doc3gpp tsg seed' or "
            f"'doc3gpp tsg list' to inspect the reference table"
        )
        self.spec_id = spec_id
        self.short_name = short_name
        self.long_name = long_name
```

- [ ] **Step 4: Refactor `SpecService.sync_spec` to add the DynaReport fallback**

In `src/doc3gpp/services/spec_service.py`, replace the existing `sync_spec` method body (lines 156-204) with:

```python
    def sync_spec(
        self,
        spec_id: str,
        *,
        force: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
        """Refresh a single spec's detail page + versions.

        Looks ``spec_id`` up in the DB to recover its TSG. When the
        row is missing, fetches the DynaReport detail page directly,
        parses the bootstrap header (``title`` / ``type`` / primary
        responsible group), validates the group against ``tsgs``, and
        funnels the freshly-built :class:`Spec` through the same
        ``_sync_one_spec`` pipeline as the stored-row path.

        Honours the per-TSG ``tsgs.spec_last_sync`` skip rule unless
        ``force``, and stamps it again on success — identical to
        :meth:`sync`. A spec that is unknown on the upstream raises
        :class:`SpecUnknownOnUpstreamError`; a normalised TSG that is
        not in the seeded reference table raises
        :class:`UnknownTsgError`.
        # Superseded — the per-TSG skip was replaced by a per-spec
        # `specs.last_synced_at` skip in the per-spec skip rule plan
        # ([docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md](2026-08-13-per-spec-skip-rule.md));
        # the `tsgs.spec_last_sync` and the TSG-repo stamp are gone.
        """
        spec = self._repository.get(spec_id)
        if spec is None:
            spec = self._bootstrap_spec_from_dynareport(spec_id)

        canonical = spec.tsg.upper() if spec.tsg else ""
        if not force:
            skipped = self._is_sync_skipped(
                canonical, f"{spec.spec_id} (TSG {canonical})"
            )
            if skipped is not None:
                return skipped

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
        # Superseded — the per-TSG stamp is gone; the per-spec
        # `specs.last_synced_at` is stamped by the per-worker
        # pipeline.

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
        label) and :class:`UnknownTsgError` when the normalised
        short name is not in the seeded ``tsgs`` table.
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

        if self._tsg_repository is not None:
            tsg_record = self._tsg_repository.get_by_short_name(short_name)
            if tsg_record is None:
                raise UnknownTsgError(spec_id, short_name, header.tsg_long_name)

        return Spec(
            spec_id=spec_id,
            type=header.type,
            title=header.title,
            tsg=short_name,
        )
```

Add the new imports to the top of `src/doc3gpp/services/spec_service.py`:

```python
from doc3gpp.parsers.spec_parser import (
    normalise_tsg_long_name,
    parse_dynareport_header,
)
from doc3gpp.scraping.spec_source import fetch_dynareport_detail
```

(If the existing `from doc3gpp.parsers.spec_parser import ...` / `from doc3gpp.scraping.spec_source import ...` lines exist already, extend them; the existing imports at the top of `spec_service.py` are `from doc3gpp.parsers.spec_release import ...` and `from doc3gpp.scraping.client import ScraperClient` — the parser and source imports are new.)

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `pytest tests/unit/test_spec_service.py -v`
Expected: PASS (all tests in the file, including the new ones; the existing tests in this file should still pass because the stored-row path is unchanged).

- [ ] **Step 6: Run ruff + commit**

Run: `ruff check src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py`
Expected: clean.

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "feat(spec): bootstrap spec header from DynaReport on sync_spec miss"
```

---

### Task 4: CLI — drop pre-flight, wrap `sync_spec` with the new error types

**Files:**
- Modify: `src/doc3gpp/cli.py:3905-3921` (the `--spec-id` branch in `spec_sync`)
- Test: `tests/integration/test_spec_cli.py`

**Interfaces:**
- Consumes: `SpecUnknownOnUpstreamError`, `UnknownTsgError` from Task 3 (import from `doc3gpp.services.spec_service`); the existing `tqdm` / `service.sync_spec` call at `cli.py:3911-3919`.
- Produces: a `try / except` around the `service.sync_spec(...)` call that re-raises both new error types as `typer.BadParameter`.

- [ ] **Step 1: Add the failing tests**

Append to `tests/integration/test_spec_cli.py`:

```python
from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
    UnknownTsgError,
)
from doc3gpp.services.tsg_service import TsgService
from doc3gpp.storage.db.migrate import create_schema
from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository


def test_spec_sync_spec_id_dynareport_404_bad_parameter(sqlite_env, monkeypatch) -> None:
    """``spec sync --spec-id`` of a missing spec surfaces BadParameter.

    Pre-flights with an empty `specs` table, mocks `service.sync_spec`
    to raise `SpecUnknownOnUpstreamError`, and asserts the CLI maps it
    to `typer.BadParameter` carrying the upstream message.
    """
    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()

    svc = MagicMock()
    svc.sync_spec.side_effect = SpecUnknownOnUpstreamError(
        "38.523-1", "missing fields: title, type, tsg_long_name"
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())

    result = runner.invoke(app, ["spec", "sync", "--spec-id", "38.523-1"])
    assert result.exit_code != 0
    assert "38.523-1" in result.stdout
    assert "unknown on the 3GPP DynaReport upstream" in result.stdout


def test_spec_sync_spec_id_unknown_tsg_bad_parameter(sqlite_env, monkeypatch) -> None:
    """``spec sync --spec-id`` of a spec whose TSG is not in tsgs surfaces BadParameter."""
    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()

    svc = MagicMock()
    svc.sync_spec.side_effect = UnknownTsgError("38.523-1", "R5", "RAN 5")
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())

    result = runner.invoke(app, ["spec", "sync", "--spec-id", "38.523-1"])
    assert result.exit_code != 0
    assert "unknown TSG short name" in result.stdout
    assert "R5" in result.stdout
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/integration/test_spec_cli.py -v -k "dynareport_404 or unknown_tsg_bad_parameter"`
Expected: FAIL — the CLI's pre-flight `service.get(spec_id)` returns
`None` (the `svc` is a plain `MagicMock`) and raises
`typer.BadParameter("Unknown spec id '38.523-1'. Run 'doc3gpp spec
sync --tsg <tsg>' first.")`. That message does not contain "unknown
on the 3GPP DynaReport upstream", so the new tests fail before
`service.sync_spec` is ever reached — which is the desired RED state.

- [ ] **Step 3: Drop the pre-flight and add the `try / except` in `cli.py`**

In `src/doc3gpp/cli.py`, add the new import alongside the other
service-layer imports (the file already imports several symbols from
`doc3gpp.services.*`; add this near them):

```python
from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
    UnknownTsgError,
)
```

Replace the body of the `--spec-id` branch in `spec_sync` (lines
3905-3922 of `src/doc3gpp/cli.py` — the entire `if spec_id is not
None:` block, currently `service.get(spec_id)` + early `raise
typer.BadParameter` + tqdm setup + `service.sync_spec(...)` + `bar.close()`) with:

```python
    if spec_id is not None:
        from tqdm import tqdm

        bar = tqdm(total=1, desc=f"spec {spec_id}", unit="spec", dynamic_ncols=True)

        def _on_progress(event: str, data: dict) -> None:
            if event == "spec_done":
                bar.update(1)

        try:
            outcome = service.sync_spec(
                spec_id, force=force, on_progress=_on_progress
            )
        except SpecUnknownOnUpstreamError as exc:
            bar.close()
            raise typer.BadParameter(str(exc)) from exc
        except UnknownTsgError as exc:
            bar.close()
            raise typer.BadParameter(str(exc)) from exc
        bar.close()
        typer.echo(outcome.reason)
        return
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest tests/integration/test_spec_cli.py -v -k "dynareport_404 or unknown_tsg_bad_parameter"`
Expected: PASS.

Run the full spec CLI file: `pytest tests/integration/test_spec_cli.py -v`
Expected: PASS (the existing tests still pass — the stored-row path is unchanged; the `test_spec_sync_spec_id_stored_row_unchanged`-style regression is implicit because the existing `test_spec_sync_*` tests exercise the `--tsg` branch, which is untouched).

- [ ] **Step 5: Run ruff + commit**

Run: `ruff check src/doc3gpp/cli.py tests/integration/test_spec_cli.py`
Expected: clean.

```bash
git add src/doc3gpp/cli.py tests/integration/test_spec_cli.py
git commit -m "feat(spec): map new sync_spec errors to BadParameter in CLI"
```

---

### Task 5: Web — register the new error types in `web/errors.py`

**Files:**
- Modify: `src/doc3gpp/web/errors.py:1-90` (imports), `src/doc3gpp/web/errors.py:95-105` (`_MCP_RESOURCE_BY_EXC`), `src/doc3gpp/web/errors.py:131-143` (`_ERROR_SLUGS`), `src/doc3gpp/web/errors.py:146-155` (`_STATUS_BY_EXC`)
- Test: `tests/integration/test_mcp_end_to_end.py` (existing test infrastructure; add a regression assertion)

**Interfaces:**
- Consumes: `SpecUnknownOnUpstreamError`, `UnknownTsgError` from Task 3.
- Produces: two new rows in each of the three existing lookup tables (mirrors `SpecNotFoundError`'s structure).

- [ ] **Step 1: Add the imports**

In `src/doc3gpp/web/errors.py`, add the new imports alongside the existing `from doc3gpp...` lines (search for `SpecNotFoundError` — the existing import list varies; add the new ones to the same import statement or as a new line near it):

```python
from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
    UnknownTsgError,
)
```

- [ ] **Step 2: Add the registry rows**

In `src/doc3gpp/web/errors.py`, append the new rows in each of the three dicts (after the existing `SpecNotFoundError` row in each):

```python
_MCP_RESOURCE_BY_EXC = {
    ...,
    SpecNotFoundError: ("spec", MCP_CODE_NOT_FOUND),
    SpecUnknownOnUpstreamError: ("spec", MCP_CODE_NOT_FOUND),
    UnknownTsgError: ("spec", MCP_CODE_INVALID_PARAMS),
}
```

```python
_ERROR_SLUGS = {
    ...,
    SpecNotFoundError: "spec_not_found",
    SpecUnknownOnUpstreamError: "spec_unknown_on_upstream",
    UnknownTsgError: "unknown_tsg",
}
```

```python
_STATUS_BY_EXC = {
    ...,
    SpecNotFoundError: 404,
    SpecUnknownOnUpstreamError: 404,
    UnknownTsgError: 400,
}
```

(If `UnknownTsgError` is added with HTTP 400, also add a corresponding `MCP_CODE_INVALID_PARAMS` mapping in `_MCP_RESOURCE_BY_EXC` — already specified above.)

- [ ] **Step 3: Verify the existing test still passes**

Run: `pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS (the existing tests exercise `SpecNotFoundError`; the new rows are additive).

Run: `pytest tests/integration/test_web_errors.py -v` (if that file exists; otherwise skip)
Expected: PASS.

- [ ] **Step 4: Add a focused regression test**

Append to `tests/integration/test_mcp_end_to_end.py`:

```python
from doc3gpp.services.spec_service import (
    SpecUnknownOnUpstreamError,
    UnknownTsgError,
)
from doc3gpp.web.errors import map_domain_error, map_mcp_error


def test_web_errors_maps_new_spec_errors() -> None:
    resp_unknown = map_domain_error(
        SpecUnknownOnUpstreamError("38.523-1", "missing fields: title, type")
    )
    assert resp_unknown.status_code == 404
    body_unknown = json.loads(resp_unknown.body)
    assert body_unknown["error"] == "spec_unknown_on_upstream"
    assert "38.523-1" in body_unknown["detail"]

    resp_tsg = map_domain_error(UnknownTsgError("38.523-1", "R5", "RAN 5"))
    assert resp_tsg.status_code == 400
    body_tsg = json.loads(resp_tsg.body)
    assert body_tsg["error"] == "unknown_tsg"

    mcp1 = map_mcp_error(SpecUnknownOnUpstreamError("38.523-1", "missing"))
    assert mcp1 is not None
    code, _msg, data = mcp1
    assert code == -32004
    assert data["error"] == "spec_unknown_on_upstream"
    assert data["resource"] == "spec"

    mcp2 = map_mcp_error(UnknownTsgError("38.523-1", "R5", "RAN 5"))
    assert mcp2 is not None
    code2, _msg2, data2 = mcp2
    assert code2 == -32602
    assert data2["error"] == "unknown_tsg"
```

Add the `import json` at the top of the file if not already imported.

- [ ] **Step 5: Run the new test + the full suite**

Run: `pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS.

Run: `./scripts/test_sqlite.sh`
Expected: PASS (the full sqlite suite).

- [ ] **Step 6: Run ruff + commit**

Run: `ruff check src/doc3gpp/web/errors.py tests/integration/test_mcp_end_to_end.py`
Expected: clean.

```bash
git add src/doc3gpp/web/errors.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(web): map new spec sync errors to HTTP 404/400 and MCP codes"
```

---

### Task 6: Docs — update `AGENTS.md` and `docs/cli.md`

**Files:**
- Modify: `AGENTS.md` (the `## Where to look` row for spec sync; the `## Architecture boundaries` description for the workflow line that mentions `SpecService.sync_spec`).
- Modify: `docs/cli.md` (the `doc3gpp spec sync --spec-id` section: the DynaReport-direct-fetch behaviour, the new error messages).

**Interfaces:**
- Consumes: nothing — pure documentation.
- Produces: updated prose that matches the new behaviour.

- [ ] **Step 1: Update `AGENTS.md`**

In `AGENTS.md`, find the row in the "Where to look" table that mentions `SpecService.sync` / `SpecService.sync_spec` (the entry currently reads "Add a spec list / detail source"). The new behaviour is that `SpecService.sync_spec` now bootstraps a missing spec from the DynaReport detail page before calling `_sync_one_spec`; update the prose in the `SpecService.sync` row (the existing one near "spec sync --spec-id") to mention the fallback.

In the same file, the "Workflows in one line" bullet for `doc3gpp spec sync --spec-id` (currently describes only the stored-row path) needs a second sentence covering the DynaReport-direct-fetch fallback.

Use this exact prose (drop into the existing bullet):

> `doc3gpp spec sync --spec-id <id>` → `SpecService.sync_spec` →
> look up in the local `specs` table; if missing, fetch
> `https://www.3gpp.org/DynaReport/{no_dot}.htm`, parse
> `#titleVal` / `#typeVal` / `#PrimaryResponsibleGroupLbl`, normalise
> the group to a seeded `tsgs.short_name`, build an in-memory
> `Spec`, and hand off to the same `_sync_one_spec` pipeline as the
> stored-row path. A 404 or unrecognised group label surfaces as
> `typer.BadParameter("spec {id!r} is unknown on the 3GPP DynaReport
> upstream")`; an unknown seeded-TSG short name surfaces as
> `typer.BadParameter("spec {id!r} has unknown TSG short name
> {short!r} (normalised from {long!r})")`.

- [ ] **Step 2: Update `docs/cli.md`**

In `docs/cli.md`, find the `doc3gpp spec sync` section and add (or update) the paragraph that documents the `--spec-id` flag. Drop in this exact prose:

> `--spec-id <id>` syncs a single spec by its dotted id. When the
> spec is already in the local `specs` table, the existing stored-row
> path runs. When the spec is missing, `SpecService.sync_spec` fetches
> the 3GPP DynaReport detail page
> (`https://www.3gpp.org/DynaReport/{no_dot}.htm`), parses the title,
> type, and primary responsible group, normalises the group to a
> seeded `tsgs.short_name` (e.g. `RAN 5` → `R5`, `CT 1` → `C1`,
> `SA WG2` → `S2`; legacy groups like `RAN AH1` are rejected), and
> inserts the row. A 404 or unparseable detail page surfaces as
> `typer.BadParameter` with a `Spec unknown on the 3GPP DynaReport
> upstream` message; an unknown normalised TSG surfaces as
> `typer.BadParameter` with an `unknown TSG short name` message.
> `--force` bypasses the per-TSG skip rule in both paths.

- [ ] **Step 3: Commit**

```bash
git add AGENTS.md docs/cli.md
git commit -m "docs: document spec sync DynaReport direct fetch"
```

---

### Task 7: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full sqlite test suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS (all unit + integration tests; the full suite is the project-level gate).

- [ ] **Step 2: Run ruff**

Run: `ruff check .`
Expected: clean.

- [ ] **Step 3: Run the new test subset one more time to confirm the vertical slice is green**

Run: `pytest tests/unit/test_spec_parser.py tests/unit/test_spec_source.py tests/unit/test_spec_service.py tests/integration/test_spec_cli.py tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS.

- [ ] **Step 4: Confirm no stray TODOs / placeholders**

Run: `grep -rn "TODO\|FIXME\|XXX" src/doc3gpp/parsers/spec_parser.py src/doc3gpp/scraping/spec_source.py src/doc3gpp/services/spec_service.py src/doc3gpp/cli.py src/doc3gpp/web/errors.py`
Expected: no output (the global-constraint rule "no comments unless non-obvious" still applies; any TODO would be a violation).

- [ ] **Step 5: Commit if any verification-only cleanups happened**

If `ruff` or the test suite required a final small edit, commit it now with a `chore(spec-sync): final verification cleanup` message. Otherwise, do nothing — the plan is complete.

---

## Brief Defects Caught and Fixed During Execution

The original plan briefs contained **seven** real defects that
implementers caught in self-test, fixed in place, and validated with
reviewers during execution of this plan. This section is annotated so
that a future re-run of this plan from scratch does not trip on the
same bugs.

### Task 3 brief (commit `c699d25`) — 3 defects

1. **Missing mocks for `fetch_spec_detail` / `fetch_etsi_pdf_text` /
   `fetch_cr_list` in the bootstrap path tests.** The bootstrap path
   in `_bootstrap_spec_from_dynareport` flows through
   `_sync_one_spec`, which calls those three fetchers. The brief
   only stubbed `fetch_dynareport_detail`, not the downstream ones,
   so the new tests would have hit live 3gpp.org and returned 53
   versions in ~21 seconds — failing the `len == 1` assertion. The
   implementer added `monkeypatch.setattr` calls for the three
   downstream fetchers and shared a small `_stub_followups` helper
   across the new tests. Reviewer confirmed the tests now run in
   milliseconds.

2. **The "_StubTsgRepo replacement is backward-compatible" claim
   was wrong.** The brief's `_StubTsgRepo` defaulted `_known` to an
   empty set, so `get_by_short_name` returned `None` for every
   name. That silently broke `_is_sync_skipped`, which reads
   `tsg_record.spec_last_sync` (now always `None`, so the skip rule
   would never trigger). The implementer (a) added a
   `short_names: set[str] | None = None` kwarg so existing call
   sites keep working, and (b) seeded `short_names={"R5"}` in three
   skip-rule tests so the rule still triggers as written.
   Reviewer confirmed pre-existing skip-rule tests still pass.
   **Superseded** — the entire `_is_sync_skipped` skip-rule machinery
   and the `_StubTsgRepo.spec_last_sync` plumbing were removed in
   the per-spec skip rule plan
   ([`docs/superpowers/plans/2026-08-13-per-spec-skip-rule.md`](2026-08-13-per-spec-skip-rule.md));
   the per-spec skip is now enforced against
   `Spec.last_synced_at`.

3. **`test_sync_spec_unknown_spec_raises` was testing the OLD
   pre-flight `ValueError` behaviour.** The refactor changes the
   unknown-spec path to raise `UnknownTsgError`. The implementer
   rewrote the test to monkeypatch `fetch_dynareport_detail` to
   raise a valid body and then mock the `_tsg_repository` to return
   `None` from `get_by_short_name`, so the path
   `fetch → parse → normalise → validate → raise UnknownTsgError`
   is exercised end-to-end. Reviewer confirmed the new shape.

### Task 4 brief (commit `33b82fd`) — 3 defects

1. **Both new integration tests used `assert ... in
   result.stdout`.** `typer.testing.CliRunner.result.output`
   captures both stdout and stderr; `result.stdout` reads only the
   `BytesLiteral` half of the buffer and is byte-truncated in
   practice. The implementer corrected both assertions to
   `result.output`. Reviewer confirmed the tests now hit the
   expected exit code + message pair.

2. **Each new test re-ran the same `from doc3gpp.services.spec_service
   import (...)` block.** Reviewer's diff-only check flagged the
   duplication; the implementer left the in-function imports as-is
   (each test stays self-contained). No behaviour change; the
   duplication is harmless.

3. **`test_spec_sync_spec_id_unknown_raises` was a leftover from
   the pre-flight era.** Once the CLI droped the pre-flight block,
   the test asserted the WRONG error message (it expected
   `Unknown spec id '38.523-1'. Run 'doc3gpp spec sync --tsg
   <tsg>' first.`, the pre-flight message, but the new CLI returns
   the DynaReport message instead). The implementer deleted the
   obsolete test — the new
   `test_spec_sync_spec_id_unknown_tsg_bad_parameter` covers the
   same behavioural surface from the correct angle. Reviewer
   confirmed there is no regression in the error mapping.

### Task 6 brief (commit `e2e760b`, fix round 1) — 1 defect

1. **Quoted `typer.BadParameter` format strings were truncated
   versions of the actual messages.** The brief gave two examples:

   > `typer.BadParameter("spec {id!r} is unknown on the 3GPP
   > DynaReport upstream")` and
   > `typer.BadParameter("spec {id!r} has unknown TSG short name
   > {short!r} (normalised from {long!r})")`

   but the implementation uses different parameter names
   (`spec_id`, `reason`, `short_name`, `long_name`) and includes
   the trailing phrases `"; nothing to sync"` and `"run 'doc3gpp
   tsg seed' or 'doc3gpp tsg list' to inspect the reference
   table"`. The first reviewer flagged the byte-mismatch; the
   implementer rewrote both doc snippets (in `AGENTS.md` and
   `docs/cli.md`) to quote the f-string bodies verbatim, including
   the parenthetical reason and the trailing hints. Reviewer
   confirmed byte-equal copy on a `grep -nF` diff.

### Summary

- **Total defects caught across 6 reviews: 7** (Task 3: 3, Task 4: 3,
  Task 6: 1).
- All defects were caught by implementers in self-test, fixed in
  the same commit, and validated by subagent reviewers before
  progressing.
- Every defect was the result of a brief-spec drift (or in Task
  4 case 3, a leftover from the pre-flight era) — not a flaw in
  the original design.
- Reviewers should expect these specific shapes on re-run: in
  Task 3, demand the downstream fetcher mocks; in Task 4, scan
  for `result.stdout`, double imports (in-function
  `from doc3gpp.services.spec_service import ...` per test is
  acceptable but flag it as a smell), and the obsolete
  `unknown_raises` test name; in Task 6, byte-diff the quoted
  format strings against the actual `__init__` bodies in
  `services/spec_service.py` before approving.
