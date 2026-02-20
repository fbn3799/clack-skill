#!/usr/bin/env bash
# Clack Voice Relay — automated setup
# Usage: bash setup.sh [--port 9878] [--install-dir /opt/clack] [--domain clack.example.com]
set -euo pipefail

PORT="${VOICE_RELAY_PORT:-9878}"
INSTALL_DIR="/opt/clack"
DOMAIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2;;
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    --domain) DOMAIN="$2"; shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Clack Voice Relay Setup ==="
echo "Install dir: $INSTALL_DIR"
echo "Port: $PORT"

# ── OpenClaw config ──

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

# ── ElevenLabs key ──

if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
  read -rp "ElevenLabs API key: " ELEVENLABS_API_KEY
fi

# ── Auth token ──

if [[ -z "${RELAY_AUTH_TOKEN:-}" ]]; then
  RELAY_AUTH_TOKEN="$(openssl rand -base64 32 | tr -d '/+=' | head -c 44)"
  echo "Generated RELAY_AUTH_TOKEN: $RELAY_AUTH_TOKEN"
fi

# ── Install server ──

mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/server.py" "$INSTALL_DIR/server.py"

echo "Setting up Python venv..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q fastapi uvicorn aiohttp websockets

# ── systemd service ──

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
  elif docker ps --format '{{.Names}}' 2>/dev/null | grep -q nginx; then
    PROXY="nginx-docker"
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
    # Install certbot if needed
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
    echo "Install one and configure it to proxy $DOMAIN → localhost:$PORT"
    echo ""
    echo "Quick option — install Caddy:"
    echo "  apt install caddy"
    echo "  echo '$DOMAIN { reverse_proxy localhost:$PORT }' > /etc/caddy/Caddyfile"
    echo "  systemctl start caddy"
    echo ""
    CONNECT_URL="wss://$DOMAIN/voice"
  fi

  # Save domain config for later reference
  cat > "$INSTALL_DIR/ssl.conf" <<SSLEOF
DOMAIN=$DOMAIN
PROXY=$PROXY
CONNECT_URL=$CONNECT_URL
SSLEOF

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
  echo "  ⚠️  Connection is UNENCRYPTED (ws://)"
  echo "  To enable encryption, re-run with a domain:"
  echo "    bash setup.sh --domain clack.yourdomain.com"
  echo ""
  echo "  Or set up SSL later — see docs for details."
fi
echo ""
echo "  Enter the URL and token in the Clack iOS app to connect."
echo ""
