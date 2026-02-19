#!/usr/bin/env python3
"""
VoiceLLM Relay Server (OpenClaw Edition)

WebSocket relay: iOS voice input → ElevenLabs STT → OpenClaw agent → ElevenLabs TTS → audio back.

Environment variables:
  ELEVENLABS_API_KEY    - ElevenLabs API key (STT + TTS)
  OPENCLAW_GATEWAY_URL  - OpenClaw gateway URL (default: http://127.0.0.1:18789)
  OPENCLAW_GATEWAY_TOKEN - OpenClaw gateway bearer token
  RELAY_AUTH_TOKEN       - Client auth token (query param or first message)
  VOICE_RELAY_PORT       - Server port (default: 9878)
"""

import asyncio
import json
import os
import io
import struct
import hmac
from typing import Optional
from difflib import SequenceMatcher

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
    """Wrap raw PCM data in a WAV header."""
    data_size = len(pcm_data)
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    buf = io.BytesIO()
    buf.write(b'RIFF')
    buf.write(struct.pack('<I', 36 + data_size))
    buf.write(b'WAVE')
    buf.write(b'fmt ')
    buf.write(struct.pack('<I', 16))
    buf.write(struct.pack('<H', 1))
    buf.write(struct.pack('<H', channels))
    buf.write(struct.pack('<I', sample_rate))
    buf.write(struct.pack('<I', byte_rate))
    buf.write(struct.pack('<H', block_align))
    buf.write(struct.pack('<H', bits_per_sample))
    buf.write(b'data')
    buf.write(struct.pack('<I', data_size))
    buf.write(pcm_data)
    return buf.getvalue()


def _is_echo(transcript: str, last_response: str) -> bool:
    """Check if transcript is an echo of the last assistant response."""
    t = transcript.lower().strip().replace("[speaker speaker_0]: ", "")
    r = last_response.lower().strip()
    if t in r or r in t:
        return True
    ratio = SequenceMatcher(None, t, r).ratio()
    if ratio > 0.6:
        print(f"[Echo] Similarity: {ratio:.2f}")
        return True
    return False


# --- App setup ---
app = FastAPI(title="VoiceLLM Relay Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
RELAY_AUTH_TOKEN = os.getenv("RELAY_AUTH_TOKEN", "")
DEFAULT_VOICE_ID = "bIHbv24MWmeRgasZH58o"  # Will
VOICE_ALIASES = {"will": "bIHbv24MWmeRgasZH58o"}
DEFAULT_SYSTEM_PROMPT = (
    "You are a voice assistant. The user is talking to you via voice. "
    "Keep responses concise and conversational — this is spoken, not written. "
    "Avoid markdown, bullet points, or long lists. Be natural."
)

print(f"[VoiceLLM] Starting relay server...")
print(f"[VoiceLLM] ElevenLabs: {'set' if ELEVENLABS_API_KEY else 'NOT SET'}")
print(f"[VoiceLLM] Gateway: {OPENCLAW_GATEWAY_URL}")
print(f"[VoiceLLM] Gateway token: {'set' if OPENCLAW_GATEWAY_TOKEN else 'NOT SET'}")
print(f"[VoiceLLM] Relay auth: {'ENABLED' if RELAY_AUTH_TOKEN else 'DISABLED (open!)'}")


def verify_token(token: str) -> bool:
    if not RELAY_AUTH_TOKEN:
        return True
    return hmac.compare_digest(token, RELAY_AUTH_TOKEN)


class VoiceSession:
    def __init__(self, websocket: WebSocket, config: dict):
        self.websocket = websocket
        self.config = config
        voice = config.get("voice", DEFAULT_VOICE_ID)
        self.voice_id = VOICE_ALIASES.get(voice, voice)
        self.system_prompt = config.get("systemPrompt", DEFAULT_SYSTEM_PROMPT)
        self.conversation_history = []
        self.audio_buffer = bytearray()
        self.last_assistant_response = ""
        self.session_user = config.get("userId", "voicellm-user")

    async def send_json(self, data: dict):
        await self.websocket.send_text(json.dumps(data))

    async def send_audio(self, audio_data: bytes):
        await self.websocket.send_bytes(audio_data)

    async def transcribe_audio(self, audio_data: bytes) -> Optional[str]:
        if not ELEVENLABS_API_KEY:
            print("[STT] No ElevenLabs key")
            return None
        MAX_CHUNK_BYTES = 960000
        if len(audio_data) > MAX_CHUNK_BYTES:
            chunks = [audio_data[i:i + MAX_CHUNK_BYTES] for i in range(0, len(audio_data), MAX_CHUNK_BYTES)]
            print(f"[STT] Splitting {len(audio_data)} bytes into {len(chunks)} chunks")
            transcripts = []
            for idx, chunk in enumerate(chunks):
                result = await self._transcribe_chunk(chunk)
                if result:
                    transcripts.append(result)
            combined = " ".join(transcripts)
            return combined if combined.strip() else None
        return await self._transcribe_chunk(audio_data)

    async def _transcribe_chunk(self, audio_data: bytes) -> Optional[str]:
        async with aiohttp.ClientSession() as session:
            wav_data = pcm_to_wav(audio_data)
            print(f"[STT] Sending {len(wav_data)} bytes WAV ({len(audio_data)} PCM)")
            form = aiohttp.FormData()
            form.add_field('file', wav_data, filename='audio.wav', content_type='audio/wav')
            form.add_field('model_id', 'scribe_v1')
            form.add_field('diarize', 'true')
            form.add_field('tag_audio_events', 'false')
            headers = {"xi-api-key": ELEVENLABS_API_KEY}
            async with session.post("https://api.elevenlabs.io/v1/speech-to-text", headers=headers, data=form) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    lang_prob = result.get("language_probability", 0)
                    text = result.get("text", "").strip()
                    print(f"[STT] Raw: '{text[:200]}' (lang_prob={lang_prob:.2f})")
                    if lang_prob < 0.4:
                        print(f"[STT] Filtered: low confidence ({lang_prob:.2f})")
                        return None
                    if len(text) < 2:
                        print(f"[STT] Filtered: too short")
                        return None
                    words = result.get("words", [])
                    if words:
                        confidences = [w.get("confidence", 1.0) for w in words if "confidence" in w]
                        if confidences:
                            avg_conf = sum(confidences) / len(confidences)
                            if avg_conf < 0.4:
                                print(f"[STT] Filtered: low word confidence ({avg_conf:.2f})")
                                return None
                    if words and any(w.get("speaker_id") for w in words):
                        parts, current_speaker, current_text = [], None, []
                        for w in words:
                            speaker = w.get("speaker_id", "?")
                            if speaker != current_speaker:
                                if current_text:
                                    parts.append(f"[Speaker {current_speaker}]: {' '.join(current_text)}")
                                current_speaker = speaker
                                current_text = [w.get("text", "")]
                            else:
                                current_text.append(w.get("text", ""))
                        if current_text:
                            parts.append(f"[Speaker {current_speaker}]: {' '.join(current_text)}")
                        transcript = "\n".join(parts)
                    else:
                        transcript = text
                    return transcript if transcript else None
                else:
                    error = await resp.text()
                    print(f"[STT] ElevenLabs error: {resp.status} - {error}")
                    return None

    async def get_llm_response(self, user_message: str) -> Optional[str]:
        self.conversation_history.append({"role": "user", "content": user_message})
        messages = [{"role": "system", "content": self.system_prompt}] + self.conversation_history
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}", "Content-Type": "application/json"}
            payload = {"model": "openclaw", "messages": messages, "max_tokens": 500, "user": self.session_user}
            try:
                async with session.post(
                    f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                    headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        content = result["choices"][0]["message"]["content"]
                        self.conversation_history.append({"role": "assistant", "content": content})
                        return content
                    else:
                        error = await resp.text()
                        print(f"[LLM] OpenClaw error: {resp.status} - {error}")
                        return "Sorry, I had trouble processing that."
            except Exception as e:
                print(f"[LLM] Connection error: {e}")
                return "Sorry, I couldn't reach the assistant right now."

    async def synthesize_speech(self, text: str) -> Optional[bytes]:
        if not ELEVENLABS_API_KEY:
            print("[TTS] No ElevenLabs key")
            return None
        async with aiohttp.ClientSession() as session:
            headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
            payload = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            }
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=pcm_16000",
                headers=headers, json=payload
            ) as resp:
                if resp.status == 200:
                    pcm = await resp.read()
                    print(f"[TTS] Got {len(pcm)} bytes PCM")
                    return pcm
                else:
                    error = await resp.text()
                    print(f"[TTS] ElevenLabs error: {resp.status} - {error}")
                    return None


@app.get("/")
async def root():
    return {"status": "ok", "service": "VoiceLLM Relay (OpenClaw)"}

@app.get("/health")
async def health():
    return {"status": "ok", "backend": "openclaw"}


@app.websocket("/voice")
async def voice_endpoint(websocket: WebSocket, token: str = Query(default="")):
    await websocket.accept()
    print(f"[WS] Client connected: {websocket.client}")
    authenticated = verify_token(token) if token else not RELAY_AUTH_TOKEN
    session = None
    try:
        while True:
            message = await websocket.receive()
            if "text" in message:
                data = json.loads(message["text"])
                msg_type = data.get("type")
                if msg_type == "auth" and not authenticated:
                    if verify_token(data.get("token", "")):
                        authenticated = True
                        await websocket.send_text(json.dumps({"type": "auth_ok"}))
                    else:
                        await websocket.send_text(json.dumps({"type": "auth_failed"}))
                        await websocket.close(code=4001, reason="Invalid token")
                        return
                    continue
                if not authenticated:
                    await websocket.send_text(json.dumps({"type": "auth_required"}))
                    await websocket.close(code=4001, reason="Authentication required")
                    return
                if msg_type == "start":
                    config = data.get("config", {})
                    session = VoiceSession(websocket, config)
                    print(f"[WS] Session started: voice={session.voice_id}, user={session.session_user}")
                    await session.send_json({"type": "ready"})
                elif msg_type == "end_speech" and session:
                    if session.audio_buffer:
                        print(f"[WS] Processing {len(session.audio_buffer)} bytes of audio")
                        await session.send_json({"type": "processing", "stage": "transcribing"})
                        transcript = await session.transcribe_audio(bytes(session.audio_buffer))
                        if transcript:
                            if session.last_assistant_response and _is_echo(transcript, session.last_assistant_response):
                                print(f"[STT] Filtered: echo of last response")
                                session.audio_buffer = bytearray()
                                continue
                            print(f"[STT] Transcript: {transcript}")
                            await session.send_json({"type": "transcript", "text": transcript, "final": True})
                            await session.send_json({"type": "processing", "stage": "thinking"})
                            response = await session.get_llm_response(transcript)
                            if response:
                                print(f"[LLM] Response: {response[:100]}...")
                                session.last_assistant_response = response
                                await session.send_json({"type": "response_text", "text": response})
                                await session.send_json({"type": "processing", "stage": "speaking"})
                                await session.send_json({"type": "response_start", "format": "pcm_16000"})
                                audio = await session.synthesize_speech(response)
                                if audio:
                                    await session.send_audio(audio)
                                await session.send_json({"type": "response_end"})
                        session.audio_buffer = bytearray()
                elif msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            elif "bytes" in message and session:
                if not authenticated:
                    continue
                session.audio_buffer.extend(message["bytes"])
    except WebSocketDisconnect:
        print(f"[WS] Client disconnected")
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        print(f"[WS] Session ended")


if __name__ == "__main__":
    port = int(os.getenv("VOICE_RELAY_PORT", "9878"))
    uvicorn.run(app, host="0.0.0.0", port=port)
