# Task 4 Report: Spec Parsed CLI Output

## Status

Implemented the approved CLI-only parsed-output contract.

## Changed Files

- `src/doc3gpp/cli.py`
  - Added `_parse_bool_option(value, option_name)` for strict, case-insensitive `true` / `false` parsing.
  - Added `spec list --parsed` and forwarded its native `bool | None` value to `SpecService.list_recent`.
  - Added a spec-specific structured JSON row builder so `Spec.parsed is None` is emitted as JSON `null`; all other list fields retain the existing string coercion.
  - Added `parsed` to `spec show` version fields.
  - Preserved native per-version booleans in show JSON.
  - Added boolean-aware table/Markdown display so `False` is rendered as `False`, not `-`.
  - Preserved `--no-wis-crs` filtering and existing compact-output handling.
- `tests/integration/test_spec_cli.py`
  - Added coverage for native spec-list `null`, case-insensitive parsed filter forwarding, invalid filter rejection, native spec-show booleans, `--no-wis-crs` preservation, and table/Markdown `False` rendering.

No persistence, schema, migration, service, web, MCP, or generic emitter changes were made.

## Behavior

- `doc3gpp spec list --parsed true` and `--parsed TRUE` forward `True`.
- `doc3gpp spec list --parsed false` forwards `False`.
- Omitting `--parsed` forwards `None`.
- Any other value fails with `--parsed must be 'true' or 'false'`.
- Spec-list JSON keeps `parsed` as native `null`, `true`, or `false`; other fields keep their legacy string values.
- Spec-show JSON includes `versions[*].parsed` as native booleans, including when `--no-wis-crs` is used.
- Spec-show table and Markdown output render an unparsed version as `False`.
- Existing compact JSON/Markdown behavior and generic table/Markdown output paths remain unchanged.

## Test-First Verification

The first valid red run after correcting the test-only missing `pytest` import was:

```text
rtk pytest tests/integration/test_spec_cli.py -q
Pytest: 19 passed, 8 failed
Failures:
- spec-list JSON emitted '-' instead of null
- spec-list parsed option was missing
- invalid parsed value was not rejected
- spec-show version parsed field was missing from JSON
- --no-wis-crs output omitted parsed
- spec-show table did not render parsed
```

After implementation, the focused CLI suite passed:

```text
rtk pytest tests/integration/test_spec_cli.py -q
Pytest: 28 passed
```

## Final Commands and Outputs

```text
rtk pytest tests/integration/test_spec_cli.py tests/unit/test_compact_helpers.py -q
Pytest: 59 passed
```

```text
rtk ruff check .
[]
```

```text
rtk git diff --check
clean (no output)
```

The final pre-commit worktree contained only the two intended modified files plus the pre-existing untracked plan file:

```text
 M src/doc3gpp/cli.py
 M tests/integration/test_spec_cli.py
?? docs/superpowers/plans/2026-09-25-spec-parsed-output.md
```

The untracked plan file was not staged or modified.

## Commits

- `8b89d9a` (`feat(spec): add parsed CLI output`) — implementation and tests.

## Concerns

- The full test suite was not run; verification was limited to the covering spec CLI tests, compact-output regression tests, Ruff, and diff checks.
- No known functional concerns remain within Task 4 scope.
