#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

HOST="${MARKET_SIGNAL_HOST:-127.0.0.1}"
PORT="${MARKET_SIGNAL_PORT:-8000}"
URL="http://${HOST}:${PORT}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required but was not found."
  echo "Install Python 3.9+ from https://www.python.org/downloads/ and try again."
  exit 1
fi

if ! python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
then
  echo "Python 3.9+ is required."
  python3 --version || true
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

PYTHON=".venv/bin/python"

echo "Installing/updating dependencies..."
"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r requirements.txt

if [ ! -f "config/config.yaml" ] && [ -f "config/config.example.yaml" ]; then
  echo "Creating config/config.yaml from config/config.example.yaml..."
  cp config/config.example.yaml config/config.yaml
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "A server is already listening on ${URL}."
  echo "Opening the existing app..."
  open "$URL" >/dev/null 2>&1 || true
  exit 0
fi

echo "Starting market-signal-scanner at ${URL}"
open "$URL" >/dev/null 2>&1 || true
exec "$PYTHON" -m market_signal_scanner.api.server
