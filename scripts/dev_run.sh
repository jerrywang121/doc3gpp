#!/usr/bin/env bash
set -euo pipefail

pip install -e ".[dev]"
doc3gpp db init
doc3gpp db check
