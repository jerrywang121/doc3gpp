#!/usr/bin/env bash
set -euo pipefail

# SQLite-only profile. The default ``addopts`` in pyproject.toml
# already enforces ``-m 'not online'`` (so plain ``pytest`` is
# offline too); this script adds coverage. It runs sequentially:
# xdist fan-out stays off until the Windows xdist run recorded in
# docs/TODO.md is done (collection divergence on WSL2 is fixed).
python -m pytest -q \
  -m "not online" \
  --cov=src/doc3gpp \
  --cov-report=term-missing \
  "$@"
