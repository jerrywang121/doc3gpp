# Single-Spec Sync + Sync-All-in-DB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `--spec-id` selector to `spec sync` (mutually exclusive with `--tsg`), change the no-selector fallback to iterate the distinct TSGs of the `specs` table, expose single-spec sync on the HTTP/MCP/job surfaces, and add a sync button with a `--force` checkbox to the web spec detail page that auto-refreshes on completion.

**Architecture:** A new `SpecService.sync_spec(spec_id, ...)` reuses the existing `_sync_one_spec` worker to refresh a single stored spec (looking up its TSG from the stored row and honouring the per-TSG skip rule). A new `SpecRepository.list_distinct_tsgs()` returns the distinct TSGs from `specs`. The CLI, HTTP job route, job handler and MCP tool all select between `tsg` and `spec_id`; the web detail page reuses the shared `bindJobPolling` helper for the button lifecycle.

**Tech Stack:** Python 3.10+, Typer, FastAPI, SQLAlchemy 2.0, Jinja2, HTMX, plain JS.

## Global Constraints

- Layered architecture is strict in `src/doc3gpp/`: `models/` never leaks ORM attrs; `services/` reaches storage only through `repository/` Protocols; `scraping/` is network-only; `parsers/` is parse-only.
- `SpecRepository` Protocol and its `SQLAlchemySpecRepository` impl must stay in sync (update **both**).
- XOR rule: `--spec-id` and `--tsg` are mutually exclusive on every surface; exactly one must be provided for the single-selector paths, neither selects "all in DB".
- `tsgs.spec_last_sync` skip rule is honoured for both `sync()` and `sync_spec()` unless `force`; single-spec sync stamps the owning TSG.
- No comments in code unless the surrounding block documents non-obvious behavior (match existing style).
- Run `ruff check .` and the full sqlite suite (`./scripts/test_sqlite.sh`) before completion.

---

### Task 1: `SpecRepository.list_distinct_tsgs` — Protocol + SQL impl

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:342-390`
- Modify: `src/doc3gpp/storage/repositories/spec_sql.py`
- Test: `tests/integration/test_spec_sql.py`

**Interfaces:**
- Produces: `SpecRepository.list_distinct_tsgs(self) -> list[str]` — distinct, non-null TSG short names from `specs`, ordered alphabetically. Mirror of `SQLAlchemyMeetingRepository.list_distinct_tsgs`.

- [ ] **Step 1: Write the failing test** — append to `tests/integration/test_spec_sql.py`:

```python
def test_list_distinct_tsgs(session_factory) -> None:
    repo = SQLAlchemySpecRepository(session_factory)
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="A", tsg="R5"))
    repo.upsert(Spec(spec_id="38.523-3", type="TS", title="B", tsg="R5"))
    repo.upsert(Spec(spec_id="23.100", type="TR", title="C", tsg="SA2"))
    repo.upsert(Spec(spec_id="24.301", type="TS", title="D", tsg="CT1"))
    assert repo.list_distinct_tsgs() == ["CT1", "R5", "SA2"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_spec_sql.py::test_list_distinct_tsgs -v`
Expected: FAIL with `AttributeError: 'SQLAlchemySpecRepository' object has no attribute 'list_distinct_tsgs'`.

- [ ] **Step 3: Add the Protocol method** — in `repository/protocols.py`, inside `class SpecRepository`, after `list_versions`:

```python
    def list_distinct_tsgs(self) -> list[str]:
        """Return distinct, non-null TSG short names stored in ``specs``.

        Results are ordered alphabetically so iteration is deterministic.
        Rows with a ``NULL`` ``tsg`` are ignored.
        """
        ...
```

- [ ] **Step 4: Implement in `SQLAlchemySpecRepository`** — in `spec_sql.py`, add `from sqlalchemy import distinct` to the imports (check it is not already imported) and add the method:

```python
    def list_distinct_tsgs(self) -> list[str]:
        """Return distinct, non-null TSG short names stored in ``specs.tsg``."""
        with self._session_factory() as session:
            stmt = (
                select(distinct(SpecORM.tsg))
                .where(SpecORM.tsg.isnot(None))
                .order_by(SpecORM.tsg)
            )
            rows = session.scalars(stmt).all()
        return [str(row) for row in rows]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/integration/test_spec_sql.py::test_list_distinct_tsgs -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/repository/protocols.py src/doc3gpp/storage/repositories/spec_sql.py tests/integration/test_spec_sql.py
git commit -m "feat: add SpecRepository.list_distinct_tsgs"
```

---

### Task 2: `SpecService.sync_spec` + `list_distinct_tsgs`

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py`
- Test: `tests/unit/test_spec_service.py`

**Interfaces:**
- Consumes: `SpecRepository.list_distinct_tsgs()` (Task 1); `_sync_one_spec(spec, canonical, followup_executor, client)` (already present).
- Produces:
  - `SpecService.sync_spec(self, spec_id: str, *, force: bool = False, on_progress: SpecProgressFn | None = None) -> SyncOutcome` — raises `ValueError` when `spec_id` is not stored; honours the per-TSG skip rule (unless `force`); returns a `SyncOutcome` with `status`, `reason`, `synced_count` (0 or 1) and `version_count`.
  - `SpecService.list_distinct_tsgs(self) -> list[str]` — delegates to `self._repository.list_distinct_tsgs()`.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_spec_service.py`:

```python
def test_sync_spec_syncs_single_stored_spec(monkeypatch) -> None:
    """``sync_spec`` fetches only the detail page of one stored spec."""
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_spec_detail",
        lambda slug, **k: DETAIL_HTML,
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_etsi_pdf_text",
        lambda wki, client: "<html><a href='x.pdf'>d</a></html>",
    )
    monkeypatch.setattr(
        "doc3gpp.services.spec_service.fetch_cr_list",
        lambda version_id, client: "<html><a id='wgTdocDetailsLink'>R5-1</a></html>",
    )
    repo = _StubSpecRepo()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    tsg = _StubTsgRepo()
    svc = SpecService(repo, tsg)

    outcome = svc.sync_spec("36.579-5")

    assert outcome.status == "synced"
    assert outcome.synced_count == 1
    assert repo.versions["36.579-5"]
    assert tsg.spec_sync_calls, "sync_spec must stamp tsgs.spec_last_sync"


def test_sync_spec_unknown_spec_raises() -> None:
    """``sync_spec`` on a spec not in the DB raises ValueError."""
    repo = _StubSpecRepo()
    svc = SpecService(repo, _StubTsgRepo())
    try:
        svc.sync_spec("99.999-9")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_sync_spec_honours_skip_rule() -> None:
    """``sync_spec`` skips when the TSG was synced within the interval."""
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo = _StubSpecRepo()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    tsg = _StubTsgRepo(last_spec_sync=recent)
    svc = SpecService(repo, tsg, sync_interval=timedelta(hours=24))

    outcome = svc.sync_spec("36.579-5")

    assert outcome.status == "skipped"
    assert "Use --force to override" in outcome.reason


def test_list_distinct_tsgs_delegates_to_repo() -> None:
    """``SpecService.list_distinct_tsgs`` forwards to the repo."""
    repo = _StubSpecRepo()
    repo.list_distinct_tsgs = MagicMock(return_value=["R5", "S2"])
    svc = SpecService(repo)
    assert svc.list_distinct_tsgs() == ["R5", "S2"]
    repo.list_distinct_tsgs.assert_called_once_with()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_spec_service.py -k "sync_spec or list_distinct_tsgs" -v`
Expected: FAIL with `AttributeError` (no `sync_spec` / `list_distinct_tsgs` on `SpecService`).

- [ ] **Step 3: Implement `sync_spec` + `list_distinct_tsgs`** — in `spec_service.py`. Extract the skip-check into a helper and reuse it. Add after `sync()`:

```python
    def sync_spec(
        self,
        spec_id: str,
        *,
        force: bool = False,
        on_progress: SpecProgressFn | None = None,
    ) -> SyncOutcome:
        """Refresh a single stored spec's detail page + versions.

        Looks up ``spec_id`` in the DB to recover its TSG (required for
        the FK and for ``parse_spec_detail``). A spec that is not
        stored cannot be synced on its own and raises ``ValueError``.
        Honours the per-TSG ``tsgs.spec_last_sync`` skip rule unless
        ``force``, and stamps it again on success — identical to
        :meth:`sync`.
        """
        spec = self._repository.get(spec_id)
        if spec is None:
            raise ValueError(
                f"spec {spec_id!r} is not in the database; run 'doc3gpp spec sync --tsg <tsg>' first"
            )
        canonical = spec.tsg.upper() if spec.tsg else ""
        if not force and self._tsg_repository is not None:
            tsg_record = self._tsg_repository.get_by_short_name(canonical)
            last_sync = tsg_record.spec_last_sync if tsg_record is not None else None
            now = datetime.now(timezone.utc)
            if last_sync is not None and (now - last_sync) < self._sync_interval:
                ago = now - last_sync
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"Spec sync skipped for {spec.spec_id} (TSG {canonical}): "
                        f"last sync {_format_duration(ago)} ago "
                        f"(sync interval {_format_duration(self._sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

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

        return SyncOutcome(
            status="synced",
            reason=f"Spec sync complete for {spec.spec_id}: 1 spec, {version_count} versions stored",
            synced_count=1,
            version_count=version_count,
        )

    def list_distinct_tsgs(self) -> list[str]:
        """Return distinct TSG short names currently stored in specs."""
        return self._repository.list_distinct_tsgs()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_spec_service.py -k "sync_spec or list_distinct_tsgs" -v`
Expected: PASS.

- [ ] **Step 5: Run the full spec-service test module to catch regressions**

Run: `pytest tests/unit/test_spec_service.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/services/spec_service.py tests/unit/test_spec_service.py
git commit -m "feat: add SpecService.sync_spec + list_distinct_tsgs"
```

---

### Task 3: CLI `spec sync` — `--spec-id` selector + specs-table fallback

**Files:**
- Modify: `src/doc3gpp/cli.py:3854-3930`
- Test: `tests/integration/test_spec_cli.py`

**Interfaces:**
- Consumes: `SpecService.sync_spec(spec_id, *, force, on_progress)`, `SpecService.list_distinct_tsgs()`, `SpecService.get(spec_id)` (Task 2).

- [ ] **Step 1: Write the failing tests** — append to `tests/integration/test_spec_cli.py`:

```python
def test_spec_sync_spec_id_syncs_single(monkeypatch) -> None:
    """``spec sync --spec-id 36.579-5`` calls ``sync_spec`` once."""
    from doc3gpp.models.spec import Spec
    from doc3gpp.models.sync import SyncOutcome

    svc = MagicMock()
    svc.get.return_value = Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5")
    svc.sync_spec.return_value = SyncOutcome(
        status="synced",
        reason="Spec sync complete for 36.579-5: 1 spec, 2 versions stored",
        synced_count=1,
        version_count=2,
    )
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--spec-id", "36.579-5"])
    assert result.exit_code == 0, result.stdout
    args, kwargs = svc.sync_spec.call_args
    assert args == ("36.579-5",)
    assert kwargs.get("force") is False
    assert kwargs.get("on_progress") is not None
    assert "Spec sync complete for 36.579-5" in result.stdout


def test_spec_sync_spec_id_unknown_raises(monkeypatch) -> None:
    """``spec sync --spec-id <unknown>`` raises a BadParameter."""
    svc = MagicMock()
    svc.get.return_value = None
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--spec-id", "99.999-9"])
    assert result.exit_code != 0
    assert "Unknown spec id" in (result.output + result.stdout)


def test_spec_sync_tsg_and_spec_id_conflict(monkeypatch) -> None:
    """Passing both ``--tsg`` and ``--spec-id`` is rejected."""
    svc = MagicMock()
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    result = runner.invoke(app, ["spec", "sync", "--tsg", "R5", "--spec-id", "36.579-5"])
    assert result.exit_code != 0
    assert "mutually exclusive" in (result.output + result.stdout)


def test_spec_sync_no_selector_iterates_specs_tsgs(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no selector syncs every TSG in the specs table."""
    from doc3gpp.models.sync import SyncOutcome

    svc = MagicMock()
    svc.list_distinct_tsgs.return_value = ["R5", "S2"]
    svc.sync.side_effect = [
        SyncOutcome(status="synced", reason="Spec sync complete for TSG R5: 1 spec, 1 version stored", synced_count=1, version_count=1),
        SyncOutcome(status="synced", reason="Spec sync complete for TSG S2: 1 spec, 1 version stored", synced_count=1, version_count=1),
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())
    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    assert svc.sync.call_count == 2
    synced_tsgs = {call.args[0] for call in svc.sync.call_args_list}
    assert synced_tsgs == {"R5", "S2"}
    svc.list_distinct_tsgs.assert_called_once()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/integration/test_spec_cli.py -k "spec_id or no_selector_iterates" -v`
Expected: FAIL (no `--spec-id` option, so Typer reports unknown option).

- [ ] **Step 3: Update the `spec_sync` command** — replace the body/options in `cli.py:3854-3930`:

```python
@spec_app.command("sync")
def spec_sync(
    tsg: str | None = typer.Option(
        None,
        "--tsg",
        help=(
            "TSG short name (e.g. R5) for the spec list page to sync. "
            "Mutually exclusive with --spec-id. When neither --tsg nor "
            "--spec-id is given, every distinct TSG found in the local "
            "specs table is synced."
        ),
    ),
    spec_id: str | None = typer.Option(
        None,
        "--spec-id",
        help=(
            "Dotted spec id (e.g. 36.579-5) to sync a single stored spec. "
            "Mutually exclusive with --tsg. When neither --tsg nor "
            "--spec-id is given, every distinct TSG found in the local "
            "specs table is synced."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Bypass the spec sync interval skip rule.",
    ),
) -> None:
    """Fetch and store specs (and their versions) from 3gpp.org.

    Valid --tsg values are:
    `R1`, `R2`, `R3`, `R4`, `R5`, `RT`, `RP`,
    `C1`, `C3`, `C4`, `C6`, `CP`,
    `S1`, `S2`, `S3`, `S4`, `S5`, `S6`, `SP`

    When no ``--tsg`` and no ``--spec-id`` is given, every distinct TSG
    found in the local specs table is synced.

    When a TSG was synced within ``sync.spec_sync_interval``
    the sync is skipped unless ``--force`` is passed.
    """
    create_schema()
    tsg_service = _ensure_tsg_ready(build_tsg_service())
    service = build_spec_service()

    if tsg is not None and spec_id is not None:
        raise typer.BadParameter(
            "--tsg and --spec-id are mutually exclusive; pass exactly one."
        )

    if spec_id is not None:
        spec = service.get(spec_id)
        if spec is None:
            raise typer.BadParameter(
                f"Unknown spec id '{spec_id}'. Run 'doc3gpp spec sync --tsg <tsg>' first."
            )
        from tqdm import tqdm

        bar = tqdm(total=1, desc=f"spec {spec_id}", unit="spec", dynamic_ncols=True)

        def _on_progress(event: str, data: dict) -> None:
            if event == "spec_done":
                bar.update(1)

        outcome = service.sync_spec(spec_id, force=force, on_progress=_on_progress)
        bar.close()
        typer.echo(outcome.reason)
        return

    if tsg is None:
        tsgs = service.list_distinct_tsgs()
        if not tsgs:
            logger.info("No stored specs with a TSG found; nothing to sync")
            typer.echo("No stored specs with a TSG found; nothing to sync.")
            return
        logger.info(
            "Starting spec sync for %s stored TSG(s): %s", len(tsgs), ", ".join(tsgs)
        )
    else:
        tsgs = [_validate_tsg_short_name(tsg, tsg_service)]
        logger.info("Starting spec sync for TSG %s", tsgs[0])

    for tsg_short in tsgs:
        if not tsg_service.is_known_short_name(tsg_short):
            logger.warning("Skipping unknown TSG '%s' found in specs table", tsg_short)
            typer.echo(f"Skipping unknown TSG '{tsg_short}' found in specs table.")
            continue

        from tqdm import tqdm

        bar: tqdm | None = None

        def _on_progress(event: str, data: dict) -> None:
            nonlocal bar
            if event == "list_parsed":
                bar = tqdm(
                    total=data["total"],
                    desc=f"spec {tsg_short}",
                    unit="spec",
                    dynamic_ncols=True,
                )
            elif event == "spec_done" and bar is not None:
                bar.update(1)

        outcome: SyncOutcome = service.sync(
            tsg_short, force=force, on_progress=_on_progress
        )
        if bar is not None:
            bar.close()
        typer.echo(outcome.reason)
```

Note: the existing `test_spec_sync_no_tsg_iterates_meetings_distinct_tsgs` and
`test_spec_sync_no_tsg_loop_shows_progress_bar_per_tsg` tests seed **meetings**
and expect the fallback to iterate the meetings table. These tests are now
obsolete — replace them in **Step 4** to seed `specs` instead (see below).

- [ ] **Step 4: Update the two obsolete no-selector tests** — rewrite them to seed the `specs` table (not meetings) so the new fallback works. Locate `test_spec_sync_no_tsg_iterates_meetings_distinct_tsgs` and `test_spec_sync_no_tsg_loop_shows_progress_bar_per_tsg` and change their fixtures/assertions to use `SQLAlchemySpecRepository` upserts instead of `SQLAlchemyMeetingRepository`, and assert `service.list_distinct_tsgs` drives the loop. Example replacement for the first:

```python
def test_spec_sync_no_tsg_iterates_specs_distinct_tsgs(sqlite_env, monkeypatch) -> None:
    """``spec sync`` with no selector loops over the distinct TSGs in the
    specs table and calls :meth:`SpecService.sync` once per TSG."""
    from doc3gpp.models.spec import Spec
    from doc3gpp.services.tsg_service import TsgService
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.spec_sql import SQLAlchemySpecRepository
    from doc3gpp.storage.repositories.tsg_sql import SQLAlchemyTsgRepository

    create_schema()
    TsgService(SQLAlchemyTsgRepository()).seed_defaults()
    repo = SQLAlchemySpecRepository()
    repo.upsert(Spec(spec_id="36.579-5", type="TS", title="NR conformance", tsg="R5"))
    repo.upsert(Spec(spec_id="38.523-3", type="TS", title="NR signalling", tsg="R5"))
    repo.upsert(Spec(spec_id="23.100", type="TR", title="Arch", tsg="S2"))

    svc = MagicMock()
    svc.list_distinct_tsgs.return_value = ["R5", "S2"]
    svc.sync.side_effect = [
        SyncOutcome(status="synced", reason="Spec sync complete for TSG R5: 1 spec, 1 version stored", synced_count=1, version_count=1),
        SyncOutcome(status="synced", reason="Spec sync complete for TSG S2: 1 spec, 1 version stored", synced_count=1, version_count=1),
    ]
    monkeypatch.setattr("doc3gpp.cli.build_spec_service", lambda: svc)
    monkeypatch.setattr("doc3gpp.cli.build_meeting_service", lambda: MagicMock())

    result = runner.invoke(app, ["spec", "sync"])
    assert result.exit_code == 0, result.stdout
    assert svc.sync.call_count == 2
    synced_tsgs = {call.args[0] for call in svc.sync.call_args_list}
    assert synced_tsgs == {"R5", "S2"}
    assert "Spec sync complete for TSG R5" in result.stdout
    assert "Spec sync complete for TSG S2" in result.stdout
```

Apply the same substitution to the progress-bar test (`test_spec_sync_no_tsg_loop_shows_progress_bar_per_tsg`), seeding `specs` and asserting the R5/S2 bars via `list_distinct_tsgs`. The `SyncOutcome` import must be at module top (it already is — line 20).

- [ ] **Step 5: Run the CLI spec-sync tests**

Run: `pytest tests/integration/test_spec_cli.py -k "spec_sync" -v`
Expected: all PASS.

- [ ] **Step 6: Run the full spec CLI module**

Run: `pytest tests/integration/test_spec_cli.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/cli.py tests/integration/test_spec_cli.py
git commit -m "feat: add spec sync --spec-id + specs-table fallback"
```

---

### Task 4: HTTP job route — accept `spec_id`

**Files:**
- Modify: `src/doc3gpp/web/routes/jobs.py:116-118,182-191`
- Test: `tests/unit/test_web_jobs_routes.py`

**Interfaces:**
- Consumes: `JobKind.SYNC_SPECS`, `InvalidFilterError`.
- Produces: `_SyncSpecsBody(tsg: str | None = None, spec_id: str | None = None, force: bool = False)`; `POST /jobs/sync/specs` accepts exactly one of `tsg`/`spec_id`, writes `{"tsg": ...}` or `{"spec_id": ...}` + `{"force": ...}`.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/test_web_jobs_routes.py`:

```python
def test_post_sync_specs_by_spec_id(client: Any) -> None:
    c, repo, _ = client
    r = c.post("/jobs/sync/specs", json={"spec_id": "36.579-5", "force": False})
    assert r.status_code == 202
    job = repo.get(r.json()["job_id"])
    assert job is not None
    assert job.kind is JobKind.SYNC_SPECS
    assert job.params == {"spec_id": "36.579-5", "force": False}


def test_post_sync_specs_requires_one_selector(client: Any) -> None:
    c, _, _ = client
    r = c.post("/jobs/sync/specs", json={"tsg": "R5", "spec_id": "36.579-5"})
    assert r.status_code == 400
    r2 = c.post("/jobs/sync/specs", json={})
    assert r2.status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_web_jobs_routes.py -k "specs" -v`
Expected: FAIL (current body requires `tsg`; `spec_id` case → 422, both/none cases differ).

- [ ] **Step 3: Update the body model and route** — in `jobs.py`:

Replace the `_SyncSpecsBody` class:

```python
class _SyncSpecsBody(BaseModel):
    tsg: str | None = None
    spec_id: str | None = None
    force: bool = False
```

Replace the `post_sync_specs` handler:

```python
@router.post("/sync/specs", status_code=202)
async def post_sync_specs(
    body: _SyncSpecsBody,
    job_repo: JobRepository = Depends(get_job_repo),
) -> JSONResponse:
    if (body.tsg is None) == (body.spec_id is None):
        raise InvalidFilterError(
            "sync/specs requires exactly one of 'tsg' or 'spec_id' in the body"
        )
    params: dict[str, JSONValue] = {"force": body.force}
    if body.tsg is not None:
        params["tsg"] = body.tsg
    else:
        params["spec_id"] = body.spec_id
    job = job_repo.create(JobKind.SYNC_SPECS, params)
    return JSONResponse(status_code=202, content=_envelope(job, queued=True))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_web_jobs_routes.py -k "specs" -v`
Expected: PASS.

- [ ] **Step 5: Run the full jobs-routes module**

Run: `pytest tests/unit/test_web_jobs_routes.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/routes/jobs.py tests/unit/test_web_jobs_routes.py
git commit -m "feat: accept spec_id in POST /jobs/sync/specs"
```

---

### Task 5: Job handler — dispatch on `spec_id` or `tsg`

**Files:**
- Modify: `src/doc3gpp/web/workers/handlers.py:128-155`
- Test: `tests/unit/test_job_worker.py`

**Interfaces:**
- Consumes: `SpecService.sync_spec(spec_id, *, force, on_progress)` (Task 2), `SyncOutcome`.
- Produces: `_sync_specs` reads either `spec_id` or `tsg` from `job.params`, raises `ValueError` when neither, returns `{"status", "reason", "synced_count", "version_count"}`.

- [ ] **Step 1: Extend the fake spec service + write the failing test** — in `tests/unit/test_job_worker.py`, extend `_FakeSpecService` to record which method was called:

```python
class _FakeSpecService:
    def __init__(self, *, fail: bool = False) -> None:
        from doc3gpp.models.sync import SyncOutcome
        self.calls: list[tuple[str, dict]] = []
        if fail:
            self.sync = self._raise
            self.sync_spec = self._raise
        else:
            self.sync = lambda *a, **k: self._record("sync", k, SyncOutcome(
                status="synced", reason="spec sync ok", synced_count=5, version_count=12))
            self.sync_spec = lambda *a, **k: self._record("sync_spec", k, SyncOutcome(
                status="synced", reason="spec sync ok", synced_count=1, version_count=2))

    def _raise(self, *a, **k):
        raise RuntimeError("boom")

    def _record(self, name, kwargs, outcome):
        self.calls.append((name, kwargs))
        return outcome
```

Append a test:

```python
def test_worker_runs_spec_sync_by_spec_id() -> None:
    """A ``SYNC_SPECS`` job with ``spec_id`` dispatches to ``sync_spec``."""
    repo = _make_repo()
    state = _make_state(repo)
    job = repo.create(JobKind.SYNC_SPECS, {"spec_id": "36.579-5", "force": True})
    worker = JobWorker(state, repo=repo)

    _run_worker_once(worker, repo)

    done = repo.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED
    assert done.result_summary == {
        "status": "synced",
        "reason": "spec sync ok",
        "synced_count": 1,
        "version_count": 2,
    }
    fsvc = state.services.spec
    assert fsvc.calls == [("sync_spec", {"force": True})]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/unit/test_job_worker.py -k "spec_sync" -v`
Expected: the existing `test_worker_runs_spec_sync_job` still passes but the new `test_worker_runs_spec_sync_by_spec_id` fails (handler currently requires `tsg`).

- [ ] **Step 3: Update the `_sync_specs` handler** — in `handlers.py`:

```python
async def _sync_specs(
    job: Job,
    services: ServiceContainer,
    settings: Settings,
    *,
    progress: ProgressFn,
    cancel_event: asyncio.Event,
) -> Mapping[str, JSONValue]:
    tsg = job.params.get("tsg")
    spec_id = job.params.get("spec_id")
    force = bool(job.params.get("force", False))
    if (tsg is None) == (spec_id is None):
        raise ValueError("sync_specs job requires exactly one of 'tsg' or 'spec_id'")

    if spec_id is not None:
        if not isinstance(spec_id, str):
            raise ValueError("sync_specs job requires a 'spec_id' string parameter")
        progress(f"syncing spec {spec_id}")
    else:
        if not tsg or not isinstance(tsg, str):
            raise ValueError("sync_specs job requires a 'tsg' string parameter")
        progress(f"syncing specs for TSG {tsg}")

    def on_progress(event: str, data: Mapping[str, object]) -> None:
        if event == "list_parsed":
            progress(f"parsed {data.get('total', 0)} specs for TSG {tsg}")
        elif event == "spec_done":
            progress(f"spec {data.get('spec_id', '')} done")

    if spec_id is not None:
        outcome = services.spec.sync_spec(spec_id, force=force, on_progress=on_progress)
    else:
        outcome = services.spec.sync(tsg, force=force, on_progress=on_progress)
    progress(outcome.reason)
    return {
        "status": outcome.status,
        "reason": outcome.reason,
        "synced_count": outcome.synced_count,
        "version_count": outcome.version_count,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/test_job_worker.py -k "spec_sync" -v`
Expected: PASS.

- [ ] **Step 5: Run the full job-worker module**

Run: `pytest tests/unit/test_job_worker.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/workers/handlers.py tests/unit/test_job_worker.py
git commit -m "feat: dispatch sync_specs job on spec_id or tsg"
```

---

### Task 6: MCP tool — accept `spec_id`

**Files:**
- Modify: `src/doc3gpp/web/mcp_server.py:480-493`
- Test: `tests/integration/test_mcp_end_to_end.py`

**Interfaces:**
- Consumes: `_enqueue`, `JobKind.SYNC_SPECS`, `InvalidFilterError`.
- Produces: `sync_specs(tsg=None, spec_id=None, force=False)` — requires exactly one of `tsg`/`spec_id`.

- [ ] **Step 1: Write the failing test** — append to `tests/integration/test_mcp_end_to_end.py`:

```python
def test_sync_specs_tool_by_spec_id_enqueues(sqlite_env) -> None:
    import asyncio
    import json

    state, server = _state_and_server()

    async def run():
        created = await server.call_tool("sync_specs", {"spec_id": "36.579-5", "force": False})
        envelope = json.loads(created.content[0].text)
        assert envelope["status"] == "queued"
        job_id = envelope["job_id"]
        detail = await server.call_tool("get_job", {"job_id": job_id})
        return detail

    detail = asyncio.run(run())
    assert detail.is_error is False
    detail_payload = json.loads(detail.content[0].text)
    assert detail_payload["kind"] == "sync_specs"
    assert detail_payload["params"] == {"spec_id": "36.579-5", "force": False}
    del state.engine
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_sync_specs_tool_by_spec_id_enqueues -v`
Expected: FAIL (tool signature has no `spec_id`).

- [ ] **Step 3: Update the `sync_specs` MCP tool** — in `mcp_server.py`:

```python
    @server.tool(name="sync_specs", description="Enqueue a spec sync for a TSG or a single stored spec.")
    @_mcp_error_guard
    def sync_specs(
        tsg: Annotated[str | None, Field(description="TSG short name to sync specs for (e.g. 'R5').")] = None,
        spec_id: Annotated[str | None, Field(description="Dotted spec id to sync a single stored spec (e.g. '36.579-5').")] = None,
        force: Annotated[bool, Field(description="Bypass the spec sync interval skip rule.")] = False,
    ) -> str:
        if (tsg is None) == (spec_id is None):
            raise InvalidFilterError("exactly one of 'tsg' or 'spec_id' is required")
        if spec_id is not None:
            params: dict[str, Any] = {"spec_id": spec_id, "force": force}
            message = f"queued sync_specs for spec {spec_id}"
        else:
            params = {"tsg": tsg, "force": force}
            message = f"queued sync_specs for TSG {tsg}"
        return _enqueue(state, JobKind.SYNC_SPECS, params, message)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/integration/test_mcp_end_to_end.py::test_sync_specs_tool_by_spec_id_enqueues -v`
Expected: PASS.

- [ ] **Step 5: Run the MCP sync tests + the existing sync_specs test**

Run: `pytest tests/integration/test_mcp_end_to_end.py -k "sync_specs" -v`
Expected: PASS (both the old `tsg`-based test and the new `spec_id` test).

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/web/mcp_server.py tests/integration/test_mcp_end_to_end.py
git commit -m "feat: accept spec_id in sync_specs MCP tool"
```

---

### Task 7: Web spec detail page — sync button with `--force` checkbox

**Files:**
- Modify: `src/doc3gpp/web/templates/spec_show.html`
- Create: `src/doc3gpp/web/static/js/spec_sync.js`
- Test: `tests/unit/test_web_routes.py`

**Interfaces:**
- Consumes: `bindJobPolling` (from `job_poller.js`), `POST /jobs/sync/specs` (Task 4).
- Produces: a `#spec-sync-form` form that POSTs JSON `{spec_id, force}` and auto-refreshes on job terminal state.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/test_web_routes.py`:

```python
def test_get_spec_show_renders_sync_form(client: TestClient) -> None:
    """``GET /specs/{id}`` renders the sync form with a force checkbox."""
    from doc3gpp.web.deps import get_spec_service

    client.app.dependency_overrides[get_spec_service] = lambda: FakeSpecService()
    try:
        response = client.get("/specs/36.579-5")
    finally:
        client.app.dependency_overrides.pop(get_spec_service, None)
    assert response.status_code == 200
    assert 'id="spec-sync-form"' in response.text
    assert 'action="/jobs/sync/specs"' in response.text
    assert 'name="force"' in response.text
    assert "spec_sync.js" in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/test_web_routes.py::test_get_spec_show_renders_sync_form -v`
Expected: FAIL (no `spec-sync-form` in the rendered template).

- [ ] **Step 3: Add the sync card to `spec_show.html`** — insert after the `<h1>`/`<p class="meta">` header block and before `<dl class="kv">` (around line 8):

```html
  <section class="card">
    <h2>Sync</h2>
    <form
      id="spec-sync-form"
      class="spec-sync-form"
      method="post"
      action="/jobs/sync/specs"
      data-spec-id="{{ spec.spec_id }}"
    >
      <label class="inline-check">
        <input type="checkbox" name="force"> Force sync
      </label>
      <button type="submit" class="btn primary">Sync this spec</button>
      <span class="spec-sync-queued" style="display:none">Sync job queued</span>
    </form>
    <div id="spec-sync-job-target"></div>
  </section>
```

And at the bottom of the file (before `{% endblock %}`), alongside the existing clipboard script:

```html
  <script src="/static/js/job_poller.js" defer></script>
  <script src="/static/js/spec_sync.js" defer></script>
```

- [ ] **Step 4: Create `spec_sync.js`** — mirror `tdoc_parse.js`:

```javascript
// Sync-trigger for the spec detail page.
//
// Thin wrapper over the shared ``bindJobPolling`` helper from
// ``job_poller.js``. The spec sync route (``POST /jobs/sync/specs``)
// expects a JSON envelope (spec_id + force), so we override the body
// via ``buildBody`` and the Content-Type via ``contentType``, and let
// the poller own the polling + reload lifecycle.
(function () {
  "use strict";

  function init() {
    var form = document.getElementById("spec-sync-form");
    if (!form || !window.bindJobPolling) {
      return;
    }
    var specId = form.getAttribute("data-spec-id");
    var queued = form.querySelector(".spec-sync-queued");
    if (queued) {
      queued.dataset.label = queued.textContent;
    }
    window.bindJobPolling(form, {
      queuedSelector: ".spec-sync-queued",
      targetSelector: "#spec-sync-job-target",
      contentType: "application/json",
      buildBody: function (form) {
        var force = form.querySelector('input[name="force"]').checked;
        return JSON.stringify({ spec_id: specId, force: force });
      },
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/unit/test_web_routes.py::test_get_spec_show_renders_sync_form -v`
Expected: PASS.

- [ ] **Step 6: Run the spec-show web tests**

Run: `pytest tests/unit/test_web_routes.py -k "spec_show or spec_show_renders_sync_form" -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/doc3gpp/web/templates/spec_show.html src/doc3gpp/web/static/js/spec_sync.js tests/unit/test_web_routes.py
git commit -m "feat: add sync button to spec detail page"
```

---

### Task 8: Docs sync

**Files:**
- Modify: `docs/cli.md`, `docs/web-server.md`, `docs/code-map.md`, `docs/architecture.md`, `README.md`, `AGENTS.md`

**Interfaces:**
- Consumes: the behavior implemented in Tasks 2–7.

- [ ] **Step 1: `docs/cli.md` — update the `spec sync` section** (around lines 1811-1856). Add `--spec-id` and the XOR + fallback rules to Options and Behavior, and examples:

```text
Options:

- --tsg: TSG short name (e.g. `R5`). default: none. Validated against
  the `tsgs` reference table; unknown values raise an error listing the
  known short names. Mutually exclusive with --spec-id.
- --spec-id: Dotted spec id (e.g. `36.579-5`) to sync a single stored
  spec. Mutually exclusive with --tsg. The spec must already be stored
  (run `spec sync --tsg <tsg>` first).
- --force, -f: Bypass the spec sync interval skip rule.

Behavior:

- With --tsg: fetches the TSG list page, fans out per-spec detail pages.
- With --spec-id: looks up the stored spec to recover its TSG, fetches
  only that spec's detail page + versions (no list page).
- With neither: every distinct TSG found in the `specs` table is synced
  (each via the --tsg path). The single-spec and per-TSG paths both
  honour `tsgs.spec_last_sync` (skip unless --force) and stamp it again
  on success.

Examples:

doc3gpp spec sync --spec-id 36.579-5
doc3gpp spec sync --spec-id 36.579-5 --force
```

- [ ] **Step 2: `docs/web-server.md`** — update the `POST /jobs/sync/specs` row (line 182) to note it accepts exactly one of `tsg`/`spec_id`; add a sentence near the spec list/detail docs describing the detail-page sync button. Insert after line 201:

```text
The spec detail page shows a Sync card with a Force sync checkbox that
enqueues a single-spec sync job for that spec; the page auto-refreshes
when the job completes.
```

- [ ] **Step 3: `docs/code-map.md`** — update the `SpecService` row (line 54) and `SpecRepository` row (line 41) to mention `sync_spec` and `list_distinct_tsgs`. Add `sync_spec` to the `SpecService` description and `list_distinct_tsgs` to both rows.

- [ ] **Step 4: `docs/architecture.md`** — in the "Spec sync (list + detail)" section (lines 329-370), add a note that `spec sync --spec-id <id>` and the web button use `SpecService.sync_spec` (single detail page, no list fetch) and that the no-selector fallback iterates the `specs` table.

- [ ] **Step 5: `README.md`** — update the `spec sync` lines (around 216-217) to mention `--spec-id`; the CLI examples near lines 130/216.

- [ ] **Step 6: `AGENTS.md`** — update the "Where to look" row for `SpecService.sync` (line ~146) to mention `sync_spec` and the `--spec-id` selector; update the doc-pointer row for `spec sync / list / show` if present.

- [ ] **Step 7: Commit**

```bash
git add docs/cli.md docs/web-server.md docs/code-map.md docs/architecture.md README.md AGENTS.md
git commit -m "docs: document single-spec sync + specs-table fallback"
```

---

### Task 9: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Run ruff**

Run: `ruff check .`
Expected: no errors.

- [ ] **Step 2: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: all unit + integration tests PASS (online tests excluded).

- [ ] **Step 3: Fix any failures** — if a test fails, fix the root cause in the relevant task's files, then re-run the suite until green.
