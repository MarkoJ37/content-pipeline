"""Forced alignment with faster-whisper — local, free.

Gemini TTS returns audio only (no timestamps — verified, see CLAUDE.md). We
already know the exact transcript, so this is alignment, not transcription:
run faster-whisper locally with word_timestamps=True and the known script as
initial_prompt, and get [{"word", "start", "end"}, ...] back. That drives both
caption burn-in and shot cut points.

Never replace this with a paid transcription API, and never estimate timing
from character counts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from .tts import WordTiming

DEFAULT_MODEL = "base"  # tiny is too inaccurate, medium+ too slow on CPU


@lru_cache(maxsize=1)
def _load_model(model_size: str):
    from faster_whisper import WhisperModel  # heavy import; keep it lazy

    return WhisperModel(model_size, device="cpu", compute_type="int8")


def _transcribe(model, audio_path: Path, script: str, use_prompt: bool) -> list[WordTiming]:
    segments, _info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        initial_prompt=script if use_prompt else None,
        beam_size=5,
    )
    timings: list[WordTiming] = []
    for segment in segments:
        for word in segment.words or []:
            token = word.word.strip()
            if token:
                timings.append(
                    {"word": token, "start": round(word.start, 3), "end": round(word.end, 3)}
                )
    return timings


def _count_gap(timings: list[WordTiming], script: str) -> int:
    return abs(len(timings) - len(script.split()))


def align(
    audio_path: str | Path,
    script: str,
    model_size: str = DEFAULT_MODEL,
) -> list[WordTiming]:
    """Word-level timings for known-script audio. ~10–20s of CPU for a 30s clip.

    First attempt passes the script as initial_prompt (usually improves
    accuracy). On some audio that conditioning backfires — whisper treats the
    prompt as already-decoded text and emits only a fraction of the words — so
    if the result fails the sanity check, retry without the prompt and keep
    whichever attempt lands closer to the script's word count.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    if not script.strip():
        raise ValueError("script must not be empty")

    model = _load_model(model_size)
    timings = _transcribe(model, audio_path, script, use_prompt=True)
    if not check_alignment(timings, script):
        retry = _transcribe(model, audio_path, script, use_prompt=False)
        if _count_gap(retry, script) < _count_gap(timings, script):
            timings = retry
    if not timings:
        raise RuntimeError(f"alignment produced no words for {audio_path}")
    return substitute_script_words(timings, script)


def substitute_script_words(timings: list[WordTiming], script: str) -> list[WordTiming]:
    """When counts match, put the script's exact words onto the timings.

    We know the true transcript, so burned-in captions should show it verbatim
    (whisper occasionally mishears a word, e.g. "build log" -> "build blog").
    If the counts differ, keep whisper's words — a positional swap would drift.
    """
    script_words = script.split()
    if len(timings) != len(script_words):
        return timings
    return [
        {"word": word, "start": t["start"], "end": t["end"]}
        for word, t in zip(script_words, timings, strict=True)
    ]


def check_alignment(timings: list[WordTiming], script: str, tolerance: float = 0.25) -> bool:
    """Cheap sanity check before spending anything downstream (fail loudly, cheaply).

    True when the aligned word count is within `tolerance` of the script's word
    count and timings are monotonically non-decreasing.
    """
    script_words = len(script.split())
    if script_words == 0:
        return False
    ratio = abs(len(timings) - script_words) / script_words
    if ratio > tolerance:
        return False
    return all(
        t["end"] >= t["start"] and t["start"] >= timings[i - 1]["start"] - 0.01
        for i, t in enumerate(timings)
        if i > 0
    )
