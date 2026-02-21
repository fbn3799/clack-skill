# App Store Listing — Clack

## App Name (30 chars max)
Clack – AI Voice Assistant

## Subtitle (30 chars max)
Talk to your AI, privately

## Keywords (100 chars max, comma-separated)
openclaw,voice assistant,AI talk,speech,elevenlabs,11labs,whisper,TTS,STT,clawdbot,moltbot,self-host

## Promotional Text (170 chars, can be updated without review)
Independent voice input & output — choose ElevenLabs, OpenAI, Deepgram, or free on-device speech for STT and TTS separately. Save money, keep quality.

## Description

Talk to your AI — on your terms.

Clack is a voice interface for self-hosted AI assistants. Your voice goes directly to YOUR server. No middleman. No cloud. No data harvesting.

🔒 PRIVATE & ENCRYPTED BY DESIGN

• All connections encrypted — Domain with SSL or Tailscale, no unencrypted public access
• Your voice goes to your server and only to the providers you choose — we never see your data
• Zero analytics, zero tracking, zero telemetry
• No account required — pair with a 6-digit code and go
• Auth tokens use constant-time HMAC verification to prevent timing attacks
• Pairing codes are one-time use, expire in 5 minutes, and are rate-limited
• All settings stored locally on your device — nothing leaves your phone

🎤 INDEPENDENT VOICE INPUT & OUTPUT

Configure speech-to-text and text-to-speech separately — full control over how your voice is processed:

• ElevenLabs — studio-quality AI voices
• OpenAI Whisper — best-in-class speech recognition
• Deepgram — low-latency voice processing
• On-device (Apple Speech) — completely free, works offline

Mix and match to fit your needs and budget:
→ Free on-device transcription + premium ElevenLabs voices = great quality, zero STT costs
→ Fully on-device = no API spend at all
→ Different cloud providers for input vs output = maximum flexibility

20+ premium voices included. Choose per-provider. Even pick iOS system voices for local TTS.

💰 SAVE ON API COSTS

On-device speech recognition is free and unlimited. Use it for transcription while keeping premium cloud voices for output — or go fully local and pay nothing for speech processing.

🗣️ NATURAL CONVERSATIONS

• Real-time voice chat — speak naturally, get spoken responses
• Automatic silence detection with adjustable sensitivity
• Interrupt anytime — tap to cut in mid-response
• Conversation history persists across calls
• AirPods support — mute/unmute with play/pause
• Language selection for on-device STT
• Voice picker for on-device TTS

⚡ HOW IT WORKS

1. Set up an OpenClaw agent on your server (or any machine)
2. Install the Clack voice relay skill
3. Enter your server address and pair with a one-time code
4. Tap the call button and start talking

Works over WiFi, cellular, or any network. Two secure connection options: Domain with automatic SSL, or Tailscale for zero-config encryption.

🔧 FULL CONTROL

• Choose your AI model — runs through your OpenClaw gateway
• Set custom system prompts and context per call
• Session picker for multi-conversation context
• Adjustable noise threshold and silence timing
• Echo test mode to dial in your mic settings

Clack is open-source. The server, the protocol, and the app — all auditable. Your AI assistant, your infrastructure, your data.

No subscription. No API fees from us. You bring your own keys.

## What's New (v1.5.0)

• Independent STT & TTS provider selection — choose different engines for voice input and voice output
• New Voice settings page with provider pickers for both directions
• On-device language selection for local speech recognition
• On-device voice picker for local text-to-speech
• Server auto-detection of available providers (ElevenLabs, OpenAI, Deepgram)
• Save on API costs — use free on-device STT with premium cloud TTS
• Improved encryption warnings — clearer messaging about what's protected
• Lock screen player only appears during active calls
• Fixed duplicate messages with on-device transcription
