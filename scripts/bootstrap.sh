#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example — set FILINGEDGE_SEC_EMAIL before live SEC calls."
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install from https://github.com/astral-sh/uv" >&2
  exit 1
fi

# Install the project as a regular (non-editable) package. On macOS, editable
# .pth files can receive the UF_HIDDEN flag and be ignored by Python 3.12+.
uv sync --all-groups --no-editable

uv run filingedge init-db
echo "Bootstrap complete. Next: uv run filingedge ingest AAPL"
