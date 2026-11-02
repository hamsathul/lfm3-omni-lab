#!/usr/bin/env bash
# Start LFM3-Omni Lab: sets up its Python environment on first run, then opens the page.
# The model itself is started from the page (Start model button).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
PYTHON="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv and installing mlx-vlm (a minute or two)..."
  "$PYTHON" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

if ! .venv/bin/python -c "import playwright" 2>/dev/null; then
  echo "Installing Playwright, which the Web tools use to drive a browser..."
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
  echo "Note: Web uses Google Chrome, which isn't installed. Install it, or run: .venv/bin/playwright install chromium"
fi

if ! command -v ffmpeg >/dev/null && [ ! -x /opt/homebrew/bin/ffmpeg ]; then
  echo "Note: ffmpeg not found. Audio, recording and video need it: brew install ffmpeg"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. If the lab is already running, open http://localhost:$PORT"
  echo "Otherwise pick another port: PORT=8766 ./start.sh"
  exit 1
fi

URL="http://localhost:$PORT"
echo "LFM3-Omni Lab: $URL  (Ctrl+C to quit; this also stops the model)"
if [ "${NO_BROWSER:-0}" != "1" ]; then
  ( sleep 1.5; open "$URL" ) &
fi
exec .venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port "$PORT" --log-level warning
