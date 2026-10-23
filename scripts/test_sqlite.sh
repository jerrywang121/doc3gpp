#!/usr/bin/env bash
set -euo pipefail

# SQLite-only profile. The default ``addopts`` in pyproject.toml
# already enforces ``-m 'not online'`` (so plain ``pytest`` is
# offline too); this script adds coverage. It runs sequentially:
# xdist fan-out is off because parallel runs fail on shared
# engine/settings state (see docs/TODO.md).
python -m pytest -q \
  -m "not online" \
  --cov=src/doc3gpp \
  --cov-report=term-missing \
  "$@"
