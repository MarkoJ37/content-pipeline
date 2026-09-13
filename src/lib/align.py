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

import math
import re
from difflib import SequenceMatcher
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
    only an attempt that covers every script word with valid timing.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    if not script.strip():
        raise ValueError("script must not be empty")

    model = _load_model(model_size)
    errors = []
    for use_prompt in (True, False):
        timings = _transcribe(model, audio_path, script, use_prompt=use_prompt)
        if not timings:
            errors.append("no words")
            continue
        try:
            corrected = substitute_script_words(timings, script)
            if check_alignment(corrected, script):
                return corrected
        except ValueError as error:
            errors.append(str(error))
    raise RuntimeError("alignment needs review: " + "; ".join(errors or ["invalid timestamps"]))


def normalized(word: str) -> str:
    return re.sub(r"[^\w]", "", word.casefold())


def substitute_script_words(timings: list[WordTiming], script: str) -> list[WordTiming]:
    """Match transcript tokens to script words without shifting later timestamps.

    We know the true transcript, so burned-in captions should show it verbatim
    (whisper occasionally mishears a word, e.g. "build log" -> "build blog").
    If the counts differ, keep whisper's words — a positional swap would drift.
    """
    expected = script.split()
    actual = [t["word"] for t in timings]
    result = []
    matcher = SequenceMatcher(
        None, [normalized(w) for w in expected], [normalized(w) for w in actual], autojunk=False
    )
    for operation, a, b, c, d in matcher.get_opcodes():
        if operation == "insert":
            continue  # ignore extra recognizer tokens; never shift later matching words
        if operation == "equal":
            result.extend(
                {**timings[j], "word": expected[i]}
                for i, j in zip(range(a, b), range(c, d), strict=True)
            )
        elif (
            b - a == 1
            and d > c
            and (
                normalized(expected[a]) == "".join(normalized(w) for w in actual[c:d])
                or (
                    d - c == 1
                    and SequenceMatcher(
                        None, normalized(expected[a]), normalized(actual[c])
                    ).ratio()
                    >= 0.5
                )
            )
        ):
            result.append(
                {"word": expected[a], "start": timings[c]["start"], "end": timings[d - 1]["end"]}
            )
        else:
            raise ValueError(f"cannot locate script words: {' '.join(expected[a:b])}")
    return result


def check_alignment(timings: list[WordTiming], script: str, tolerance: float = 0.0) -> bool:
    """Require script coverage and finite, positive, ordered word intervals."""
    expected = script.split()
    if not expected or len(timings) != len(expected):
        return False
    previous_end = 0.0
    for timing, word in zip(timings, expected, strict=True):
        start, end = timing["start"], timing["end"]
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or start < previous_end - 0.03
            or normalized(timing["word"]) != normalized(word)
        ):
            return False
        previous_end = end
    return True
