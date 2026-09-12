"""Step 5: ASSEMBLE — cut shots to word-timed slots and build the Reel (ffmpeg, free).

Timing math and caption generation are pure functions so they're unit-testable
without touching ffmpeg.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..lib.media import run_ffmpeg
from ..lib.tts import WordTiming
from .cards import render_card
from .shots import Shot

FPS = 30
WIDTH, HEIGHT = 1080, 1920
TAIL_SECONDS = 0.4  # video breathes slightly past the last spoken word
MIN_SHOT_SECONDS = 0.35
CAPTION_WORDS_PER_CHUNK = 3


def shot_boundaries(
    script: str,
    shots: list[Shot],
    timings: list[WordTiming],
    audio_duration: float,
) -> list[tuple[float, float]]:
    """(start, end) per shot, derived from aligned word timings.

    Shot i starts when its first word starts; ends when shot i+1 starts (last
    shot ends at audio end + tail). If whisper returned a slightly different
    word count than the script, indices are scaled proportionally.
    """
    script_words = script.split()
    counts = [len(s.spoken.split()) for s in shots]
    if sum(counts) != len(script_words):
        raise ValueError("shots' word counts do not partition the script")

    scale = len(timings) / len(script_words)
    starts: list[float] = []
    cumulative = 0
    for i, count in enumerate(counts):
        if i == 0:
            starts.append(0.0)
        else:
            idx = min(round(cumulative * scale), len(timings) - 1)
            start = timings[idx]["start"]
            starts.append(max(start, starts[-1] + MIN_SHOT_SECONDS))
        cumulative += count

    end = max(audio_duration + TAIL_SECONDS, starts[-1] + MIN_SHOT_SECONDS)
    boundaries = [(starts[i], starts[i + 1]) for i in range(len(starts) - 1)]
    boundaries.append((starts[-1], end))
    return boundaries


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial,84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,5,2,2,90,90,560,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(
    timings: list[WordTiming], words_per_chunk: int = CAPTION_WORDS_PER_CHUNK,
    brand: dict | None = None,
) -> str:
    """Burned-in caption track: chunks of a few words, timed to the voiceover."""
    if words_per_chunk < 1:
        raise ValueError("words_per_chunk must be positive")
    chunks = []
    chunk = []
    for word in timings:
        if chunk and (len(chunk) >= words_per_chunk
                      or len(" ".join(w["word"] for w in [*chunk, word])) > 24
                      or word["start"] - chunk[-1]["end"] > 0.45):
            chunks.append(chunk)
            chunk = []
        chunk.append(word)
        if re.search(r'[.!?;:]["\')]*$', word["word"]):
            chunks.append(chunk)
            chunk = []
    if chunk:
        chunks.append(chunk)
    events = []
    for i, chunk in enumerate(chunks):
        start = chunk[0]["start"]
        # hold until the next chunk starts so captions never flicker off mid-speech
        end = chunks[i + 1][0]["start"] if i + 1 < len(chunks) else chunk[-1]["end"] + 0.3
        end = min(end, chunk[-1]["end"] + 0.3)
        text = " ".join(w["word"] for w in chunk).replace("{", "(").replace("}", ")").replace("\\", "/")
        events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Cap,,0,0,0,,{text}")
    header = ASS_HEADER
    if brand is not None:
        from ..lib.projects import validate_recipe
        validate_recipe({"timings": timings, "segments": []},
                        {"words": [t["word"] for t in timings], "clips": [], "brand": brand})
        color = brand["color"].lstrip("#")
        ass_color = f"&H00{color[4:6]}{color[2:4]}{color[0:2]}"
        header = header.replace("Cap,Arial,84,&H00FFFFFF", f"Cap,{brand['font']},{brand['size']},{ass_color}")
        header = header.replace(",90,90,560,1", f",90,90,{brand['position']},1")
    return header + "\n".join(events) + "\n"


ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
]


def _render_card_segment(card_png: Path, duration: float, index: int, out_path: Path) -> None:
    frames = max(1, round(duration * FPS))
    # Alternate a slow Ken Burns zoom in/out so cards aren't static
    step = 0.0007
    if index % 2 == 0:
        z = f"min(1+{step}*on,1.10)"
    else:
        z = f"max(1.10-{step}*on,1.0)"
    zoompan = (
        f"zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom)/2':y='ih/2-(ih/zoom)/2'"
        f":s={WIDTH}x{HEIGHT}:fps={FPS}"
    )
    run_ffmpeg(
        [
            "-i", str(card_png),
            "-vf", zoompan,
            "-frames:v", str(frames),
            *ENCODE_ARGS,
            str(out_path),
        ]
    )


def _render_stock_segment(clip: Path, duration: float, out_path: Path) -> None:
    """Trim a stock clip to its word-timed slot: cover-crop to 9:16, mute, loop if short."""
    cover = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},fps={FPS}"
    )
    run_ffmpeg(
        [
            "-stream_loop", "-1",
            "-i", str(clip),
            "-t", f"{duration:.3f}",
            "-vf", cover,
            "-an",
            *ENCODE_ARGS,
            str(out_path),
        ]
    )


def assemble_reel(
    script: str,
    shots: list[Shot],
    timings: list[WordTiming],
    audio_path: str | Path,
    audio_duration: float,
    out_path: str | Path,
    workdir: str | Path,
    stock_clips: dict[int, Path] | None = None,
) -> Path:
    """Footage -> word-timed segments -> concat -> captions + voiceover -> mp4.

    stock_clips maps shot index -> downloaded clip path for STOCK shots
    (from footage.resolve_stock_shots).
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stock_clips = stock_clips or {}

    boundaries = shot_boundaries(script, shots, timings, audio_duration)

    segment_paths = []
    for i, (shot, (start, end)) in enumerate(zip(shots, boundaries, strict=True)):
        segment = workdir / f"seg_{i:02d}.mp4"
        if shot.kind == "TEXT_CARD":
            card = render_card(shot.card_text, i, workdir / f"card_{i:02d}.png")
            _render_card_segment(card, end - start, i, segment)
        elif shot.kind == "STOCK":
            if i not in stock_clips:
                raise ValueError(f"shot {i} is STOCK but no clip was resolved for it")
            _render_stock_segment(stock_clips[i], end - start, segment)
        else:
            raise NotImplementedError(f"shot kind {shot.kind} not wired up yet (shot {i})")
        segment_paths.append(segment)

    concat_list = workdir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{p.name}'\n" for p in segment_paths), encoding="utf-8"
    )
    silent = workdir / "silent.mp4"
    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", silent.name],
        cwd=workdir,
    )

    (workdir / "captions.ass").write_text(build_ass(timings), encoding="utf-8")
    run_ffmpeg(
        [
            "-i", "silent.mp4",
            "-i", str(Path(audio_path).resolve()),
            "-vf", "ass=captions.ass",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
        ],
        cwd=workdir,
    )
    return out_path
