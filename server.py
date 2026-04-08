#!/usr/bin/env python3
"""
Clack — Voice Relay Server for OpenClaw

WebSocket relay: voice input → STT → OpenClaw agent → TTS → audio back.

Supported providers:
  STT: elevenlabs, openai, deepgram
  TTS: elevenlabs, openai, deepgram

Environment variables:
  STT_PROVIDER          - STT provider (default: elevenlabs)
  TTS_PROVIDER          - TTS provider (default: elevenlabs)
  ELEVENLABS_API_KEY    - ElevenLabs API key
  OPENAI_API_KEY        - OpenAI API key
  DEEPGRAM_API_KEY      - Deepgram API key
  OPENCLAW_GATEWAY_URL  - OpenClaw gateway URL (default: http://127.0.0.1:18789)
  OPENCLAW_GATEWAY_TOKEN - OpenClaw gateway bearer token
  RELAY_AUTH_TOKEN       - Client auth token
  VOICE_RELAY_PORT       - Server port (default: 9878)
  TTS_VOICE             - Voice ID/name (provider-specific)
  CLACK_HISTORY_DIR     - History storage dir (default: /var/lib/clack/history)
  CLACK_MAX_HISTORY     - Max messages to keep (default: 200)
"""

import asyncio
import json
import os
import io
import struct
import hmac
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, AsyncIterator
from difflib import SequenceMatcher

import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn


# ── Config ──

_CONFIG_PATHS = [
    Path(__file__).parent / "config.json",
    Path("/var/lib/clack/config.json"),
]

def _load_config() -> dict:
    """Load config from JSON file. Provider keys and optional settings live here."""
    for p in _CONFIG_PATHS:
        try:
            return json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}

_config = _load_config()

def _conf(key: str, default: str = "") -> str:
    """Read a config value: config.json first, then env var fallback."""
    return _config.get(key, "") or os.getenv(key, default)


# ── Utilities ──

def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1, bits_per_sample: int = 16) -> bytes:
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


def _get_agent_name() -> str:
    """Read agent name from env or current user's IDENTITY.md."""
    env_name = os.getenv("CLACK_AGENT_NAME", "")
    if env_name:
        return env_name
    # Only check the current user's home directory
    path = Path.home() / ".openclaw" / "workspace" / "IDENTITY.md"
    try:
        text = path.read_text()
        for line in text.splitlines():
            if line.startswith("- **Name:**"):
                name = line.split("**Name:**")[-1].strip()
                if name:
                    return name
    except (FileNotFoundError, PermissionError):
        pass
    return ""


def _is_echo(transcript: str, last_response: str) -> bool:
    t = transcript.lower().strip().replace("[speaker speaker_0]: ", "")
    r = last_response.lower().strip()
    if t in r or r in t:
        return True
    ratio = SequenceMatcher(None, t, r).ratio()
    if ratio > 0.6:
        print(f"[Echo] Similarity: {ratio:.2f}")
        return True
    return False


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting that breaks TTS."""
    # Headers: # Heading → Heading
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Bold/italic: **bold**, *italic*, __bold__, _italic_
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', text)
    # Strikethrough: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    # Inline code: `code`
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Code blocks: ```...```
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Links: [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    # Images: ![alt](url) → alt
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', text)
    # Bullet points: - item or * item
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)
    # Numbered lists: 1. item
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Blockquotes: > text
    text = re.sub(r'^\s*>\s?', '', text, flags=re.MULTILINE)
    # Horizontal rules: --- or ***
    text = re.sub(r'^\s*[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── Provider Errors ──

class ProviderError(Exception):
    """Raised when an API provider returns an error (auth, quota, etc.)."""
    def __init__(self, provider: str, status: int, message: str = ""):
        self.provider = provider
        self.status = status
        self.message = message
        super().__init__(f"[{provider}] HTTP {status}: {message}")

# ── STT Providers ──

class STTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_pcm: bytes) -> Optional[str]:
        """Transcribe PCM audio, return text or None."""
        pass


class ElevenLabsSTT(STTProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def transcribe(self, audio_pcm: bytes) -> Optional[str]:
        async with aiohttp.ClientSession() as session:
            wav_data = pcm_to_wav(audio_pcm)
            print(f"[STT/11labs] Sending {len(wav_data)} bytes WAV")
            form = aiohttp.FormData()
            form.add_field('file', wav_data, filename='audio.wav', content_type='audio/wav')
            form.add_field('model_id', 'scribe_v1')
            form.add_field('diarize', 'false')
            form.add_field('tag_audio_events', 'false')
            async with session.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": self.api_key},
                data=form
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    print(f"[STT/11labs] Error: {resp.status} - {err_text}")
                    raise ProviderError("ElevenLabs STT", resp.status, err_text)
                result = await resp.json()
                lang_prob = result.get("language_probability", 0)
                text = result.get("text", "").strip()
                print(f"[STT/11labs] Raw: '{text[:200]}' (lang_prob={lang_prob:.2f})")
                if lang_prob < 0.4 or len(text) < 2:
                    print(f"[STT/11labs] Filtered: low confidence or too short")
                    return None
                # Filter hallucinated noise: only punctuation/whitespace
                import re as _re
                if not _re.search(r'[a-zA-ZäöüÄÖÜß]', text):
                    print(f"[STT/11labs] Filtered: no real words")
                    return None
                # Filter suspiciously long transcripts (likely hallucination)
                if len(text) > 500:
                    print(f"[STT/11labs] Filtered: too long ({len(text)} chars, likely hallucination)")
                    return None
                # Detect repetitive/nonsense hallucinations
                words = text.split()
                if len(words) > 5:
                    unique_ratio = len(set(w.lower() for w in words)) / len(words)
                    if unique_ratio < 0.3:
                        print(f"[STT/11labs] Filtered: repetitive (unique ratio {unique_ratio:.2f})")
                        return None
                words = result.get("words", [])
                if words:
                    confidences = [w.get("confidence", 1.0) for w in words if "confidence" in w]
                    if confidences and sum(confidences) / len(confidences) < 0.4:
                        print(f"[STT/11labs] Filtered: low word confidence")
                        return None
                if words and any(w.get("speaker_id") for w in words):
                    parts, cur_spk, cur_txt = [], None, []
                    for w in words:
                        spk = w.get("speaker_id", "?")
                        if spk != cur_spk:
                            if cur_txt:
                                parts.append(f"[Speaker {cur_spk}]: {' '.join(cur_txt)}")
                            cur_spk, cur_txt = spk, [w.get("text", "")]
                        else:
                            cur_txt.append(w.get("text", ""))
                    if cur_txt:
                        parts.append(f"[Speaker {cur_spk}]: {' '.join(cur_txt)}")
                    return "\n".join(parts)
                return text if text else None


class OpenAISTT(STTProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def transcribe(self, audio_pcm: bytes) -> Optional[str]:
        async with aiohttp.ClientSession() as session:
            wav_data = pcm_to_wav(audio_pcm)
            print(f"[STT/openai] Sending {len(wav_data)} bytes WAV")
            form = aiohttp.FormData()
            form.add_field('file', wav_data, filename='audio.wav', content_type='audio/wav')
            form.add_field('model', 'whisper-1')
            async with session.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data=form
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    print(f"[STT/openai] Error: {resp.status} - {err_text}")
                    raise ProviderError("OpenAI STT", resp.status, err_text)
                result = await resp.json()
                text = result.get("text", "").strip()
                print(f"[STT/openai] Text: '{text[:200]}'")
                if len(text) < 2 or len(text) > 500:
                    return None
                return text


class DeepgramSTT(STTProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def transcribe(self, audio_pcm: bytes) -> Optional[str]:
        async with aiohttp.ClientSession() as session:
            wav_data = pcm_to_wav(audio_pcm)
            print(f"[STT/deepgram] Sending {len(wav_data)} bytes WAV")
            async with session.post(
                "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true&detect_language=true",
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": "audio/wav"},
                data=wav_data
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    print(f"[STT/deepgram] Error: {resp.status} - {err_text}")
                    raise ProviderError("Deepgram STT", resp.status, err_text)
                result = await resp.json()
                alt = result.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])[0]
                text = alt.get("transcript", "").strip()
                confidence = alt.get("confidence", 0)
                print(f"[STT/deepgram] Text: '{text[:200]}' (conf={confidence:.2f})")
                if confidence < 0.4 or len(text) < 2:
                    print(f"[STT/deepgram] Filtered")
                    return None
                return text


# ── TTS Providers ──

class TTSProvider(ABC):
    @abstractmethod
    async def synthesize_stream(self, text: str, send_audio) -> bool:
        """Stream TTS audio via send_audio(bytes) callback. Return True if audio was sent."""
        pass


class ElevenLabsTTS(TTSProvider):
    def __init__(self, api_key: str, voice_id: str = "bIHbv24MWmeRgasZH58o"):
        self.api_key = api_key
        self.voice_id = voice_id

    async def synthesize_stream(self, text: str, send_audio) -> bool:
        async with aiohttp.ClientSession() as session:
            payload = {
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            }
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=pcm_16000",
                headers={"xi-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    print(f"[TTS/11labs] Error: {resp.status} - {err_text}")
                    raise ProviderError("ElevenLabs TTS", resp.status, err_text)
                total = 0
                buf = bytearray()
                CHUNK_SIZE = 16000
                async for chunk in resp.content.iter_any():
                    buf.extend(chunk)
                    while len(buf) >= CHUNK_SIZE:
                        await send_audio(bytes(buf[:CHUNK_SIZE]))
                        total += CHUNK_SIZE
                        del buf[:CHUNK_SIZE]
                if buf:
                    if len(buf) % 2 != 0:
                        buf.append(0)
                    await send_audio(bytes(buf))
                    total += len(buf)
                print(f"[TTS/11labs] Streamed {total} bytes PCM")
                return total > 0


class OpenAITTS(TTSProvider):
    def __init__(self, api_key: str, voice: str = "alloy"):
        self.api_key = api_key
        self.voice = voice

    async def synthesize_stream(self, text: str, send_audio) -> bool:
        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "tts-1",
                "input": text,
                "voice": self.voice,
                "response_format": "pcm",  # raw 24kHz 16-bit mono PCM
            }
            async with session.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    print(f"[TTS/openai] Error: {resp.status} - {err_text}")
                    raise ProviderError("OpenAI TTS", resp.status, err_text)
                # OpenAI returns 24kHz PCM, we need to resample to 16kHz
                total = 0
                buf = bytearray()
                CHUNK_SIZE = 24000  # 500ms at 24kHz
                async for chunk in resp.content.iter_any():
                    buf.extend(chunk)
                    while len(buf) >= CHUNK_SIZE:
                        resampled = _resample_24k_to_16k(bytes(buf[:CHUNK_SIZE]))
                        await send_audio(resampled)
                        total += len(resampled)
                        del buf[:CHUNK_SIZE]
                if buf:
                    if len(buf) % 2 != 0:
                        buf.append(0)
                    resampled = _resample_24k_to_16k(bytes(buf))
                    await send_audio(resampled)
                    total += len(resampled)
                print(f"[TTS/openai] Streamed {total} bytes PCM (resampled 24k→16k)")
                return total > 0


class DeepgramTTS(TTSProvider):
    def __init__(self, api_key: str, voice: str = "aura-asteria-en"):
        self.api_key = api_key
        self.voice = voice

    async def synthesize_stream(self, text: str, send_audio) -> bool:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.deepgram.com/v1/speak?model={self.voice}&encoding=linear16&sample_rate=16000",
                headers={"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"},
                json={"text": text}
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    print(f"[TTS/deepgram] Error: {resp.status} - {err_text}")
                    raise ProviderError("Deepgram TTS", resp.status, err_text)
                total = 0
                buf = bytearray()
                CHUNK_SIZE = 16000
                async for chunk in resp.content.iter_any():
                    buf.extend(chunk)
                    while len(buf) >= CHUNK_SIZE:
                        await send_audio(bytes(buf[:CHUNK_SIZE]))
                        total += CHUNK_SIZE
                        del buf[:CHUNK_SIZE]
                if buf:
                    if len(buf) % 2 != 0:
                        buf.append(0)
                    await send_audio(bytes(buf))
                    total += len(buf)
                print(f"[TTS/deepgram] Streamed {total} bytes PCM")
                return total > 0


def _resample_24k_to_16k(data: bytes) -> bytes:
    """Simple 3:2 decimation from 24kHz to 16kHz (linear interpolation)."""
    import array
    samples = array.array('h')
    samples.frombytes(data[:len(data) - len(data) % 2])
    n = len(samples)
    out = array.array('h')
    ratio = 24000 / 16000  # 1.5
    out_len = int(n / ratio)
    for i in range(out_len):
        src = i * ratio
        idx = int(src)
        frac = src - idx
        if idx + 1 < n:
            val = int(samples[idx] * (1 - frac) + samples[idx + 1] * frac)
        else:
            val = samples[min(idx, n - 1)]
        out.append(max(-32768, min(32767, val)))
    return out.tobytes()


# ── Provider factory ──

_STT_FACTORIES = {
    "elevenlabs": lambda: ElevenLabsSTT(k) if (k := _conf("ELEVENLABS_API_KEY")) else None,
    "openai": lambda: OpenAISTT(k) if (k := _conf("OPENAI_API_KEY")) else None,
    "deepgram": lambda: DeepgramSTT(k) if (k := _conf("DEEPGRAM_API_KEY")) else None,
}

_TTS_FACTORIES = {
    "elevenlabs": lambda v: ElevenLabsTTS(k, v or "bIHbv24MWmeRgasZH58o") if (k := _conf("ELEVENLABS_API_KEY")) else None,
    "openai": lambda v: OpenAITTS(k, v or "alloy") if (k := _conf("OPENAI_API_KEY")) else None,
    "deepgram": lambda v: DeepgramTTS(k, v or "aura-asteria-en") if (k := _conf("DEEPGRAM_API_KEY")) else None,
}


def create_stt_provider() -> tuple[Optional[STTProvider], str]:
    preferred = os.getenv("STT_PROVIDER", "elevenlabs").lower()
    # Try preferred provider first
    if preferred in _STT_FACTORIES:
        result = _STT_FACTORIES[preferred]()
        if result:
            return result, preferred
    # Fallback to first available
    for name, factory in _STT_FACTORIES.items():
        if name != preferred:
            result = factory()
            if result:
                print(f"[STT] {preferred} not available, falling back to {name}")
                return result, name
    return None, preferred


def create_tts_provider() -> tuple[Optional[TTSProvider], str]:
    preferred = os.getenv("TTS_PROVIDER", "elevenlabs").lower()
    voice = os.getenv("TTS_VOICE", "")
    # Try preferred provider first
    if preferred in _TTS_FACTORIES:
        result = _TTS_FACTORIES[preferred](voice)
        if result:
            return result, preferred
    # Fallback to first available
    for name, factory in _TTS_FACTORIES.items():
        if name != preferred:
            result = factory(voice)
            if result:
                print(f"[TTS] {preferred} not available, falling back to {name}")
                return result, name
    return None, preferred


# ── App setup ──

app = FastAPI(title="Clack Voice Relay")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
RELAY_AUTH_TOKEN = os.getenv("RELAY_AUTH_TOKEN", "")
DEFAULT_VOICE_ID = "bIHbv24MWmeRgasZH58o"
VOICE_ALIASES = {
    "will": "bIHbv24MWmeRgasZH58o",
    "aria": "9BWtsMINqrJLrRacOk9x",
    "roger": "CwhRBWXzGAHq8TQ4Fs17",
    "sarah": "EXAVITQu4vr4xnSDxMaL",
    "laura": "FGY2WhTYpPnrIDTdsKH5",
    "charlie": "IKne3meq5aSn9XLyUdCD",
    "george": "JBFqnCBsd6RMkjVDRZzb",
    "callum": "N2lVS1w4EtoT3dr4eOWO",
    "river": "SAz9YHcvj6GT2YYXdXww",
    "liam": "TX3LPaxmHKxFdv7VOQHJ",
    "charlotte": "XB0fDUnXU5powFXDhCwa",
    "alice": "Xb7hH8MSUJpSbSDYk0k2",
    "matilda": "XrExE9yKIg1WjnnlVkGX",
    "jessica": "cgSgspJ2msm6clMCkdW9",
    "eric": "cjVigY5qzO86Huf0OWal",
    "chris": "iP95p4xoKVk53GoZ742B",
    "brian": "nPczCjzI2devNBz1zQrb",
    "daniel": "onwK4e9ZLuTAKqWW03F9",
    "lily": "pFZP5JQG7iQjIQuC4Bku",
    "bill": "pqHfZKP75CvOlQylNhV4",
}
VOICE_METADATA = [
    {"id": "bIHbv24MWmeRgasZH58o", "name": "Will", "gender": "male", "accent": "American", "style": "warm, relaxed"},
    {"id": "9BWtsMINqrJLrRacOk9x", "name": "Aria", "gender": "female", "accent": "American", "style": "expressive, confident"},
    {"id": "CwhRBWXzGAHq8TQ4Fs17", "name": "Roger", "gender": "male", "accent": "American", "style": "deep, authoritative"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Sarah", "gender": "female", "accent": "American", "style": "soft, friendly"},
    {"id": "FGY2WhTYpPnrIDTdsKH5", "name": "Laura", "gender": "female", "accent": "American", "style": "gentle, soothing"},
    {"id": "IKne3meq5aSn9XLyUdCD", "name": "Charlie", "gender": "male", "accent": "Australian", "style": "casual, natural"},
    {"id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "gender": "male", "accent": "British", "style": "warm, narrative"},
    {"id": "N2lVS1w4EtoT3dr4eOWO", "name": "Callum", "gender": "male", "accent": "Scottish", "style": "intense, character"},
    {"id": "SAz9YHcvj6GT2YYXdXww", "name": "River", "gender": "nonbinary", "accent": "American", "style": "calm, confident"},
    {"id": "TX3LPaxmHKxFdv7VOQHJ", "name": "Liam", "gender": "male", "accent": "American", "style": "articulate, neutral"},
    {"id": "XB0fDUnXU5powFXDhCwa", "name": "Charlotte", "gender": "female", "accent": "Swedish", "style": "elegant, seductive"},
    {"id": "Xb7hH8MSUJpSbSDYk0k2", "name": "Alice", "gender": "female", "accent": "British", "style": "confident, clear"},
    {"id": "XrExE9yKIg1WjnnlVkGX", "name": "Matilda", "gender": "female", "accent": "American", "style": "warm, pleasant"},
    {"id": "cgSgspJ2msm6clMCkdW9", "name": "Jessica", "gender": "female", "accent": "American", "style": "expressive, upbeat"},
    {"id": "cjVigY5qzO86Huf0OWal", "name": "Eric", "gender": "male", "accent": "American", "style": "friendly, conversational"},
    {"id": "iP95p4xoKVk53GoZ742B", "name": "Chris", "gender": "male", "accent": "American", "style": "casual, clear"},
    {"id": "nPczCjzI2devNBz1zQrb", "name": "Brian", "gender": "male", "accent": "American", "style": "deep, narrative"},
    {"id": "onwK4e9ZLuTAKqWW03F9", "name": "Daniel", "gender": "male", "accent": "British", "style": "authoritative, deep"},
    {"id": "pFZP5JQG7iQjIQuC4Bku", "name": "Lily", "gender": "female", "accent": "British", "style": "warm, clear"},
    {"id": "pqHfZKP75CvOlQylNhV4", "name": "Bill", "gender": "male", "accent": "American", "style": "trustworthy, documentary"},
]
MAX_INPUT_CHARS = int(os.getenv("CLACK_MAX_INPUT_CHARS", "300"))

DEFAULT_SYSTEM_PROMPT = (
    "You are a voice assistant. The user is talking to you via voice. "
    "RESPONSE RULES — these are MANDATORY:\n"
    "- Keep responses to 1-3 sentences MAX. This is spoken conversation, not text.\n"
    "- NEVER use markdown formatting of any kind: no **bold**, *italics*, `code`, headers (#), "
    "bullet points (- or *), numbered lists (1.), links, or code blocks. Your output goes directly to TTS.\n"
    "- NEVER give long explanations. Be brief like a real person talking.\n"
    "- If the user asks something complex, give a short summary and offer to elaborate.\n"
    "- Respond naturally and directly. No filler phrases.\n"
    "- Do not include or reference any metadata, labels, or formatting artifacts.\n"
    "SAFETY: This is a voice session — transcription errors and hallucinations are common. "
    "NEVER execute destructive actions (delete files, send emails/messages, modify system settings, "
    "run shell commands, make purchases, or change configurations) based on voice input alone. "
    "For any action that modifies data or has external effects, describe what you WOULD do and ask for explicit confirmation. "
    "Read-only actions (search, weather, info lookups) are fine without confirmation."
)

stt_provider, STT_NAME = create_stt_provider()
tts_provider, TTS_NAME = create_tts_provider()


def detect_available_providers():
    """Detect all available STT and TTS providers based on API keys."""
    stt_providers = {}
    tts_providers = {}
    voice = os.getenv("TTS_VOICE", "")
    for name, factory in _STT_FACTORIES.items():
        result = factory()
        if result:
            stt_providers[name] = result
    for name, factory in _TTS_FACTORIES.items():
        result = factory(voice)
        if result:
            tts_providers[name] = result
    return stt_providers, tts_providers


available_stt, available_tts = detect_available_providers()
print(f"[Clack] Starting voice relay server...")
print(f"[Clack] STT: {STT_NAME} ({'ready' if stt_provider else 'NOT CONFIGURED'})")
print(f"[Clack] TTS: {TTS_NAME} ({'ready' if tts_provider else 'NOT CONFIGURED'})")
print(f"[Clack] Available STT: {list(available_stt.keys())}")
print(f"[Clack] Available TTS: {list(available_tts.keys())}")
print(f"[Clack] Gateway: {OPENCLAW_GATEWAY_URL}")
print(f"[Clack] Relay auth: {'ENABLED' if RELAY_AUTH_TOKEN else 'DISABLED (open!)'}")


# ── History ──

HISTORY_DIR = Path(os.getenv("CLACK_HISTORY_DIR", "/var/lib/clack/history"))
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
MAX_HISTORY_MESSAGES = int(os.getenv("CLACK_MAX_HISTORY", "200"))


def _history_path(conversation_id: str = None) -> Path:
    if conversation_id:
        return HISTORY_DIR / f"{conversation_id}.json"
    return HISTORY_DIR / "conversation.json"


def load_history(conversation_id: str = None) -> list:
    path = _history_path(conversation_id)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            print(f"[History] Loaded {len(data)} messages" + (f" (conv={conversation_id[:8]})" if conversation_id else ""))
            return data[-MAX_HISTORY_MESSAGES:]
        except Exception as e:
            print(f"[History] Error loading: {e}")
    return []


def save_history(history: list, conversation_id: str = None):
    path = _history_path(conversation_id)
    try:
        path.write_text(json.dumps(history[-MAX_HISTORY_MESSAGES:]))
    except Exception as e:
        print(f"[History] Error saving: {e}")


# ── Conversations metadata ──

CONVERSATIONS_META_FILE = HISTORY_DIR / "conversations_meta.json"

def load_conversations_meta() -> dict:
    if CONVERSATIONS_META_FILE.exists():
        try:
            return json.loads(CONVERSATIONS_META_FILE.read_text())
        except Exception as e:
            print(f"[Conversations] Error loading meta: {e}")
    return {}

def save_conversations_meta(meta: dict):
    try:
        CONVERSATIONS_META_FILE.write_text(json.dumps(meta, indent=2))
    except Exception as e:
        print(f"[Conversations] Error saving meta: {e}")

# Per-conversation locks to prevent race conditions between voice + text
_conversation_locks: dict[str, asyncio.Lock] = {}

def _get_conversation_lock(conversation_id: str) -> asyncio.Lock:
    if conversation_id not in _conversation_locks:
        _conversation_locks[conversation_id] = asyncio.Lock()
    return _conversation_locks[conversation_id]


def _migrate_legacy_history():
    """Migrate legacy single conversation.json to per-conversation storage."""
    legacy_path = HISTORY_DIR / "conversation.json"
    if not legacy_path.exists():
        return
    try:
        data = json.loads(legacy_path.read_text())
        if not data:
            return
    except Exception:
        return
    meta = load_conversations_meta()
    # Check if already migrated
    if any(m.get("migrated_from_legacy") for m in meta.values()):
        return
    import uuid
    conv_id = str(uuid.uuid4())
    now = time.time()
    # Get title from first user message
    title = "Conversation"
    for msg in data:
        if msg.get("role") == "user" and msg.get("content"):
            title = msg["content"][:60]
            break
    meta[conv_id] = {
        "title": title,
        "session_key": "",
        "created_at": now,
        "updated_at": now,
        "migrated_from_legacy": True,
    }
    # Copy history to new file (keep legacy intact for v1 clients)
    save_history(data, conv_id)
    save_conversations_meta(meta)
    print(f"[Migration] Migrated legacy history → conversation {conv_id[:8]} ({len(data)} messages)")


# Run migration on startup
_migrate_legacy_history()


import ipaddress

TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")

def is_tailscale_ip(ip: str) -> bool:
    """Check if an IP is in the Tailscale CGNAT range (100.64.0.0/10)."""
    try:
        return ipaddress.ip_address(ip) in TAILSCALE_NETWORK
    except ValueError:
        return False

CLACK_GUEST_TOKEN = os.getenv("CLACK_GUEST_TOKEN", "")

def verify_token(token: str, client_ip: str = "") -> bool:
    """Verify auth token. Tailscale IPs bypass pairing. Non-Tailscale requires valid token."""
    if is_tailscale_ip(client_ip):
        return True
    if not RELAY_AUTH_TOKEN:
        return False  # No token configured = Tailscale-only mode
    if CLACK_GUEST_TOKEN and hmac.compare_digest(token, CLACK_GUEST_TOKEN):
        return True
    return hmac.compare_digest(token, RELAY_AUTH_TOKEN)


# ── Pairing ──

import secrets
import string

_pairing_codes: dict = {}  # code -> {"expires": timestamp}
PAIRING_TTL = 300  # 5 minutes


def _generate_pairing_code() -> str:
    """Generate a 6-char alphanumeric one-time pairing code."""
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    _pairing_codes[code] = {"expires": time.time() + PAIRING_TTL}
    # Clean expired codes
    now = time.time()
    expired = [c for c, v in _pairing_codes.items() if v["expires"] < now]
    for c in expired:
        del _pairing_codes[c]
    return code


def _redeem_pairing_code(code: str) -> bool:
    """Validate and consume a one-time pairing code."""
    code = code.upper().strip()
    entry = _pairing_codes.pop(code, None)
    if not entry:
        return False
    if time.time() > entry["expires"]:
        return False
    return True


# ── Context store ──
# Persistent context that gets injected into the system prompt.
# Can be set via HTTP endpoint or WebSocket message.

CONTEXT_FILE = HISTORY_DIR / "context.json"


def sanitize_context(text: str) -> str:
    """Sanitize user-provided context for voice-first input.

    Only allows natural language characters that could reasonably
    appear in spoken or dictated text:
    - Letters (any script), numbers, whitespace
    - Common punctuation: . , ! ? ; : ' " - ( ) @ # & + = %
    - Newlines and tabs (for lists and structure)
    - Currency symbols, accented characters, emoji
    Strips everything else (control chars, escape sequences, slashes,
    IP addresses, domains, URLs, etc.).
    """
    import re as _re
    # Keep: word chars (any script), digits, whitespace, common punctuation (no slashes)
    text = _re.sub(r'[^\w\s.,!?;:\'\"()\-–—@#&+=*%€$£¥°…]', '', text, flags=_re.UNICODE)
    # Strip IP addresses (with optional port)
    text = _re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?\b', '', text)
    # Strip domains (e.g. example.com, sub.example.co.uk)
    text = _re.sub(r'\b[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}(\.[a-zA-Z]{2,})*(:\d+)?\b', '', text)
    # Collapse excessive whitespace
    text = _re.sub(r'\n{4,}', '\n\n\n', text)
    text = _re.sub(r' {4,}', '   ', text)
    # Truncate
    return text.strip()[:1000]


def load_context() -> dict:
    """Load saved context. Returns dict with 'text' and optional metadata."""
    if CONTEXT_FILE.exists():
        try:
            return json.loads(CONTEXT_FILE.read_text())
        except Exception as e:
            print(f"[Context] Error loading: {e}")
    return {}


def save_context(ctx: dict):
    """Save context to disk."""
    try:
        CONTEXT_FILE.write_text(json.dumps(ctx))
        print(f"[Context] Saved: {json.dumps(ctx)[:100]}")
    except Exception as e:
        print(f"[Context] Error saving: {e}")


# ── Voice Session ──

class VoiceSession:
    def __init__(self, websocket: WebSocket, config: dict):
        self.websocket = websocket
        self.config = config
        self.conversation_id = config.get("conversation_id")
        self.user_context = load_context()
        self.system_prompt = self._build_system_prompt(config)
        self.conversation_history = load_history(self.conversation_id)
        self.audio_buffer = bytearray()
        self.last_assistant_response = ""
        self.processing = False
        self.interrupted = False
        self.greeting_enabled = config.get("greetingEnabled", True)

        # STT provider selection
        stt_choice = config.get("sttProvider", "").lower() if config.get("sttProvider") else ""
        if stt_choice == "local":
            self.stt = None
            self.local_stt = True
        elif stt_choice and stt_choice in available_stt:
            self.stt = available_stt[stt_choice]
            self.local_stt = False
        else:
            self.stt = stt_provider
            self.local_stt = False

        # TTS provider selection
        tts_choice = config.get("ttsProvider", "").lower() if config.get("ttsProvider") else ""
        voice = config.get("voice", "")
        if tts_choice == "local":
            self.tts = None
            self.local_tts = True
        elif tts_choice and tts_choice in available_tts:
            self.tts = self._create_tts_with_voice(tts_choice, voice, available_tts[tts_choice])
            self.local_tts = False
        else:
            if voice and tts_provider:
                self.tts = self._create_tts_with_voice(TTS_NAME.lower(), voice, tts_provider)
            else:
                self.tts = tts_provider
            self.local_tts = config.get("localTTS", False)

        # Apply context from start config if provided
        if config.get("context"):
            self.update_context(config["context"])

    @staticmethod
    def _create_tts_with_voice(provider_name: str, voice: str, fallback_provider):
        """Create a TTS provider instance with the requested voice."""
        if not voice:
            return fallback_provider
        if provider_name == "elevenlabs":
            el_key = _conf("ELEVENLABS_API_KEY")
            resolved = VOICE_ALIASES.get(voice.lower(), voice)
            return ElevenLabsTTS(el_key, resolved)
        elif provider_name == "openai":
            # OpenAI voices: alloy, ash, coral, echo, fable, onyx, nova, sage, shimmer
            oai_key = _conf("OPENAI_API_KEY")
            return OpenAITTS(oai_key, voice.lower())
        elif provider_name == "deepgram":
            # Deepgram voices: allow short name (e.g. "asteria") or full ID
            dg_key = _conf("DEEPGRAM_API_KEY")
            # Map short names to full IDs
            dg_aliases = {v["name"].lower(): v["id"] for v in DEEPGRAM_VOICES}
            resolved = dg_aliases.get(voice.lower(), voice)
            return DeepgramTTS(dg_key, resolved)
        return fallback_provider

    def _build_system_prompt(self, config: dict) -> str:
        """Build system prompt with optional user context injected."""
        base = config.get("systemPrompt", DEFAULT_SYSTEM_PROMPT)
        ctx = self.user_context
        if ctx.get("text"):
            sanitized = sanitize_context(ctx['text'])
            base += (
                "\n\n--- BEGIN USER CONTEXT ---\n"
                f"{sanitized}\n"
                "--- END USER CONTEXT ---"
            )
        # Inject session key from conversation metadata if available
        if self.conversation_id:
            meta = load_conversations_meta()
            conv_meta = meta.get(self.conversation_id, {})
            session_key = conv_meta.get("session_key", "")
            if session_key:
                base += f"\n\nSession: {session_key}"
        return base

    def update_context(self, text: str) -> str:
        """Update the user context and rebuild system prompt. Returns sanitized text."""
        sanitized = sanitize_context(text)
        self.user_context = {"text": sanitized, "updated": time.time()}
        save_context(self.user_context)
        self.system_prompt = self._build_system_prompt(self.config)
        print(f"[Context] Updated: {sanitized[:100]}")
        return sanitized

    async def send_json(self, data: dict):
        await self.websocket.send_text(json.dumps(data))

    async def send_audio(self, audio_data: bytes):
        if self.interrupted:
            return  # Stop sending audio when interrupted
        await self.websocket.send_bytes(audio_data)

    def interrupt(self):
        """Mark session as interrupted — stops audio streaming."""
        self.interrupted = True
        self.processing = False
        print("[Session] Interrupted by client")

    async def synthesize_chunked(self, text: str):
        """Split long text into sentence chunks and synthesize sequentially.
        Respects interrupt — stops immediately if session is interrupted."""
        if not self.tts:
            return
        # Split at sentence boundaries
        chunks = re.split(r'(?<=[.!?])\s+', text.strip())
        # Merge small chunks to avoid too many API calls
        merged = []
        current = ""
        for chunk in chunks:
            if len(current) + len(chunk) + 1 <= 500:
                current = (current + " " + chunk).strip() if current else chunk
            else:
                if current:
                    merged.append(current)
                current = chunk
        if current:
            merged.append(current)

        print(f"[TTS] Chunked: {len(merged)} parts from {len(text)} chars")
        for i, chunk in enumerate(merged):
            if self.interrupted:
                print(f"[TTS] Interrupted at chunk {i+1}/{len(merged)}")
                break
            try:
                await self.tts.synthesize_stream(chunk, self.send_audio)
            except ProviderError as e:
                await self.send_json({"type": "error", "message": f"TTS failed: {e.provider} (HTTP {e.status})"})
                break

    async def transcribe_audio(self, audio_data: bytes) -> Optional[str]:
        if not self.stt:
            print("[STT] No provider configured")
            return None
        MAX_CHUNK = 960000
        if len(audio_data) > MAX_CHUNK:
            chunks = [audio_data[i:i + MAX_CHUNK] for i in range(0, len(audio_data), MAX_CHUNK)]
            print(f"[STT] Splitting {len(audio_data)} bytes into {len(chunks)} chunks")
            parts = [await self.stt.transcribe(c) for c in chunks]
            combined = " ".join(p for p in parts if p)
            return combined if combined.strip() else None
        return await self.stt.transcribe(audio_data)

    async def greet(self):
        if not self.greeting_enabled:
            print("[Greeting] Disabled by client")
            return
        messages = [
            {"role": "system", "content": self.system_prompt},
            *[{"role": m["role"], "content": m["content"]} for m in self.conversation_history],
            {"role": "user", "content": "[Voice session started. Greet the user briefly.]"},
        ]
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}", "Content-Type": "application/json"}
            payload = {"model": "openclaw", "messages": messages, "max_tokens": 150}
            try:
                async with session.post(
                    f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                    headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        greeting = _strip_markdown(result["choices"][0]["message"]["content"])
                    else:
                        greeting = "Hey!"
            except Exception:
                greeting = "Hey!"
        self.conversation_history.append({"role": "assistant", "content": greeting, "voice": True})
        self.last_assistant_response = greeting
        print(f"[Greeting] {greeting} (localTTS={self.local_tts})")
        await self.send_json({"type": "response_text", "text": greeting})
        if not self.local_tts:
            await self.send_json({"type": "response_start", "format": "pcm_16000"})
            await self.synthesize_chunked(greeting)
        await self.send_json({"type": "response_end"})

    async def get_llm_response(self, user_message: str) -> Optional[str]:
        self.conversation_history.append({"role": "user", "content": user_message, "voice": True})
        save_history(self.conversation_history, self.conversation_id)
        # Strip non-standard fields (voice) before sending to LLM
        messages = [{"role": "system", "content": self.system_prompt}] + [
            {"role": m["role"], "content": m["content"]} for m in self.conversation_history
        ]

        async def _llm_call():
            for attempt in range(3):
                try:
                    async with aiohttp.ClientSession() as session:
                        headers = {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}", "Content-Type": "application/json"}
                        payload = {"model": "openclaw", "messages": messages, "max_tokens": 150}
                        async with session.post(
                            f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                            headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
                        ) as resp:
                            if resp.status == 200:
                                result = await resp.json()
                                content = result["choices"][0]["message"]["content"]
                                if content and "no response from" not in content.lower():
                                    return content
                                print(f"[LLM] Gateway returned empty/timeout response (attempt {attempt+1}/3): {content}")
                            else:
                                print(f"[LLM] OpenClaw error: {resp.status} - {await resp.text()}")
                except Exception as e:
                    print(f"[LLM] Connection error (attempt {attempt+1}/3): {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
            return None

        # Run LLM call with keepalive pings to prevent client timeout
        try:
            llm_task = asyncio.create_task(_llm_call())
            while not llm_task.done():
                done, _ = await asyncio.wait({llm_task}, timeout=5.0)
                if not done:
                    try:
                        await self.send_json({"type": "processing", "stage": "thinking"})
                        print(f"[LLM] Still waiting, sent keepalive")
                    except Exception:
                        pass
            content = llm_task.result()
            if content:
                content = _strip_markdown(content)
                self.conversation_history.append({"role": "assistant", "content": content, "voice": True})
                save_history(self.conversation_history, self.conversation_id)
                # Update conversation metadata timestamp
                if self.conversation_id:
                    meta = load_conversations_meta()
                    if self.conversation_id in meta:
                        meta[self.conversation_id]["updated_at"] = time.time()
                        save_conversations_meta(meta)
                return content
            return "Sorry, I had trouble processing that."
        except Exception as e:
            print(f"[LLM] Connection error: {e}")
            return "Sorry, I couldn't reach the assistant right now."


# ── HTTP endpoints ──

@app.get("/")
async def root():
    return {"status": "ok", "service": "Clack Voice Relay", "stt": STT_NAME, "tts": TTS_NAME}

@app.get("/health")
async def health():
    return {"status": "ok", "backend": "openclaw", "stt": STT_NAME, "tts": TTS_NAME}

OPENAI_VOICES = [
    {"id": "alloy", "name": "Alloy", "gender": "neutral", "style": "balanced, versatile"},
    {"id": "ash", "name": "Ash", "gender": "male", "style": "clear, direct"},
    {"id": "coral", "name": "Coral", "gender": "female", "style": "warm, engaging"},
    {"id": "echo", "name": "Echo", "gender": "male", "style": "smooth, narrative"},
    {"id": "fable", "name": "Fable", "gender": "male", "style": "expressive, British"},
    {"id": "onyx", "name": "Onyx", "gender": "male", "style": "deep, authoritative"},
    {"id": "nova", "name": "Nova", "gender": "female", "style": "warm, friendly"},
    {"id": "sage", "name": "Sage", "gender": "female", "style": "calm, thoughtful"},
    {"id": "shimmer", "name": "Shimmer", "gender": "female", "style": "bright, optimistic"},
]

DEEPGRAM_VOICES = [
    {"id": "aura-asteria-en", "name": "Asteria", "gender": "female", "accent": "American", "style": "warm, professional"},
    {"id": "aura-luna-en", "name": "Luna", "gender": "female", "accent": "American", "style": "soft, soothing"},
    {"id": "aura-stella-en", "name": "Stella", "gender": "female", "accent": "American", "style": "clear, confident"},
    {"id": "aura-athena-en", "name": "Athena", "gender": "female", "accent": "British", "style": "elegant, articulate"},
    {"id": "aura-hera-en", "name": "Hera", "gender": "female", "accent": "American", "style": "warm, narrative"},
    {"id": "aura-orion-en", "name": "Orion", "gender": "male", "accent": "American", "style": "deep, clear"},
    {"id": "aura-arcas-en", "name": "Arcas", "gender": "male", "accent": "American", "style": "confident, engaging"},
    {"id": "aura-perseus-en", "name": "Perseus", "gender": "male", "accent": "American", "style": "strong, narrative"},
    {"id": "aura-angus-en", "name": "Angus", "gender": "male", "accent": "Irish", "style": "warm, friendly"},
    {"id": "aura-orpheus-en", "name": "Orpheus", "gender": "male", "accent": "American", "style": "smooth, rich"},
    {"id": "aura-helios-en", "name": "Helios", "gender": "male", "accent": "British", "style": "refined, clear"},
    {"id": "aura-zeus-en", "name": "Zeus", "gender": "male", "accent": "American", "style": "authoritative, deep"},
]

@app.get("/voices")
async def list_voices(request: Request, token: str = Query(default=""), provider: str = Query(default="")):
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    default = os.getenv("TTS_VOICE", "bIHbv24MWmeRgasZH58o")

    if provider == "openai":
        return {"voices": OPENAI_VOICES, "default": "alloy", "provider": "openai"}
    elif provider == "deepgram":
        return {"voices": DEEPGRAM_VOICES, "default": "aura-asteria-en", "provider": "deepgram"}
    else:
        # Default: ElevenLabs
        return {"voices": VOICE_METADATA, "default": default, "provider": "elevenlabs"}


CLACK_SERVER_VERSION = "2.0.0"
CLACK_MIN_APP_VERSION = os.getenv("CLACK_MIN_APP_VERSION", "1.0.0")

@app.get("/info")
async def info(request: Request, token: str = Query(default="")):
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {
        "agentName": _get_agent_name(),
        "serverVersion": CLACK_SERVER_VERSION,
        "minAppVersion": CLACK_MIN_APP_VERSION,
        "stt": {
            "available": list(available_stt.keys()),
            "default": os.getenv("STT_PROVIDER", "elevenlabs").lower()
        },
        "tts": {
            "available": list(available_tts.keys()),
            "default": os.getenv("TTS_PROVIDER", "elevenlabs").lower()
        }
    }


@app.get("/pair")
async def create_pairing(request: Request, token: str = Query(default="")):
    """Generate a one-time pairing code. Requires admin auth (relay token)."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    code = _generate_pairing_code()
    print(f"[Pair] Generated code: {code} (expires in {PAIRING_TTL}s)")
    return {"code": code, "expires_in": PAIRING_TTL}


@app.post("/pair")
async def redeem_pairing(code: str = Query(default="")):
    """Redeem a pairing code to get the auth token. No auth required."""
    if not code:
        return JSONResponse({"error": "code required"}, status_code=400)
    # Guest token acts as a permanent pairing code
    if CLACK_GUEST_TOKEN and hmac.compare_digest(code.upper().strip(), CLACK_GUEST_TOKEN.upper().strip()):
        print(f"[Pair] Guest token accepted")
        return {"token": RELAY_AUTH_TOKEN}
    if _redeem_pairing_code(code):
        print(f"[Pair] Code redeemed successfully")
        return {"token": RELAY_AUTH_TOKEN}
    else:
        print(f"[Pair] Invalid/expired code: {code}")
        return JSONResponse({"error": "invalid or expired code"}, status_code=401)


@app.get("/sessions")
async def list_sessions(request: Request, token: str = Query(default="")):
    """List available OpenClaw sessions."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        headers = {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{OPENCLAW_GATEWAY_URL}/tools/invoke",
                headers=headers,
                json={"tool": "sessions_list", "args": {"limit": 50, "messageLimit": 0}},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
              if resp.status == 200:
                data = await resp.json()
                raw_sessions = data.get("result", {}).get("details", {}).get("sessions", [])
                sessions = []
                for s in raw_sessions:
                    name = s.get("displayName", "")
                    if not name:
                        continue
                    sessions.append({
                        "key": s.get("key", ""),
                        "name": name,
                        "channel": s.get("channel", ""),
                    })
                return {"sessions": sessions}
    except Exception as e:
        print(f"[Sessions] Error: {e}")
    return {"sessions": []}


@app.get("/history")
async def get_history(request: Request, token: str = Query(default="")):
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    history = load_history()
    return {"messages": history, "count": len(history)}

@app.delete("/history")
async def clear_history(request: Request, token: str = Query(default="")):
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    path = _history_path()
    if path.exists():
        path.unlink()
    return {"cleared": True}


@app.get("/context")
async def get_context(request: Request, token: str = Query(default="")):
    """Get the current user context."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return load_context() or {"text": ""}


@app.put("/context")
async def set_context_put(request: Request, token: str = Query(default=""), text: str = Query(default="")):
    """Set user context via query param."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    sanitized = sanitize_context(text)
    ctx = {"text": sanitized, "updated": time.time()}
    save_context(ctx)
    return {"ok": True, "context": ctx}



@app.post("/context")
async def set_context_post(request: Request, token: str = Query(default="")):
    """Set user context via JSON body: {"text": "..."}"""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        text = body.get("text", "")
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    sanitized = sanitize_context(text)
    ctx = {"text": sanitized, "updated": time.time()}
    save_context(ctx)
    return {"ok": True, "context": ctx}


@app.delete("/context")
async def clear_context(request: Request, token: str = Query(default="")):
    """Clear user context."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if CONTEXT_FILE.exists():
        CONTEXT_FILE.unlink()
    return {"cleared": True}


# ── Conversations CRUD ──

@app.get("/conversations")
async def list_conversations(request: Request, token: str = Query(default="")):
    """List all conversations with metadata."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meta = load_conversations_meta()
    conversations = []
    for conv_id, info in sorted(meta.items(), key=lambda x: x[1].get("updated_at", 0), reverse=True):
        history = load_history(conv_id)
        last_preview = ""
        if history:
            last_msg = history[-1]
            last_preview = last_msg.get("content", "")[:100]
        conversations.append({
            "id": conv_id,
            "title": info.get("title", "Untitled"),
            "session_key": info.get("session_key", ""),
            "last_message_preview": last_preview,
            "message_count": len(history),
            "created_at": info.get("created_at", 0),
            "updated_at": info.get("updated_at", 0),
        })
    return {"conversations": conversations}


@app.post("/conversations")
async def create_conversation(request: Request, token: str = Query(default="")):
    """Create a new conversation."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    import uuid
    conv_id = str(uuid.uuid4())
    now = time.time()
    meta = load_conversations_meta()
    meta[conv_id] = {
        "title": body.get("title", "New Conversation"),
        "session_key": body.get("session_key", ""),
        "created_at": now,
        "updated_at": now,
    }
    save_conversations_meta(meta)
    save_history([], conv_id)
    print(f"[Conversations] Created {conv_id[:8]}: {meta[conv_id]['title']}")
    return {
        "id": conv_id,
        "title": meta[conv_id]["title"],
        "session_key": meta[conv_id]["session_key"],
        "message_count": 0,
        "created_at": now,
        "updated_at": now,
    }


@app.get("/conversations/{conversation_id}/history")
async def get_conversation_history(conversation_id: str, request: Request, token: str = Query(default="")):
    """Get history for a specific conversation."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meta = load_conversations_meta()
    if conversation_id not in meta:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    history = load_history(conversation_id)
    return {"messages": history, "count": len(history), "conversation_id": conversation_id}


@app.patch("/conversations/{conversation_id}")
async def update_conversation(conversation_id: str, request: Request, token: str = Query(default="")):
    """Update a conversation's metadata (title)."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meta = load_conversations_meta()
    if conversation_id not in meta:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if "title" in body:
        meta[conversation_id]["title"] = body["title"][:100]
    meta[conversation_id]["updated_at"] = time.time()
    save_conversations_meta(meta)
    return {"ok": True, "title": meta[conversation_id]["title"]}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request, token: str = Query(default="")):
    """Delete a conversation and its history."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    meta = load_conversations_meta()
    if conversation_id not in meta:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    del meta[conversation_id]
    save_conversations_meta(meta)
    path = _history_path(conversation_id)
    if path.exists():
        path.unlink()
    # Clean up lock
    _conversation_locks.pop(conversation_id, None)
    print(f"[Conversations] Deleted {conversation_id[:8]}")
    return {"deleted": True}


# ── Text Chat ──

TEXT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. The user is chatting with you via text. "
    "Respond naturally and helpfully. You may use markdown formatting when it aids readability. "
    "SAFETY: NEVER execute destructive actions (delete files, send emails/messages, modify system settings, "
    "run shell commands, make purchases, or change configurations) based on text input alone. "
    "For any action that modifies data or has external effects, describe what you WOULD do and ask for explicit confirmation. "
    "Read-only actions (search, weather, info lookups) are fine without confirmation."
)


@app.post("/chat")
async def chat(request: Request, token: str = Query(default="")):
    """Send a text message in a conversation and get a response."""
    if not verify_token(token, request.client.host):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    conversation_id = body.get("conversation_id", "")
    message = body.get("message", "").strip()

    if not conversation_id:
        return JSONResponse({"error": "conversation_id required"}, status_code=400)
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    meta = load_conversations_meta()
    if conversation_id not in meta:
        return JSONResponse({"error": "conversation not found"}, status_code=404)

    conv_meta = meta[conversation_id]
    lock = _get_conversation_lock(conversation_id)

    async with lock:
        history = load_history(conversation_id)

        # Build system prompt for text mode
        system_prompt = TEXT_SYSTEM_PROMPT

        # Inject user context if available
        ctx = load_context()
        if ctx.get("text"):
            system_prompt += (
                "\n\n--- BEGIN USER CONTEXT ---\n"
                f"{sanitize_context(ctx['text'])}\n"
                "--- END USER CONTEXT ---"
            )

        # Inject session key as context for OpenClaw routing
        session_key = conv_meta.get("session_key", "")
        if session_key:
            system_prompt += f"\n\nSession: {session_key}"

        # Add user message
        history.append({"role": "user", "content": message})

        # Auto-title: use first user message as title
        if conv_meta.get("title") == "New Conversation":
            conv_meta["title"] = message[:60]
            meta[conversation_id] = conv_meta
            save_conversations_meta(meta)

        messages = [{"role": "system", "content": system_prompt}] + history

        # Call OpenClaw (retry if gateway returns empty/timeout placeholder)
        content = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}", "Content-Type": "application/json"}
                    payload = {"model": "openclaw", "messages": messages, "max_tokens": 1000}
                    async with session.post(
                        f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions",
                        headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
                    ) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            content = result["choices"][0]["message"]["content"]
                            if content and "no response from" not in content.lower():
                                break
                            print(f"[Chat] Gateway returned empty/timeout response (attempt {attempt+1}/3): {content}")
                            content = None
                        else:
                            err = await resp.text()
                            print(f"[Chat] LLM error: {resp.status} - {err}")
            except Exception as e:
                print(f"[Chat] Connection error (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2)
        if not content:
            content = "Sorry, I had trouble processing that. Please try again."

        # Save to history
        history.append({"role": "assistant", "content": content})
        save_history(history, conversation_id)

        # Update metadata timestamp
        conv_meta["updated_at"] = time.time()
        meta[conversation_id] = conv_meta
        save_conversations_meta(meta)

        print(f"[Chat] conv={conversation_id[:8]} user='{message[:50]}' → '{content[:50]}'")

        return {
            "role": "assistant",
            "content": content,
            "conversation_id": conversation_id,
        }


# ── WebSocket ──

@app.websocket("/voice")
async def voice_endpoint(websocket: WebSocket, token: str = Query(default="")):
    await websocket.accept()
    print(f"[WS] Client connected: {websocket.client}")
    client_ip = websocket.client.host if websocket.client else ""
    authenticated = verify_token(token, client_ip) if token else (is_tailscale_ip(client_ip) or not RELAY_AUTH_TOKEN)
    session = None
    try:
        while True:
            message = await websocket.receive()
            if "text" in message:
                data = json.loads(message["text"])
                msg_type = data.get("type")
                if msg_type == "auth" and not authenticated:
                    if verify_token(data.get("token", ""), client_ip):
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
                    print(f"[WS] Session started")
                    await session.send_json({"type": "ready"})
                    if session.conversation_history:
                        print(f"[WS] Resuming ({len(session.conversation_history)} messages)")
                    await session.greet()
                elif msg_type == "set_context" and session:
                    ctx_text = data.get("text", "")
                    if ctx_text:
                        sanitized = session.update_context(ctx_text)
                        await session.send_json({"type": "context_updated", "text": sanitized})
                    else:
                        # Clear context
                        session.user_context = {}
                        if CONTEXT_FILE.exists():
                            CONTEXT_FILE.unlink()
                        session.system_prompt = session._build_system_prompt(session.config)
                        await session.send_json({"type": "context_cleared"})
                elif msg_type == "text_input" and session:
                    text = data.get("text", "").strip()
                    local_tts_override = data.get("localTTS", session.local_tts)
                    if text and not session.processing:
                        session.interrupted = False
                        session.processing = True
                        print(f"[WS] text_input: '{text[:100]}' (localTTS={local_tts_override})")
                        if len(text) > MAX_INPUT_CHARS:
                            text = text[:MAX_INPUT_CHARS]
                        await session.send_json({"type": "transcript", "text": text, "final": True})
                        await session.send_json({"type": "processing", "stage": "thinking"})
                        response = await session.get_llm_response(text)
                        if response:
                            print(f"[LLM] Response: {response[:100]}...")
                            session.last_assistant_response = response
                            await session.send_json({"type": "response_text", "text": response})
                            if not session.interrupted and not local_tts_override:
                                await session.send_json({"type": "processing", "stage": "speaking"})
                                await session.send_json({"type": "response_start", "format": "pcm_16000"})
                                await session.synthesize_chunked(response)
                                if not session.interrupted:
                                    await session.send_json({"type": "response_end"})
                            else:
                                await session.send_json({"type": "response_end"})
                        session.processing = False
                elif msg_type == "interrupt" and session:
                    print(f"[WS] Client interrupted")
                    session.interrupt()
                    session.audio_buffer = bytearray()
                    await session.send_json({"type": "response_end"})
                elif msg_type == "end_speech" and session:
                    if session.local_stt:
                        session.audio_buffer = bytearray()
                        continue
                    if session.processing:
                        print(f"[WS] Ignoring end_speech — still processing")
                        session.audio_buffer = bytearray()
                        continue
                    if session.audio_buffer:
                        session.interrupted = False  # Reset interrupt flag for new processing
                        session.processing = True
                        print(f"[WS] Processing {len(session.audio_buffer)} bytes")
                        await session.send_json({"type": "processing", "stage": "transcribing"})
                        try:
                            transcript = await session.transcribe_audio(bytes(session.audio_buffer))
                        except ProviderError as e:
                            await session.send_json({"type": "error", "message": f"STT failed: {e.provider} (HTTP {e.status})"})
                            await session.send_json({"type": "response_end"})
                            session.audio_buffer = bytearray()
                            session.processing = False
                            continue
                        if not transcript:
                            await session.send_json({"type": "processing", "stage": "filtered"})
                            await session.send_json({"type": "response_end"})
                            session.audio_buffer = bytearray()
                            session.processing = False
                            continue
                        if session.last_assistant_response and _is_echo(transcript, session.last_assistant_response):
                            print(f"[STT] Filtered: echo")
                            await session.send_json({"type": "processing", "stage": "filtered"})
                            await session.send_json({"type": "response_end"})
                            session.audio_buffer = bytearray()
                            session.processing = False
                            continue
                        # Strip speaker labels — they confuse the LLM
                        transcript = re.sub(r'\[Speaker \w+\]:\s*', '', transcript).strip()
                        # Enforce input length limit
                        if len(transcript) > MAX_INPUT_CHARS:
                            print(f"[STT] Truncated: {len(transcript)} → {MAX_INPUT_CHARS} chars")
                            transcript = transcript[:MAX_INPUT_CHARS]
                        print(f"[STT] Transcript: {transcript}")
                        await session.send_json({"type": "transcript", "text": transcript, "final": True})
                        await session.send_json({"type": "processing", "stage": "thinking"})
                        response = await session.get_llm_response(transcript)
                        if response:
                            print(f"[LLM] Response: {response[:100]}...")
                            session.last_assistant_response = response
                            await session.send_json({"type": "response_text", "text": response})
                            if not session.interrupted and not session.local_tts:
                                await session.send_json({"type": "processing", "stage": "speaking"})
                                await session.send_json({"type": "response_start", "format": "pcm_16000"})
                                await session.synthesize_chunked(response)
                                if not session.interrupted:
                                    await session.send_json({"type": "response_end"})
                            else:
                                if not session.interrupted:
                                    await session.send_json({"type": "response_end"})
                        session.audio_buffer = bytearray()
                        session.processing = False
                elif msg_type == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            elif "bytes" in message and session:
                if not authenticated:
                    continue
                if not session.local_stt:
                    session.audio_buffer.extend(message["bytes"])
    except WebSocketDisconnect:
        print(f"[WS] Client disconnected")
    except RuntimeError as e:
        if "disconnect" in str(e).lower():
            print(f"[WS] Client disconnected (runtime)")
        else:
            print(f"[WS] Runtime error: {e}")
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        print(f"[WS] Session ended")


if __name__ == "__main__":
    port = int(os.getenv("VOICE_RELAY_PORT", "9878"))
    uvicorn.run(app, host="0.0.0.0", port=port, ws_ping_interval=20, ws_ping_timeout=None)
