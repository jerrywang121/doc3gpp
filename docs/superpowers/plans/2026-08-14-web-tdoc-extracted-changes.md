# Web tdoc detail — Extracted Changes Sections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the parser's structured extraction results (TTCN `required_changes`, non-TTCN body-derived change blocks) as two new cards on the web tdoc detail page (`GET /tdocs/{id}` → `tdoc_show.html`).

**Architecture:** Template-only change. The data is already in `TDocShowRecord.ttcn` and `TDocShowRecord.changes`; the existing template silently drops both. Add two `{% if %}` blocks to `tdoc_show.html` that render the new cards when populated, using the same `card` / `kv` / `<pre>` patterns already in the file. No route, service, model, or repo changes. The CLI renderers, JSON envelope, and MCP tool are all unaffected.

**Tech Stack:** Jinja2 templates, FastAPI, SQLAlchemy 2.0 (read-only), existing `TDocShowRecord` composition.

## Global Constraints

- Python 3.10+, SQLAlchemy 2.0, Pydantic v2 — no new dependencies.
- Lint: `ruff check .` must pass.
- Tests: full sqlite suite via `./scripts/test_sqlite.sh` must pass; new test file is unit-level (no online marks, no network).
- No changes to `cli.py`, `web/routes/tdocs.py`, `web/render.py`, `web/filters.py`, `models/`, or any storage repo.
- `tdoc_show.html` line numbers cited in the spec are accurate as of commit `367c67a`; the existing TTCN card ends at line 109, the "Extracted at" card starts at line 111. New cards slot between.
- The new `change-block` CSS class on the `<pre>` element is a future-styling hook; no CSS file changes ship with this plan.
- `?format=json` and the MCP `tdoc_show` tool are byte-identical before and after this plan (no model changes).

## File Structure

| File | Responsibility |
| --- | --- |
| `src/doc3gpp/web/templates/tdoc_show.html` | Add two new `{% if %}` cards (Card 1 "Required changes", Card 2 "Extracted changes") between the existing TTCN card and the "Extracted at" card. |
| `tests/unit/test_web_routes.py` | Append four new test functions covering the two new cards (TTCN with corrections, non-TTCN with body changes, no sidecars, TTCN with empty `required_changes`). Reuses the existing `client` / `sqlite_env` fixtures. |
| `AGENTS.md` | One-line addition to the "Where to look" table. |
| `docs/web-server.md` | One-sentence addition in the tdoc detail page section. |

No new files outside the four above.

---

### Task 1: Add the "Required changes" card to the template

**Files:**
- Modify: `src/doc3gpp/web/templates/tdoc_show.html:109-110` (insert after the closing `{% endif %}` of the existing TTCN card)
- Test: `tests/unit/test_web_routes.py` (append at end of file)

**Interfaces:**
- Consumes: `record.ttcn` (`TDocCRTTCNDetails | None`) and `record.ttcn.required_changes` (`list[dict[str, Any]]`). Each dict has these keys (per the TTCN parser's `TTCNCorrectionsParser` in `src/doc3gpp/parsers/cr/ttcn_sections.py`): `function_name`, `ttcn_module`, `reason_for_change`, `summary_of_change`, `mcc160_comment` — each optional (any key may be missing or `None`).
- Produces: a `<section class="card">` containing one `<article class="change-entry">` per entry, each with a `<dl class="kv">` listing the present fields.

- [ ] **Step 1: Write the failing test**

Append the following test function to the end of `tests/unit/test_web_routes.py`:

```python
def test_tdoc_show_required_changes_card_for_ttcn(
    client: TestClient, sqlite_env: Any,
) -> None:
    """The TTCN section is followed by a 'Required changes' card listing each entry.

    Mirrors the existing ``test_tdoc_show_ttcn_changed_functions`` pattern:
    seed a tdoc row + a TTCN sidecar with two required_changes entries,
    GET the page, and assert the new card surfaces both entries with
    their structured fields.
    """
    from doc3gpp.models.tdoc_cr import TDocCRTTCNDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrTtcnRepository().upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            testcase="TC_1",
            changed_functions=["mod_a.fn_one"],
            required_changes=[
                {
                    "function_name": "fl_TC_7_1_3_5_3_Body",
                    "ttcn_module": "NR_DC_Testcases.ttcn",
                    "reason_for_change": "Change due to MCX feature addition.",
                    "summary_of_change": "Use new PDCP function.",
                    "mcc160_comment": "OK",
                },
                {
                    "function_name": "fl_TC_7_1_3_5_4_Body",
                },
            ],
        ),
    )
    response = client.get("/tdocs/R5s260001")
    assert response.status_code == 200
    body = response.text
    # Card heading present
    assert "<h2>Required changes</h2>" in body
    # Two <article class="change-entry"> elements
    assert body.count('class="change-entry"') == 2
    # First entry renders all five fields
    assert "<dt>Function name</dt>" in body
    assert "<code>fl_TC_7_1_3_5_3_Body</code>" in body
    assert "<dt>TTCN module</dt>" in body
    assert "<code>NR_DC_Testcases.ttcn</code>" in body
    assert "<dt>Reason for change</dt>" in body
    assert "Change due to MCX feature addition." in body
    assert "<dt>Summary of change</dt>" in body
    assert "Use new PDCP function." in body
    assert "<dt>MCC160 comment</dt>" in body
    # Second entry only renders function_name
    assert body.count("<dt>Function name</dt>") == 2
    assert body.count("<dt>TTCN module</dt>") == 1
    assert body.count("<dt>Reason for change</dt>") == 1
    # Existing TTCN card regression guard — changed_functions still present
    assert "<dt>Changed functions</dt>" in body
    assert "<code>mod_a.fn_one</code>" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jerry/personal/doc3gpp && python -m pytest tests/unit/test_web_routes.py::test_tdoc_show_required_changes_card_for_ttcn -v`
Expected: FAIL with `AssertionError: '<h2>Required changes</h2>' not in body` (the card doesn't exist yet).

- [ ] **Step 3: Add the "Required changes" card to the template**

Open `src/doc3gpp/web/templates/tdoc_show.html` and insert the following block immediately after line 109 (the closing `{% endif %}` of the existing TTCN card), and before line 110 (the blank line preceding the "Extracted at" card):

```jinja2

  {% if record.ttcn and record.ttcn.required_changes %}
    <section class="card">
      <h2>Required changes</h2>
      {% for change in record.ttcn.required_changes %}
        <article class="change-entry">
          <dl class="kv">
            {% if change.function_name %}<dt>Function name</dt><dd><code>{{ change.function_name }}</code></dd>{% endif %}
            {% if change.ttcn_module %}<dt>TTCN module</dt><dd><code>{{ change.ttcn_module }}</code></dd>{% endif %}
            {% if change.reason_for_change %}<dt>Reason for change</dt><dd>{{ change.reason_for_change }}</dd>{% endif %}
            {% if change.summary_of_change %}<dt>Summary of change</dt><dd>{{ change.summary_of_change }}</dd>{% endif %}
            {% if change.mcc160_comment %}<dt>MCC160 comment</dt><dd>{{ change.mcc160_comment }}</dd>{% endif %}
          </dl>
        </article>
      {% endfor %}
    </section>
  {% endif %}
```

Preserve the exact indentation of the surrounding lines (two-space indent for `{% if %}` / `{% endfor %}` tags, four-space indent for inner elements). The final result must be: existing TTCN card `{% endif %}`, blank line, new `{% if %}` block, blank line, then the existing "Extracted at" card.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jerry/personal/doc3gpp && python -m pytest tests/unit/test_web_routes.py::test_tdoc_show_required_changes_card_for_ttcn -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add src/doc3gpp/web/templates/tdoc_show.html tests/unit/test_web_routes.py
git commit -m "feat(web): add 'Required changes' card to tdoc detail for TTCN CRs"
```

---

### Task 2: Add the "Extracted changes" card to the template (non-TTCN)

**Files:**
- Modify: `src/doc3gpp/web/templates/tdoc_show.html` (insert immediately after the closing `{% endif %}` of the new "Required changes" card from Task 1)
- Test: `tests/unit/test_web_routes.py` (append at end of file)

**Interfaces:**
- Consumes: `record.changes` (`TDocCRChangeDetails | None`). Carries `clauses: tuple[str, ...]` and `changes: tuple[ChangeBlock, ...]`. Each `ChangeBlock` is `{"clauses": list[str], "text": str}`. Both `clauses` (the per-block one) and `text` may be empty.
- Produces: a `<section class="card">` with a header line (clauses + block count) and one `<article class="change-block-entry">` per block, each with an `<h3>` (block number + per-block clauses) and a `<pre><code class="change-block">` carrying the captured text.

- [ ] **Step 1: Write the failing test**

Append the following test function to the end of `tests/unit/test_web_routes.py`:

```python
def test_tdoc_show_extracted_changes_card_for_non_ttcn(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A non-TTCN CR with body-derived change blocks renders the 'Extracted changes' card.

    Seeds a non-TTCN tdoc + a tdoc_cr_change_details row with 2 clauses
    and 2 change blocks (block 1 has clauses + text; block 2 has clauses
    only, empty text), GETs the page, and asserts the new card surfaces
    the header line, block headings, and <pre> block text.
    """
    from doc3gpp.models.tdoc_cr_change_details import TDocCRChangeDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_change_details_sql import (
        SQLAlchemyTDocCrChangeDetailsRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5-260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url=url),
    )
    SQLAlchemyTDocCrChangeDetailsRepository().upsert(
        TDocCRChangeDetails(
            tdoc_id="R5-260001",
            ftp_url=url,
            clauses=("5.2.3", "Table 5.2.3-1"),
            changes=(
                {
                    "clauses": ["5.2.3"],
                    "text": "first block\n<ins>added line</ins>\nmore text",
                },
                {
                    "clauses": ["Table 5.2.3-1"],
                    "text": "",
                },
            ),
        ),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    body = response.text
    # Card heading present
    assert "<h2>Extracted changes</h2>" in body
    # Header line: clauses string + block count
    assert "5.2.3, Table 5.2.3-1" in body
    assert "2 block(s)" in body
    # Two <article class="change-block-entry"> elements
    assert body.count('class="change-block-entry"') == 2
    # Two <pre><code class="change-block"> elements
    assert body.count('class="change-block"') == 2
    # Block headings render block number + per-block clauses
    assert "<h3>Block 1 · clauses: 5.2.3</h3>" in body
    assert "<h3>Block 2 · clauses: Table 5.2.3-1</h3>" in body
    # Captured text byte-faithful
    assert "first block" in body
    assert "<ins>added line</ins>" in body
    # TTCN card is not present (non-TTCN CRs never have record.ttcn)
    assert "<h2>Required changes</h2>" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/jerry/personal/doc3gpp && python -m pytest tests/unit/test_web_routes.py::test_tdoc_show_extracted_changes_card_for_non_ttcn -v`
Expected: FAIL with `AssertionError: '<h2>Extracted changes</h2>' not in body` (the card doesn't exist yet).

- [ ] **Step 3: Add the "Extracted changes" card to the template**

Open `src/doc3gpp/web/templates/tdoc_show.html` and insert the following block immediately after the closing `{% endif %}` of the new "Required changes" card (from Task 1), and before the existing "Extracted at" card:

```jinja2

  {% if record.changes %}
    <section class="card">
      <h2>Extracted changes</h2>
      <p class="meta">
        <span>Clauses: <code>{{ record.changes.clauses | join(', ') or '—' }}</code></span>
        <span>{{ record.changes.changes | length }} block(s)</span>
      </p>
      {% for block in record.changes.changes %}
        <article class="change-block-entry">
          <h3>Block {{ loop.index }}{% if block.clauses %} · clauses: {{ block.clauses | join(', ') }}{% endif %}</h3>
          <pre><code class="change-block">{{ block.text }}</code></pre>
        </article>
      {% endfor %}
    </section>
  {% endif %}
```

Same indentation rules as Task 1: two-space indent for `{% if %}` / `{% for %}` / `{% endfor %}` tags, four-space indent for inner elements. Final structure: existing TTCN card → "Required changes" card (Task 1) → "Extracted changes" card (Task 2) → "Extracted at" card.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/jerry/personal/doc3gpp && python -m pytest tests/unit/test_web_routes.py::test_tdoc_show_extracted_changes_card_for_non_ttcn -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add src/doc3gpp/web/templates/tdoc_show.html tests/unit/test_web_routes.py
git commit -m "feat(web): add 'Extracted changes' card to tdoc detail for non-TTCN CRs"
```

---

### Task 3: Add the negative-case tests (no sidecars, empty required_changes)

**Files:**
- Test: `tests/unit/test_web_routes.py` (append at end of file)

**Interfaces:**
- Same as Tasks 1 and 2. Verifies the absence gates work: empty record → neither card; TTCN with empty `required_changes` → "Required changes" card omitted, "TTCN" card still present.

- [ ] **Step 1: Write the two failing tests**

Append the following two test functions to the end of `tests/unit/test_web_routes.py`:

```python
def test_tdoc_show_no_extracted_changes_cards_when_no_sidecars(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A TDoc with no cover/ttcn/changes sidecars shows neither new card.

    The 'Cover page' placeholder is still rendered (regression guard).
    """
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5-260001", ftp_url="R5/26.001/R5-260001.zip"),
    )
    response = client.get("/tdocs/R5-260001")
    assert response.status_code == 200
    body = response.text
    assert "<h2>Required changes</h2>" not in body
    assert "<h2>Extracted changes</h2>" not in body
    # Regression guard: existing placeholder still renders
    assert "<h2>Cover page</h2>" in body
    assert "Not yet extracted" in body


def test_tdoc_show_ttcn_without_required_changes_omits_card(
    client: TestClient, sqlite_env: Any,
) -> None:
    """A TTCN CR with no corrections (empty required_changes) hides the new card.

    The existing TTCN card still renders (regression guard).
    """
    from doc3gpp.models.tdoc_cr import TDocCRTTCNDetails
    from doc3gpp.storage.db.migrate import create_schema
    from doc3gpp.storage.repositories.tdoc_cr_ttcn_sql import (
        SQLAlchemyTDocCrTtcnRepository,
    )
    from doc3gpp.storage.repositories.tdoc_sql import SQLAlchemyTDocRepository

    create_schema()
    url = "R5/26.001/R5s260001.zip"
    SQLAlchemyTDocRepository().upsert(
        TDoc(tdoc_id="R5s260001", ftp_url=url),
    )
    SQLAlchemyTDocCrTtcnRepository().upsert(
        TDocCRTTCNDetails(
            tdoc_id="R5s260001",
            ftp_url=url,
            testcase="TC_1",
            required_changes=[],
        ),
    )
    response = client.get("/tdocs/R5s260001")
    assert response.status_code == 200
    body = response.text
    assert "<h2>Required changes</h2>" not in body
    # Regression guard: existing TTCN card still renders
    assert "<h2>TTCN</h2>" in body
    assert "<dt>Testcase</dt>" in body
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd /home/jerry/personal/doc3gpp && python -m pytest tests/unit/test_web_routes.py::test_tdoc_show_no_extracted_changes_cards_when_no_sidecars tests/unit/test_web_routes.py::test_tdoc_show_ttcn_without_required_changes_omits_card -v`
Expected: PASS (the template already gates both cards on the correct conditions; these tests pin the existing behavior).

- [ ] **Step 3: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add tests/unit/test_web_routes.py
git commit -m "test(web): pin absent/extracted-changes-card behavior on tdoc show"
```

---

### Task 4: Run full sqlite test suite and lint

**Files:**
- None modified.

- [ ] **Step 1: Run the full sqlite test suite**

Run: `cd /home/jerry/personal/doc3gpp && ./scripts/test_sqlite.sh`
Expected: ALL PASS, no regressions. The four new test functions should appear in the output under `tests/unit/test_web_routes.py`. Pre-existing tests for the tdoc show route, TTCN sidecar, and tdoc_cr_change_details should also pass unchanged.

- [ ] **Step 2: Run ruff**

Run: `cd /home/jerry/personal/doc3gpp && ruff check .`
Expected: no findings. The new tests use the same imports and style as the rest of `test_web_routes.py`, so this should be a clean pass.

- [ ] **Step 3: Confirm all green**

If either step 1 or step 2 reports a failure, fix the underlying issue (do not skip or `--exitfirst`-bypass). Common fixes:
- A new test references a symbol that has moved — check imports in `tests/unit/test_web_routes.py`.
- A template `{% if %}` block has a syntax error — Jinja raises at template compile; check the indentation of the new blocks matches the surrounding lines.

---

### Task 5: Update AGENTS.md and docs/web-server.md

**Files:**
- Modify: `AGENTS.md` (one-line addition to the "Where to look" table)
- Modify: `docs/web-server.md` (one-sentence addition in the tdoc detail page section)

**Interfaces:**
- None. Documentation-only changes.

- [ ] **Step 1: Find the "Where to look" table row for the tdoc detail page**

In `AGENTS.md`, locate the existing row in the "Where to look" table that mentions the tdoc detail page or `tdoc_show.html`. Add a new bullet (or extend the existing bullet) noting the two new cards. The cleanest minimal addition: find the row that begins "Add a web route / HTML page" or any row mentioning `tdoc_show.html`, and append a single bullet:

```markdown
- The tdoc detail page (`tdoc_show.html`) renders two extra cards when the parent TDoc has been parsed: 'Required changes' (one entry per TTCN `required_changes` dict) for TTCN CRs, and 'Extracted changes' (one entry per body-derived change block) for non-TTCN CRs. Both cards are gated on the sidecar's presence and are mutually exclusive.
```

- [ ] **Step 2: Find the tdoc detail page section in docs/web-server.md**

In `docs/web-server.md`, locate the section that describes the web tdoc detail page (`GET /tdocs/{id}`). Add one sentence noting the new cards, immediately after the sentence that mentions the existing "Cover page" / "TTCN" / "Auxiliary files" cards. Suggested wording:

> "When a parsed TDoc has structured sidecar data, the page also surfaces a 'Required changes' card (TTCN CRs) and an 'Extracted changes' card (non-TTCN CRs); both are mutually exclusive and omitted when their respective sidecar is absent."

- [ ] **Step 3: Re-run lint to confirm docs edits are clean**

Run: `cd /home/jerry/personal/doc3gpp && ruff check .`
Expected: no findings (markdown lint is not configured; only Python files are linted).

- [ ] **Step 4: Commit**

```bash
cd /home/jerry/personal/doc3gpp
git add AGENTS.md docs/web-server.md
git commit -m "docs: note extracted changes cards on tdoc detail page"
```

---

## Self-Review

**1. Spec coverage:**
- Goal (surface `required_changes` and `changes` on the web tdoc detail page) → Tasks 1 + 2.
- Non-goal: no new repo reads / model changes → all tasks leave `cli.py`, `routes/tdocs.py`, `models/`, repos untouched. Verified by `git diff --stat` after each commit.
- Card 1 "Required changes" with the five known fields and skip-on-absent → Task 1.
- Card 2 "Extracted changes" with header line + per-block `<h3>` + `<pre>` text → Task 2.
- Empty / missing data states (TTCN with empty `required_changes`, no sidecars) → Task 3.
- JSON / MCP consistency (no change) → guaranteed by the no-model-changes constraint; the existing `test_tdoc_show_*` integration tests in `tests/integration/test_tdoc_cr_ttcn_sqlite.py` and `test_tdoc_cr_change_details_sqlite.py` continue to pass against the unchanged `to_jsonable(record)` path (re-asserted by Task 4's full suite run).
- Files-touched table: `tdoc_show.html` → Tasks 1 + 2; new test file → all tasks; `AGENTS.md` and `docs/web-server.md` → Task 5. No other files.

**2. Placeholder scan:** No "TBD" / "TODO" / "fill in details". All test code is fully specified. Template snippets are exact.

**3. Type / symbol consistency:**
- `record.ttcn.required_changes` — used in Task 1 (template + test) and Task 3 (test). Matches `TDocCRTTCNDetails.required_changes: list[dict[str, Any]]` in `src/doc3gpp/models/tdoc_cr.py:181`.
- `record.changes` — used in Task 2 (template + test). Matches `TDocShowRecord.changes: TDocCRChangeDetails | None` in `src/doc3gpp/models/tdoc_show.py:71`.
- `record.changes.clauses` and `record.changes.changes` — used in Task 2 template. Match `TDocCRChangeDetails.clauses: tuple[str, ...]` and `TDocCRChangeDetails.changes: tuple[ChangeBlock, ...]` in `src/doc3gpp/models/tdoc_cr_change_details.py:72-73`.
- `block.clauses` and `block.text` — used in Task 2 template. Match the `ChangeBlock` TypedDict in `src/doc3gpp/models/tdoc_cr_change_details.py:33-37`.
- `change.function_name` / `change.ttcn_module` / `change.reason_for_change` / `change.summary_of_change` / `change.mcc160_comment` — used in Task 1 template + Task 1 test. Match the keys the TTCN corrections parser emits (verified by `tests/unit/test_cr_parser.py:280-303`).
- Test fixture repo class names (`SQLAlchemyTDocRepository`, `SQLAlchemyTDocCrTtcnRepository`, `SQLAlchemyTDocCrChangeDetailsRepository`) — consistent across Tasks 1, 2, 3 (matches the existing `test_tdoc_show_ttcn_changed_functions` pattern at `tests/unit/test_web_routes.py:754`).
- `client` / `sqlite_env` fixture names — used in all three test tasks. Match the existing `test_web_routes.py:308-310` definitions.
- `create_schema` import — used in all three test tasks. Matches the existing pattern.
