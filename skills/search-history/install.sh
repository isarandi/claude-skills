#!/usr/bin/env bash
# Install the history indexer daemon as a systemd user service.
# The search itself (search_history.py) works without the daemon by scanning
# JSONL files directly; the daemon adds the fast SQLite index and plan search.
set -euo pipefail
cd "$(dirname "$0")"

UV="$HOME/.local/bin/uv"
if [ ! -x "$UV" ]; then
    echo "Error: uv not found at $UV (the unit file expects it there)." >&2
    echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

if [ "$(uname -s)" != "Linux" ]; then
    echo "Error: the daemon requires Linux (inotify)." >&2
    exit 1
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
cp claude-history-indexer.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now claude-history-indexer.service

echo "Installed and started claude-history-indexer.service"
echo "Index DB: ~/.local/share/claude-history-index/history.db (created and synced automatically)"
echo "Check status: systemctl --user status claude-history-indexer.service"
