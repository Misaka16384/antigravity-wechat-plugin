#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[1/2] Installing WeChat Assistant plugin into Antigravity..."
agy plugin install "$REPO_ROOT"

INSTALLED_ROOT="$HOME/.gemini/config/plugins/wechat-assistant"
if [ ! -d "$INSTALLED_ROOT" ]; then
  INSTALLED_ROOT="$REPO_ROOT"
fi

echo "[2/2] Launching guided first-run setup wizard..."
node "$INSTALLED_ROOT/scripts/run-py.mjs" "$INSTALLED_ROOT/sidecars/bridge/setup.py" --all
