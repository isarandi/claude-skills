#!/usr/bin/env bash
# Install the history indexer daemon as a background service: a systemd user
# service on Linux, a launchd agent on macOS.
# The search itself (search_history.py) works without the daemon by scanning
# JSONL files directly; the daemon adds the fast SQLite index and plan search.
set -euo pipefail
cd "$(dirname "$0")"

case "$(uname -s)" in
Linux)
    UV="$HOME/.local/bin/uv"
    if [ ! -x "$UV" ]; then
        echo "Error: uv not found at $UV (the unit file expects it there)." >&2
        echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
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
    ;;

Darwin)
    UV="$(command -v uv || true)"
    if [ -z "$UV" ]; then
        echo "Error: uv not found on PATH." >&2
        echo "Install it with: brew install uv" >&2
        exit 1
    fi

    LABEL=local.claude-history-indexer
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    LOG="$HOME/Library/Logs/claude-history-indexer.log"
    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"

    # launchd starts agents with a minimal PATH; add uv's directory so uv can
    # find a Python there (Homebrew's) before downloading one.
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV</string>
        <string>run</string>
        <string>$HOME/.claude/skills/search-history/daemon.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$UV"):/usr/bin:/bin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <!-- Restart only on failure, like Restart=on-failure -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>19</integer>
    <key>LowPriorityIO</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
EOF

    # Reload if already installed, so re-running picks up changes
    DOMAIN="gui/$(id -u)"
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
        launchctl bootout "$DOMAIN/$LABEL"
        while launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; do sleep 0.2; done
    fi
    launchctl bootstrap "$DOMAIN" "$PLIST"

    echo "Installed and started $LABEL"
    echo "Index DB: ~/.local/share/claude-history-index/history.db (created and synced automatically)"
    echo "Check status: launchctl print $DOMAIN/$LABEL | head -20"
    echo "Log: ~/Library/Logs/claude-history-indexer.log"
    ;;

*)
    echo "Error: the daemon supports Linux (inotify) and macOS (polling) only." >&2
    exit 1
    ;;
esac
