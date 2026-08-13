# Spec Sync Per-Spec Skip Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-TSG `tsgs.spec_last_sync` skip rule with a per-spec skip rule keyed on the existing `specs.last_synced_at` column, so each spec is throttled independently. Remove the `tsgs.spec_last_sync` column, its schema migration, the model field, the repo method, and the service skip helper; add a per-spec check inside `SpecService.sync` (per-worker) and `SpecService.sync_spec` (stored-row path).

**Architecture:** Two surgical changes inside `SpecService`: (1) drop the `_is_sync_skipped` TSG-level helper and the two `update_spec_last_sync` stamps; (2) check `spec.last_synced_at` against `_sync_interval` per spec before re-fetching the detail page, so a sweep walks the list page (cheap), then re-syncs only the spec rows whose individual throttle has expired. The per-spec stamp on success stays — `specs.last_synced_at` is already written at `src/doc3gpp/services/spec_service.py:365`. The `--force` flag bypasses all checks, same as today.

**Tech Stack:** Python 3.10+, SQLAlchemy 2.0, pytest, ruff, existing `SpecService` / `SQLAlchemySpecRepository` / `SpecORM` plumbing.

## Global Constraints

- **Doc sync:** `AGENTS.md`, `README.md`, `docs/code-map.md`, `docs/architecture.md`, `docs/cli.md`, `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md`, `docs/superpowers/specs/2026-08-12-single-spec-sync-design.md`, and `docs/superpowers/plans/2026-08-10-spec-sync-list-show.md` all describe the current per-TSG skip rule. Each one needs the matching doc-update step. Update them in the same change set as the code change per [`docs/conventions.md`](docs/conventions.md) §"Documentation sync".
- **Backward-compatible schema migration:** the column was added via `_migrate_tsg_spec_last_sync` (one-shot `ALTER TABLE`). On removal we must `ALTER TABLE tsgs DROP COLUMN spec_last_sync` so an existing DB does not carry a dead column. SQLite ≥ 3.35 supports `DROP COLUMN`; the migration must probe via `PRAGMA table_info` like the existing function does.
- **Idempotency:** the new `ALTER TABLE ... DROP COLUMN` must be a no-op when the column is already gone (probe first, like the existing migration).
- **No CLI flag changes.** `--force` keeps its current meaning.
- **Per-spec grain:** never reintroduce a TSG-level skip on the spec sync path.
- **Existing `specs.last_synced_at` column** stays and is the source of truth for the per-spec throttle.

---

## File Structure

| File | Change |
| --- | --- |
| `src/doc3gpp/storage/db/models.py` | Drop `TsgORM.spec_last_sync` |
| `src/doc3gpp/storage/db/migrate.py` | Add `_migrate_drop_tsg_spec_last_sync`; call it from `create_schema` |
| `src/doc3gpp/models/tsg.py` | Drop `Tsg.spec_last_sync` field |
| `src/doc3gpp/repository/protocols.py` | Drop `TsgRepository.update_spec_last_sync` |
| `src/doc3gpp/storage/repositories/tsg_sql.py` | Drop `SQLAlchemyTsgRepository.update_spec_last_sync`; drop `spec_last_sync` from `_orm_to_domain` |
| `src/doc3gpp/services/spec_service.py` | Drop `_is_sync_skipped`; drop `tsg_repository` parameter + stamps; add per-spec skip in `sync` (worker) and `sync_spec` (stored-row path) |
| `src/doc3gpp/services/factory.py` | Stop passing `SQLAlchemyTsgRepository()` to `SpecService` |
| `tests/unit/test_spec_orm.py` | Drop `test_tsgs_has_spec_last_sync_column` |
| `tests/unit/test_tsg_service.py` | Drop `_FakeTsgRepository.update_spec_last_sync` + `spec_last_sync` field on stub `Tsg` |
| `tests/unit/test_tsg_cli.py` | Update the comment that mentions the dropped field |
| `tests/integration/test_tsg_sqlite.py` | Drop the `test_update_spec_last_sync_sql` integration test |
| `tests/unit/test_spec_service.py` | Drop `_FakeTsgRepository.update_spec_last_sync` + the `MagicMock(spec_last_sync=...)` helper; rewrite the per-spec skip tests; add per-worker skip test for `sync` |
| `tests/integration/test_spec_cli.py` | Confirm / update the `_ProgressFakeSpecService` if its return shape is observed by tests |
| `AGENTS.md`, `README.md`, `docs/code-map.md`, `docs/architecture.md`, `docs/cli.md`, `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md`, `docs/superpowers/specs/2026-08-12-single-spec-sync-design.md`, `docs/superpowers/specs/2026-08-12-spec-sync-dynareport-fetch-design.md`, `docs/superpowers/plans/2026-08-10-spec-sync-list-show.md`, `docs/superpowers/plans/2026-08-12-single-spec-sync.md`, `docs/superpowers/plans/2026-08-12-spec-sync-dynareport-fetch.md` | Update prose |

---

## Task 1: Drop `TsgORM.spec_last_sync` column

**Files:**
- Modify: `src/doc3gpp/storage/db/models.py:99-101`

**Produces:** `TsgORM` no longer declares `spec_last_sync`.

- [ ] **Step 1: Remove the column from `TsgORM`**

In `src/doc3gpp/storage/db/models.py`, delete lines 99-101:

```python
    spec_last_sync: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

Keep the surrounding `meeting_last_sync` column and the rest of `TsgORM` unchanged.

- [ ] **Step 2: Run the offline test suite to confirm no regression in `TsgORM`**

Run: `./scripts/test_sqlite.sh`
Expected: `tests/unit/test_spec_orm.py::test_spec_tables_created` still passes; `test_tsgs_has_spec_last_sync_column` now fails (we delete it in Task 6, so the failure here is expected — don't be alarmed).

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/storage/db/models.py
git commit -m "refactor(db): drop TsgORM.spec_last_sync column"
```

---

## Task 2: Add `ALTER TABLE ... DROP COLUMN` migration for `tsgs.spec_last_sync`

**Files:**
- Modify: `src/doc3gpp/storage/db/migrate.py:58-91` (replace the old one-shot `_migrate_tsg_spec_last_sync` with a drop-column version) and `src/doc3gpp/storage/db/migrate.py:286` (rename the call)

**Produces:** `_migrate_drop_tsg_spec_last_sync()` that issues `ALTER TABLE tsgs DROP COLUMN spec_last_sync` only when the column exists, and is invoked from `create_schema`.

- [ ] **Step 1: Write a failing test that asserts the column is gone after `create_schema`**

Open `tests/integration/test_db_reset_sqlite.py` (any file under `tests/integration/` that already runs against sqlite) and add this test (mirror the existing module's sqlite fixture pattern — use `sqlite_env` if present, else copy the same pattern from `tests/integration/test_tsg_sqlite.py`):

```python
def test_create_schema_drops_tsg_spec_last_sync(sqlite_env) -> None:
    from sqlalchemy import create_engine, inspect, text

    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.db.session import get_engine

    # Simulate an older DB that still carries the column.
    engine = get_engine()
    with engine.begin() as conn:
        # Create tsgs without spec_last_sync, then add the legacy column.
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS tsgs ("
                "  short_name VARCHAR(16) PRIMARY KEY,"
                "  tsg_name VARCHAR(120),"
                "  description VARCHAR(500),"
                "  url TEXT"
                ")"
            )
        )
        # ``IF NOT EXISTS`` is a no-op when the column is already gone.
        try:
            conn.execute(text("ALTER TABLE tsgs ADD COLUMN spec_last_sync DATETIME"))
        except Exception:
            pass  # column already present on a fresh run; that's fine

    create_schema()

    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("tsgs")}
    assert "spec_last_sync" not in cols
```

If the test file already has a fixture-style `sqlite_env` skip this step. If there's no suitable file, place the test in a new file `tests/integration/test_migrate_tsg_spec_last_sync_drop.py` and add a `sqlite_env` fixture identical to the one in `tests/integration/test_tsg_sqlite.py`.

- [ ] **Step 2: Run the new test to verify it fails**

Run: `python -m pytest tests/integration/test_migrate_tsg_spec_last_sync_drop.py -v`
Expected: FAIL with `AssertionError: 'spec_last_sync' in cols`-style error (the column is still present because the migration has not been added yet).

- [ ] **Step 3: Replace `_migrate_tsg_spec_last_sync` with the drop-column version**

In `src/doc3gpp/storage/db/migrate.py`, replace the body of the function (lines 58-91) with:

```python
def _migrate_drop_tsg_spec_last_sync() -> None:
    """Drop the obsolete ``tsgs.spec_last_sync`` column from databases
    that carried it before the per-spec skip rule landed.

    One-shot, idempotent: ``ALTER TABLE ... DROP COLUMN`` raises if the
    column is already absent, so we probe ``PRAGMA table_info`` first
    and only issue the ALTER when the column is genuinely present.
    ``Base.metadata.create_all`` is a no-op on tables that already
    exist, so pre-existing ``tsgs`` rows on older databases carry the
    legacy column forever.
    """
    engine = get_engine()
    with engine.begin() as conn:
        # sqlite_master entry for the table — guard against a fresh DB
        # (no ``tsgs`` yet, in which case ``Base.metadata.create_all``
        # will create it without the column).
        table_exists = conn.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='tsgs' LIMIT 1"
            )
        ).first()
        if not table_exists:
            return
        rows = conn.execute(text("PRAGMA table_info(tsgs)")).all()
        column_names = {row[1] for row in rows}
        if "spec_last_sync" not in column_names:
            return
        conn.execute(text("ALTER TABLE tsgs DROP COLUMN spec_last_sync"))
```

Then update the call site at line 286 of the same file:

```python
    _migrate_drop_tsg_spec_last_sync()
```

- [ ] **Step 4: Run the new test to verify it passes**

Run: `python -m pytest tests/integration/test_migrate_tsg_spec_last_sync_drop.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full sqlite suite to confirm no other migration broke**

Run: `./scripts/test_sqlite.sh`
Expected: no new failures from this change.

- [ ] **Step 6: Commit**

```bash
git add src/doc3gpp/storage/db/migrate.py tests/integration/test_migrate_tsg_spec_last_sync_drop.py
git commit -m "feat(db): drop legacy tsgs.spec_last_sync column"
```

---

## Task 3: Drop `Tsg.spec_last_sync` from the domain model

**Files:**
- Modify: `src/doc3gpp/models/tsg.py:21-23,31`

**Produces:** `Tsg` dataclass no longer carries the `spec_last_sync` field; docstring adjusted.

- [ ] **Step 1: Remove the field and its docstring entry**

In `src/doc3gpp/models/tsg.py`, delete line 31:

```python
    spec_last_sync: datetime | None = None
```

Also delete the matching attribute docstring block (lines 21-23):

```python
        spec_last_sync: UTC timestamp of the last successful spec-list
            sync for this TSG, or ``None`` if the spec list has never
            been synced.
```

Leave `meeting_last_sync` and its docstring alone.

- [ ] **Step 2: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: failure in `tests/unit/test_tsg_service.py` and `tests/integration/test_tsg_sqlite.py` (those still reference the field on a stub `Tsg`). That's expected — fixed in Tasks 7 and 8. No new failures from this change.

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/models/tsg.py
git commit -m "refactor(model): drop Tsg.spec_last_sync field"
```

---

## Task 4: Drop `TsgRepository.update_spec_last_sync` from the Protocol

**Files:**
- Modify: `src/doc3gpp/repository/protocols.py:303-311`

**Produces:** `TsgRepository` Protocol no longer declares `update_spec_last_sync`.

- [ ] **Step 1: Remove the method from the Protocol**

In `src/doc3gpp/repository/protocols.py`, delete the entire `update_spec_last_sync` method block:

```python
    def update_spec_last_sync(self, short_name: str, synced_at: datetime) -> bool:
        """Record when the spec list was last synced for a TSG.

        Returns ``True`` when a matching row existed and was updated,
        ``False`` otherwise.
        """
        ...
```

Leave `update_meeting_last_sync` and the rest of the Protocol intact.

- [ ] **Step 2: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: failure in `tests/unit/test_tsg_service.py` and `tests/unit/test_spec_service.py` (those still implement the method on a stub). No new failures from this change.

- [ ] **Step 3: Commit**

```bash
git add src/doc3gpp/repository/protocols.py
git commit -m "refactor(repo): drop TsgRepository.update_spec_last_sync from protocol"
```

---

## Task 5: Drop `SQLAlchemyTsgRepository.update_spec_last_sync` + `spec_last_sync` from `_orm_to_domain`

**Files:**
- Modify: `src/doc3gpp/storage/repositories/tsg_sql.py:103-117,127`

**Produces:** SQL impl no longer writes/reads the column.

- [ ] **Step 1: Remove the `update_spec_last_sync` method**

In `src/doc3gpp/storage/repositories/tsg_sql.py`, delete lines 103-117 (the entire `update_spec_last_sync` method).

- [ ] **Step 2: Remove the `spec_last_sync=...` kwarg from `_orm_to_domain`**

In the same file, locate the `_orm_to_domain` function around line 127 and remove the `spec_last_sync=_as_utc(row.spec_last_sync),` argument. The function should look like:

```python
def _orm_to_domain(row: TsgORM) -> Tsg:
    """Map a TsgORM row into a Tsg dataclass."""
    return Tsg(
        tsg_name=row.tsg_name,
        short_name=row.short_name,
        description=row.description,
        url=row.url,
        meeting_last_sync=_as_utc(row.meeting_last_sync),
    )
```

- [ ] **Step 3: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: same stub-implementation failures as Task 4 (no new failures from this change).

- [ ] **Step 4: Commit**

```bash
git add src/doc3gpp/storage/repositories/tsg_sql.py
git commit -m "refactor(repo): drop SQLAlchemyTsgRepository.update_spec_last_sync"
```

---

## Task 6: Drop `test_tsgs_has_spec_last_sync_column` and the integration test

**Files:**
- Modify: `tests/unit/test_spec_orm.py:16-21`
- Modify: `tests/integration/test_tsg_sqlite.py:115-...` (`test_update_spec_last_sync_sql` and the `test_update_spec_last_sync_unknown_returns_false` style sibling if it exists)

**Produces:** No tests assert the legacy column exists, and no test calls a removed repo method.

- [ ] **Step 1: Inspect the integration test file to identify the exact function names**

Run: `grep -n "update_spec_last_sync" tests/integration/test_tsg_sqlite.py`
Capture the function names. They are `test_update_spec_last_sync_sql` and at least one sibling (likely `test_update_spec_last_sync_unknown_returns_false`).

- [ ] **Step 2: Remove `test_tsgs_has_spec_last_sync_column` from the unit test**

In `tests/unit/test_spec_orm.py`, delete lines 16-21 (the entire test function). Leave the imports and the other test in place.

- [ ] **Step 3: Remove the `update_spec_last_sync` integration tests**

In `tests/integration/test_tsg_sqlite.py`, delete every test function whose name contains `update_spec_last_sync`. Use the function names from Step 1 as the deletion set.

- [ ] **Step 4: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS. No more `update_spec_last_sync` references should remain in tests.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_spec_orm.py tests/integration/test_tsg_sqlite.py
git commit -m "test: drop legacy spec_last_sync tests"
```

---

## Task 7: Update `_FakeTsgRepository` and the spec-service test stub

**Files:**
- Modify: `tests/unit/test_tsg_service.py:39-44`
- Modify: `tests/unit/test_spec_service.py:1-50` (`_FakeTsgRepository` block + the `MagicMock(spec_last_sync=...)` helper around line 1673 of the design plan)

**Produces:** The two stubs no longer implement `update_spec_last_sync` and no longer set `spec_last_sync` on the `Tsg` they return.

- [ ] **Step 1: Strip `update_spec_last_sync` + `spec_last_sync` from `_FakeTsgRepository` in `test_tsg_service.py`**

In `tests/unit/test_tsg_service.py`, delete the `update_spec_last_sync` method (lines 42-44) and the `spec_last_sync=self._last,` argument inside the returned `Tsg(...)` (line 39). The class should expose only `get_by_short_name` (and the constructor).

- [ ] **Step 2: Drop the same from the stub inside `test_spec_service.py`**

In `tests/unit/test_spec_service.py`, locate the `_FakeTsgRepository` class (around line 25-45) and:
  - delete the `update_spec_last_sync` method (line 42-44);
  - delete the `spec_last_sync=self._last,` argument on the returned `Tsg(...)` (line 39).

- [ ] **Step 3: Drop the `MagicMock(spec_last_sync=...)` helper near line 1673**

Search for `spec_last_sync=self._last` inside the spec service test file. If a `MagicMock` call sets `spec_last_sync`, remove that kwarg. Run:

```bash
grep -n "spec_last_sync" tests/unit/test_spec_service.py
```

…and clean every match.

- [ ] **Step 4: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS for this test file. Existing spec-service tests that assert the *old* per-TSG skip behavior (e.g. `assert tsg.spec_sync_calls, "spec_last_sync not stamped"`) will fail and are fixed in Task 9.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_tsg_service.py tests/unit/test_spec_service.py
git commit -m "test: drop update_spec_last_sync from tsg/spec service stubs"
```

---

## Task 8: Refactor `SpecService` to use the per-spec skip rule

**Files:**
- Modify: `src/doc3gpp/services/spec_service.py:82-326`
- Modify: `src/doc3gpp/services/factory.py:127-134`

**Produces:** `SpecService.sync` and `SpecService.sync_spec` skip per spec using `spec.last_synced_at`; the per-TSG `_is_sync_skipped` helper and the two `update_spec_last_sync` stamps are gone; the `tsg_repository` constructor parameter is gone (no callers reach for it any more).

- [ ] **Step 1: Write failing tests for the per-spec skip rule**

In `tests/unit/test_spec_service.py`, add three tests below the existing test class. They reuse the in-module `_StubSpecRepo` and a thin `_StubTsgRepo` (define inline):

```python
class _StubTsgRepo:
    """Minimal stub for the spec service — the per-spec skip rule no
    longer reads from the TSG repo, so this stub is empty."""

    def get_by_short_name(self, short_name: str):
        return None


def test_sync_spec_skips_when_last_synced_recently() -> None:
    repo = _StubSpecRepo()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    existing = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
        last_synced_at=now - timedelta(hours=1),
    )
    repo.upsert(existing)

    svc = SpecService(
        repository=repo,
        tsg_repository=None,
        sync_interval=timedelta(hours=24),
    )
    out = svc.sync_spec("36.579-5")
    assert out.status == "skipped"
    assert "36.579-5" in out.reason
    # detail-page fetch must not have run; versions remain empty.
    assert repo.versions.get("36.579-5", []) == []


def test_sync_spec_force_overrides_recent_sync() -> None:
    repo = _StubSpecRepo()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    existing = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
        last_synced_at=now - timedelta(hours=1),
    )
    repo.upsert(existing)
    # Bootstrap returns a Spec that will be parsed via _sync_one_spec.
    # To keep this test simple, we monkey-patch _sync_one_spec to a no-op
    # that upserts a fresh Spec.
    svc = SpecService(
        repository=repo,
        tsg_repository=None,
        sync_interval=timedelta(hours=24),
    )

    def _fake_sync_one(spec, canonical, executor, client):
        repo.upsert(
            Spec(
                spec_id=spec.spec_id,
                type=spec.type,
                title=spec.title,
                tsg=spec.tsg,
                last_synced_at=now,
            )
        )
        return 1

    svc._sync_one_spec = _fake_sync_one  # type: ignore[assignment]
    out = svc.sync_spec("36.579-5", force=True)
    assert out.status == "synced"
    # last_synced_at was advanced.
    assert repo.specs["36.579-5"].last_synced_at == now


def test_sync_spec_proceeds_when_no_last_synced_at() -> None:
    repo = _StubSpecRepo()
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    existing = Spec(
        spec_id="36.579-5",
        type="TS",
        title="NR conformance",
        tsg="R5",
        last_synced_at=None,
    )
    repo.upsert(existing)
    svc = SpecService(
        repository=repo,
        tsg_repository=None,
        sync_interval=timedelta(hours=24),
    )

    def _fake_sync_one(spec, canonical, executor, client):
        repo.upsert(
            Spec(
                spec_id=spec.spec_id,
                type=spec.type,
                title=spec.title,
                tsg=spec.tsg,
                last_synced_at=now,
            )
        )
        return 1

    svc._sync_one_spec = _fake_sync_one  # type: ignore[assignment]
    out = svc.sync_spec("36.579-5")
    assert out.status == "synced"
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest tests/unit/test_spec_service.py::test_sync_spec_skips_when_last_synced_recently tests/unit/test_spec_service.py::test_sync_spec_force_overrides_recent_sync tests/unit/test_spec_service.py::test_sync_spec_proceeds_when_no_last_synced_at -v`
Expected: FAIL on all three (the current `sync_spec` consults `tsgs.spec_last_sync` and never reads `spec.last_synced_at`).

- [ ] **Step 3: Drop `_is_sync_skipped` + the two `update_spec_last_sync` stamps**

In `src/doc3gpp/services/spec_service.py`:

1. Delete `_is_sync_skipped` (lines 300-326) entirely.
2. Delete the `if not force: skipped = self._is_sync_skipped(...)` block at lines 120-123 in `sync`. The function should start with `canonical = tsg.upper()` then `logger.info("Syncing specs for TSG %s", canonical)`.
3. Delete the `update_spec_last_sync` stamp block at lines 181-182 (the `if self._tsg_repository is not None: self._tsg_repository.update_spec_last_sync(...)`).
4. In `sync_spec`:
   - delete the `update_spec_last_sync` stamp block at lines 235-238.
   - replace the stored-row branch (lines 217-223) with the per-spec check. The new shape is:

```python
        spec = self._repository.get(spec_id)
        if spec is None:
            spec = self._bootstrap_spec_from_dynareport(spec_id)
        elif not force and spec.last_synced_at is not None:
            now = datetime.now(timezone.utc)
            if (now - spec.last_synced_at) < self._sync_interval:
                ago = now - spec.last_synced_at
                return SyncOutcome(
                    status="skipped",
                    reason=(
                        f"Spec sync skipped for {spec.spec_id}: "
                        f"last sync {_format_duration(ago)} ago "
                        f"(sync interval {_format_duration(self._sync_interval)}). "
                        f"Use --force to override."
                    ),
                )

        canonical = spec.tsg.upper() if spec.tsg else ""
```

`_format_duration` is the helper at the bottom of the file (already used by `MeetingService`; copy the implementation if it is not in scope here — see Step 4 below).

- [ ] **Step 4: Add the per-spec skip inside the `sync` worker**

Replace `_sync_one_spec` (lines 328-367) so that it short-circuits when the input `Spec` carries a recent `last_synced_at`. The full new body:

```python
    def _sync_one_spec(
        self,
        spec: Spec,
        canonical: str,
        followup_executor: ThreadPoolExecutor,
        client: ScraperClient,
    ) -> int:
        # Per-spec skip rule: a sync interval throttles each spec
        # independently. Read from the *incoming* spec (which already
        # carries the persisted ``last_synced_at`` thanks to
        # ``SpecRepository.upsert`` round-tripping the column) so the
        # list sweep stays at one HTTP fetch per fresh spec and walks
        # the per-spec skip at the worker.
        now = datetime.now(timezone.utc)
        if spec.last_synced_at is not None and (now - spec.last_synced_at) < self._sync_interval:
            logger.debug(
                "Skipping spec %s: last_synced_at=%s within interval %s",
                spec.spec_id,
                spec.last_synced_at.isoformat(),
                self._sync_interval,
            )
            return 0

        slug = spec.spec_id.replace(".", "")
        detail_html = fetch_spec_detail(slug, client=client)
        header, versions = parse_spec_detail(detail_html, spec.spec_id, canonical)
        header.type = spec.type
        header.title = spec.title

        # The detail page does not carry the ETSI PDF link, so freshly
        # parsed versions always arrive with ``pdf_url`` unset. Back-fill
        # the persisted value so ``_maybe_fetch_etsi_pdf`` skips the
        # upstream fetch for versions we have already resolved — the
        # link is stable for a version and re-fetching it on every sync
        # wastes an HTTP request per spec.
        self._backfill_pdf_urls(versions)

        # The ETSI + CR follow-ups are independent HTTP requests, fanned
        # out across the dedicated follow-up executor so they overlap
        # with the detail-page fetches of the other spec workers, then
        # waited on before upserting so the in-place mutations on
        # ``versions`` (pdf_url / crs) are captured in the row write.
        self._fetch_followups_concurrently(versions, followup_executor, client)

        # Write the header first WITHOUT ``last_synced_at`` so a failure
        # in ``upsert_versions`` below leaves the timestamp unset on the
        # persisted header row. Set it only after both upserts succeed,
        # then re-upsert to stamp the column. Re-upserting with only
        # ``last_synced_at`` set is cheap (the repo's update branch
        # touches that one field) and lets a partial sync retry the
        # detail page on the next run instead of skipping it.
        self._repository.upsert(header)
        self._repository.upsert_versions(versions)
        header.last_synced_at = datetime.now(timezone.utc)
        self._repository.upsert(header)
        return len(versions)
```

`SpecService.sync`'s for-loop must count only the specs that actually ran (the worker returns `0` for skipped ones). The existing accumulator `version_total += version_count; synced += 1` is wrong for a mixed sweep — fix it to:

```python
                        version_total += version_count
                        if version_count > 0:
                            synced += 1
                        if on_progress is not None:
                            on_progress("spec_done", {"spec_id": spec.spec_id})
```

`SyncOutcome.synced_count` is the number of specs that were re-synced, not the number of specs in the list. The progress callback still fires for every spec (skipped or not) so the tqdm bar advances in lockstep with the worker.

`_format_duration` already exists at the bottom of `src/doc3gpp/services/spec_service.py` (per the code surfaced earlier; if it is missing, copy the same implementation from `src/doc3gpp/services/meetings_service.py:146` and place it below the class). It is a private helper.

- [ ] **Step 5: Drop the `tsg_repository` constructor parameter**

In `__init__` (lines 85-95), remove the `tsg_repository: TsgRepository | None = None` parameter and the `self._tsg_repository = tsg_repository` line. Update the type annotation on `self._repository` accordingly. Drop the `TsgRepository` import from `repository/protocols` if no other method references it (it currently does not).

- [ ] **Step 6: Update `factory.build_spec_service`**

In `src/doc3gpp/services/factory.py`, change `build_spec_service` (lines 127-134) to:

```python
def build_spec_service() -> SpecService:
    """Construct a :class:`SpecService` backed by the configured repo."""
    settings = get_settings()
    return SpecService(
        SQLAlchemySpecRepository(),
        sync_interval=settings.sync.spec_sync_interval,
    )
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `python -m pytest tests/unit/test_spec_service.py::test_sync_spec_skips_when_last_synced_recently tests/unit/test_spec_service.py::test_sync_spec_force_overrides_recent_sync tests/unit/test_spec_service.py::test_sync_spec_proceeds_when_no_last_synced_at -v`
Expected: PASS.

- [ ] **Step 8: Update the existing spec-service tests that asserted the old per-TSG skip**

Run: `python -m pytest tests/unit/test_spec_service.py -v` and read the failures. The likely broken assertions are:

- `assert tsg.spec_sync_calls, "spec_last_sync not stamped"` — drop the assertion; if the test was specifically about the stamp, mark it as covering the `force=True` path or the per-worker progress path instead. Keep the surrounding behaviour assertion.
- The `MagicMock(spec_last_sync=...)` helper has already been removed in Task 7.

For each broken test, either:
  - delete the dead assertion if it was the *only* point of the test, or
  - rewrite the assertion to reflect the new per-spec semantics (e.g. `assert spec.last_synced_at is not None` on the row the worker upserted).

- [ ] **Step 9: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS. The integration tests in `tests/integration/test_spec_cli.py` and `tests/integration/test_spec_sql.py` use the `SpecService` factory and the SQL repo; the migration from Task 2 ensures existing DBs lose the column. Watch for any test that pre-stamps `tsgs.spec_last_sync` and asserts a skip — none should exist (the only `update_spec_last_sync` call sites were the two `SpecService` methods, both of which we have rewritten).

- [ ] **Step 10: Lint**

Run: `ruff check src/doc3gpp tests`
Expected: PASS with no new findings.

- [ ] **Step 11: Commit**

```bash
git add src/doc3gpp/services/spec_service.py src/doc3gpp/services/factory.py tests/unit/test_spec_service.py
git commit -m "refactor(specs): switch to per-spec skip rule; drop tsgs.spec_last_sync"
```

---

## Task 9: Update `test_tsg_cli.py` comment + ensure no other docstring/test references remain

**Files:**
- Modify: `tests/unit/test_tsg_cli.py:68` (the comment that mentions the dropped field)

**Produces:** No test or comment claims `Tsg` carries `spec_last_sync`.

- [ ] **Step 1: Search for the dangling reference**

Run: `grep -n "spec_last_sync" tests/`
Expected: no matches.

- [ ] **Step 2: Tidy the comment if it survives**

In `tests/unit/test_tsg_cli.py:68`, if the comment still mentions `spec_last_sync`, replace it with a one-liner that only mentions `meeting_last_sync` (the surviving field). Example diff:

```diff
-    # field; ``Tsg`` gained ``meeting_last_sync`` and ``spec_last_sync``
+    # field; ``Tsg`` carries ``meeting_last_sync``
```

- [ ] **Step 3: Run the full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_tsg_cli.py
git commit -m "test: remove dangling spec_last_sync comment"
```

---

## Task 10: Documentation sync

**Files (modify each):**
- `AGENTS.md` — search & replace "tsgs.spec_last_sync" → "specs.last_synced_at" (and any prose describing the per-TSG skip). Update the `doc3gpp spec sync --tsg` workflow line.
- `README.md` — same search & replace.
- `docs/code-map.md` — update the `TsgRepository` and `SpecService` rows.
- `docs/architecture.md` — update the runtime flow step that stamps `tsgs.spec_last_sync`; drop the column from the schema bullet list (line 590 area).
- `docs/cli.md` — update the `spec sync` section.
- `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md` — drop the §"tsgs.spec_last_sync (additive)" sub-section.
- `docs/superpowers/specs/2026-08-12-single-spec-sync-design.md` — update the skip-rule description.
- `docs/superpowers/specs/2026-08-12-spec-sync-dynareport-fetch-design.md` — same.
- `docs/superpowers/plans/2026-08-10-spec-sync-list-show.md` — these are historical plans; replace `tsgs.spec_last_sync` with `specs.last_synced_at` everywhere it appears. The plan's Task 6 ("ORM models — `specs`, `spec_versions`, `tsgs.spec_last_sync`") should be edited to remove the `tsgs.spec_last_sync` mention. Same for Tasks 7 and 11.
- `docs/superpowers/plans/2026-08-12-single-spec-sync.md` — same.
- `docs/superpowers/plans/2026-08-12-spec-sync-dynareport-fetch.md` — same.

**Produces:** Documentation describes the per-spec skip rule.

- [ ] **Step 1: Inventory the references**

Run: `grep -rn "spec_last_sync\|tsgs.spec_last_sync" AGENTS.md README.md docs/`
Expected: a list of files. Capture it.

- [ ] **Step 2: Update each file in turn**

For every reference:

- If the prose says "stamps `tsgs.spec_last_sync` at the end" or similar, change it to "each spec's `last_synced_at` is stamped by the worker after a successful re-sync".
- If the prose says "honours the `tsgs.spec_last_sync` skip rule", change it to "honours the per-spec `specs.last_synced_at` skip rule (one row per spec, no TSG-level gate)".
- If the prose describes a column on `tsgs`, drop that bullet.
- If the prose appears in a historical plan/spec, prefix it with `> **Superseded** — the per-spec skip rule is described in …` and link to this plan, OR rewrite the sentence to the new behaviour. Pick rewrite for prose that is otherwise still load-bearing, and prefix only when the entire sub-section is about the old mechanism.

The `docs/superpowers/specs/2026-08-10-spec-sync-list-show-design.md` §"tsgs.spec_last_sync (additive)" sub-section is a candidate for `> **Superseded** …` since the whole section is about the now-deleted column. Drop the column row from the table in the same file.

- [ ] **Step 3: Lint the docs (markdownlint / repo convention)**

Run: `ruff check .` (the repo uses ruff; markdown lint is not configured per `AGENTS.md`).
Expected: no findings. If the repo has a separate markdown linter, run it too.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md README.md docs/
git commit -m "docs(specs): describe per-spec skip rule; drop tsgs.spec_last_sync prose"
```

---

## Task 11: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Full sqlite suite**

Run: `./scripts/test_sqlite.sh`
Expected: PASS, no skips beyond the usual online marker.

- [ ] **Step 2: Lint**

Run: `ruff check .`
Expected: PASS.

- [ ] **Step 3: Search the whole tree for dangling references**

Run:
```bash
grep -rn "spec_last_sync\|tsgs\.spec_last_sync\|update_spec_last_sync\|_is_sync_skipped" \
    --include="*.py" --include="*.md" src tests docs AGENTS.md README.md
```
Expected: no matches. If any remain, fix them in a follow-up commit.

- [ ] **Step 4: End-to-end smoke (optional, manual)**

Run: `doc3gpp spec sync --tsg R5` then re-run `doc3gpp spec sync --tsg R5` within a minute.
Expected: the first run hits the network and stamps `specs.last_synced_at`; the second run reports `skipped` for every spec under that TSG and writes nothing.

- [ ] **Step 5: Tag the work**

```bash
git tag per-spec-skip-rule
```

(Manual decision; drop this step if you prefer a single squash commit on the main branch instead of a tag.)

---

## Self-Review Notes

**Spec coverage (the change at a glance):**

- Drop `tsgs.spec_last_sync` column + migration ⇒ Task 1, Task 2.
- Drop `Tsg.spec_last_sync` model field ⇒ Task 3.
- Drop `TsgRepository.update_spec_last_sync` Protocol + SQL impl + stub ⇒ Tasks 4, 5, 7.
- Drop the integration/unit tests that assert the old column/method ⇒ Task 6, Task 7.
- Add per-spec skip rule in `SpecService.sync` (worker) and `SpecService.sync_spec` (stored-row path) ⇒ Task 8.
- Stop passing `SQLAlchemyTsgRepository()` into `SpecService` ⇒ Task 8 Step 6.
- Update docs to describe the per-spec skip ⇒ Task 10.
- Verify no dangling references ⇒ Task 11.

**Placeholder scan:** checked — no TBD/TODO/“implement later” strings.

**Type consistency:** the new skip rule uses `spec.last_synced_at` (already typed `datetime | None` on `Spec`), `self._sync_interval: timedelta`, `datetime.now(timezone.utc)`, and `_format_duration` (a `timedelta` → `str` helper). `_format_duration` is the same helper used by `MeetingService`, ensuring message format consistency.
