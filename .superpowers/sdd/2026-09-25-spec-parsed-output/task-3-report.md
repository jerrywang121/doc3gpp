# Task 3 Report: Spec Parsed Output

## Status

Implemented Task 3 only.

## Changed Files

- `src/doc3gpp/services/factory.py`
  - Imported `SQLAlchemySpecDocRepository`.
  - Injected a configured instance as `SpecService.parsed_status_repository` in `build_spec_service`.
  - Preserved the existing `SpecService` return type and configured sync interval.
- `src/doc3gpp/settings/schema.py`
  - Appended `parsed` to the default `spec` output fields.
- `src/doc3gpp/data/doc3gpp.toml.example`
  - Documented `parsed` in the commented spec output field list.
- `tests/unit/test_services_factory.py`
  - Added coverage for parsed-status repository injection and sync interval preservation.
- `tests/unit/test_settings_config_file.py`
  - Added coverage for the default `parsed` spec output field.

No CLI, REST/web, MCP, documentation, schema registry, ORM, or migration changes were made.

## Commits

- `fe94c9c` (`feat(spec): wire parsed status output`)

## Test Commands and Output

The required red test run was executed before production changes:

```text
rtk pytest tests/unit/test_services_factory.py tests/unit/test_settings_config_file.py -q
Pytest: 41 passed, 2 failed
Failures:
- parsed-status repository assertion failed because the factory did not inject it
- default parsed-field assertion failed because the default ended with rapporteurs
```

Focused regression tests after implementation:

```text
rtk pytest tests/unit/test_services_factory.py tests/unit/test_settings_config_file.py tests/unit/test_spec_service.py -q
Pytest: 71 passed
```

Ruff:

```text
rtk ruff check .
[]
```

Additional verification:

```text
rtk git diff --check
clean
```

## Self-Review

- The factory test uses sentinel repository and settings objects and verifies the service's actual repository, parsed-status reader, and sync interval.
- The settings test verifies that `parsed` is the final default spec field as required.
- The packaged TOML example matches the settings default.
- Existing callers that omit `parsed_status_repository` remain supported by the optional constructor argument; the focused service regression suite passed.
- The implementation commit contains only the five files listed above.
- The pre-existing untracked plan file `docs/superpowers/plans/2026-09-25-spec-parsed-output.md` was not staged or modified.

## Concerns

- The full test suite was not run; verification was limited to the focused tests specified in the brief and the full Ruff check.
- No known functional concerns remain within Task 3 scope.
