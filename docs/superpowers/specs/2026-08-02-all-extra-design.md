# `doc3gpp[all]` Install Extra — Design

**Date:** 2026-08-02
**Status:** Approved
**Branch:** `remove-mysql-postgres-add-lancedb`

## Goal

Add a single `[all]` optional extra so users can install every runtime
capability with one command: `pip install "doc3gpp[all]"`.

## Background

`pyproject.toml` `[project.optional-dependencies]` currently defines five
extras:

- `cli` — `typer`, `tqdm` (the `doc3gpp` command)
- `extract` — `python-docx` (TDoc extraction pipeline)
- `search` — empty (FTS5 is stdlib sqlite; exists for the install story)
- `semantic` — `sentence-transformers`, `sqlite-vec` (hybrid vector search)
- `dev` — `doc3gpp[cli]` + pytest + pytest-cov + ruff (test/lint tooling)

There is no way to install all runtime extras without listing them
individually.

## Decision

Add a new `all` extra that aggregates the four runtime extras. Dev tooling
is deliberately excluded — `[all]` is user-facing; `[dev]` stays the
contributor/test target.

```toml
[project.optional-dependencies]
cli = ["typer>=0.12.3", "tqdm>=4.66.0"]
extract = ["python-docx>=0.8.11"]
search = []
semantic = [
  "sentence-transformers>=2.7.0",
  "sqlite-vec>=0.1.0",
]
all = [
  "doc3gpp[cli]",
  "doc3gpp[extract]",
  "doc3gpp[search]",
  "doc3gpp[semantic]",
]
dev = [
  "doc3gpp[cli]",
  "pytest>=8.2.0",
  "pytest-cov>=5.0.0",
  "ruff>=0.5.0",
]
```

Referencing other extras via `doc3gpp[<name>]` requirements is standard
PEP 508 syntax; pip and hatchling resolve them against the same project.

## Scope

### Files to modify

| File | Change |
| --- | --- |
| `pyproject.toml` | Add the `all` extra (entry above, between `semantic` and `dev`). |
| `README.md` | Install section (~lines 63–88): add `pip install "doc3gpp[all]"` with a one-line description ("every runtime extra: CLI, extraction, search, and semantic"); existing per-extra lines stay. |
| `AGENTS.md` | Extras list (~line 28): add `- .[all]` bullet. |
| `docs/cli.md` | Intro install block (~lines 7–20): mention `[all]` alongside `[cli]` as an install option. |

### Verification

- No runtime code, settings, or existing extras change.
- No test files change (extras are a packaging concern).
- Verify: `python -c "import tomllib; tomllib.load(open('pyproject.toml','rb'))"` parses; `pip install -e ".[all]"` resolves and installs cleanly (or, if the environment forbids installs, validate that pip can resolve the extra requirements with `pip install --dry-run`).
- `ruff check .` stays clean (no Python changes, but the repo gate is unconditional).
- `./scripts/test_sqlite.sh` stays green (unchanged code paths).

## Non-goals

- No `[all]` references in runtime code, error messages, or settings.
- No change to `dev` or any existing extra.
- No documentation of `[all]` beyond the three files above.
- `docs/superpowers/**` historical spec/plan documents are not retro-edited.

## Acceptance criteria

1. `rg -n "doc3gpp\\[all\\]" pyproject.toml README.md AGENTS.md docs/cli.md`
   matches the four intended sites (pyproject entry + 3 doc mentions).
2. `pyproject.toml` parses as valid TOML; the `all` extra lists exactly
   `doc3gpp[cli]`, `doc3gpp[extract]`, `doc3gpp[search]`,
   `doc3gpp[semantic]`.
3. `pip install -e ".[all]"` (or `--dry-run`) succeeds.
4. `ruff check .` clean; `./scripts/test_sqlite.sh` green.
