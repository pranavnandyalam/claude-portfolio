#!/bin/zsh
# launchd-friendly wrapper: runs the deterministic engine WITHOUT Claude, so paper trading
# continues when the desktop app is closed. Usage: ops/engine.sh {trade|report}
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
mkdir -p logs
LOG="logs/launchd-$(date +%Y-%m-%d).log"
case "${1:-}" in
  trade)  caffeinate -i -m .venv/bin/python scripts/run_daily.py --execute >> "$LOG" 2>&1; echo "trade exit=$?" >> "$LOG" ;;
  report) caffeinate -i -m .venv/bin/python scripts/report.py >> "$LOG" 2>&1; echo "report exit=$?" >> "$LOG" ;;
  *) echo "usage: $0 {trade|report}"; exit 2 ;;
esac
