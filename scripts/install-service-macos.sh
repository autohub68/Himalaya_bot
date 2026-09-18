#!/usr/bin/env bash
# One-time setup on macOS: runs the backend as a launchd agent so it starts
# automatically on login and stays running in the background.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${HIM_VENV_DIR:-$HOME/.venvs/him}"
LABEL="com.him.hiring-assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "Installing dependencies"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>app.main:app</string>
        <string>--host</string>
        <string>127.0.0.1</string>
        <string>--port</string>
        <string>8765</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/him-hiring-assistant.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/him-hiring-assistant.err</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST"

echo
echo "Backend installed and running as a login item."
echo "It will start automatically on every login from now on."
echo
echo "Useful commands:"
echo "  launchctl list | grep $LABEL           # check it's running"
echo "  tail -f /tmp/him-hiring-assistant.log  # follow logs"
echo "  launchctl unload $PLIST                # stop + remove for this session"
echo "  ./scripts/uninstall-service-macos.sh   # fully uninstall"
