#!/usr/bin/env bash
# Start LFM3-Omni Lab: sets up its Python environment on first run, picks and downloads the
# model checkpoint for this Mac if it isn't here yet, then opens the page.
# The model itself is started from the page (Start model button).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"

if [ "$(uname -s)" != "Darwin" ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != "1" ]; then
  echo "LFM3-Omni Lab runs the model with MLX, which needs a Mac with Apple Silicon (M1 or later)."
  exit 1
fi

# MLX needs an arm64 Python 3.10 or newer. macOS's own /usr/bin/python3 is 3.9.
pick_python() {
  for candidate in ${PYTHON:-} python3.13 python3.12 python3.11 python3.10 python3.14 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import platform, sys; sys.exit(not (sys.version_info >= (3, 10) and platform.machine() == "arm64"))' 2>/dev/null; then
      echo "$candidate"
      return
    fi
  done
}

if [ ! -x .venv/bin/python ]; then
  PYTHON="$(pick_python)"
  if [ -z "$PYTHON" ]; then
    echo "No Python 3.10 or newer was found (macOS's own python3 is 3.9). Install one with: brew install python"
    exit 1
  fi
  echo "First run: creating .venv with $PYTHON and installing mlx-vlm (a minute or two)..."
  "$PYTHON" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

# The Web tools drive a private Chrome through agent-browser (github.com/vercel-labs/agent-browser).
if ! command -v agent-browser >/dev/null; then
  echo "Note: Web needs agent-browser: brew install agent-browser (or npm install -g agent-browser), then agent-browser install"
elif [ ! -d "$HOME/.agent-browser/browsers" ]; then
  echo "Downloading the browser agent-browser drives (once)..."
  agent-browser install || echo "Note: agent-browser install failed; Web won't work until it succeeds."
fi

if ! command -v ffmpeg >/dev/null && [ ! -x /opt/homebrew/bin/ffmpeg ]; then
  echo "Note: ffmpeg not found. Audio, recording and video need it: brew install ffmpeg"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. If the lab is already running, open http://localhost:$PORT"
  echo "Otherwise pick another port: PORT=8766 ./start.sh"
  exit 1
fi

# The checkpoint: one already downloaded that fits this Mac, else the right one for its memory.
# LFM3_MODEL picks one by hand. See app/pick_model.py.
LFM3_MODEL="$(.venv/bin/python -m app.pick_model)"
export LFM3_MODEL

URL="http://localhost:$PORT"
echo "LFM3-Omni Lab: $URL  (Ctrl+C to quit; this also stops the model)"
if [ "${NO_BROWSER:-0}" != "1" ]; then
  ( sleep 1.5; open "$URL" ) &
fi
exec .venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port "$PORT" --log-level warning
