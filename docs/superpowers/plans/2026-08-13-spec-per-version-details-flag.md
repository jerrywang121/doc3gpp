# Spec Sync `--per-version-details` Flag Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--per-version-details` / `per_version_details` boolean flag to `spec sync` (CLI), the web spec detail page Sync form, the `POST /jobs/sync/specs` job route, the `_sync_specs` job handler, and the MCP `sync_specs` tool. When `False` (the new default) the per-version follow-up HTTP fetches (ETSI PDF link + 3GPP CR list) are skipped. Existing stored `pdf_url` and `crs` values on `spec_versions` rows are preserved on a flag-OFF re-sync.

**Architecture:** A single boolean is plumbed through `SpecService.sync` / `sync_spec` / `_sync_one_spec` / `_fetch_followups_concurrently`. The follow-up submission is gated at the dispatch site (`_fetch_followups_concurrently` early-returns when the flag is `False`). A renamed helper `_backfill_followup_fields` always copies the stored `pdf_url` and `crs` from the DB onto freshly-parsed versions before `upsert_versions` so a flag-OFF re-sync cannot clobber existing values. CLI, HTTP route, job handler, and MCP tool each gain a `per_version_details: bool = False` arg/field and forward it into the service call / `job.params`.

**Tech Stack:** Python 3.10+, Typer, FastAPI, SQLAlchemy 2.0, Jinja2, HTMX, plain JS, Pydantic v2.

## Global Constraints

- Layered architecture is strict in `src/doc3gpp/`: `models/` never leaks ORM attrs; `services/` reaches storage only through `repository/` Protocols; `scraping/` is network-only; `parsers/` is parse-only.
- The flag is a per-call knob, **not** a `Settings` field. The CLI flag, the JSON body field, the MCP tool arg, the `job.params` key, and the service keyword arg all use the same name: `per_version_details` (snake_case).
- Default is `False` everywhere. This is a behaviour change for existing callers: re-synced specs stop doing the per-version HTTP follow-ups by default. Existing stored `pdf_url` / `crs` rows are preserved.
- The detail-page fetch itself is **always** re-run; only the two `fetch_*` calls in `_fetch_followups_concurrently` are gated.
- No comments in code unless the surrounding block documents non-obvious behavior (match existing style).
- Run `ruff check .` and the full sqlite suite (`./scripts/test_sqlite.sh`) before completion.
- Existing tests that monkeypatch `fetch_etsi_pdf_text` / `fetch_cr_list` and assert the mocks were called must be updated to pass `per_version_details=True` (otherwise the new default would swallow the follow-up call). Tests that only assert the spec header / `last_synced_at` / version count are unaffected.

---

### Task 1: Service — extend `_backfill_pdf_urls` to also back-fill `crs` and rename to `_backfill_followup_fields`

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:332-347`
- Test: `tests/unit/test_spec_service.py`

**Interfaces:**
- Produces: `SpecService._backfill_followup_fields(self, versions: list[SpecVersion]) -> None` — copies stored `pdf_url` AND `crs` from the DB onto freshly-parsed `SpecVersion` objects whose corresponding fields are `None`.

- [ ] **Step 1: Add failing test** — append to `tests/unit/test_spec_service.py`:

```python
def test_backfill_followup_fields_copies_stored_pdf_and_crs(monkeypatch) -> None:
    """``_backfill_followup_fields`` copies both ``pdf_url`` and ``crs``
    from persisted ``spec_versions`` rows onto freshly-parsed versions
    that arrived from ``parse_spec_detail`` with ``None`` for both."""
    from datetime import date

    from doc3gpp.models.spec import Spec, SpecVersion

    repo = _StubSpecRepo()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="X", tsg="R5"))
    repo.upsert_versions([
        SpecVersion(
            spec_id="36.579-5",
            version="18.3.0",
            ftp_url="https://example/x.zip",
            upload_date=date(2026, 1, 1),
            pdf_url="https://etsi.example/x.pdf",
            crs="R5-260001,R5-260002",
        ),
    ])

    svc = SpecService(repo)
    fresh = [
        SpecVersion(
            spec_id="36.579-5",
            version="18.3.0",
            ftp_url="https://example/x.zip",
            upload_date=date(2026, 1, 1),
        ),
    ]
    svc._backfill_followup_fields(fresh)
    assert fresh[0].pdf_url == "https://etsi.example/x.pdf"
    assert fresh[0].crs == "R5-260001,R5-260002"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_spec_service.py::test_backfill_followup_fields_copies_stored_pdf_and_crs -v`
Expected: FAIL — `_backfill_followup_fields` does not exist on `SpecService`.

- [ ] **Step 3: Rename and extend the helper** — in `spec_service.py`, replace the body of `_backfill_pdf_urls` (lines 332-347) with:

```python
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
```

- [ ] **Step 4: Find and replace the call site** — in `spec_service.py` line 310, change `self._backfill_pdf_urls(versions)` to `self._backfill_followup_fields(versions)`.

- [ ] **Step 5: Run the new test to verify it passes**

Run: `pytest tests/unit/test_spec_service.py::test_backfill_followup_fields_copies_stored_pdf_and_crs -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "refactor(spec): rename _backfill_pdf_urls to _backfill_followup_fields; cover crs"
```

---

### Task 2: Service — add `per_version_details` keyword to `SpecService.sync`

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:82-171`
- Test: `tests/unit/test_spec_service.py`

**Interfaces:**
- Produces: `SpecService.sync(self, tsg, *, force=False, per_version_details=False, on_progress=None) -> SyncOutcome`. Threads `per_version_details` into each `executor.submit(self._sync_one_spec, ...)` call.

- [ ] **Step 1: Add failing test** — append to `tests/unit/test_spec_service.py`:

```python
def test_sync_skips_followups_when_per_version_details_false(monkeypatch) -> None:
    """A sync with the default ``per_version_details=False`` must NOT
    invoke the ETSI PDF or CR list fetchers."""
    etsi_calls: list[int] = []
    cr_calls: list[int] = []

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: (etsi_calls.append(wki) or "<html><a href='x.pdf'>d</a></html>"),
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: (cr_calls.append(version_id) or "<html><a id='wgTdocDetailsLink'>R5-1</a></html>"),
    )

    svc = SpecService(_StubSpecRepo())
    outcome = svc.sync("R5")
    assert outcome.status == "synced"
    assert etsi_calls == [], "ETSI fetch must be skipped when per_version_details=False"
    assert cr_calls == [], "CR list fetch must be skipped when per_version_details=False"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_spec_service.py::test_sync_skips_followups_when_per_version_details_false -v`
Expected: FAIL — `etsi_calls` and `cr_calls` are non-empty (current code always fetches).

- [ ] **Step 3: Add the keyword to `sync`** — in `spec_service.py:82-88`, change the signature to:

```python
    def sync(
        self,
        tsg: str,
        *,
        force: bool = False,
        per_version_details: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
```

and update the docstring's "Follow-ups" mention to:

```
        and the conditional ETSI / CR follow-ups (skipped when
        ``per_version_details`` is ``False``), and the upsert
        inside each worker.
```

- [ ] **Step 4: Thread it into the per-worker submission** — in the `executor.submit(self._sync_one_spec, ...)` call inside `sync` (around line 139), add `per_version_details=per_version_details` as the trailing keyword arg.

- [ ] **Step 5: Run the new test to verify it passes**

Run: `pytest tests/unit/test_spec_service.py::test_sync_skips_followups_when_per_version_details_false -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "feat(spec): add per_version_details=False to SpecService.sync"
```

---

### Task 3: Service — thread `per_version_details` into `_sync_one_spec` and `_fetch_followups_concurrently`

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:275-330` and `349-435`
- Test: `tests/unit/test_spec_service.py`

**Interfaces:**
- Produces: `_sync_one_spec(self, spec, canonical, followup_executor, client, per_version_details=False) -> int`. Calls `_fetch_followups_concurrently(versions, followup_executor, client, per_version_details)`.
- Produces: `_fetch_followups_concurrently(self, versions, executor, client, per_version_details) -> None` — early-returns when `per_version_details` is `False`; otherwise submits the existing per-version futures unchanged.

- [ ] **Step 1: Add failing test** — append to `tests/unit/test_spec_service.py`:

```python
def test_sync_preserves_stored_crs_when_per_version_details_false(monkeypatch) -> None:
    """A flag-OFF re-sync preserves the stored ``crs`` and ``pdf_url``
    even though ``parse_spec_detail`` returns ``None`` for both."""
    from datetime import date

    from doc3gpp.models.spec import Spec, SpecVersion

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: LIST_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: pytest.fail("ETSI must not be called"),
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: pytest.fail("CR list must not be called"),
    )

    repo = _StubSpecRepo()
    recent_upload = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    detail_html_recent = DETAIL_HTML.replace("2025-06-01", recent_upload)
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: detail_html_recent,
    )

    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    repo.upsert_versions([
        SpecVersion(
            spec_id="36.579-5",
            version="18.3.0",
            ftp_url="https://www.3gpp.org/ftp/Specs/archive/36_series/36.579-5/36579-5-i30.zip",
            upload_date=date.today(),
            pdf_url="https://etsi.example/x.pdf",
            crs="R5-260001,R5-260002",
        ),
    ])

    svc = SpecService(repo)
    svc.sync("R5")  # no per_version_details → default False
    persisted = repo.list_versions("36.579-5")
    assert persisted[0].pdf_url == "https://etsi.example/x.pdf"
    assert persisted[0].crs == "R5-260001,R5-260002"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_spec_service.py::test_sync_preserves_stored_crs_when_per_version_details_false -v`
Expected: FAIL — `persisted[0].pdf_url` is `None` and `persisted[0].crs` is `None` (current code clobbers them via the always-on follow-up path: the back-fill runs but the follow-up submission overwrites with the freshly-fetched values; with the back-fill in Task 1 plus the new gate in this task, both stay preserved).

- [ ] **Step 3: Extend `_sync_one_spec`** — in `spec_service.py:275-330`, add `per_version_details: bool = False` to the signature and change the `_backfill_followup_fields` + `_fetch_followups_concurrently` calls to pass it:

```python
    def _sync_one_spec(
        self,
        spec: Spec,
        canonical: str,
        followup_executor: ThreadPoolExecutor,
        client: ScraperClient,
        per_version_details: bool = False,
    ) -> int:
        ...
        self._backfill_followup_fields(versions)
        self._fetch_followups_concurrently(versions, followup_executor, client, per_version_details)
        ...
```

- [ ] **Step 4: Add the gate to `_fetch_followups_concurrently`** — in `spec_service.py:349-369`, change the signature to add `per_version_details: bool = False` and add the early return at the top of the body:

```python
    def _fetch_followups_concurrently(
        self,
        versions: list[SpecVersion],
        executor: ThreadPoolExecutor,
        client: ScraperClient,
        per_version_details: bool = False,
    ) -> None:
        """..."""
        if not per_version_details:
            return
        followup_futures = []
        ...
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `pytest tests/unit/test_spec_service.py::test_sync_preserves_stored_crs_when_per_version_details_false -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "feat(spec): gate per-version follow-up HTTP by per_version_details"
```

---

### Task 4: Service — add `per_version_details` keyword to `SpecService.sync_spec`

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:173-226`
- Test: `tests/unit/test_spec_service.py`

**Interfaces:**
- Produces: `SpecService.sync_spec(self, spec_id, *, force=False, per_version_details=False, on_progress=None) -> SyncOutcome`. Threads the flag into the single `_sync_one_spec` call.

- [ ] **Step 1: Add failing test** — append to `tests/unit/test_spec_service.py`:

```python
def test_sync_spec_skips_followups_when_per_version_details_false(monkeypatch) -> None:
    """The single-spec path honours ``per_version_details=False``."""
    from doc3gpp.models.spec import Spec

    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_list",
        lambda tsg, **k: pytest.fail("list must not be called"),
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: pytest.fail("ETSI must not be called"),
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: pytest.fail("CR list must not be called"),
    )

    repo = _StubSpecRepo()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    svc = SpecService(repo)
    outcome = svc.sync_spec("36.579-5")
    assert outcome.status == "synced"
    assert outcome.synced_count == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_spec_service.py::test_sync_spec_skips_followups_when_per_version_details_false -v`
Expected: FAIL — current code calls the follow-up fetches.

- [ ] **Step 3: Add the keyword to `sync_spec`** — in `spec_service.py:173-179`, change the signature to:

```python
    def sync_spec(
        self,
        spec_id: str,
        *,
        force: bool = False,
        per_version_details: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
```

- [ ] **Step 4: Thread it into the `_sync_one_spec` call** — in the body of `sync_spec` (around line 215), pass `per_version_details=per_version_details` as the trailing keyword arg to `_sync_one_spec`.

- [ ] **Step 5: Run the new test to verify it passes**

Run: `pytest tests/unit/test_spec_service.py::test_sync_spec_skips_followups_when_per_version_details_false -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "feat(spec): add per_version_details=False to SpecService.sync_spec"
```

---

### Task 5: Update existing unit tests that depended on the old "always fetch" default

**Files:**
- Modify: `tests/unit/test_spec_service.py`

The four pre-existing tests that monkeypatch `fetch_etsi_pdf_text` / `fetch_cr_list` and assert the mocks were called must now opt in via `per_version_details=True`. Tests that only check the spec header / version count are unaffected.

- [ ] **Step 1: Find every existing test that calls the service with follow-up assertions** — in `tests/unit/test_spec_service.py`, search for `fetch_etsi_pdf_text` and `fetch_cr_list`. Tests that read the `etsi_calls` / `cr_calls` lists or assert a follow-up ran are the targets.

- [ ] **Step 2: Update `test_sync_smoke`** — find the `svc = SpecService(repo)` + `svc.sync("R5")` lines and change the `sync` call to `svc.sync("R5", per_version_details=True)`. The test's assertions on `outcome.status`, `synced_count`, `version_count` are unchanged.

- [ ] **Step 3: Update `test_sync_skips_etsi_fetch_when_pdf_url_already_persisted`** — change both `svc.sync("R5")` calls to `svc.sync("R5", per_version_details=True)`. The test's intent (asserting that `etsi_calls == []` after the second sync because the back-fill restored the `pdf_url`) is unchanged.

- [ ] **Step 4: Update `test_sync_skips_etsi_fetch_for_stale_versions`** — change the single `svc.sync("R5")` call to `svc.sync("R5", per_version_details=True)`. Same intent.

- [ ] **Step 5: Update any other test that exercises the follow-up path with a default-flag sync** — scan again for `svc.sync(` and `svc.sync_spec(` calls that don't pass `per_version_details=`. If any test asserts the follow-up mocks were called, add `per_version_details=True` to that call.

- [ ] **Step 6: Run the full `test_spec_service.py` to verify everything passes**

Run: `pytest tests/unit/test_spec_service.py -v`
Expected: PASS for every test in the module.

- [ ] **Step 7: Commit**

```bash
git add tests/unit/test_spec_service.py
git commit -m "test(spec): opt existing follow-up tests into per_version_details=True"
```

---

### Task 6: CLI — add `--per-version-details` to `doc3gpp spec sync`

**Files:**
- Modify: `src/doc3gpp/cli.py:3857-3968`
- Test: `tests/integration/test_spec_cli.py`

**Interfaces:**
- Produces: `spec_sync(..., per_version_details: bool = typer.Option(False, "--per-version-details", "-d", help=...))`. Forwards the flag to `service.sync(...)` and `service.sync_spec(...)`.

- [ ] **Step 1: Update the CLI test fake `_ProgressFakeSpecService`** — in `tests/integration/test_spec_cli.py:96`, change the signature to:

```python
    def sync(
        self,
        tsg: str,
        *,
        force: bool = False,
        per_version_details: bool = False,
        on_progress=None,
    ) -> SyncOutcome:
        self.sync_calls.append(tsg)
        self.sync_kwargs.append({"force": force, "per_version_details": per_version_details})
        ...
```

and add `self.sync_kwargs: list[dict] = []` to `__init__`. Mirror the change for any other `sync_spec` / `sync` fakes in this file (search for `def sync(` / `def sync_spec(` and update each).

- [ ] **Step 2: Add failing CLI test** — append to `tests/integration/test_spec_cli.py`:

```python
def test_spec_sync_per_version_details_flag(runner, monkeypatch) -> None:
    """``--per-version-details`` is forwarded to the service as
    ``per_version_details=True``; the default is ``False``."""
    from typer.testing import CliRunner
    from doc3gpp.cli import app
    from tests.integration.test_spec_cli import _ProgressFakeSpecService

    monkeypatch.setattr("doc3gpp.cli.create_schema", lambda: None)
    monkeypatch.setattr(
        "doc3gpp.cli._ensure_tsg_ready",
        lambda svc: type("_T", (), {"is_known_short_name": staticmethod(lambda s: True)})(),
    )
    monkeypatch.setattr(
        "doc3gpp.cli.build_tsg_service",
        lambda: type("_T", (), {"is_known_short_name": staticmethod(lambda s: True)})(),
    )

    monkeypatch.setattr(
        "doc3gpp.cli.build_spec_service",
        lambda: _ProgressFakeSpecService({"R5": 1}),
    )
    _install_fake_tqdm(monkeypatch)

    r = CliRunner().invoke(app, ["spec", "sync", "--tsg", "R5"])
    assert r.exit_code == 0
    svc = _ProgressFakeSpecService._last_instance  # type: ignore[attr-defined]
    assert svc.sync_kwargs[0]["per_version_details"] is False

    r = CliRunner().invoke(app, ["spec", "sync", "--tsg", "R5", "--per-version-details"])
    assert r.exit_code == 0
    assert svc.sync_kwargs[-1]["per_version_details"] is True
```

If the test file's `_ProgressFakeSpecService` does not currently expose `_last_instance`, set it inside `sync()` (`_ProgressFakeSpecService._last_instance = self`) and assert against that.

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/integration/test_spec_cli.py::test_spec_sync_per_version_details_flag -v`
Expected: FAIL — `--per-version-details` is not a known CLI option.

- [ ] **Step 4: Add the new option to `spec_sync`** — in `cli.py:3857`, after the `force` option (line 3879-3884) and before the `)` closing the parameter list, add:

```python
    per_version_details: bool = typer.Option(
        False,
        "--per-version-details",
        "-d",
        help=(
            "Also fetch per-version follow-ups (ETSI PDF link + CR list). "
            "Default off so the sync stays cheap; existing stored "
            "pdf_url and crs values are preserved either way."
        ),
    ),
```

- [ ] **Step 5: Forward to the service in both selectors** — in the `--spec-id` branch (around line 3918), change:

```python
            outcome = service.sync_spec(
                spec_id, force=force, on_progress=_on_progress
            )
```

to:

```python
            outcome = service.sync_spec(
                spec_id, force=force, per_version_details=per_version_details, on_progress=_on_progress
            )
```

and in the `--tsg` / no-selector branch (around line 3963), change `service.sync(tsg_short, force=force, on_progress=_on_progress)` to `service.sync(tsg_short, force=force, per_version_details=per_version_details, on_progress=_on_progress)`.

- [ ] **Step 6: Run the new test to verify it passes**

Run: `pytest tests/integration/test_spec_cli.py::test_spec_sync_per_version_details_flag -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/cli.py tests/integration/test_spec_cli.py
git commit -m "feat(cli): add --per-version-details flag to spec sync"
```

---

### Task 7: HTTP job route — extend `_SyncSpecsBody` and `post_sync_specs`

**Files:**
- Modify: `src/doc3gpp/web/routes/jobs.py:116-198`
- Test: `tests/unit/test_web_jobs_routes.py`

**Interfaces:**
- Produces: `_SyncSpecsBody` with `per_version_details: bool = False`. `post_sync_specs` writes `params["per_version_details"] = body.per_version_details` alongside the existing `force` and `tsg` / `spec_id`.

- [ ] **Step 1: Update the two existing `post_sync_specs` assertions** — in `tests/unit/test_web_jobs_routes.py:149` and `:159`, change the expected `job.params` dicts to include `"per_version_details": False`:

```python
    assert job.params == {"tsg": "R5", "force": True, "per_version_details": False}
    ...
    assert job.params == {"spec_id": "36.579-5", "force": False, "per_version_details": False}
```

- [ ] **Step 2: Add failing test** — append to `tests/unit/test_web_jobs_routes.py`:

```python
def test_post_sync_specs_forwards_per_version_details(client: Any) -> None:
    """``per_version_details`` in the JSON body is written into ``job.params``."""
    c, repo, _ = client
    r = c.post(
        "/jobs/sync/specs",
        json={"tsg": "R5", "force": False, "per_version_details": True},
    )
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.params == {"tsg": "R5", "force": False, "per_version_details": True}
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/unit/test_web_jobs_routes.py::test_post_sync_specs_forwards_per_version_details -v`
Expected: FAIL — `_SyncSpecsBody` rejects the unknown field with 422.

- [ ] **Step 4: Extend `_SyncSpecsBody`** — in `web/routes/jobs.py:116`, add the field:

```python
class _SyncSpecsBody(BaseModel):
    tsg: str | None = None
    spec_id: str | None = None
    force: bool = False
    per_version_details: bool = False
```

- [ ] **Step 5: Forward into `params`** — in `post_sync_specs` (around line 192), change:

```python
    params: dict[str, JSONValue] = {"force": body.force}
    if body.tsg is not None:
        params["tsg"] = body.tsg
    else:
        params["spec_id"] = body.spec_id
```

to:

```python
    params: dict[str, JSONValue] = {
        "force": body.force,
        "per_version_details": body.per_version_details,
    }
    if body.tsg is not None:
        params["tsg"] = body.tsg
    else:
        params["spec_id"] = body.spec_id
```

- [ ] **Step 6: Run the new test and the existing ones**

Run: `pytest tests/unit/test_web_jobs_routes.py -v`
Expected: PASS for every test in the module.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/routes/jobs.py tests/unit/test_web_jobs_routes.py
git commit -m "feat(web): add per_version_details to /jobs/sync/specs body"
```

---

### Task 8: Job handler — read `per_version_details` from `job.params` and forward to the service

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py:128-164`
- Test: `tests/unit/test_job_worker.py`

**Interfaces:**
- Produces: `_sync_specs` reads `per_version_details = bool(job.params.get("per_version_details", False))` and forwards it to `services.spec.sync(...)` and `services.spec.sync_spec(...)`.

- [ ] **Step 1: Add failing test** — append to `tests/unit/test_job_worker.py`:

```python
def test_worker_runs_spec_sync_with_per_version_details() -> None:
    """The handler forwards ``per_version_details=True`` to the service."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(
        JobKind.SYNC_SPECS,
        {"tsg": "R5", "force": True, "per_version_details": True},
    )
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    fsvc = state.services.spec
    assert len(fsvc.calls) == 1
    name, kwargs = fsvc.calls[0]
    assert name == "sync"
    assert kwargs.get("per_version_details") is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_job_worker.py::test_worker_runs_spec_sync_with_per_version_details -v`
Expected: FAIL — the handler does not pass the flag through; `kwargs.get("per_version_details")` is `None`.

- [ ] **Step 3: Update the handler** — in `web/workers/handlers.py:128-160`, after reading `force`, add:

```python
    per_version_details = bool(job.params.get("per_version_details", False))
```

and change the two service calls to:

```python
    if spec_id is not None:
        outcome = services.spec.sync_spec(
            spec_id,
            force=force,
            per_version_details=per_version_details,
            on_progress=on_progress,
        )
    else:
        outcome = services.spec.sync(
            tsg,
            force=force,
            per_version_details=per_version_details,
            on_progress=on_progress,
        )
```

- [ ] **Step 4: Update existing assertions** — in `tests/unit/test_job_worker.py:212` and any other assertion that checks `kwargs.get("force")`, also assert `kwargs.get("per_version_details") is False` (the default).

- [ ] **Step 5: Run the new test and the existing ones**

Run: `pytest tests/unit/test_job_worker.py -v`
Expected: PASS for every test in the module.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py
git commit -m "feat(web): forward per_version_details from sync_specs job to service"
```

---

### Task 9: MCP tool — add `per_version_details` to the `sync_specs` tool

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:480-495`
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Produces: `sync_specs(tsg=None, spec_id=None, force=False, per_version_details=False) -> str`. Writes `params["per_version_details"] = per_version_details` in both branches.

- [ ] **Step 1: Update existing assertions** — in `tests/integration/test_mcp_end_to_end.py:183` and `:205`, change the expected `params` dicts to include `"per_version_details": False`:

```python
    assert detail_payload["params"] == {"tsg": "R5", "force": True, "per_version_details": False}
    ...
    assert detail_payload["params"] == {"spec_id": "36.579-5", "force": False, "per_version_details": False}
```

- [ ] **Step 2: Add failing test** — append to `tests/integration/test_mcp_end_to_end.py`:

```python
def test_sync_specs_tool_per_version_details_enqueues(sqlite_env) -> None:
    """The MCP tool's ``per_version_details=True`` reaches ``job.params``."""
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool(
            "sync_specs", {"tsg": "R5", "force": False, "per_version_details": True}
        )
        envelope = json.loads(created.content[0].text)
        return envelope["job_id"]

    job_id = asyncio.run(run())
    detail = asyncio.run(server.call_tool("get_job", {"job_id": job_id}))
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["params"] == {
        "tsg": "R5",
        "force": False,
        "per_version_details": True,
    }
    del state.engine
```

- [ ] **Step 3: Run to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_sync_specs_tool_per_version_details_enqueues -v`
Expected: FAIL — the MCP tool's `per_version_details` is unknown.

- [ ] **Step 4: Add the MCP arg** — in `mcp_server.py:482`, change the signature to:

```python
    def sync_specs(
        tsg: Annotated[str | None, Field(description="TSG short name to sync specs for (e.g. 'R5').")] = None,
        spec_id: Annotated[str | None, Field(description="Dotted spec id to sync a single stored spec (e.g. '36.579-5').")] = None,
        force: Annotated[bool, Field(description="Bypass the spec sync interval skip rule.")] = False,
        per_version_details: Annotated[
            bool,
            Field(
                description=(
                    "Also fetch per-version follow-ups (ETSI PDF link + CR list). "
                    "Defaults to false to keep the sync cheap; existing stored "
                    "pdf_url and crs values are preserved either way."
                )
            ),
        ] = False,
    ) -> str:
```

- [ ] **Step 5: Forward into `params`** — in `mcp_server.py:490` and `:493`, change the two `params` assignments to:

```python
        if spec_id is not None:
            params: dict[str, Any] = {
                "spec_id": spec_id,
                "force": force,
                "per_version_details": per_version_details,
            }
            message = f"queued sync_specs for spec {spec_id}"
        else:
            params = {
                "tsg": tsg,
                "force": force,
                "per_version_details": per_version_details,
            }
            message = f"queued sync_specs for TSG {tsg}"
```

- [ ] **Step 6: Run the new test and the existing ones**

Run: `pytest tests/integration/test_mcp_end_to_end.py -v`
Expected: PASS for every test in the module.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat(mcp): add per_version_details to sync_specs tool"
```

---

### Task 10: Web — add the per-version-details checkbox to the spec detail page and wire it in JS

**Files:**
- Modify: `src/doc3gpp/web/templates/spec_show.html:9-22`
- Modify: `src/doc3gpp/web/static/js/spec_sync.js:21-30`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Produces: A second checkbox `name="per_version_details"` in `#spec-sync-form`, unchecked by default. `spec_sync.js` reads both checkboxes and posts `{spec_id, force, per_version_details}`.

- [ ] **Step 1: Add failing test** — in `tests/unit/test_web_routes.py`, update `test_get_spec_show_renders_sync_form` (around line 2403) to also assert the new checkbox is present, or add a new test:

```python
def test_get_spec_show_renders_per_version_details_checkbox(client: TestClient) -> None:
    """The spec detail page sync form has a ``per_version_details`` checkbox."""
    from doc3gpp.web.deps import get_spec_service

    client.app.dependency_overrides[get_spec_service] = lambda: FakeSpecService()
    try:
        response = client.get("/specs/36.579-5")
    finally:
        client.app.dependency_overrides.pop(get_spec_service, None)
    assert response.status_code == 200
    assert 'name="per_version_details"' in response.text
    assert "Also fetch per-version details" in response.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/test_web_routes.py::test_get_spec_show_renders_per_version_details_checkbox -v`
Expected: FAIL — checkbox is not in the HTML.

- [ ] **Step 3: Add the checkbox to the template** — in `spec_show.html:9-22`, after the existing `force` checkbox label, add:

```html
    <label class="inline-check">
      <input type="checkbox" name="per_version_details">
      Also fetch per-version details (ETSI PDF + CR list)
    </label>
```

- [ ] **Step 4: Update `spec_sync.js`** — change the `buildBody` closure to:

```js
      buildBody: function (form) {
        var forceEl = form.querySelector('input[name="force"]');
        var perVersionEl = form.querySelector('input[name="per_version_details"]');
        var force = !!forceEl && forceEl.checked;
        var perVersion = !!perVersionEl && perVersionEl.checked;
        return JSON.stringify({
          spec_id: specId,
          force: force,
          per_version_details: perVersion,
        });
      },
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `pytest tests/unit/test_web_routes.py::test_get_spec_show_renders_per_version_details_checkbox -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/templates/spec_show.html src/doc3gpp/web/static/js/spec_sync.js tests/unit/test_web_routes.py
git commit -m "feat(web): add per-version-details checkbox to spec detail page"
```

---

### Task 11: Docs — update `README.md`, `AGENTS.md`, `docs/cli.md`, `docs/web-server.md`, `docs/conventions.md`, `docs/code-map.md`

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `docs/cli.md`
- Modify: `docs/web-server.md`
- Modify: `docs/conventions.md`
- Modify: `docs/code-map.md`

- [ ] **Step 1: `README.md` and `AGENTS.md`** — find the `spec sync` / `SpecService.sync` mention and add a sentence about the new flag and the default-OFF follow-up behaviour. Mirror the existing one-line blurb style.

- [ ] **Step 2: `docs/cli.md`** — find the `spec sync` reference and add a row for `--per-version-details` in the options table; mention the default in the prose.

- [ ] **Step 3: `docs/web-server.md`** — find the spec detail page section and add a one-line mention of the new checkbox alongside the existing `force` checkbox description.

- [ ] **Step 4: `docs/conventions.md`** — under the CLI conventions section, add a short paragraph: "Spec sync defaults to skipping per-version follow-ups (ETSI PDF + CR list). Pass `--per-version-details` to opt back in. The default preserves any previously-fetched `pdf_url` / `crs` values on existing `spec_versions` rows."

- [ ] **Step 5: `docs/code-map.md`** — find the `SpecService` row in the symbol-to-file table and append `per_version_details keyword on sync / sync_spec` to the description.

- [ ] **Step 6: Run docs cross-check** — search the repo for any other place that documents the spec sync behaviour (e.g. `docs/architecture.md`, the `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md` historical spec) and add a one-line note if it materially misleads. Skip the historical spec; it is superseded content and is fine as-is.

- [ ] **Step 7: Commit**

```bash
git add README.md AGENTS.md docs/cli.md docs/web-server.md docs/conventions.md docs/code-map.md
git commit -m "docs(spec): document per-version-details flag and new default"
```

---

### Task 12: Lint + full sqlite suite

**Files:** none (verification only)

- [ ] **Step 1: Run `ruff check .`**

Run: `ruff check .`
Expected: exit 0, no lint errors.

- [ ] **Step 2: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: all unit + integration tests pass, exit 0.

- [ ] **Step 3: If any test fails, fix it before committing** — common pitfalls:
  - A test that asserts the full `job.params` dict and did not pick up the new `per_version_details` key.
  - A test that monkeypatches `fetch_etsi_pdf_text` / `fetch_cr_list` but calls the service without `per_version_details=True` (the new default would skip the call).
  - A CLI test whose fake's `sync` signature is missing `per_version_details` and now raises `TypeError`.

- [ ] **Step 4: Final commit (if fixes were made)**

```bash
git add -A
git commit -m "test: fix any remaining test fixtures for per_version_details default"
```

---

## Self-Review

**1. Spec coverage:**
- §1 Service `sync` / `sync_spec` signature change → Tasks 2, 4. ✓
- §1 `_backfill_followup_fields` rename + crs extension → Task 1. ✓
- §1 `_fetch_followups_concurrently` early-return gate → Task 3. ✓
- §2 CLI flag → Task 6. ✓
- §3 HTTP route body field → Task 7. ✓
- §4 Job handler forwarding → Task 8. ✓
- §5 MCP tool arg → Task 9. ✓
- §6 Web form checkbox + JS body → Task 10. ✓
- §7 Auto-sync (no change) → covered in design doc, no task needed. ✓
- §8 Tests across service / CLI / web / MCP / job handler → Tasks 1, 2, 3, 4, 5, 6, 7, 8, 9, 10. ✓
- §9 Docs → Task 11. ✓

**2. Placeholder scan:** No "TBD" / "TODO" / "implement later" / "similar to Task N" in any task. Every step shows the actual code or test content. ✓

**3. Type consistency:**
- Service keyword: `per_version_details: bool = False` everywhere (Tasks 2, 3, 4). ✓
- HTTP body field: `per_version_details: bool = False` (Task 7). ✓
- MCP tool arg: `per_version_details: bool = False` (Task 9). ✓
- `job.params` key: `"per_version_details"` (Tasks 7, 8, 9). ✓
- JS / HTML: `name="per_version_details"` (Task 10). ✓
- Helper name: `_backfill_followup_fields` (Tasks 1, 3) — same name throughout. ✓
- `_fetch_followups_concurrently` signature: `(versions, executor, client, per_version_details=False)` (Tasks 2, 3). ✓

No issues found; ready to execute.
