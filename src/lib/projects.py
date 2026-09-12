"""Editable Reel bundles and validated recipes. No paid services."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import storage
from .media import run_ffmpeg

FONTS = {"Arial", "DejaVu Sans", "DejaVu Serif"}
DEFAULT_BRAND = {"font": "Arial", "color": "#FFFFFF", "size": 76, "position": 560}


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
    if any(not isinstance(w, str) or not w.strip() or len(w) > 40
           or any(c.isspace() for c in w) for w in words):
        raise ValueError("Each caption entry must be one word, up to 40 characters")
    if not isinstance(clips, list) or len(clips) != len(project["segments"]):
        raise ValueError("Choose one clip for each scene")
    if any(type(i) is not int or not 0 <= i < len(clips) for i in clips):
        raise ValueError("Clip selection is out of range")
    if not isinstance(brand, dict) or brand.get("font") not in FONTS:
        raise ValueError("Unsupported caption font")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(brand.get("color", ""))):
        raise ValueError("Invalid caption color")
    if brand.get("size") not in (64, 76, 84) or brand.get("position") not in (360, 560, 800):
        raise ValueError("Unsupported caption size or position")
    return {"words": words, "clips": clips, "brand": brand}


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
        run_ffmpeg(["-ss", str(min(0.2, (end - start) / 2)), "-i", str(segment),
                    "-frames:v", "1", "-vf", "scale=270:480", str(poster)])
        poster_key = f"{prefix}/poster_{i:02d}.jpg"
        storage.upload(poster_key, poster, content_type="image/jpeg")
        segments.append({"key": key, "poster_key": poster_key,
                         "start": start, "end": end, "label": shot.spoken})
    project = {"id": run_id, "audio_key": audio_key, "segments": segments,
               "timings": timings, "brand": DEFAULT_BRAND}
    storage.upload(f"{prefix}/project.json", json.dumps(project).encode(),
                   content_type="application/json")
    return project
