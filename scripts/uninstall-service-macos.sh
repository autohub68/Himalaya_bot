#!/usr/bin/env bash
set -euo pipefail

LABEL="com.him.hiring-assistant"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl unload "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"

echo "Backend service removed. Run scripts/install-service-macos.sh to reinstall it."
