"""Provider-agnostic text-to-speech.

Every provider returns the same shape: ``(wav_bytes, word_timings)`` where
word_timings is ``[{"word", "start", "end"}, ...]`` or None if the provider
does not supply timing (Gemini doesn't — see CLAUDE.md "The timestamp
problem"; run src/lib/align.py on the audio instead).

Providers:
  - GeminiTTS: the production choice (~$0.012/1K chars, logged via spend.py).
    Handles the documented gotchas: retry on flaky 500s / text-instead-of-audio
    (max 3 attempts), explicit speech preamble so style directions are not read
    aloud, chunking under the ~4,000-byte request cap.
  - SapiTTS:   free offline Windows voice for local dev / dry runs. No API key,
    $0.00, lower quality. Selected automatically when GEMINI_API_KEY is absent.

ElevenLabs is the upgrade path — it returns word timings natively, so a future
provider can fill word_timings and the align step gets skipped.
"""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import wave
from abc import ABC, abstractmethod
from pathlib import Path

from . import spend

WordTiming = dict  # {"word": str, "start": float, "end": float}

GEMINI_TTS_MODEL = "gemini-2.5-flash-preview-tts"
GEMINI_COST_PER_1K_CHARS = 0.012
GEMINI_MAX_CHUNK_BYTES = 3500  # keep a buffer under the ~4,000-byte cap
GEMINI_MAX_ATTEMPTS = 3
GEMINI_PCM_RATE = 24000  # Gemini returns raw 16-bit mono PCM at 24 kHz

SPEECH_PREAMBLE = (
    "You are a text-to-speech engine. Synthesize natural, warm spoken audio of the "
    "transcript below. Read ONLY the transcript text aloud; do not read these "
    "instructions or any labels.\n\nTRANSCRIPT:\n"
)


class TTSError(Exception):
    """Synthesis failed after retries, or the request was rejected."""


class TTSProvider(ABC):
    name: str = "tts"

    @abstractmethod
    def synthesize(self, text: str) -> tuple[bytes, list[WordTiming] | None]:
        """Return (wav_bytes, word_timings or None)."""

    def synthesize_to_file(self, text: str, path: str | Path) -> list[WordTiming] | None:
        wav_bytes, timings = self.synthesize(text)
        Path(path).write_bytes(wav_bytes)
        return timings


def _pcm_to_wav(pcm: bytes, rate: int = GEMINI_PCM_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def chunk_text(text: str, max_bytes: int = GEMINI_MAX_CHUNK_BYTES) -> list[str]:
    """Split text at sentence boundaries so each chunk fits the request cap."""
    text = text.strip()
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    sentences: list[str] = []
    current = ""
    for piece in text.replace("\n", " ").split(" "):
        current = f"{current} {piece}".strip()
        if piece.endswith((".", "!", "?")):
            sentences.append(current)
            current = ""
    if current:
        sentences.append(current)

    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate.encode("utf-8")) > max_bytes:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
        if len(current.encode("utf-8")) > max_bytes:
            raise TTSError(
                f"Single sentence exceeds {max_bytes} bytes; split the script: "
                f"{current[:80]}..."
            )
    if current:
        chunks.append(current)
    return chunks


class GeminiTTS(TTSProvider):
    name = "gemini-tts"

    def __init__(self, api_key: str | None = None, voice: str = "Kore"):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            raise TTSError("GEMINI_API_KEY is not set")
        self.voice = voice

    def synthesize(self, text: str) -> tuple[bytes, list[WordTiming] | None]:
        if not text.strip():
            raise TTSError("empty text")
        pcm = b""
        for chunk in chunk_text(text):
            pcm += self._synthesize_chunk(chunk)
        return _pcm_to_wav(pcm), None  # Gemini returns audio only; align separately

    # -- internals -----------------------------------------------------------

    def _synthesize_chunk(self, chunk: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
            try:
                pcm = self._request(chunk)
                spend.log_spend(
                    self.name,
                    cost_usd=len(chunk) / 1000 * GEMINI_COST_PER_1K_CHARS,
                    units=f"{len(chunk)} chars",
                    description=f"TTS chunk: {chunk[:60]}...",
                )
                return pcm
            except TTSError as e:
                if "PROHIBITED_CONTENT" in str(e) or "blocked" in str(e).lower():
                    raise  # classifier rejection — retrying won't help
                last_error = e
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                    raise TTSError(f"Gemini TTS request failed: {e}") from e
                last_error = e
            if attempt < GEMINI_MAX_ATTEMPTS:
                time.sleep(2**attempt)  # backoff: 2s, 4s
        raise TTSError(
            f"Gemini TTS failed after {GEMINI_MAX_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def _request(self, chunk: str) -> bytes:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_TTS_MODEL}:generateContent?key={self.api_key}"
        )
        body = {
            "contents": [{"parts": [{"text": SPEECH_PREAMBLE + chunk}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}
                },
            },
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
        return self._extract_pcm(payload)

    @staticmethod
    def _extract_pcm(payload: dict) -> bytes:
        feedback = payload.get("promptFeedback", {})
        if feedback.get("blockReason"):
            raise TTSError(f"Gemini TTS rejected the prompt: {feedback['blockReason']}")
        try:
            parts = payload["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError) as e:
            raise TTSError(f"Gemini TTS returned no candidates: {payload}") from e
        for part in parts:
            data = part.get("inlineData", {}).get("data")
            if data:
                return base64.b64decode(data)
        # Documented flake: model sometimes returns text tokens instead of audio.
        raise TTSError("Gemini TTS returned text instead of audio (known flake, retryable)")


class SapiTTS(TTSProvider):
    """Free offline Windows voice (System.Speech via PowerShell). $0.00."""

    name = "sapi-tts"

    def synthesize(self, text: str) -> tuple[bytes, list[WordTiming] | None]:
        if not text.strip():
            raise TTSError("empty text")
        with tempfile.TemporaryDirectory() as tmp:
            text_path = Path(tmp) / "text.txt"
            wav_path = Path(tmp) / "out.wav"
            text_path.write_text(text, encoding="utf-8")
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile('{wav_path}'); "
                f"$s.Speak([IO.File]::ReadAllText('{text_path}', "
                "[Text.Encoding]::UTF8)); $s.Dispose()"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0 or not wav_path.exists():
                raise TTSError(f"SAPI synthesis failed: {result.stderr.strip()}")
            return wav_path.read_bytes(), None


def get_tts_provider(name: str | None = None) -> TTSProvider:
    """Pick a provider: explicit name, TTS_PROVIDER env var, or auto.

    Auto = Gemini when GEMINI_API_KEY is set, otherwise the free offline voice.
    """
    name = (name or os.environ.get("TTS_PROVIDER") or "").lower()
    if name == "gemini":
        return GeminiTTS()
    if name == "sapi":
        return SapiTTS()
    if name:
        raise TTSError(f"unknown TTS provider: {name!r} (expected 'gemini' or 'sapi')")
    return GeminiTTS() if os.environ.get("GEMINI_API_KEY") else SapiTTS()


def wav_duration_seconds(wav_bytes: bytes) -> float:
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        return w.getnframes() / w.getframerate()
