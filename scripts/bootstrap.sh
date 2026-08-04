#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — set EQUITYTRACE_SEC_EMAIL before live SEC calls."
fi

uv sync --all-groups --no-editable
uv run equitytrace init-db
echo "Bootstrap complete. Next: uv run equitytrace ingest AAPL"
