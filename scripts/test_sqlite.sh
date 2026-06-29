#!/usr/bin/env bash
set -euo pipefail

# SQLite-only profile: excludes mysql backend tests and online tests.
python -m pytest -q \
  --cov=src/doc3gpp \
  --cov-report=term-missing \
  -m "not mysql and not online"
