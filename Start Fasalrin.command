#!/bin/bash
# Fasalrin control panel for macOS: double-click this file.
#   first start : makes its own Python environment, installs the libraries and the Chromium browser
#   every start : gets the newest code (git pull, or the public repo's zip), then opens the panel
# Your data (master/, branches/, logs, logins) stays in this folder and is never touched by an update.

cd "$(dirname "$0")" || exit 1
APP="$(pwd)"
REPO="sohil2312/fasalrin-panel"
PORT="${FASALRIN_PORT:-8765}"
URL="http://localhost:$PORT"

say() { printf '\n== %s\n' "$*"; }
fail() { printf '\n!! %s\n\nPress Enter to close.' "$*"; read -r; exit 1; }

# ---- 1. newest code -------------------------------------------------------------------------
if [ -d .git ] && command -v git >/dev/null 2>&1; then
  say "Checking for updates (git)"
  git pull --ff-only -q || echo "   (update skipped: no internet, or local changes)"
else
  say "Checking for updates"
  latest=$(curl -fsSL --max-time 15 "https://api.github.com/repos/$REPO/commits/main" 2>/dev/null \
           | sed -n 's/^  "sha": "\([0-9a-f]*\)".*/\1/p' | head -1)
  have=$(cat .version 2>/dev/null)
  if [ -n "$latest" ] && [ "$latest" != "$have" ]; then
    tmp=$(mktemp -d)
    if curl -fsSL --max-time 120 "https://codeload.github.com/$REPO/zip/refs/heads/main" -o "$tmp/code.zip" \
       && unzip -q "$tmp/code.zip" -d "$tmp"; then
      src=$(find "$tmp" -maxdepth 1 -type d -name 'fasalrin-panel-*' | head -1)
      rsync -a "$src/" "$APP/" && echo "$latest" > .version && echo "   updated to ${latest:0:7}"
      chmod +x "$APP/Start Fasalrin.command" 2>/dev/null
    else
      echo "   (update skipped: download failed)"
    fi
    rm -rf "$tmp"
  elif [ -n "$latest" ]; then
    echo "   up to date (${latest:0:7})"
  else
    echo "   (update skipped: no internet)"
  fi
fi

# ---- 2. Python ------------------------------------------------------------------------------
PY=""
for p in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  open "https://www.python.org/downloads/macos/" 2>/dev/null
  fail "Python 3.10 or newer is needed. Install it from python.org (the page is opening), then double-click this file again."
fi

# ---- 3. own environment + libraries + browser (first start, or when requirements change) ---------
if [ ! -x .venv/bin/python ]; then
  say "First start: setting up (a few minutes)"
  "$PY" -m venv .venv || fail "could not create the Python environment"
fi
req=$(shasum requirements.txt 2>/dev/null | cut -d' ' -f1)
if [ "$req" != "$(cat .venv/.req 2>/dev/null)" ]; then
  say "Installing libraries"
  .venv/bin/python -m pip install -q --upgrade pip >/dev/null 2>&1
  .venv/bin/python -m pip install -q -r requirements.txt || fail "library install failed (internet?)"
  echo "$req" > .venv/.req
fi
if [ ! -f .venv/.chromium ]; then
  say "Installing the Chromium browser for the portal"
  .venv/bin/python -m playwright install chromium || fail "Chromium install failed (internet?)"
  touch .venv/.chromium
fi

# ---- 4. the panel -----------------------------------------------------------------------------
if curl -fs -o /dev/null --max-time 2 "$URL/"; then
  say "The panel is already running: opening it"
  open "$URL"; exit 0
fi
say "Starting the panel on $URL  (keep this window open; close it to stop the panel)"
FASALRIN_PORT="$PORT" .venv/bin/python -W ignore portal.py &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT INT TERM
for _ in $(seq 1 30); do
  curl -fs -o /dev/null --max-time 1 "$URL/" && break
  kill -0 $PID 2>/dev/null || fail "the panel stopped while starting (see the lines above)"
  sleep 1
done
open "$URL"
wait $PID
