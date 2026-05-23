#!/bin/bash
# Cron-ready daily pipeline runner.
# Do NOT install as a cron job without explicit approval.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"
source .venv/bin/activate
python scripts/daily_pipeline.py
