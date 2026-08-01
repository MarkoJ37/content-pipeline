"""Locate and run ffmpeg (free local compute — never a paid API for editing)."""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str:
    """FFMPEG_PATH env var, then PATH, then the winget install location."""
    explicit = os.environ.get("FFMPEG_PATH")
    if explicit and Path(explicit).exists():
        return explicit
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    winget_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if winget_root.exists():
        for candidate in winget_root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            return str(candidate)
    raise FileNotFoundError(
        "ffmpeg not found. Install it (winget install Gyan.FFmpeg) or set FFMPEG_PATH."
    )


def run_ffmpeg(args: list[str], cwd: str | Path | None = None) -> None:
    """Run ffmpeg with -y and hard-fail on any error (fail loudly)."""
    cmd = [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}):\n"
            f"  cmd: {' '.join(cmd)}\n  stderr: {result.stderr.strip()[-2000:]}"
        )


def run_ffmpeg_analysis(args: list[str]) -> str:
    """Run an ffmpeg analysis pass (null output) and return its stderr.

    Filters like blackdetect and volumedetect report on stderr at info level,
    so this uses -loglevel info and does not suppress output.
    """
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "info", *args, "-f", "null", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg analysis failed: {result.stderr.strip()[-2000:]}")
    return result.stderr


@functools.lru_cache(maxsize=1)
def find_ffprobe() -> str:
    """ffprobe lives next to ffmpeg in every install layout we support."""
    sibling = Path(find_ffmpeg()).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.exists():
        return str(sibling)
    on_path = shutil.which("ffprobe")
    if on_path:
        return on_path
    raise FileNotFoundError("ffprobe not found next to ffmpeg or on PATH")
