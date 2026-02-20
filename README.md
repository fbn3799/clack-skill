# Clack — Voice Relay for OpenClaw

> Talk to your AI assistant by voice. Real-time, self-hosted, private.

Clack is an [OpenClaw](https://github.com/openclaw/openclaw) skill that sets up a WebSocket voice relay server. It bridges voice input through speech-to-text → your OpenClaw agent → text-to-speech, enabling natural voice conversations.

## Quick Start

```bash
# Install the skill
openclaw skill install github:fbn3799/clack-skill

# Or manually:
git clone https://github.com/fbn3799/clack-skill.git ~/.openclaw/workspace/skills/clack
export ELEVENLABS_API_KEY="sk_..."
bash ~/.openclaw/workspace/skills/clack/scripts/setup.sh
```

## How It Works

```
📱 Clack App          🖥️ Your Server          🌐 APIs
┌──────────┐    WS    ┌──────────────┐    HTTP    ┌─────────────┐
│ Voice in ├──────────►│ Clack Relay  ├──────────►│ STT Provider│
│          │           │              │◄───────────┤ (ElevenLabs)│
│          │    WS     │              │    HTTP    ├─────────────┤
│ Audio out│◄──────────┤              ├──────────►│ OpenClaw GW │
│          │           │              │◄───────────┤ (your agent)│
│          │           │              │    HTTP    ├─────────────┤
│          │           │              ├──────────►│ TTS Provider│
└──────────┘           └──────────────┘◄───────────┤ (ElevenLabs)│
                                                   └─────────────┘
```

## Features

- **Multi-provider**: ElevenLabs, OpenAI, Deepgram for STT/TTS
- **20 built-in voices** with easy aliases
- **Smart filtering**: Noise rejection, echo detection, hallucination filtering
- **Streaming audio**: Responses start playing immediately
- **Conversation memory**: Persisted across sessions
- **Secure pairing**: One-time codes for device authentication
- **Self-hosted**: All data flows through YOUR server

## Client App

The **Clack** iOS app is available on the [App Store](https://github.com/fbn3799/clack-app) or build from source.

## Requirements

- Python 3.10+
- An API key for at least one provider ([ElevenLabs](https://elevenlabs.io), [OpenAI](https://openai.com), or [Deepgram](https://deepgram.com))
- OpenClaw with gateway `chatCompletions` endpoint enabled

## Documentation

See [SKILL.md](SKILL.md) for full protocol docs, environment variables, and configuration.

## License

MIT
