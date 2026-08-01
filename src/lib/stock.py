"""Pexels stock video client (free tier — no spend logging needed).

Stock-first is a guiding principle: Pexels covers anything generic, AI video
is the expensive last resort. This client searches portrait videos, picks the
best-fitting file per result, and caches downloads by video ID so re-runs are
free and fast.

License note: no attribution required, commercial use OK — but clips with
identifiable faces must not imply endorsement. Search queries and the vision
picker both steer toward faceless / hands-only / object b-roll.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"
TARGET_W, TARGET_H = 1080, 1920

# Pexels' CDN (Cloudflare) 403s python-urllib's default User-Agent
_HEADERS = {"User-Agent": "content-pipeline/0.1 (github actions; stock b-roll fetcher)"}


class StockError(Exception):
    """Search or download failed, or no key configured."""


@dataclass
class StockVideo:
    id: int
    duration: float  # seconds
    width: int
    height: int
    thumbnail_url: str  # preview image, used by the vision picker
    file_url: str  # the chosen video file to download
    file_width: int = 0
    file_height: int = 0
    keywords: list[str] = field(default_factory=list)


def _api_key() -> str:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        raise StockError("PEXELS_API_KEY is not set (free key at pexels.com/api)")
    return key


def _best_file(video_files: list[dict]) -> dict | None:
    """Smallest file that still covers 1080x1920; else the largest portrait file."""
    usable = [f for f in video_files if f.get("width") and f.get("height")]
    covering = [f for f in usable if f["width"] >= TARGET_W and f["height"] >= TARGET_H]
    if covering:
        return min(covering, key=lambda f: f["width"] * f["height"])
    portrait = [f for f in usable if f["height"] > f["width"]]
    if portrait:
        return max(portrait, key=lambda f: f["width"] * f["height"])
    return None


def search_videos(
    query: str,
    per_page: int = 4,  # fewer thumbnails = cheaper vision picks; 4 still gives real choice
    min_duration: float = 3.0,
) -> list[StockVideo]:
    """Top portrait matches for a query, one best-fitting file per video."""
    params = urllib.parse.urlencode(
        {"query": query, "per_page": per_page, "orientation": "portrait", "size": "medium"}
    )
    req = urllib.request.Request(
        f"{PEXELS_SEARCH_URL}?{params}", headers={**_HEADERS, "Authorization": _api_key()}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        raise StockError(f"Pexels search failed for {query!r}: {e}") from e

    results = []
    for video in payload.get("videos", []):
        if video.get("duration", 0) < min_duration:
            continue
        best = _best_file(video.get("video_files", []))
        if not best:
            continue
        results.append(
            StockVideo(
                id=video["id"],
                duration=video["duration"],
                width=video["width"],
                height=video["height"],
                thumbnail_url=video.get("image", ""),
                file_url=best["link"],
                file_width=best["width"],
                file_height=best["height"],
                keywords=query.split(),
            )
        )
    return results


def download_video(video: StockVideo, cache_dir: str | Path) -> Path:
    """Download a clip, cached by video ID — repeat runs cost nothing."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"pexels_{video.id}.mp4"
    if path.exists() and path.stat().st_size > 0:
        return path
    req = urllib.request.Request(video.file_url, headers=dict(_HEADERS))
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, path.open("wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        path.unlink(missing_ok=True)
        raise StockError(f"download failed for video {video.id}: {e}") from e
    return path


def fetch_thumbnail(video: StockVideo, cache_dir: str | Path, max_width: int = 400) -> Path:
    """Small preview image for the vision picker (keeps Claude input tokens tiny)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"thumb_{video.id}.jpg"
    if path.exists() and path.stat().st_size > 0:
        return path
    url = video.thumbnail_url
    if "?" in url:  # Pexels image CDN accepts resize params
        url = f"{url.split('?')[0]}?auto=compress&cs=tinysrgb&w={max_width}"
    try:
        req = urllib.request.Request(url, headers=dict(_HEADERS))
        with urllib.request.urlopen(req, timeout=60) as resp:
            path.write_bytes(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        path.unlink(missing_ok=True)
        raise StockError(f"thumbnail fetch failed for video {video.id}: {e}") from e
    return path
