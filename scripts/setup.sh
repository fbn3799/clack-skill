#!/usr/bin/env bash
# Clack Voice Relay — automated setup
# Usage: bash setup.sh [--port 9878] [--install-dir /opt/clack]
set -euo pipefail

PORT="${VOICE_RELAY_PORT:-9878}"
INSTALL_DIR="/opt/clack"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2;;
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Clack Voice Relay Setup ==="
echo "Install dir: $INSTALL_DIR"
echo "Port: $PORT"

# Collect required env vars
if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
  read -rp "ElevenLabs API key: " ELEVENLABS_API_KEY
fi
if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  read -rp "OpenClaw gateway token: " OPENCLAW_GATEWAY_TOKEN
fi
OPENCLAW_GATEWAY_URL="${OPENCLAW_GATEWAY_URL:-http://127.0.0.1:18789}"

# Generate relay auth token if not set
if [[ -z "${RELAY_AUTH_TOKEN:-}" ]]; then
  RELAY_AUTH_TOKEN="$(openssl rand -base64 32 | tr -d '/+=' | head -c 44)"
  echo "Generated RELAY_AUTH_TOKEN: $RELAY_AUTH_TOKEN"
fi

# Create install dir
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"

# Python venv
echo "Setting up Python venv..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q fastapi uvicorn aiohttp websockets

# systemd unit
SERVICE_FILE="/etc/systemd/system/clack.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Clack Voice Relay (OpenClaw)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=ELEVENLABS_API_KEY=$ELEVENLABS_API_KEY
Environment=OPENCLAW_GATEWAY_URL=$OPENCLAW_GATEWAY_URL
Environment=OPENCLAW_GATEWAY_TOKEN=$OPENCLAW_GATEWAY_TOKEN
Environment=RELAY_AUTH_TOKEN=$RELAY_AUTH_TOKEN
Environment=VOICE_RELAY_PORT=$PORT
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/venv/bin/python server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable clack
systemctl restart clack

echo ""
echo "=== Setup complete ==="
echo "Service: clack (systemctl status clack)"
echo "Logs: journalctl -u clack -f"
echo "Endpoint: ws://<your-ip>:$PORT/voice?token=$RELAY_AUTH_TOKEN"
