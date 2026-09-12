"""CI entry point for frontend-triggered runs.

    python -m scripts.run_and_publish --script /tmp/script.md --run-id abc123

Mirrors src/generate/run.py's pipeline stage-for-stage, but publishes
runs/<run-id>/status.json to R2 after each stage so the static app's poll
loop (frontend/app.js) has live progress and a ticking cost counter to read.
On success, also uploads the finished video, prepends it to gallery.json, and
refreshes spend.json (today's total — what the Worker's daily cap checks).

Needs everything src/generate/run.py needs, plus CLOUDFLARE_API_TOKEN /
CLOUDFLARE_ACCOUNT_ID / R2_PUBLIC_DOMAIN.

Kept as a separate script rather than refactoring run.py with a status
callback: the two orchestrators differ only in how they report progress
(print vs. publish to R2), and duplicating ~40 lines of glue is cheaper than
adding a callback parameter to already-tested, manually-verified code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from src.generate.assemble import assemble_reel, shot_boundaries
from src.generate.footage import resolve_stock_shots
from src.generate.shots import generate_shot_list, validate_shot_list
from src.lib import align, spend, storage, tts
from src.lib.media import find_ffmpeg
from src.review.qc import review_reel

STAGE_ORDER = ["shotlist", "voice", "align", "footage", "assemble", "review"]


class StatusPublisher:
    """Accumulates stage state and pushes runs/<run_id>/status.json to R2."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.stages: dict[str, dict] = {}

    def stage_started(self, name: str) -> None:
        self.stages[name] = {"done": False}
        self._publish("running")

    def stage_done(self, name: str, cost_usd: float = 0.0) -> None:
        self.stages[name] = {"done": True, "cost_usd": round(cost_usd, 4)}
        self._publish("running")

    def done(self, video_key: str) -> None:
        self._publish("done", video_key=video_key)

    def failed(self, failed_stage: str, error: str) -> None:
        self._publish("failed", failed_stage=failed_stage, error=error[:500])

    def _publish(self, state: str, **extra) -> None:
        update_daily_spend(self.run_id)
        body = {"state": state, "stages": self.stages, "cost_usd": spend.total_spend(), **extra}
        storage.upload(
            f"runs/{self.run_id}/status.json",
            json.dumps(body).encode(),
            content_type="application/json",
        )


def update_gallery(domain: str, video_key: str, title: str, cost_usd: float) -> None:
    """One object per video avoids read-modify-write races between runners."""
    item = {
        "title": title, "video_key": video_key, "cost_usd": round(cost_usd, 4),
        "created_at": datetime.now(UTC).isoformat(),
    }
    storage.upload(
        f"gallery/{Path(video_key).stem}.json", json.dumps(item).encode(),
        content_type="application/json",
    )


def update_daily_spend(run_id: str) -> None:
    """Persist this execution's daily totals; the Worker sums independent records."""
    execution = os.environ.get("GITHUB_RUN_ID", "local")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    record_id = f"{run_id}-{execution}-{attempt}"
    totals: dict[str, float] = {}
    for record in spend.read_spend():
        day = record["ts"][:10]
        totals[day] = totals.get(day, 0.0) + record["cost_usd"]
    for day, total in totals.items():
        body = {"date": day, "run_id": run_id, "total_usd": round(total, 6)}
        storage.upload(
            f"spend/{day}/{record_id}.json", json.dumps(body).encode(),
            content_type="application/json",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, help="path to the script file")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)

    domain = os.environ.get("R2_PUBLIC_DOMAIN")
    if not domain:
        print("ERROR: R2_PUBLIC_DOMAIN is not set.")
        return 1

    script_path = Path(args.script)
    script = " ".join(script_path.read_text(encoding="utf-8").split())
    name = args.run_id
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        parser.error("run-id must contain 1-64 letters, digits, underscores or hyphens")
    out_path = Path(f"output/{name}.mp4")
    workdir = Path(f"build/{name}")
    workdir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("SPEND_LOG_PATH", str(workdir / "spend.jsonl"))
    status = StatusPublisher(args.run_id)
    current_stage = "shotlist"
    try:
        status.stage_started(current_stage)
        word_count = len(script.split())
        if not 40 <= word_count <= 120:
            raise ValueError(f"script is {word_count} words; needs 40-120")
        find_ffmpeg()
        provider = tts.get_tts_provider(None)

        # 1. shot list
        cost0 = spend.total_spend()
        shots = generate_shot_list(script)
        validate_shot_list(script, shots)
        stock_count = sum(1 for s in shots if s.kind == "STOCK")
        status.stage_done("shotlist", spend.total_spend() - cost0)
        print(f"shot list: {len(shots)} shots ({stock_count} STOCK)")

        # 2. voice
        current_stage = "voice"
        status.stage_started(current_stage)
        cost0 = spend.total_spend()
        audio_path = workdir / "voiceover.wav"
        hash_path = workdir / "voiceover.sha"
        script_hash = hashlib.sha256(f"{provider.name}:{script}".encode()).hexdigest()
        if audio_path.exists() and hash_path.exists() and hash_path.read_text() == script_hash:
            wav_bytes, timings = audio_path.read_bytes(), None
        else:
            wav_bytes, timings = provider.synthesize(script)
            audio_path.write_bytes(wav_bytes)
            hash_path.write_text(script_hash)
        duration = tts.wav_duration_seconds(wav_bytes)
        status.stage_done("voice", spend.total_spend() - cost0)
        print(f"voiceover: {duration:.1f}s")

        # 3. align (free, local)
        current_stage = "align"
        status.stage_started(current_stage)
        if timings is None:
            timings = align.align(audio_path, script)
        if not align.check_alignment(timings, script):
            raise RuntimeError("alignment sanity check failed")
        status.stage_done("align")
        print(f"alignment: {len(timings)} words")

        # 4. footage
        current_stage = "footage"
        status.stage_started(current_stage)
        cost0 = spend.total_spend()
        stock_clips = resolve_stock_shots(shots, workdir)
        status.stage_done("footage", spend.total_spend() - cost0)
        print(f"footage: {len(stock_clips)}/{stock_count} stock clips")

        # 5. assemble (free, ffmpeg)
        current_stage = "assemble"
        status.stage_started(current_stage)
        out = assemble_reel(
            script, shots, timings, audio_path, duration, out_path, workdir,
            stock_clips=stock_clips,
        )
        boundaries = shot_boundaries(script, shots, timings, duration)
        status.stage_done("assemble")
        print(f"assembled: {out}")

        # 6. review
        current_stage = "review"
        status.stage_started(current_stage)
        cost0 = spend.total_spend()
        review = review_reel(out, script, boundaries[-1][1], workdir)
        status.stage_done("review", spend.total_spend() - cost0)
        if not review.passed:
            print("review: FAILED —", "; ".join(review.issues))

            status._publish("needs_review", issues=review.issues)
            update_daily_spend(name)
            return 2

        current_stage = "upload"
        # 7. publish independent video, gallery and spending records
        video_key = f"videos/{name}.mp4"
        storage.upload(video_key, out, content_type="video/mp4")
        title = script[:60] + ("..." if len(script) > 60 else "")
        update_gallery(domain, video_key, title, spend.total_spend())
        update_daily_spend(name)
        status.done(video_key)
        print(f"published: https://{domain}/{video_key}")
        print(f"total spend (all runs): ${spend.total_spend():.4f}")
        return 0 if review.passed else 2

    except Exception as e:  # top-level CI entry point: always publish a failure status
        print(f"ERROR at stage {current_stage}: {e}")
        try:
            status.failed(current_stage, str(e))
            update_daily_spend(name)
        except Exception as publish_error:  # best-effort — don't mask the original failure
            print(f"  (also failed to publish failure status: {publish_error})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
