#!/usr/bin/env bash
set -euo pipefail

# SQLite-only profile. The default ``addopts`` in pyproject.toml
# already enforces ``-m 'not online'`` (so plain ``pytest`` is
# offline too); this script adds coverage. It fans out with
# pytest-xdist (``-n auto``) when installed and runs sequentially
# otherwise. Extra arguments are passed last, so ``-n 0`` forces a
# sequential run.
if python -c "import xdist" 2>/dev/null; then
  workers=(-n auto)
else
  workers=()
fi

python -m pytest -q \
  -m "not online" \
  --cov=src/doc3gpp \
  --cov-report=term-missing \
  ${workers[@]+"${workers[@]}"} \
  "$@"
