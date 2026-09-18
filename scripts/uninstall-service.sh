#!/usr/bin/env bash
# Stops and removes the background service installed by install-service.sh.
set -euo pipefail

SERVICE_NAME="him-hiring-assistant.service"
UNIT_DIR="$HOME/.config/systemd/user"

systemctl --user disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "$UNIT_DIR/$SERVICE_NAME"
systemctl --user daemon-reload

echo "Backend service removed. Run scripts/install-service.sh to reinstall it."
