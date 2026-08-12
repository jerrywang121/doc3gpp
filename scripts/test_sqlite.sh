#!/usr/bin/env bash
set -euo pipefail

# SQLite-only profile. The default ``addopts`` in pyproject.toml
# already enforces ``-m 'not online'`` (so plain ``pytest`` is
# offline too); this script adds coverage and the xdist fan-out
# for a faster local run when pytest-xdist is installed.
extra=()
if python -c "import xdist" 2>/dev/null; then
  extra+=(-n auto)
fi
python -m pytest -q \
  -m "not online" \
  --cov=src/doc3gpp \
  --cov-report=term-missing \
  "${extra[@]}" "$@"
