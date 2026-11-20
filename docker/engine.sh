#!/bin/bash
# Agent Engine container entrypoint. Listens only on the internal Compose network.
set -euo pipefail
cd /app
mkdir -p "$HOME"
exec python -m agent_engine --port 8765
