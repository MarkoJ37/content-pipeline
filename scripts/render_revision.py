"""Render a saved edit using stored media and ffmpeg. Never calls an AI API."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.run_and_publish import StatusPublisher, update_gallery
from src.generate.assemble import _render_stock_segment, build_ass
from src.lib import storage
from src.lib.media import run_ffmpeg
from src.lib.projects import identifier, validate_recipe
from src.review.qc import technical_issues


def fetch_asset(domain, key):
    if not key.startswith("projects/") or ".." in key or "\\" in key:
        raise ValueError("Invalid project asset")
    data = storage.download_public(domain, key)
    if data is None:
        raise ValueError("Saved project asset is missing")
    return data


def render(project, recipe, domain, workdir):
    recipe = validate_recipe(project, recipe)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "audio.wav").write_bytes(fetch_asset(domain, project["audio_key"]))
    revised = []
    for i, source in enumerate(recipe["clips"]):
        scene = project["segments"][i]
        selected = project["segments"][source]
        src = workdir / f"source_{source}.mp4"
        if not src.exists():
            src.write_bytes(fetch_asset(domain, selected["key"]))
        _render_stock_segment(src, scene["end"] - scene["start"], workdir / f"seg_{i}.mp4")
        revised.append({**scene, "key": selected["key"],
                        "poster_key": selected.get("poster_key", "")})
    (workdir / "concat.txt").write_text(
        "".join(f"file 'seg_{i}.mp4'\n" for i in range(len(revised))), encoding="utf-8")
    timings = [{**t, "word": w} for t, w in zip(project["timings"], recipe["words"], strict=True)]
    (workdir / "captions.ass").write_text(build_ass(timings, brand=recipe["brand"]),
                                           encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", "concat.txt", "-i", "audio.wav",
                "-vf", "ass=captions.ass", "-map", "0:v", "-map", "1:a",
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "+faststart", "revision.mp4"], cwd=workdir)
    return workdir / "revision.mp4", {**project, "segments": revised, "timings": timings,
                                      "brand": recipe["brand"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_id = identifier(args.run_id)
    domain = os.environ["R2_PUBLIC_DOMAIN"]
    workdir = Path("build") / run_id
    os.environ["SPEND_LOG_PATH"] = str(workdir / "spend.jsonl")
    status = StatusPublisher(run_id)
    try:
        status.stage_started("assemble")
        request = json.loads(storage.download_public(domain, f"revisions/{run_id}.json"))
        project_id = identifier(request["project_id"])
        project = json.loads(fetch_asset(domain, f"projects/{project_id}/project.json"))
        out, revised = render(project, request["recipe"], domain, workdir)
        status.stage_done("assemble")
        status.stage_started("review")
        issues = technical_issues(out, project["segments"][-1]["end"])
        if issues:
            status._publish("needs_review", issues=issues)
            return 2
        status.stage_done("review")
        revised["id"] = run_id
        storage.upload(f"projects/{run_id}/project.json", json.dumps(revised).encode(),
                       content_type="application/json")
        key = f"videos/{run_id}.mp4"
        storage.upload(key, out, content_type="video/mp4")
        update_gallery(domain, key, "Edited Reel - preview before sharing", 0, project_id=run_id)
        status.done(key, project_id=run_id)
        return 0
    except Exception as error:
        status.failed("revision", str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
