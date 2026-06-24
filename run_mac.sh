#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

DB_PATH="${DB_PATH:-logs/alibaba_cert_runner_506068.sqlite3}"
LIMIT="${LIMIT:-500}"
PRODUCT_TIMEOUT="${PRODUCT_TIMEOUT:-300}"
MAX_CONSECUTIVE_FAILURES="${MAX_CONSECUTIVE_FAILURES:-10}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-1}"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

usage() {
  cat <<EOF
Alibaba Certificate Runner for macOS

Usage:
  ./run_mac.sh setup          Create .venv and install requirements
  ./run_mac.sh status         Show queue progress
  ./run_mac.sh run            Process pending products
  ./run_mac.sh retry          Retry failed products
  ./run_mac.sh sync           Download product IDs from Alibaba API

Environment overrides:
  DB_PATH                     Default: $DB_PATH
  LIMIT                       Default: $LIMIT
  PRODUCT_TIMEOUT             Default: $PRODUCT_TIMEOUT
  MAX_CONSECUTIVE_FAILURES    Default: $MAX_CONSECUTIVE_FAILURES
  SLEEP_BETWEEN               Default: $SLEEP_BETWEEN

Examples:
  LIMIT=50 ./run_mac.sh run
  LIMIT=100 ./run_mac.sh retry
EOF
}

ensure_venv() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
  fi
  "$PYTHON_BIN" -m pip install -r requirements.txt
}

ensure_ready() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing .venv. Run: ./run_mac.sh setup"
    exit 1
  fi
  if [[ ! -f "$DB_PATH" ]]; then
    echo "Missing database: $DB_PATH"
    echo "Run sync first, or restore/copy the SQLite database."
    exit 1
  fi
}

show_status() {
  ensure_ready
  sqlite3 "$DB_PATH" \
    "SELECT queue_status, count(*) FROM cert_product_queue GROUP BY queue_status ORDER BY queue_status;
     SELECT status, count(*) FROM product_runs GROUP BY status ORDER BY status;"
}

run_pending() {
  ensure_ready
  "$PYTHON_BIN" tools/alibaba_cert_chrome_runner.py \
    --db "$DB_PATH" \
    --limit "$LIMIT" \
    --publish \
    --product-timeout "$PRODUCT_TIMEOUT" \
    --max-consecutive-failures "$MAX_CONSECUTIVE_FAILURES" \
    --sleep-between "$SLEEP_BETWEEN"
}

retry_failed() {
  ensure_ready
  "$PYTHON_BIN" tools/alibaba_cert_chrome_runner.py \
    --db "$DB_PATH" \
    --limit "$LIMIT" \
    --publish \
    --retry-failed \
    --attempts 2 \
    --product-timeout "$PRODUCT_TIMEOUT" \
    --max-consecutive-failures "$MAX_CONSECUTIVE_FAILURES" \
    --sleep-between "$SLEEP_BETWEEN"
}

sync_products() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Missing .venv. Run: ./run_mac.sh setup"
    exit 1
  fi
  if [[ ! -f .env ]]; then
    echo "Missing .env. Copy .env.example to .env and fill Alibaba API credentials."
    exit 1
  fi
  "$PYTHON_BIN" tools/alibaba_cert_queue_from_api.py \
    --db "$DB_PATH" \
    --read-api-total
}

case "${1:-}" in
  setup)
    ensure_venv
    ;;
  status)
    show_status
    ;;
  run)
    run_pending
    ;;
  retry)
    retry_failed
    ;;
  sync)
    sync_products
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    echo "Unknown command: $1"
    echo
    usage
    exit 1
    ;;
esac
