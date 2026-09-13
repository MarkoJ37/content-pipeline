"""Editable Reel bundles and validated recipes. No paid services."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import brand as branding
from . import storage
from .media import run_ffmpeg

FONTS = {"Arial", "DejaVu Sans", "DejaVu Serif"}
DEFAULT_BRAND = branding.DEFAULTS


def validate_uploads(uploads):
    if not isinstance(uploads, list) or len(uploads) > 8:
        raise ValueError("Choose up to eight uploaded assets")
    for asset in uploads:
        if (
            not isinstance(asset, dict)
            or not re.fullmatch(
                r"projects/uploads/[a-f0-9-]{36}\.(mp4|png|jpg)", str(asset.get("key", ""))
            )
            or asset.get("kind") not in ("image", "video")
        ):
            raise ValueError("Invalid uploaded asset")
        if (asset["kind"] == "video") != asset["key"].endswith(".mp4"):
            raise ValueError("Asset type does not match file")
    return uploads


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Invalid project ID")
    return value


def validate_recipe(project: dict, recipe: dict) -> dict:
    words = recipe.get("words")
    clips = recipe.get("clips")
    brand = recipe.get("brand", DEFAULT_BRAND)
    if not isinstance(words, list) or len(words) != len(project["timings"]):
        raise ValueError("Keep the existing number of timed caption words")
    if any(
        not isinstance(w, str) or not w.strip() or len(w) > 40 or any(c.isspace() for c in w)
        for w in words
    ):
        raise ValueError("Each caption entry must be one word, up to 40 characters")
    if not isinstance(clips, list) or len(clips) != len(project["segments"]):
        raise ValueError("Choose one clip for each scene")
    uploads = validate_uploads(recipe.get("uploads", []))
    source_count = len(project.get("sources", project["segments"])) + len(uploads)
    if any(type(i) is not int or not 0 <= i < source_count for i in clips):
        raise ValueError("Clip selection is out of range")
    if not isinstance(brand, dict) or brand.get("font") not in FONTS:
        raise ValueError("Unsupported caption font")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(brand.get("color", ""))):
        raise ValueError("Invalid caption color")
    if brand.get("size") not in (64, 76, 84) or brand.get("position") not in (360, 560, 800):
        raise ValueError("Unsupported caption size or position")
    brand = branding.validate(brand)
    logo = recipe.get("logo_key", "")
    if logo and not any(
        a.get("key") == logo and a.get("kind") == "image"
        for a in project.get("sources", []) + uploads
    ):
        raise ValueError("Choose an uploaded image for the logo")
    return {"words": words, "clips": clips, "brand": brand, "uploads": uploads, "logo_key": logo}


def save_project(run_id, timings, boundaries, shots, audio_path, workdir):
    identifier(run_id)
    prefix = f"projects/{run_id}"
    audio_key = f"{prefix}/voiceover.wav"
    storage.upload(audio_key, audio_path, content_type="audio/wav")
    segments = []
    for i, ((start, end), shot) in enumerate(zip(boundaries, shots, strict=True)):
        key = f"{prefix}/seg_{i:02d}.mp4"
        segment = Path(workdir) / f"seg_{i:02d}.mp4"
        storage.upload(key, segment, content_type="video/mp4")
        poster = Path(workdir) / f"poster_{i:02d}.jpg"
        run_ffmpeg(
            [
                "-ss",
                str(min(0.2, (end - start) / 2)),
                "-i",
                str(segment),
                "-frames:v",
                "1",
                "-vf",
                "scale=270:480",
                str(poster),
            ]
        )
        poster_key = f"{prefix}/poster_{i:02d}.jpg"
        storage.upload(poster_key, poster, content_type="image/jpeg")
        segments.append(
            {
                "key": key,
                "poster_key": poster_key,
                "start": start,
                "end": end,
                "label": shot.spoken,
                "kind": getattr(shot, "kind", "STOCK"),
                "card_text": getattr(shot, "card_text", ""),
            }
        )
    project = {
        "id": run_id,
        "audio_key": audio_key,
        "segments": segments,
        "timings": timings,
        "brand": DEFAULT_BRAND,
    }
    storage.upload(
        f"{prefix}/project.json", json.dumps(project).encode(), content_type="application/json"
    )
    return project
