"""Step 6: REVIEW — QC pass on an assembled Reel before it goes anywhere.

Two layers, cheap first (fail loudly, cheaply):
1. Technical checks, free: ffprobe stream sanity (resolution, fps, audio
   present, duration vs voiceover) and ffmpeg blackdetect for dead frames.
2. Claude vision, paid (~$0.005): frames sampled across the Reel, checked for
   artifacts, caption overflow, identifiable faces, watermarks — the defects
   only eyes catch. Uses claude-haiku-4-5 for the same budget reason as the
   footage picker (CLAUDE.md caps ALL Claude spend at ~$0.05/Reel); frames are
   downscaled to 540px wide to keep image tokens small.

A failed review does not delete anything — the runner flags it and a human
decides. Auto-retry belongs at the shot level (e.g. re-generating an AI clip),
not here, since re-assembling identical inputs is deterministic.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..lib import spend
from ..lib.media import find_ffprobe, run_ffmpeg, run_ffmpeg_analysis

QC_MODEL = "claude-haiku-4-5"
HAIKU_INPUT_PER_MTOK = 1.00
HAIKU_OUTPUT_PER_MTOK = 5.00
FRAME_COUNT = 5  # enough coverage to catch defects; fewer frames = cheaper QC pass
FRAME_WIDTH = 540  # half-res is plenty for defect spotting, ~4x fewer tokens
DURATION_TOLERANCE = 1.5  # seconds of drift allowed vs the voiceover
BLACK_MIN_SECONDS = 0.4

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "issues"],
    "additionalProperties": False,
}

QC_PROMPT = """\
You are the quality gate for an auto-generated faceless Instagram Reel. The \
{n} frames above were sampled evenly across the video. The voiceover script is:

"{script}"

Fail the Reel ONLY for defects a viewer would notice:
- garbled, corrupted, or heavily artifacted visuals
- burned-in captions cut off at the edges or overlapping unreadably
- solid black / empty frames
- stock-site watermarks or timestamps burned into footage
- a clearly identifiable person's face as the subject of a shot \
(faceless b-roll is required; incidental small/background faces are fine)
- footage wildly unrelated to the script (a cat video under a SaaS pitch)

Do NOT fail for subjective taste: color grading, pacing you'd personally \
change, or stock footage being generic. List each real issue with the frame \
number where you saw it."""


@dataclass
class ReviewResult:
    passed: bool
    issues: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({"passed": self.passed, "issues": self.issues}, indent=2)


# -- layer 1: free technical checks --------------------------------------------


def probe_streams(mp4: str | Path) -> dict:
    import subprocess

    result = subprocess.run(
        [
            find_ffprobe(), "-v", "error", "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate,duration",
            "-of", "json", str(mp4),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def technical_issues(
    mp4: str | Path,
    expected_duration: float,
    expected_size: tuple[int, int] = (1080, 1920),
) -> list[str]:
    issues = []
    streams = probe_streams(mp4).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        return ["no video stream"]
    if (video.get("width"), video.get("height")) != expected_size:
        issues.append(
            f"resolution is {video.get('width')}x{video.get('height')}, "
            f"expected {expected_size[0]}x{expected_size[1]}"
        )
    if audio is None:
        issues.append("no audio stream (voiceover missing)")
    duration = float(video.get("duration") or 0)
    if abs(duration - expected_duration) > DURATION_TOLERANCE:
        issues.append(
            f"video is {duration:.1f}s but the voiceover implies "
            f"~{expected_duration:.1f}s (audio desync or dropped shots)"
        )

    stderr = run_ffmpeg_analysis(
        ["-i", str(mp4), "-vf", f"blackdetect=d={BLACK_MIN_SECONDS}:pix_th=0.10", "-an"]
    )
    issues.extend(parse_blackdetect(stderr))
    return issues


def parse_blackdetect(stderr: str) -> list[str]:
    return [
        f"black frames from {m.group(1)}s to {m.group(2)}s"
        for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", stderr)
    ]


# -- layer 2: Claude vision -----------------------------------------------------


def frame_timestamps(duration: float, count: int = FRAME_COUNT) -> list[float]:
    """Evenly spread, avoiding the very first/last instants."""
    step = duration / (count + 1)
    return [round(step * (i + 1), 2) for i in range(count)]


def extract_frames(mp4: str | Path, duration: float, workdir: str | Path) -> list[Path]:
    frames = []
    frame_dir = Path(workdir) / "qc_frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    for i, ts in enumerate(frame_timestamps(duration)):
        out = frame_dir / f"frame_{i}.jpg"
        run_ffmpeg(
            ["-ss", f"{ts}", "-i", str(mp4), "-frames:v", "1",
             "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "4", str(out)]
        )
        frames.append(out)
    return frames


def vision_review(frames: list[Path], script: str) -> ReviewResult:
    import anthropic

    content = []
    for i, frame in enumerate(frames):
        content.append({"type": "text", "text": f"Frame {i + 1}:"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(frame.read_bytes()).decode(),
                },
            }
        )
    content.append({"type": "text", "text": QC_PROMPT.format(n=len(frames), script=script)})

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=QC_MODEL,
        max_tokens=1000,
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    cost = (
        response.usage.input_tokens * HAIKU_INPUT_PER_MTOK
        + response.usage.output_tokens * HAIKU_OUTPUT_PER_MTOK
    ) / 1_000_000
    spend.log_spend(
        "claude-qc",
        cost_usd=cost,
        units=f"{response.usage.input_tokens} in / {response.usage.output_tokens} out",
        description=f"QC review: {script[:50]}...",
    )
    verdict = json.loads(next(b.text for b in response.content if b.type == "text"))
    return ReviewResult(passed=verdict["passed"], issues=verdict["issues"])


def review_reel(
    mp4: str | Path,
    script: str,
    expected_duration: float,
    workdir: str | Path,
) -> ReviewResult:
    """Free technical checks first; Claude vision only if they pass."""
    issues = technical_issues(mp4, expected_duration)
    if issues:
        return ReviewResult(passed=False, issues=issues)  # don't pay to confirm a broken file
    frames = extract_frames(mp4, expected_duration, workdir)
    return vision_review(frames, script)
