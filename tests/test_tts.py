import base64
import io
import wave

import pytest

from src.lib import spend, tts


@pytest.fixture(autouse=True)
def spend_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEND_LOG_PATH", str(tmp_path / "spend_log.jsonl"))


@pytest.fixture
def gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return tts.GeminiTTS()


def _audio_payload(pcm: bytes = b"\x00\x01" * 100) -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]}}
        ]
    }


# -- chunking ----------------------------------------------------------------


def test_short_text_is_single_chunk():
    assert tts.chunk_text("Hello world.") == ["Hello world."]


def test_long_text_splits_at_sentence_boundaries():
    text = " ".join(f"Sentence number {i} is right here." for i in range(300))
    chunks = tts.chunk_text(text)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= tts.GEMINI_MAX_CHUNK_BYTES
        assert chunk.endswith(".")
    assert " ".join(chunks) == text


def test_oversized_single_sentence_fails_loudly():
    with pytest.raises(tts.TTSError, match="exceeds"):
        tts.chunk_text("word " * 2000 + "end.")


# -- Gemini provider ---------------------------------------------------------


def test_gemini_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(tts.TTSError, match="GEMINI_API_KEY"):
        tts.GeminiTTS()


def test_gemini_returns_wav_and_no_timings(gemini, monkeypatch):
    pcm = b"\x01\x02" * 2400
    monkeypatch.setattr(gemini, "_request", lambda chunk: pcm)

    wav_bytes, timings = gemini.synthesize("Hello there, world.")

    assert timings is None  # Gemini gives audio only; alignment is a separate step
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        assert w.getframerate() == tts.GEMINI_PCM_RATE
        assert w.getnchannels() == 1
        assert w.readframes(w.getnframes()) == pcm


def test_gemini_logs_spend(gemini, monkeypatch):
    monkeypatch.setattr(gemini, "_request", lambda chunk: b"\x00\x00")
    text = "x" * 1000 + "."

    gemini.synthesize(text)

    assert spend.total_spend("gemini-tts") == pytest.approx(
        len(text) / 1000 * tts.GEMINI_COST_PER_1K_CHARS
    )


def test_gemini_retries_on_flaky_response_then_succeeds(gemini, monkeypatch):
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    attempts = []

    def flaky(chunk):
        attempts.append(1)
        if len(attempts) < 3:
            raise tts.TTSError("Gemini TTS returned text instead of audio")
        return b"\x00\x00"

    monkeypatch.setattr(gemini, "_request", flaky)
    wav_bytes, _ = gemini.synthesize("Hello.")

    assert len(attempts) == 3
    assert wav_bytes


def test_gemini_gives_up_after_max_attempts(gemini, monkeypatch):
    monkeypatch.setattr(tts.time, "sleep", lambda s: None)
    attempts = []

    def always_flaky(chunk):
        attempts.append(1)
        raise tts.TTSError("Gemini TTS returned text instead of audio")

    monkeypatch.setattr(gemini, "_request", always_flaky)
    with pytest.raises(tts.TTSError, match="after 3 attempts"):
        gemini.synthesize("Hello.")

    assert len(attempts) == tts.GEMINI_MAX_ATTEMPTS
    assert spend.total_spend() == 0  # failed calls must not be billed as spend


def test_gemini_does_not_retry_prohibited_content(gemini, monkeypatch):
    attempts = []

    def rejected(chunk):
        attempts.append(1)
        raise tts.TTSError("Gemini TTS rejected the prompt: PROHIBITED_CONTENT")

    monkeypatch.setattr(gemini, "_request", rejected)
    with pytest.raises(tts.TTSError, match="PROHIBITED_CONTENT"):
        gemini.synthesize("Hello.")

    assert len(attempts) == 1  # classifier rejections are not retried


def test_extract_pcm_happy_path():
    assert tts.GeminiTTS._extract_pcm(_audio_payload(b"\xaa\xbb")) == b"\xaa\xbb"


def test_extract_pcm_text_instead_of_audio_is_retryable_error():
    payload = {"candidates": [{"content": {"parts": [{"text": "sorry, here is text"}]}}]}
    with pytest.raises(tts.TTSError, match="text instead of audio"):
        tts.GeminiTTS._extract_pcm(payload)


def test_extract_pcm_block_reason():
    with pytest.raises(tts.TTSError, match="PROHIBITED_CONTENT"):
        tts.GeminiTTS._extract_pcm({"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}})


def test_preamble_wraps_transcript(gemini, monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent["body"] = req.data.decode("utf-8")

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                import json

                return json.dumps(_audio_payload()).encode()

        return Resp()

    monkeypatch.setattr(tts.urllib.request, "urlopen", fake_urlopen)
    gemini.synthesize("Buy the thing today.")

    assert "Synthesize natural" in sent["body"]
    assert "Buy the thing today." in sent["body"]
    assert '"responseModalities": ["AUDIO"]' in sent["body"]


# -- provider selection ------------------------------------------------------


def test_factory_prefers_gemini_when_key_present(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    assert isinstance(tts.get_tts_provider(), tts.GeminiTTS)


def test_factory_falls_back_to_free_provider(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    assert isinstance(tts.get_tts_provider(), tts.SapiTTS)


def test_factory_env_override(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TTS_PROVIDER", "sapi")
    assert isinstance(tts.get_tts_provider(), tts.SapiTTS)


def test_factory_rejects_unknown_provider():
    with pytest.raises(tts.TTSError, match="unknown TTS provider"):
        tts.get_tts_provider("espeak")


# -- offline provider (integration: real PowerShell/SAPI) ---------------------


@pytest.mark.integration
def test_sapi_produces_playable_wav(tmp_path):
    provider = tts.SapiTTS()
    wav_bytes, timings = provider.synthesize("Testing one two three.")

    assert timings is None
    assert tts.wav_duration_seconds(wav_bytes) > 0.5
