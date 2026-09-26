#!/usr/bin/env bash
set -euo pipefail

pip install -e ".[dev]"
doc3gpp db init
doc3gpp db check

# Optional smoke test for the web server + MCP (requires the `[web]` extra).
# Start it with:
#   doc3gpp server start --no-open
# then browse the HTML UI at http://127.0.0.1:13999/ or hit the MCP endpoint
# at http://127.0.0.1:13999/mcp. Stop with `doc3gpp server stop`.
