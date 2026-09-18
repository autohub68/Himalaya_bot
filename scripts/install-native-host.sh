#!/usr/bin/env bash
# One-time setup: registers the native messaging helper so the extension's
# "Start server" button can launch the backend. Supports Linux and macOS.
# Run it once, then reload the extension at chrome://extensions.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_NAME="com.himalayas.hiring_assistant"
HOST_PATH="$PROJECT_DIR/native/him_host.py"
# Fixed by the "key" field in extension/manifest.json, so it is the same on every machine.
EXTENSION_ID="$(python3 - "$PROJECT_DIR/extension/manifest.json" <<'PY'
import base64, hashlib, json, sys
der = base64.b64decode(json.load(open(sys.argv[1]))["key"])
print("".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32]))
PY
)"

chmod +x "$HOST_PATH"

if [ "$(uname)" = "Darwin" ]; then
  BASE="$HOME/Library/Application Support"
  DIRS=("Google/Chrome" "Chromium" "BraveSoftware/Brave-Browser" "Microsoft Edge")
else
  BASE="$HOME/.config"
  DIRS=("google-chrome" "chromium" "BraveSoftware/Brave-Browser" "microsoft-edge")
fi

for dir in "${DIRS[@]}"; do
  [ -d "$BASE/$dir" ] || continue
  target="$BASE/$dir/NativeMessagingHosts"
  mkdir -p "$target"
  cat > "$target/$HOST_NAME.json" <<JSON
{
  "name": "$HOST_NAME",
  "description": "Starts and stops the Himalayas Hiring Assistant backend",
  "path": "$HOST_PATH",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
JSON
  echo "Registered for $dir"
done

echo "Extension ID: $EXTENSION_ID"
echo "Reload the extension at chrome://extensions, then use the Start server button."
