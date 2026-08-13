# Docs Review & Update — 2026-08-13

## Goal

Bring the 7 top-level docs files into sync with the current code so readers
get an accurate picture. Primary goal: **drift vs current code**. Secondary:
fix typos, broken refs, dead links.

## Scope

In scope (7 files):

- `README.md` — user-facing project intro
- `AGENTS.md` — contributor-facing layout + workflow guide
- `docs/architecture.md` — layered diagram, runtime data flow, ORM schema
- `docs/cli.md` — per-command CLI reference (2313 lines, highest drift risk)
- `docs/code-map.md` — symbol → file reference table
- `docs/conventions.md` — filter grammar, settings caching, commit policy
- `docs/web-server.md` — web app, MCP, jobs
- `docs/3gpp-knowledge.md` — 3GPP URL/naming/parser semantics
- `docs/known-constraints.md` — open limitations

Out of scope: `docs/superpowers/specs/*` and `docs/superpowers/plans/*`
are historical artifacts (decisions as-of-the-time); not retroactively
edited. `doc3gpp.toml.example` is the canonical config reference — only
updated if drift is found in the Settings schema.

## Approach

Symptom-driven audit. For each file:

1. Read the file.
2. Extract every concrete claim: command flag, table row, file path,
   setting name, code symbol, URL pattern, cross-doc reference.
3. Cross-check each claim against the actual code:
   - **Typer CLI flags:** `src/doc3gpp/cli.py` + `src/doc3gpp/cli_server.py`
   - **Settings names:** `src/doc3gpp/settings/schema.py`
   - **Module paths:** `ls src/doc3gpp/`
   - **Web routes:** `src/doc3gpp/web/routes/*.py` + `web/workers/handlers.py`
   - **Job kinds:** `src/doc3gpp/models/jobs.py` `JobKind` enum
   - **SQL tables / repo methods:** `src/doc3gpp/storage/db/` ORM +
     `storage/repositories/*.py`
   - **Cross-doc references:** README → AGENTS.md → docs/*.md must agree
4. Record findings in `docs/.audit-2026-08-13.md` (scratchpad, not committed).
5. Fix drift in-place.
6. Commit per file with conventional message `docs(<file>): <summary>`.
7. Add a one-line `> Last reviewed: 2026-08-13` header to the top of each
   updated file.

## Execution order (drift risk descending)

1. `docs/cli.md` (2313 lines, flags change often)
2. `docs/web-server.md` (new jobs/routes land here)
3. `docs/code-map.md` (new modules land here)
4. `docs/architecture.md` (drift in service boundaries / schema)
5. `docs/conventions.md` (drift in filter grammar / settings)
6. `docs/known-constraints.md` (drift in resolved items)
7. `docs/3gpp-knowledge.md` (3GPP-domain, mostly stable)
8. `AGENTS.md` (drift in code layout)
9. `README.md` (drift in install + CLI surface)

Stop early on any file where the audit shows zero drift.

## What I'll change vs leave

- **Fix:** drift, broken refs, wrong code paths, missing flags, wrong
  default values, dead links.
- **Add:** one-line `> Last reviewed: 2026-08-13` header per updated file.
- **Leave:** prose, structure, examples that still work.
- **Won't add:** docstrings to code, new sections, structural rewrites.

## Verification

- `ruff check .` after every commit.
- `./scripts/test_sqlite.sh` once at the end (no code changes expected to
  break tests, but it confirms no accidentally-edited code).
- The audit scratchpad is deleted (or not committed) at the end.

## Deliverable

Branch `docs/full-review-2026-08-13` off `main`, in main worktree.
Commits pushed, no PR.

## Out of scope — confirm

- No docstrings added to Python code.
- No new sections / no restructuring of existing prose.
- `docs/superpowers/*` historical specs/plans not edited.
- `doc3gpp.toml.example` only edited if Settings schema drift is found.
