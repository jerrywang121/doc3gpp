#!/usr/bin/env bash
set -euo pipefail

# SQLite-only profile: excludes online tests.
python -m pytest -q \
  --cov=src/doc3gpp \
  --cov-report=term-missing \
  -m "not online"
