# Changelog

## 1.0.0 (2026-02-20)

### Features
- Multi-provider support: ElevenLabs, OpenAI, and Deepgram for both STT and TTS
- 20 built-in ElevenLabs voice aliases
- Voice response rules: AI responses enforced short (1-3 sentences) for natural conversation
- Input length limiting: Configurable max transcript length (default 300 chars)
- Confidence filtering: Low-confidence and short STT results are discarded
- Echo detection: Prevents TTS → mic → STT feedback loops
- Hallucination detection: Filters repetitive/nonsense transcriptions
- Audio chunking: Auto-splits long recordings for reliable transcription
- Device pairing: One-time code system for secure authentication
- Conversation history: Persistent across sessions with configurable depth
- Keepalive pings during LLM processing to prevent client timeouts
- Per-session voice override via WebSocket config
- HTTP endpoints: health, voices, history, pairing
- Automated setup script with systemd service configuration
- Auto-detection of OpenClaw gateway config

### Protocol
- WebSocket endpoint at `/voice` with token auth (query param or message)
- Binary PCM audio frames (16kHz, 16-bit, mono)
- JSON control messages for session lifecycle
- Processing stage notifications (transcribing, thinking, speaking, filtered)
