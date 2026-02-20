---
name: clack
version: 1.0.0
description: Deploy and manage Clack, a voice relay server for OpenClaw. Bridges voice input (WebSocket) through STT → OpenClaw agent → TTS, enabling real-time voice conversations with your agent. Supports ElevenLabs, OpenAI, and Deepgram for STT/TTS. Use when a user wants to set up voice chat, voice relay, voice interface, Clack, or talk to their agent by voice.
---

# Clack

WebSocket relay server that enables real-time voice conversations with an OpenClaw agent.

**Flow:** Client audio (PCM 16kHz/16-bit/mono) → STT → OpenClaw Gateway → TTS → PCM audio back to client.

## Prerequisites

- Python 3.10+
- API key for at least one provider (ElevenLabs, OpenAI, or Deepgram)
- OpenClaw Gateway with `chatCompletions` endpoint enabled
- Root/sudo access (for systemd)

## Setup

Run the setup script. It creates a venv, installs deps, and configures a systemd service:

```bash
# Set required env vars first (or the script will prompt)
export ELEVENLABS_API_KEY="sk_..."
export OPENCLAW_GATEWAY_TOKEN="..."

# Optional overrides
export OPENCLAW_GATEWAY_URL="http://127.0.0.1:18789"  # default
export VOICE_RELAY_PORT="9878"                          # default
export RELAY_AUTH_TOKEN="..."                            # auto-generated if empty
export STT_PROVIDER="elevenlabs"                        # elevenlabs|openai|deepgram
export TTS_PROVIDER="elevenlabs"                        # elevenlabs|openai|deepgram
export TTS_VOICE="will"                                 # voice name or ID

bash scripts/setup.sh [--port 9878] [--install-dir /opt/clack]
```

### Enable OpenClaw Gateway endpoint

The gateway must have `chatCompletions` enabled. Apply this config patch:

```json
{"http": {"endpoints": {"chatCompletions": {"enabled": true}}}}
```

## Management

```bash
systemctl status clack
systemctl restart clack
journalctl -u clack -f
```

## Client App

The Clack iOS app is available on the App Store (or build from source at [github.com/fbn3799/clack-app](https://github.com/fbn3799/clack-app)).

## WebSocket Protocol

**Endpoint:** `ws://<host>:<port>/voice?token=<RELAY_AUTH_TOKEN>`

### Client → Server

| Message | Format | Description |
|---------|--------|-------------|
| `{"type":"start","config":{...}}` | JSON | Start session. Config: `voice`, `systemPrompt` |
| Binary frames | bytes | Raw PCM audio (16kHz, 16-bit, mono) |
| `{"type":"end_speech"}` | JSON | Signal end of speech, triggers processing |
| `{"type":"ping"}` | JSON | Keepalive |
| `{"type":"auth","token":"..."}` | JSON | Authenticate (alternative to query param) |

### Server → Client

| Message | Format | Description |
|---------|--------|-------------|
| `{"type":"ready"}` | JSON | Session ready |
| `{"type":"auth_ok"}` / `{"type":"auth_failed"}` | JSON | Auth result |
| `{"type":"processing","stage":"..."}` | JSON | Stage: `transcribing`, `thinking`, `speaking`, `filtered` |
| `{"type":"transcript","text":"...","final":true}` | JSON | STT result |
| `{"type":"response_text","text":"..."}` | JSON | LLM text response |
| `{"type":"response_start","format":"pcm_16000"}` | JSON | Audio stream starting |
| Binary frames | bytes | TTS audio (PCM 16kHz, 16-bit, mono) |
| `{"type":"response_end"}` | JSON | Audio stream done |

## HTTP Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/` | GET | No | Service status |
| `/health` | GET | No | Health check |
| `/voices` | GET | Yes | List available voices |
| `/pair` | GET | Yes | Generate one-time pairing code |
| `/pair` | POST | No | Redeem pairing code → get auth token |
| `/history` | GET | Yes | Get conversation history |
| `/history` | DELETE | Yes | Clear conversation history |

## Features

- **Multi-provider STT/TTS**: ElevenLabs, OpenAI, and Deepgram support
- **Voice response rules**: AI responses are enforced short (1-3 sentences) for natural conversation
- **Input length limiting**: Configurable max transcript length (default 300 chars)
- **Confidence filtering**: Low-confidence STT results are discarded
- **Echo detection**: Prevents feedback loops (TTS → mic → STT)
- **Audio chunking**: Long recordings auto-split for reliable transcription
- **Hallucination detection**: Filters repetitive/nonsense STT output
- **Pairing system**: One-time codes for secure device pairing
- **Conversation history**: Persistent across sessions with configurable depth
- **Token auth**: Constant-time HMAC verification
- **Keepalive pings**: Prevents client timeout during long LLM responses
- **Auto-restart**: systemd restarts on crash

## Voice Configuration

20 built-in ElevenLabs voices available. Default: `Will`. Pass voice name or ID in session config:

```json
{"type": "start", "config": {"voice": "aria"}}
```

Available aliases: will, aria, roger, sarah, laura, charlie, george, callum, river, liam, charlotte, alice, matilda, jessica, eric, chris, brian, daniel, lily, bill.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STT_PROVIDER` | `elevenlabs` | STT provider |
| `TTS_PROVIDER` | `elevenlabs` | TTS provider |
| `ELEVENLABS_API_KEY` | — | ElevenLabs API key |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `DEEPGRAM_API_KEY` | — | Deepgram API key |
| `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | Gateway URL |
| `OPENCLAW_GATEWAY_TOKEN` | — | Gateway bearer token |
| `RELAY_AUTH_TOKEN` | — | Client auth token |
| `VOICE_RELAY_PORT` | `9878` | Server port |
| `TTS_VOICE` | `bIHbv24MWmeRgasZH58o` | Voice ID or alias |
| `CLACK_MAX_INPUT_CHARS` | `300` | Max transcript length |
| `CLACK_HISTORY_DIR` | `/var/lib/clack/history` | History storage |
| `CLACK_MAX_HISTORY` | `50` | Max history messages |
