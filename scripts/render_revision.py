"""Render a saved edit using stored media and ffmpeg. Never calls an AI API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from scripts.run_and_publish import StatusPublisher, update_gallery
from src.generate.assemble import _render_card_segment, _render_stock_segment, scene_captions
from src.generate.cards import render_card
from src.lib import storage
from src.lib.media import run_ffmpeg
from src.lib.projects import identifier, validate_recipe
from src.review.qc import technical_issues, timeline_issues


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
    sources = project.get("sources", project["segments"]) + recipe["uploads"]
    for i, source in enumerate(recipe["clips"]):
        scene = project["segments"][i]
        selected = sources[source]
        duration = scene["end"] - scene["start"]
        output = workdir / f"seg_{i}.mp4"
        if selected.get("kind") == "TEXT_CARD" and selected.get("card_text"):
            card = render_card(
                selected["card_text"], i, workdir / f"card_{i}.png", brand=recipe["brand"]
            )
            _render_card_segment(card, duration, i, output)
        else:
            src = workdir / f"source_{source}{Path(selected['key']).suffix}"
            if not src.exists():
                src.write_bytes(fetch_asset(domain, selected["key"]))
            if selected.get("kind") == "image":
                from PIL import Image, ImageOps

                with Image.open(src) as image:
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    canvas = Image.new("RGB", (1080, 1920), recipe["brand"]["background"])
                    image = ImageOps.contain(image, (900, 1100))
                    canvas.paste(image, ((1080 - image.width) // 2, 220))
                    card = workdir / f"image_{i}.png"
                    canvas.save(card)
                _render_card_segment(card, duration, i, output)
            else:
                _render_stock_segment(src, duration, output)
        revised.append(
            {
                **selected,
                "start": scene["start"],
                "end": scene["end"],
                "label": scene.get("label", "Scene"),
            }
        )
    (workdir / "concat.txt").write_text(
        "".join(f"file 'seg_{i}.mp4'\n" for i in range(len(revised))), encoding="utf-8"
    )
    timings = [{**t, "word": w} for t, w in zip(project["timings"], recipe["words"], strict=True)]
    (workdir / "captions.ass").write_text(
        scene_captions(timings, revised, recipe["brand"]), encoding="utf-8"
    )
    logo_args = []
    video_args = ["-vf", "ass=captions.ass", "-map", "0:v"]
    if recipe["logo_key"]:
        import io

        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(fetch_asset(domain, recipe["logo_key"]))) as logo:
            logo = ImageOps.contain(ImageOps.exif_transpose(logo).convert("RGBA"), (180, 120))
            logo.save(workdir / "logo.png")
        logo_args = ["-i", "logo.png"]
        video_args = [
            "-filter_complex",
            "[0:v][2:v]overlay=90:160,ass=captions.ass[v]",
            "-map",
            "[v]",
        ]
    run_ffmpeg(
        [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            "concat.txt",
            "-i",
            "audio.wav",
            *logo_args,
            *video_args,
            "-map",
            "1:a",
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "revision.mp4",
        ],
        cwd=workdir,
    )
    return workdir / "revision.mp4", {
        **project,
        "segments": revised,
        "timings": timings,
        "brand": recipe["brand"],
        "sources": sources,
        "logo_key": recipe["logo_key"],
    }


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
        issues.extend(
            timeline_issues(
                revised["timings"],
                [(s["start"], s["end"]) for s in revised["segments"]],
                project["segments"][-1]["end"],
            )
        )
        if issues:
            status._publish("needs_review", issues=issues)
            return 2
        status.stage_done("review")
        revised["id"] = run_id
        storage.upload(
            f"projects/{run_id}/project.json",
            json.dumps(revised).encode(),
            content_type="application/json",
        )
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
