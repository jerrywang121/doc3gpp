# `fts5_hit` → `hit` Field Rename Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the `fts5_hit` field on `SemanticSearchHit` to `hit` so the rendered JSON / markdown / table output stops misleading users about whether a hit came from the FTS5 path.

**Architecture:** Single dataclass field rename plus a cascading sweep of all accessor / kwarg / JSON-key sites in `src/` and `tests/`. Two user-facing docs and two historical specs update in the same change set per the AGENTS.md doc-sync rule.

**Tech Stack:** Python 3.10+, pydantic v2 (for the dataclass), Typer CLI, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-08-01-fts5-hit-rename-design.md`](../specs/2026-08-01-fts5-hit-rename-design.md)

## Global Constraints

- Python ≥ 3.10. `SemanticSearchHit` is `@dataclass(slots=True, frozen=True)`; renaming a field does not require any `.replace()` updates (none exist for `fts5_hit` in the codebase).
- The new field name is exactly `hit` (not `tdoc`, not `hit_data`, not `metadata`). The brief from the user locked this.
- `fts5_hits` (plural) — the input list parameter to `rrf_merge()` — STAYS. It is genuinely an FTS5 result list; renaming it would touch the `rrf_merge()` signature for no user-visible benefit.
- `SearchHit` (the TDoc-metadata dataclass) STAYS.
- `_build_fts5_stub` helper name STAYS — the `_fts5_stub` suffix describes what it builds (a stub FTS5-shaped `SearchHit`), not what it's called from.
- All CLI flags, settings, pyproject extras, and the `[semantic]` extras package list are unchanged.
- All commits on the existing branch `feat/embedding-search`.
- Run `ruff check .` at the end of every task touching `src/` or `tests/`.

## File / Symbol Map

| File | Action |
|---|---|
| `src/doc3gpp/models/semantic_search.py` | Rename `fts5_hit` field → `hit`; update the 4-line docstring (3 references). |
| `src/doc3gpp/services/semantic_search_service.py` | Update 2× `fts5_hit=` kwarg; 1× guard; 1× `or` chain; 1× `replace` call; 4 docstring references. |
| `src/doc3gpp/cli.py` | Update JSON dict key + 4 accessor reads; 1 docstring. |
| `tests/unit/test_rrf.py` | Rename test function; update 1 assertion; update 1 comment. |
| `tests/unit/test_semantic_models.py` | Update 2 constructor calls; 2 assertions. |
| `tests/unit/test_semantic_search_service.py` | Update ~10 accessor reads (the helper `_hit()` builder is unchanged). |
| `tests/integration/test_search_sem_end_to_end.py` | Update 1 assertion. |
| `docs/cli.md` | Update `search sem` JSON example. |
| `README.md` | Update `search sem` JSON example. |
| `docs/superpowers/specs/2026-07-31-embedding-search-design.md` | Historical spec amendment. |
| `docs/superpowers/specs/2026-08-01-semantic-search-revision-design.md` | Historical spec amendment. |

---

## Task 1: Rename the dataclass field + cascade through service + CLI

**Files:**
- Modify: `src/doc3gpp/models/semantic_search.py:40-62`
- Modify: `src/doc3gpp/services/semantic_search_service.py` (8 sites)
- Modify: `src/doc3gpp/cli.py:4506-4554` (5 sites)

**Interfaces:**
- Consumes: `SemanticSearchHit(fts5_hit=SearchHit, ...)` kwarg from existing call sites
- Produces: `SemanticSearchHit(hit=SearchHit, ...)` kwarg — same shape, renamed

- [ ] **Step 1: Rename the field in `models/semantic_search.py`**

Read the file first. Then edit the dataclass block (lines 40-62):

1. Rename `fts5_hit: SearchHit` → `hit: SearchHit` (line 56).
2. Update the docstring (lines 47-54). Replace the 4 lines that mention `fts5_hit` so the field is referenced consistently:

```python
    """A single merged hit from the RRF fusion of FTS5 + vector rankings.

    ``rank_fts5`` / ``rank_vec`` are the 0-based positions in the
    respective fan-out lists, or ``None`` when the ``tdoc_id`` was not
    present in that side's fan-out. ``min_chunk_distance`` is the
    lowest cosine distance across all chunks for this ``tdoc_id``
    (``None`` when the tdoc had no vector rows). ``best_chunk_id`` is
    the chunk that produced the min distance (for ``--explain``
    rendering). ``hit`` is the existing :class:`SearchHit` sub-record
    carrying the TDoc's metadata bag (title, ftp_url, meeting, tsg,
    uploaded_date, wis, previews); when the tdoc was vector-only, the
    service synthesizes a minimal :class:`SearchHit` from the
    ``tdocs`` JOIN so the renderer can reuse the existing shape.
    """
```

(Note the trailing-semicolon → colon after "minimal :class:`SearchHit` from the" was a typo in the prior version; the edit above corrects it. If you prefer to leave it as a semicolon, do so — but do not propagate the typo to other docstrings.)

- [ ] **Step 2: Update `services/semantic_search_service.py`**

Read the file first. Apply 8 mechanical renames. Use `replaceAll` for safety:

1. Line ~54 (docstring of `rrf_merge`): change `"``fts5_hit`` is ``None`` for vector-only tdogs"` → `"``hit`` is ``None`` for vector-only tdogs"`.
2. Line ~79 (`rrf_merge` `SemanticSearchHit(...)` constructor kwarg): `fts5_hit=fts5_by_id.get(tdoc_id)` → `hit=fts5_by_id.get(tdoc_id)`. Also flip the inline comment `# None for vector-only` (no change needed — already factually correct under the new name).
3. Line ~161 (vector-only branch constructor kwarg): `fts5_hit=None,  # populated below` → `hit=None,  # populated below`.
4. Line ~179 (`fts5_hits = self._fts5.search(...)`): NO CHANGE — `fts5_hits` is the plural parameter that stays.
5. Line ~182 (`rrf_merge(fts5_hits, vec_hits, ...)`): NO CHANGE.
6. Line ~193 (docstring of `_populate_metadata_stubs`): change `"Synthesize the ``fts5_hit`` SearchHit for hits missing one."` → `"Synthesize the ``hit`` SearchHit for hits missing one."`.
7. Line ~198 (docstring continuation): `"the synthesized ``fts5_hit`` from"` → `"the synthesized ``hit`` from"`.
8. Line ~199 (docstring continuation): `"the JOIN result. Returns the same list with each ``fts5_hit``"` → `"the JOIN result. Returns the same list with each ``hit``"`.
9. Line ~203 (`missing_ids = [h.tdoc_id for h in hits if h.fts5_hit is None]`): `h.fts5_hit` → `h.hit`.
10. Line ~210 (`fts5_hit=h.fts5_hit or _build_fts5_stub(...)` inside `dataclasses.replace(...)`): `fts5_hit=h.fts5_hit` → `hit=h.hit`.

Use `replaceAll` for the simple `fts5_hit` → `hit` substitution in this file, then manually fix any false positives (e.g. don't touch `fts5_hits`, `_fts5_stub`, `_get_spacy_pipeline` is already gone).

- [ ] **Step 3: Update `cli.py`**

Read `src/doc3gpp/cli.py:4506-4554` (`_render_semantic_hits`). Apply 5 mechanical renames:

1. Line ~4508 (renderer docstring): `"The ``fts5_hit`` sub-record is"` → `"The ``hit`` sub-record is"`.
2. Line ~4520 (JSON dict key): `"fts5_hit": {` → `"hit": {`.
3. Line ~4521-4522 (inside the dict comprehension): `h.fts5_hit.tdoc_id`, `h.fts5_hit.title`, `h.fts5_hit.ftp_url`, `h.fts5_hit.wis` → `h.hit.tdoc_id`, `h.hit.title`, `h.hit.ftp_url`, `h.hit.wis`.
4. Line ~4539 (markdown renderer): `if h.fts5_hit.title:` → `if h.hit.title:`.
5. Line ~4540: `f"   title: {h.fts5_hit.title}"` → `f"   title: {h.hit.title}"`.
6. Line ~4554 (table renderer): `title = (h.fts5_hit.title or "")[:40]` → `title = (h.hit.title or "")[:40]`.

- [ ] **Step 4: Sanity check src/ is consistent**

Run:

```bash
cd /home/jerry/personal/doc3gpp
rg -n "fts5_hit" src/
```

Expected: 0 hits. (All `fts5_hit` → `hit` replacements have landed.)

- [ ] **Step 5: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add src/doc3gpp/models/semantic_search.py src/doc3gpp/services/semantic_search_service.py src/doc3gpp/cli.py
git commit -m "refactor(semantic): rename SemanticSearchHit.fts5_hit → hit"
```

---

## Task 2: Update unit tests (`test_rrf.py`, `test_semantic_models.py`, `test_semantic_search_service.py`)

**Files:**
- Modify: `tests/unit/test_rrf.py` (1 function rename + 1 assertion + 1 comment)
- Modify: `tests/unit/test_semantic_models.py` (2 constructor calls + 2 assertions)
- Modify: `tests/unit/test_semantic_search_service.py` (~10 accessor reads)

- [ ] **Step 1: Run the affected unit test files first; confirm they fail**

Run:

```bash
cd /home/jerry/personal/doc3gpp && python -m pytest tests/unit/test_rrf.py tests/unit/test_semantic_models.py tests/unit/test_semantic_search_service.py -x -q
```

Expected: failures on `AttributeError: type object 'SemanticSearchHit' has no attribute 'fts5_hit'` (the dataclass field is gone).

- [ ] **Step 2: Update `tests/unit/test_rrf.py`**

1. Rename function `test_rrf_synthesizes_fts5_hit_for_vector_only_tdoc` → `test_rrf_synthesizes_hit_for_vector_only_tdoc`.
2. Update the comment `# fts5_hit is None for vector-only; service fills it later` → `# hit is None for vector-only; service fills it later`.
3. Update the assertion `assert out[0].fts5_hit is None` → `assert out[0].hit is None`.

- [ ] **Step 3: Update `tests/unit/test_semantic_models.py`**

Read the file first. Three edits:

1. Line ~27: `fts5_hit=_hit(),` (inside `SemanticSearchHit(...)`) → `hit=_hit(),`.
2. Line ~32: `assert h.fts5_hit.tdoc_id == "R5-1"` → `assert h.hit.tdoc_id == "R5-1"`.
3. Line ~37: `fts5_hit=_hit(),` → `hit=_hit(),`.

- [ ] **Step 4: Update `tests/unit/test_semantic_search_service.py`**

Read the file first. Replace every `hit.fts5_hit` accessor with `hit.hit`:

```bash
cd /home/jerry/personal/doc3gpp
rg -n "fts5_hit" tests/unit/test_semantic_search_service.py
```

For each match:
- `hit.fts5_hit is not None` → `hit.hit is not None`
- `hit.fts5_hit.title` → `hit.hit.title`
- `hit.fts5_hit.ftp_url` → `hit.hit.ftp_url`
- `hit.fts5_hit.wis` → `hit.hit.wis`
- `hit.fts5_hit.meeting` → `hit.hit.meeting`
- `hit.fts5_hit.tsg` → `hit.hit.tsg`
- `hit.fts5_hit.uploaded_date` → `hit.hit.uploaded_date`

Also: the inline comment `synthesized \`\`fts5_hit\`\` stub must still carry the real ...` → `synthesized \`\`hit\`\` stub must still carry the real ...`.

DO NOT touch any `fts5_hits` (plural) reference — there are none in this file, but if you see them, leave them alone. DO NOT touch the helper `_hit()` (it's the `SearchHit` builder, not a hit accessor).

- [ ] **Step 5: Run the unit tests; verify they pass**

Run:

```bash
cd /home/jerry/personal/doc3gpp
ruff check tests/unit/test_rrf.py tests/unit/test_semantic_models.py tests/unit/test_semantic_search_service.py
python -m pytest tests/unit/test_rrf.py tests/unit/test_semantic_models.py tests/unit/test_semantic_search_service.py -x -q
```

Expected: pass.

- [ ] **Step 6: Confirm src/ + tests/unit are clean**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" src/ tests/
```

Expected: 0 hits. (One commits, one command — Task 1 + Task 2 cover the entire rename surface.)

- [ ] **Step 7: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add tests/unit/test_rrf.py tests/unit/test_semantic_models.py tests/unit/test_semantic_search_service.py
git commit -m "test(semantic): update unit tests for fts5_hit → hit rename"
```

---

## Task 3: Update integration test (`test_search_sem_end_to_end.py`)

**Files:**
- Modify: `tests/integration/test_search_sem_end_to_end.py` (1 assertion)

- [ ] **Step 1: Run the integration test first; confirm it fails on the assertion**

Run:

```bash
cd /home/jerry/personal/doc3gpp && python -m pytest tests/integration/test_search_sem_end_to_end.py -m "not online and not mysql" -q 2>&1 | tail -5
```

Expected: failure on `assert h.fts5_hit is not None` with `AttributeError`.

- [ ] **Step 2: Update the assertion**

In `tests/integration/test_search_sem_end_to_end.py`:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" tests/integration/
```

Expected: exactly one hit at line ~244. Replace `assert h.fts5_hit is not None` with `assert h.hit is not None`.

- [ ] **Step 3: Run the integration tests; verify they pass**

Run:

```bash
cd /home/jerry/personal/doc3gpp
ruff check tests/integration/test_search_sem_end_to_end.py
python -m pytest tests/integration/test_search_sem_end_to_end.py -m "not online and not mysql" -q 2>&1 | tail -5
```

Expected: pass (or skip if fixtures unavailable; either is acceptable).

- [ ] **Step 4: Confirm tests/ is clean**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" tests/
```

Expected: 0 hits.

- [ ] **Step 5: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add tests/integration/test_search_sem_end_to_end.py
git commit -m "test(semantic): update integration test for fts5_hit → hit rename"
```

---

## Task 4: Update user-facing docs (`docs/cli.md`, `README.md`)

**Files:**
- Modify: `docs/cli.md` (search sem JSON example)
- Modify: `README.md` (search sem JSON example)

- [ ] **Step 1: Find the stale examples**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" docs/ README.md
```

Expected: a small number of hits (likely 2-4) in the JSON output examples for `search sem`.

- [ ] **Step 2: Update `docs/cli.md`**

For each `fts5_hit` hit in `docs/cli.md`:
- If it's inside a JSON example block, change `"fts5_hit": {…}` → `"hit": {…}` and update any inline prose mentioning the old key.
- If it's a docstring or comment, change `fts5_hit` → `hit`.

- [ ] **Step 3: Update `README.md`**

Same rule as Step 2.

- [ ] **Step 4: Verify both files are clean**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" README.md docs/cli.md
```

Expected: 0 hits.

- [ ] **Step 5: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add README.md docs/cli.md
git commit -m "docs(semantic): update search sem JSON examples for fts5_hit → hit rename"
```

---

## Task 5: Update historical specs and final verification

**Files:**
- Modify: `docs/superpowers/specs/2026-07-31-embedding-search-design.md`
- Modify: `docs/superpowers/specs/2026-08-01-semantic-search-revision-design.md`

- [ ] **Step 1: Find all `fts5_hit` references in the historical specs**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" docs/superpowers/specs/
```

Expected: ~10-15 hits across the two specs, mostly in:
- The `SemanticSearchHit` dataclass definition (rename `fts5_hit: SearchHit` → `hit: SearchHit`).
- The dataclass docstring (rename `fts5_hit` → `hit`).
- The service `search()` method body (rename `fts5_hit=h.fts5_hit or ...` → `hit=h.hit or ...`, etc.).

- [ ] **Step 2: Update both specs in place**

For each spec, apply the same field-rename substitutions that landed in `src/`:

1. In the `SemanticSearchHit` dataclass block: `fts5_hit: SearchHit` → `hit: SearchHit`; update the docstring.
2. In any service code samples: `fts5_hit=...` → `hit=...`; `h.fts5_hit` → `h.hit`.
3. In any JSON output examples: `"fts5_hit": {…}` → `"hit": {…}`.
4. In any prose: `fts5_hit` → `hit`.

Use `replaceAll` on each file but verify it doesn't replace `fts5_hits` (plural), `_fts5_stub`, `fts5_hit` substring in URLs, etc.

- [ ] **Step 3: Verify specs are clean**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" docs/superpowers/specs/
```

Expected: 0 hits.

- [ ] **Step 4: Final global sweep**

Run:

```bash
cd /home/jerry/personal/doc3gpp && rg -n "fts5_hit" src/ tests/ docs/ README.md AGENTS.md
```

Expected: 0 hits anywhere. (`fts5_hits` (plural) is fine — that stays.)

- [ ] **Step 5: Run the full sqlite test script + ruff + CLI help smoke check**

Run:

```bash
cd /home/jerry/personal/doc3gpp
ruff check .
./scripts/test_sqlite.sh 2>&1 | tail -3
python -c "
import sys; sys.path.insert(0, 'src')
from typer.testing import CliRunner
from doc3gpp.cli import app
r = CliRunner()
out = r.invoke(app, ['search', 'sem', '--help']).stdout
assert '--fts5-query' in out
assert '--fts5-weight' in out
assert '--fts5-hit' not in out
print('cli help smoke: ok')
"
```

Expected: ruff clean (or only pre-existing F401 in `tests/unit/test_protocols_semantic.py`), sqlite suite green (1511 passed / 1 skipped / 7 deselected — same as the prior revision), CLI help smoke `ok`.

- [ ] **Step 6: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add docs/superpowers/specs/2026-07-31-embedding-search-design.md docs/superpowers/specs/2026-08-01-semantic-search-revision-design.md
git commit -m "docs(semantic): amend historical specs for fts5_hit → hit rename"
```

---

## Self-Review Checklist (run before declaring done)

- [ ] **Spec coverage**: every requirement in `2026-08-01-fts5-hit-rename-design.md` has a task that implements it. Spot-check: dataclass field (T1), service cascade (T1), CLI cascade (T1), unit tests (T2), integration test (T3), user-facing docs (T4), historical specs (T5).
- [ ] **Placeholder scan**: zero `TODO`/`TBD`/`FIXME` in any task. Run: `rg -n "TODO|TBD|FIXME" docs/superpowers/plans/2026-08-01-fts5-hit-rename.md`.
- [ ] **Type consistency**: every `SemanticSearchHit(hit=...)` constructor call uses the same kwarg name; every `h.hit.*` accessor in src/ + tests/ uses the renamed field.
- [ ] **Plural preserved**: zero `fts5_hits` → `hit` substitutions. Run: `rg -n "fts5_hits" src/ tests/ docs/ README.md` and confirm all hits remain `fts5_hits` (plural).
- [ ] **`SearchHit` preserved**: no accidental rename of the dataclass itself. Run: `rg -n "class SearchHit" src/ tests/` and confirm both occurrences (`models/search.py` definition + use sites) are intact.
- [ ] **`_build_fts5_stub` preserved**: the helper name stays. Run: `rg -n "_build_fts5_stub" src/ tests/` and confirm it still exists.
- [ ] **Final global sweep**: `rg -n "fts5_hit" src/ tests/ docs/ README.md AGENTS.md` returns 0 hits.
- [ ] **Suite**: `./scripts/test_sqlite.sh` green; ruff clean (modulo pre-existing F401).
