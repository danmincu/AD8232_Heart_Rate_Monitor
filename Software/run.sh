#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Create venv if missing
if [ ! -d .venv ]; then
  echo "Creating Python virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

# Install dependencies
pip install -q aiohttp pyserial-asyncio

echo "Starting AD8232 ECG Web Monitor..."
exec python3 ecg_server.py "$@"
