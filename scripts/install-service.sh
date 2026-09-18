#!/usr/bin/env bash
# One-time setup: runs the backend as a systemd user service so it starts
# automatically on login and stays running in the background. After this,
# you never need to run uvicorn by hand again.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${HIM_VENV_DIR:-$HOME/.venvs/him}"
SERVICE_NAME="him-hiring-assistant.service"
UNIT_DIR="$HOME/.config/systemd/user"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtual environment at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "Installing dependencies"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SERVICE_NAME" <<EOF
[Unit]
Description=Himalayas Hiring Assistant backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_DIR/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

# Let the service keep running even when you're not logged in (e.g. after a reboot).
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now "$SERVICE_NAME"

echo
echo "Backend installed and running as a background service."
echo "It will start automatically on every login from now on."
echo
echo "Useful commands:"
echo "  systemctl --user status $SERVICE_NAME     # check it's running"
echo "  journalctl --user -u $SERVICE_NAME -f     # follow logs"
echo "  systemctl --user restart $SERVICE_NAME    # restart after a code change"
echo "  ./scripts/uninstall-service.sh             # remove the service"
