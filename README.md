# Clack — Voice Relay for OpenClaw

> Talk to your AI assistant by voice. Real-time, self-hosted, private.

Clack is an [OpenClaw](https://github.com/openclaw/openclaw) skill that sets up a WebSocket voice relay server. It bridges voice input through speech-to-text → your OpenClaw agent → text-to-speech, enabling natural voice conversations.

## Features

- 🎙️ **Real-time voice chat** with your OpenClaw agent
- 🔊 **Independent voice input/output**: Choose STT and TTS providers separately — ElevenLabs, OpenAI, Deepgram, or on-device
- 💰 **Cost-saving combos**: Free on-device transcription + premium cloud voices, or fully local for zero API spend
- 📱 **On-device speech**: Apple speech frameworks for STT and/or TTS — works offline, no API keys needed
- 🗣️ **20 built-in voices** with easy aliases
- 🧠 **Conversation memory**: Persisted across calls (up to 50 messages)
- 🔒 **Secure pairing**: Rate-limited one-time codes with 5-minute expiry
- 🏠 **Self-hosted**: All data flows through YOUR server — no third-party relays
- 🎯 **Session isolation**: Each call gets its own `clack:<uuid>` session
- ⚡ **Interrupt support**: Cancel TTS mid-sentence for natural conversation
- 🔇 **Echo test mode**: Test your audio pipeline without using LLM credits

## Quick Start

### 1. Install the skill

```bash
git clone https://github.com/fbn3799/clack-skill.git ~/.openclaw/workspace/skills/clack
```

### 2. Set your API key

```bash
export ELEVENLABS_API_KEY="sk_..."
# Or use OpenAI/Deepgram instead — see Configuration below
```

### 3. Run setup

```bash
bash ~/.openclaw/workspace/skills/clack/scripts/setup.sh
```

This will:
- Create a Python virtualenv and install dependencies
- Auto-detect your OpenClaw gateway config
- Generate a `RELAY_AUTH_TOKEN` if not set
- Configure a systemd service on port **9878**

### 4. (Recommended) Add SSL

```bash
bash scripts/setup.sh --domain clack.yourdomain.com
```

Requires a DNS A record pointing to your server. Auto-configures SSL via Caddy or nginx + certbot.

### 5. Pair the iOS app

1. Open the Clack iOS app ([App Store](https://github.com/fbn3799/clack-app) or build from source)
2. On your server, generate a pairing code: the app will guide you, or ask your OpenClaw agent
3. Enter the 6-character code in the app within 5 minutes
4. The app receives an auth token and connects automatically

## Configuration

All configuration is via environment variables (set in your systemd service or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `RELAY_AUTH_TOKEN` | — | **Required.** Auth token for all protected endpoints |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | OpenClaw Gateway URL |
| `OPENCLAW_GATEWAY_TOKEN` | — | Gateway bearer token |
| `STT_PROVIDER` | `elevenlabs` | `elevenlabs`, `openai`, or `deepgram` |
| `TTS_PROVIDER` | `elevenlabs` | `elevenlabs`, `openai`, or `deepgram` |
| `TTS_VOICE` | `Will` | Default voice (name or ID) |
| `ELEVENLABS_API_KEY` | — | ElevenLabs API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `DEEPGRAM_API_KEY` | — | Deepgram API key |
| `VOICE_RELAY_PORT` | `9878` | Server port |
| `CLACK_ECHO_MODE` | `false` | Enable echo test mode server-wide |
| `CLACK_MAX_INPUT_CHARS` | `300` | Max transcript length |
| `CLACK_HISTORY_DIR` | `/var/lib/clack/history` | History storage path |
| `CLACK_MAX_HISTORY` | `50` | Max conversation history messages |

> **Tip:** For local speech mode (on-device STT/TTS), you don't need any speech API keys — only the OpenClaw gateway connection.

## Security

- **Auth token** required for all endpoints except `GET /health` and `POST /pair`
- **Pairing is rate-limited**: 5 attempts per IP per 5 minutes, 2s delay on failure
- **One-time codes**: 6-character alphanumeric, expire after 5 minutes, single-use
- **Constant-time** token verification (HMAC) to prevent timing attacks
- **No telemetry**: Zero analytics, tracking, or data sent to developers
- **Voice audio** streams directly to your server — never through third-party relays
- The iOS app stores only local settings (server address, token, preferences)

## How It Works

STT and TTS are independently configurable — pick any combination of on-device and cloud providers per call.

### Cloud mode (default)

```
📱 Clack App          🖥️ Your Server          🌐 APIs
┌──────────┐  audio   ┌──────────────┐         ┌─────────────┐
│ 🎙️ Mic   ├─────────►│ Clack Relay  ├────────►│ STT Provider│
│          │           │              │◄────────┤ (transcript)│
│          │           │              │         ├─────────────┤
│          │           │              ├────────►│ OpenClaw GW │
│          │  audio    │              │◄────────┤ (AI reply)  │
│ 🔊 Speaker│◄─────────┤              ├────────►├─────────────┤
│          │           │              │◄────────┤ TTS Provider│
└──────────┘           └──────────────┘         └─────────────┘
```

### On-device STT + cloud TTS (cost saver)

```
📱 Clack App                    🖥️ Your Server          🌐 APIs
┌──────────────┐                ┌──────────────┐         ┌─────────────┐
│ 🎙️ Mic        │                │              │         │             │
│ ↓ Apple STT  │  text          │ Clack Relay  ├────────►│ OpenClaw GW │
│ "Hey, what…" ├───────────────►│              │◄────────┤ (AI reply)  │
│              │  audio          │              ├────────►├─────────────┤
│ 🔊 Speaker    │◄───────────────┤              │◄────────┤ TTS Provider│
└──────────────┘                └──────────────┘         └─────────────┘
```
STT happens on-device (free, unlimited) — only the transcript text is sent to the server. Great for saving transcription API costs while keeping premium cloud voices.

### Fully on-device (zero API spend)

```
📱 Clack App                    🖥️ Your Server
┌──────────────┐                ┌──────────────┐
│ 🎙️ Mic        │                │              │
│ ↓ Apple STT  │  text          │ Clack Relay  ├────────► OpenClaw GW
│ "Hey, what…" ├───────────────►│              │◄────────  (AI reply)
│              │  text           │              │
│ Apple TTS ↓  │◄───────────────┤              │
│ 🔊 Speaker    │                │              │
└──────────────┘                └──────────────┘
```
Both STT and TTS run on-device using Apple speech frameworks. The server only handles LLM routing — no speech API keys needed at all. Works offline (except for the LLM call).

### Mix and match

Choose providers per direction in **Settings → Voice**:

| STT | TTS | Trade-off |
|-----|-----|-----------|
| Cloud (ElevenLabs) | Cloud (ElevenLabs) | Best quality, highest cost |
| On-device | Cloud (ElevenLabs) | Free transcription + premium voices |
| On-device | On-device | Zero API spend, works offline* |
| Cloud (OpenAI) | Cloud (Deepgram) | Mix providers freely |

*Offline except for the LLM call to your OpenClaw gateway.

## Server Management

```bash
systemctl status clack       # Check status
systemctl restart clack      # Restart
journalctl -u clack -f       # View logs
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Connection refused | Check port 9878 is open in your firewall |
| `auth_failed` on WebSocket | Verify `RELAY_AUTH_TOKEN` matches between server and app |
| No audio response | Check your STT/TTS provider API key is valid |
| Pairing code rejected | Codes expire after 5 min — generate a fresh one |
| HTTP 429 on pairing | Rate limit hit — wait 5 minutes and try again |
| Echo/feedback loop | This is auto-detected; if persistent, check mic/speaker distance |
| High latency | Try a different STT/TTS provider, or use local speech mode |

## Documentation

See [SKILL.md](SKILL.md) for full protocol docs, WebSocket message reference, and endpoint details.

See [CHANGELOG.md](CHANGELOG.md) for version history.

## License

MIT
