---
name: clack
description: Deploy and manage Clack, a voice relay server for OpenClaw. Bridges voice input (WebSocket) through ElevenLabs STT → OpenClaw agent → ElevenLabs TTS, enabling voice conversations with your agent. Use when a user wants to set up voice chat, voice relay, voice interface, Clack, or talk to their agent by voice. Requires an ElevenLabs API key.
---

# Clack

WebSocket server that enables voice conversations with an OpenClaw agent.

**Flow:** Client audio (PCM 16kHz/16-bit/mono) → ElevenLabs STT → OpenClaw Gateway → ElevenLabs TTS → PCM audio back to client.

## Prerequisites

- Python 3.10+
- ElevenLabs API key (used for both STT and TTS)
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

bash scripts/setup.sh [--port 9878] [--install-dir /opt/voice-relay]
```

### Enable OpenClaw Gateway endpoint

The gateway must have `chatCompletions` enabled. Apply this config patch:

```json
{"http": {"endpoints": {"chatCompletions": {"enabled": true}}}}
```

## Management

```bash
systemctl status voice-relay
systemctl restart voice-relay
journalctl -u voice-relay -f
```

## WebSocket Protocol

**Endpoint:** `ws://<host>:<port>/voice?token=<RELAY_AUTH_TOKEN>`

### Client → Server

| Message | Format | Description |
|---------|--------|-------------|
| `{"type":"start","config":{...}}` | JSON | Start session. Config: `voice` (ElevenLabs voice ID or alias), `systemPrompt`, `userId` |
| Binary frames | bytes | Raw PCM audio (16kHz, 16-bit, mono) |
| `{"type":"end_speech"}` | JSON | Signal end of speech, triggers processing |
| `{"type":"ping"}` | JSON | Keepalive |

### Server → Client

| Message | Format | Description |
|---------|--------|-------------|
| `{"type":"ready"}` | JSON | Session ready |
| `{"type":"processing","stage":"..."}` | JSON | Stage: `transcribing`, `thinking`, `speaking` |
| `{"type":"transcript","text":"...","final":true}` | JSON | STT result |
| `{"type":"response_text","text":"..."}` | JSON | LLM text response |
| `{"type":"response_start","format":"pcm_16000"}` | JSON | Audio incoming |
| Binary frames | bytes | TTS audio (PCM 16kHz, 16-bit, mono) |
| `{"type":"response_end"}` | JSON | Audio done |

## Features

- **Confidence filtering**: Low-confidence STT results (background noise) are discarded
- **Echo detection**: Prevents feedback loops where TTS output gets re-transcribed
- **Audio chunking**: Long recordings are split into chunks for reliable STT
- **Token auth**: Constant-time token verification via query param or auth message
- **Auto-restart**: systemd restarts on crash

## Voice Configuration

Default voice: `Will` (`bIHbv24MWmeRgasZH58o`). Pass any ElevenLabs voice ID in the `start` config:

```json
{"type": "start", "config": {"voice": "bIHbv24MWmeRgasZH58o"}}
```
