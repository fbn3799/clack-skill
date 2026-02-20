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

# Read OpenClaw config automatically
OPENCLAW_CONFIG="${HOME}/.openclaw/openclaw.json"
if [[ -f "$OPENCLAW_CONFIG" ]]; then
  echo "Found OpenClaw config: $OPENCLAW_CONFIG"
  if command -v python3 &>/dev/null; then
    _GW_TOKEN=$(python3 -c "import json; c=json.load(open('$OPENCLAW_CONFIG')); print(c.get('gateway',{}).get('auth',{}).get('token',''))" 2>/dev/null)
    _GW_PORT=$(python3 -c "import json; c=json.load(open('$OPENCLAW_CONFIG')); print(c.get('gateway',{}).get('port',18789))" 2>/dev/null)
    [[ -n "$_GW_TOKEN" ]] && OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-$_GW_TOKEN}"
    OPENCLAW_GATEWAY_URL="${OPENCLAW_GATEWAY_URL:-http://127.0.0.1:${_GW_PORT:-18789}}"
    echo "  Gateway: $OPENCLAW_GATEWAY_URL (token: auto-detected)"
  fi
else
  echo "No OpenClaw config found at $OPENCLAW_CONFIG"
  if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
    read -rp "OpenClaw gateway token: " OPENCLAW_GATEWAY_TOKEN
  fi
  OPENCLAW_GATEWAY_URL="${OPENCLAW_GATEWAY_URL:-http://127.0.0.1:18789}"
fi

if [[ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ]]; then
  echo "ERROR: Could not determine OpenClaw gateway token"
  exit 1
fi

# ElevenLabs key — only thing the user needs to provide
if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
  read -rp "ElevenLabs API key: " ELEVENLABS_API_KEY
fi

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
