#!/usr/bin/env bash
# Clack Voice Relay — automated setup
# Usage: bash scripts/setup.sh [--port 9878] [--domain clack.example.com]
set -euo pipefail

PORT="${VOICE_RELAY_PORT:-9878}"
DOMAIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2;;
    --domain) DOMAIN="$2"; shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

# Resolve skill directory (parent of scripts/)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Clack Voice Relay Setup ==="
echo "Skill dir: $SKILL_DIR"
echo "Port: $PORT"

# ── OpenClaw config ──

# Find OpenClaw config — check SUDO_USER's home first, then current HOME
if [[ -n "${SUDO_USER:-}" ]]; then
  _REAL_HOME=$(eval echo "~$SUDO_USER")
else
  _REAL_HOME="$HOME"
fi
OPENCLAW_CONFIG="${_REAL_HOME}/.openclaw/openclaw.json"
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

# ── API keys (at least one TTS provider needed) ──

# ── API keys ──
# Prompt for any keys not already in environment

echo ""
echo "API Keys (press Enter to skip any you don't have):"
echo "  - OpenAI: Required for server-side STT (Whisper). Also provides TTS."
echo "  - ElevenLabs: Premium TTS voices."
echo "  - Deepgram: Alternative STT/TTS provider."
echo ""

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  read -rp "OpenAI API key: " _KEY
  [[ -n "$_KEY" ]] && OPENAI_API_KEY="$_KEY"
else
  echo "  OpenAI: ✓ (from env)"
fi

if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
  read -rp "ElevenLabs API key: " _KEY
  [[ -n "$_KEY" ]] && ELEVENLABS_API_KEY="$_KEY"
else
  echo "  ElevenLabs: ✓ (from env)"
fi

if [[ -z "${DEEPGRAM_API_KEY:-}" ]]; then
  read -rp "Deepgram API key: " _KEY
  [[ -n "$_KEY" ]] && DEEPGRAM_API_KEY="$_KEY"
else
  echo "  Deepgram: ✓ (from env)"
fi

if [[ -z "${OPENAI_API_KEY:-}" && -z "${ELEVENLABS_API_KEY:-}" && -z "${DEEPGRAM_API_KEY:-}" ]]; then
  echo ""
  echo "ℹ️  No provider keys set — server-side STT/TTS won't be available."
  echo "   Users can still use on-device STT/TTS from the iOS app."
fi

# ── Auth token ──

if [[ -z "${RELAY_AUTH_TOKEN:-}" ]]; then
  RELAY_AUTH_TOKEN="$(openssl rand -base64 32 | tr -d '/+=' | head -c 44)"
  echo "Generated RELAY_AUTH_TOKEN: $RELAY_AUTH_TOKEN"
fi

# ── Python venv in skill directory ──

echo "Setting up Python venv..."
python3 -m venv "$SKILL_DIR/venv"
"$SKILL_DIR/venv/bin/pip" install -q fastapi uvicorn aiohttp websockets

# ── systemd service ──

SERVICE_FILE="/etc/systemd/system/clack.service"

# Build environment lines
ENV_LINES="Environment=OPENCLAW_GATEWAY_URL=$OPENCLAW_GATEWAY_URL
Environment=OPENCLAW_GATEWAY_TOKEN=$OPENCLAW_GATEWAY_TOKEN
Environment=RELAY_AUTH_TOKEN=$RELAY_AUTH_TOKEN
Environment=VOICE_RELAY_PORT=$PORT
Environment=PYTHONUNBUFFERED=1"

[[ -n "${ELEVENLABS_API_KEY:-}" ]] && ENV_LINES="$ENV_LINES
Environment=ELEVENLABS_API_KEY=$ELEVENLABS_API_KEY"
[[ -n "${OPENAI_API_KEY:-}" ]] && ENV_LINES="$ENV_LINES
Environment=OPENAI_API_KEY=$OPENAI_API_KEY"
[[ -n "${DEEPGRAM_API_KEY:-}" ]] && ENV_LINES="$ENV_LINES
Environment=DEEPGRAM_API_KEY=$DEEPGRAM_API_KEY"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Clack Voice Relay (OpenClaw)
After=network.target

[Service]
Type=simple
WorkingDirectory=$SKILL_DIR
$ENV_LINES
ExecStart=$SKILL_DIR/venv/bin/python server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable clack
systemctl restart clack

# ── Optional: SSL with domain ──

if [[ -z "$DOMAIN" ]]; then
  echo ""
  echo "─────────────────────────────────────────────"
  echo "Do you have a domain name pointing to this server?"
  echo "A domain enables secure WSS connections (recommended)."
  echo ""
  echo "  Example: clack.yourdomain.com"
  echo ""
  read -rp "Domain (leave empty to skip): " DOMAIN
fi

SERVER_IP=$(curl -s -4 ifconfig.me 2>/dev/null || echo "<your-server-ip>")

if [[ -n "$DOMAIN" ]]; then
  echo ""
  echo "Setting up SSL for $DOMAIN..."

  # Check if domain resolves to this server
  DOMAIN_IP=$(dig +short "$DOMAIN" 2>/dev/null | tail -1)
  if [[ "$DOMAIN_IP" != "$SERVER_IP" ]]; then
    echo ""
    echo "⚠️  WARNING: $DOMAIN resolves to $DOMAIN_IP, but this server is $SERVER_IP"
    echo "   Make sure your DNS A record points $DOMAIN → $SERVER_IP"
    echo "   SSL setup will likely fail until DNS is correct."
    read -rp "Continue anyway? (y/N): " _CONT
    [[ "$_CONT" != "y" && "$_CONT" != "Y" ]] && DOMAIN=""
  fi
fi

if [[ -n "$DOMAIN" ]]; then
  # Detect reverse proxy
  if command -v caddy &>/dev/null; then
    PROXY="caddy"
  elif command -v nginx &>/dev/null; then
    PROXY="nginx"
  else
    PROXY=""
  fi

  if [[ "$PROXY" == "caddy" ]]; then
    echo "Detected Caddy — adding reverse proxy config..."
    CADDY_CONF="/etc/caddy/Caddyfile"
    if ! grep -q "$DOMAIN" "$CADDY_CONF" 2>/dev/null; then
      cat >> "$CADDY_CONF" <<CADEOF

$DOMAIN {
    reverse_proxy localhost:$PORT
}
CADEOF
      systemctl reload caddy 2>/dev/null || caddy reload --config "$CADDY_CONF" 2>/dev/null
      echo "✅ Caddy configured — SSL will be provisioned automatically"
    else
      echo "Domain already in Caddy config"
    fi
    CONNECT_URL="wss://$DOMAIN/voice"

  elif [[ "$PROXY" == "nginx" ]]; then
    echo "Detected nginx — creating config..."
    if ! command -v certbot &>/dev/null; then
      echo "Installing certbot..."
      apt-get install -y -qq certbot python3-certbot-nginx
    fi
    NGINX_CONF="/etc/nginx/sites-available/clack"
    cat > "$NGINX_CONF" <<NGEOF
server {
    listen 80;
    server_name $DOMAIN;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
NGEOF
    ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/clack
    nginx -t && systemctl reload nginx
    echo "Running certbot for SSL..."
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email 2>&1 || {
      echo "⚠️  Certbot failed — you may need to run manually: certbot --nginx -d $DOMAIN"
    }
    CONNECT_URL="wss://$DOMAIN/voice"

  else
    echo ""
    echo "No supported reverse proxy detected (Caddy or nginx)."
    echo ""
    echo "Quick option — install Caddy:"
    echo "  apt install caddy"
    echo "  Re-run: bash scripts/setup.sh --domain $DOMAIN"
    echo ""
    CONNECT_URL="wss://$DOMAIN/voice"
  fi
else
  CONNECT_URL="ws://$SERVER_IP:$PORT/voice"
fi

# ── Done ──

echo ""
echo "════════════════════════════════════════════════"
echo "  Clack Voice Relay — Setup Complete"
echo "════════════════════════════════════════════════"
echo ""
echo "  Service:  systemctl status clack"
echo "  Logs:     journalctl -u clack -f"
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │ Connection URL:                         │"
echo "  │ $CONNECT_URL"
echo "  │                                         │"
echo "  │ Auth Token:                             │"
echo "  │ $RELAY_AUTH_TOKEN"
echo "  └─────────────────────────────────────────┘"
echo ""
if [[ "$CONNECT_URL" == ws://* ]]; then
  echo "  ⚠️  No domain configured — using unencrypted ws://"
  echo "  For encryption, re-run with a domain:"
  echo "    bash scripts/setup.sh --domain clack.yourdomain.com"
  echo ""
  echo "  Or use Tailscale for encrypted connections without a domain."
fi
echo ""
echo "  Enter the URL and token in the Clack iOS app to connect."
echo ""
