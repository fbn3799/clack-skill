#!/usr/bin/env bash
# Clack Voice Relay — automated setup
# Usage: sudo bash scripts/setup.sh [--port 9878] [--domain clack.example.com]
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
echo ""

# ── System dependencies ──

echo "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl > /dev/null 2>&1
echo "  ✓ Python 3 + venv"

# ── OpenClaw config ──

if [[ -n "${SUDO_USER:-}" ]]; then
  _REAL_HOME=$(eval echo "~$SUDO_USER")
else
  _REAL_HOME="$HOME"
fi
OPENCLAW_CONFIG="${_REAL_HOME}/.openclaw/openclaw.json"
if [[ -f "$OPENCLAW_CONFIG" ]]; then
  echo "  ✓ OpenClaw config found"
  if command -v python3 &>/dev/null; then
    _GW_TOKEN=$(python3 -c "import json; c=json.load(open('$OPENCLAW_CONFIG')); print(c.get('gateway',{}).get('auth',{}).get('token',''))" 2>/dev/null)
    _GW_PORT=$(python3 -c "import json; c=json.load(open('$OPENCLAW_CONFIG')); print(c.get('gateway',{}).get('port',18789))" 2>/dev/null)
    [[ -n "$_GW_TOKEN" ]] && OPENCLAW_GATEWAY_TOKEN="${OPENCLAW_GATEWAY_TOKEN:-$_GW_TOKEN}"
    OPENCLAW_GATEWAY_URL="${OPENCLAW_GATEWAY_URL:-http://127.0.0.1:${_GW_PORT:-18789}}"
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

# ── Enable chat completions endpoint ──
if [[ -f "$OPENCLAW_CONFIG" ]] && command -v python3 &>/dev/null; then
  _CC_ENABLED=$(python3 -c "
import json
c=json.load(open('$OPENCLAW_CONFIG'))
print(c.get('gateway',{}).get('http',{}).get('endpoints',{}).get('chatCompletions',{}).get('enabled',False))
" 2>/dev/null)
  if [[ "$_CC_ENABLED" != "True" ]]; then
    echo "  Enabling /v1/chat/completions endpoint..."
    python3 -c "
import json
p='$OPENCLAW_CONFIG'
c=json.load(open(p))
c.setdefault('gateway',{}).setdefault('http',{}).setdefault('endpoints',{}).setdefault('chatCompletions',{})['enabled']=True
json.dump(c,open(p,'w'),indent=2)
print('  ✓ Chat completions enabled in', p)
"
    # Restart gateway to apply
    if command -v openclaw &>/dev/null; then
      openclaw gateway restart &>/dev/null && echo "  ✓ Gateway restarted" || echo "  ⚠ Could not restart gateway — restart manually: openclaw gateway restart"
    else
      echo "  ⚠ Restart your OpenClaw gateway to apply: openclaw gateway restart"
    fi
  else
    echo "  ✓ Chat completions endpoint already enabled"
  fi
fi

# ── API keys ──

echo ""
echo "API Keys (press Enter to skip any you don't have):"
echo "  Each provider offers both STT and TTS."
echo ""

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  read -rp "  OpenAI API key: " _KEY
  [[ -n "$_KEY" ]] && OPENAI_API_KEY="$_KEY"
else
  echo "  OpenAI: ✓ (from env)"
fi

if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
  read -rp "  ElevenLabs API key: " _KEY
  [[ -n "$_KEY" ]] && ELEVENLABS_API_KEY="$_KEY"
else
  echo "  ElevenLabs: ✓ (from env)"
fi

if [[ -z "${DEEPGRAM_API_KEY:-}" ]]; then
  read -rp "  Deepgram API key: " _KEY
  [[ -n "$_KEY" ]] && DEEPGRAM_API_KEY="$_KEY"
else
  echo "  Deepgram: ✓ (from env)"
fi

# Summary
[[ -n "${OPENAI_API_KEY:-}" ]] && echo "  ✓ OpenAI key saved"
[[ -n "${ELEVENLABS_API_KEY:-}" ]] && echo "  ✓ ElevenLabs key saved"
[[ -n "${DEEPGRAM_API_KEY:-}" ]] && echo "  ✓ Deepgram key saved"

if [[ -z "${OPENAI_API_KEY:-}" && -z "${ELEVENLABS_API_KEY:-}" && -z "${DEEPGRAM_API_KEY:-}" ]]; then
  echo ""
  echo "  ℹ️  No provider keys entered — server-side STT/TTS won't be available."
  echo "     The app can still use on-device STT/TTS."
fi

# ── Auth token ──

if [[ -z "${RELAY_AUTH_TOKEN:-}" ]]; then
  RELAY_AUTH_TOKEN="$(openssl rand -base64 32 | tr -d '/+=' | head -c 44)"
fi

# ── Python venv ──

echo ""
echo "Setting up Python environment..."
python3 -m venv "$SKILL_DIR/venv"
"$SKILL_DIR/venv/bin/pip" install -q fastapi uvicorn aiohttp websockets
echo "  ✓ Python dependencies installed"

# ── CLI command ──

if [[ -d /usr/local/bin ]]; then
  ln -sf "$SKILL_DIR/scripts/clack" /usr/local/bin/clack
  echo "  ✓ 'clack' command installed (clack setup / clack pair / clack logs)"
fi

# ── Connection mode ──

echo ""

# If domain provided via flag, use it
if [[ -z "$DOMAIN" ]]; then
  echo "─────────────────────────────────────────────"
  echo "How should the app connect to this server?"
  echo ""
  echo "  1) Domain — you have a domain pointing to this server (e.g. clack.example.com)"
  echo "  2) Tailscale — encrypted P2P, no domain needed (free)"
  echo ""
  read -rp "Choose [1/2]: " _CONN_CHOICE

  if [[ "$_CONN_CHOICE" == "1" ]]; then
    read -rp "Domain name: " DOMAIN
    if [[ -z "$DOMAIN" ]]; then
      echo "No domain entered, falling back to Tailscale."
      _CONN_CHOICE="2"
    fi
  fi

  if [[ "$_CONN_CHOICE" == "2" ]]; then
    # Install Tailscale if not present
    if ! command -v tailscale &>/dev/null; then
      echo ""
      echo "Installing Tailscale..."
      curl -fsSL https://tailscale.com/install.sh | sh
    fi
    if ! tailscale status &>/dev/null 2>&1; then
      echo ""
      echo "─────────────────────────────────────────────"
      echo "  Tailscale needs to join your network."
      echo "  A login link will appear below — open it"
      echo "  in your browser to authenticate."
      echo "─────────────────────────────────────────────"
      echo ""
      tailscale up
      echo ""
      echo "  ✓ Tailscale connected"
    fi
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || true)
    if [[ -z "$TAILSCALE_IP" ]]; then
      echo "ERROR: Could not get Tailscale IP. Run 'tailscale up' and try again."
      exit 1
    fi
    echo "  ✓ Tailscale IP: $TAILSCALE_IP"
  fi
fi

# ── Domain SSL setup ──

if [[ -n "$DOMAIN" ]]; then
  SERVER_IP=$(curl -s -4 ifconfig.me 2>/dev/null || echo "")
  DOMAIN_IP=$(dig +short "$DOMAIN" 2>/dev/null | tail -1)

  if [[ -n "$SERVER_IP" && "$DOMAIN_IP" != "$SERVER_IP" ]]; then
    echo ""
    echo "⚠️  $DOMAIN resolves to $DOMAIN_IP, but this server is $SERVER_IP"
    echo "   Make sure your DNS A record points $DOMAIN → $SERVER_IP"
    read -rp "Continue anyway? (y/N): " _CONT
    [[ "$_CONT" != "y" && "$_CONT" != "Y" ]] && { echo "Aborted."; exit 1; }
  fi

  echo ""
  echo "─────────────────────────────────────────────"
  echo "  SSL requires ports 80 and 443 to be open."
  echo "  If you're on AWS/GCP/Azure, make sure your"
  echo "  security group / firewall allows inbound"
  echo "  TCP on ports 80 and 443 from 0.0.0.0/0."
  echo "─────────────────────────────────────────────"
  read -rp "Ports 80 and 443 are open? (y/N): " _PORTS_OK
  [[ "$_PORTS_OK" != "y" && "$_PORTS_OK" != "Y" ]] && { echo "Open the ports first, then re-run setup."; exit 1; }

  echo ""
  echo "Setting up SSL for $DOMAIN..."

  # Install Caddy if no reverse proxy available
  if ! command -v caddy &>/dev/null && ! command -v nginx &>/dev/null; then
    echo "Installing Caddy (for automatic SSL)..."
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https > /dev/null 2>&1
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
    apt-get update -qq
    apt-get install -y -qq caddy > /dev/null 2>&1
    echo "  ✓ Caddy installed"
  fi

  if command -v caddy &>/dev/null; then
    CADDY_CONF="/etc/caddy/Caddyfile"
    if ! grep -q "$DOMAIN" "$CADDY_CONF" 2>/dev/null; then
      cat >> "$CADDY_CONF" <<CADEOF

$DOMAIN {
    reverse_proxy localhost:$PORT
}
CADEOF
      systemctl reload caddy 2>/dev/null || caddy reload --config "$CADDY_CONF" 2>/dev/null
    fi
    echo "  ✓ Caddy configured — SSL will be provisioned automatically"

  elif command -v nginx &>/dev/null; then
    if ! command -v certbot &>/dev/null; then
      apt-get install -y -qq certbot python3-certbot-nginx > /dev/null 2>&1
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
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email 2>&1 || {
      echo "⚠️  Certbot failed — run manually: certbot --nginx -d $DOMAIN"
    }
    echo "  ✓ nginx + SSL configured"
  fi
fi

# ── systemd service ──

SERVICE_FILE="/etc/systemd/system/clack.service"

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

# ── Done ──

echo ""
echo "════════════════════════════════════════════════"
echo "  ✅ Clack Voice Relay — Setup Complete"
echo "════════════════════════════════════════════════"
echo ""
echo "  Service:  systemctl status clack"
echo "  Logs:     journalctl -u clack -f"
echo ""

if [[ -n "$DOMAIN" ]]; then
  echo "  In the Clack app → Settings → Server:"
  echo ""
  echo "  ┌─────────────────────────────────────────┐"
  echo "  │  Server:     $DOMAIN"
  echo "  │  Connection: Domain (SSL)               │"
  echo "  └─────────────────────────────────────────┘"
  echo ""
  echo "  Then tap Pair and enter the pairing code."
  echo "  Generate a code anytime with:"
  echo "    clack pair"
  echo ""
  # Auto-generate first pairing code
  sleep 2  # wait for service to start
  echo "  Generating first pairing code..."
  PAIR_RESPONSE=$(curl -s "http://localhost:${PORT}/pair?token=${RELAY_AUTH_TOKEN}" 2>/dev/null || true)
  PAIR_CODE=$(echo "$PAIR_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['code'])" 2>/dev/null || true)
  if [[ -n "$PAIR_CODE" ]]; then
    echo ""
    echo "  ┌─────────────────────────────────────────┐"
    echo "  │  Pairing Code:  $PAIR_CODE (expires in 5 min)  │"
    echo "  └─────────────────────────────────────────┘"
  fi

elif [[ -n "${TAILSCALE_IP:-}" ]]; then
  echo "  In the Clack app → Settings → Server:"
  echo ""
  echo "  ┌─────────────────────────────────────────┐"
  echo "  │  Server:     $TAILSCALE_IP"
  echo "  │  Port:       $PORT"
  echo "  │  Connection: Tailscale                  │"
  echo "  └─────────────────────────────────────────┘"
  echo ""
  echo "  No pairing needed — Tailscale handles authentication."
  echo "  Make sure Tailscale is also installed on your iPhone."
fi
echo ""
