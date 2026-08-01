"""Step 4: FOOTAGE — resolve each STOCK shot to a downloaded Pexels clip.

Per shot: Pexels search -> Claude vision picks the best of the top few by
thumbnail -> download the winner (cached by video ID).

The picker uses claude-haiku-4-5 deliberately (not Opus): this is a simple
best-of-5 image choice, and CLAUDE.md budgets ~$0.05/Reel for ALL Claude
usage — 8 shots x 5 thumbnails on an Opus-tier model would exceed that on its
own. Haiku keeps the whole pass around a cent. The call is paid, so it goes
through spend.py; on any picker failure we fall back to the first search
result rather than failing the Reel.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from ..lib import spend
from ..lib.stock import StockVideo, download_video, fetch_thumbnail, search_videos
from .shots import Shot

VISION_MODEL = "claude-haiku-4-5"
HAIKU_INPUT_PER_MTOK = 1.00
HAIKU_OUTPUT_PER_MTOK = 5.00

PICK_PROMPT = """\
You are picking b-roll for a faceless Instagram Reel. The voiceover for this \
shot says: "{spoken}"
The search keywords were: {keywords}

Below are {n} candidate clips (thumbnails), numbered 1 to {n}. Pick the one that:
- best matches the voiceover line visually
- has NO identifiable faces (hands-only, over-the-shoulder, or object shots are ideal)
- looks sharp and well-lit, not busy or cluttered

Reply with ONLY the number of the best candidate. If several qualify, prefer \
the more visually striking one."""


def _client():
    import anthropic  # lazy so tests and no-key dry runs never touch it

    return anthropic.Anthropic()


def pick_best_clip(shot: Shot, candidates: list[StockVideo], workdir: str | Path) -> StockVideo:
    """Claude vision picks best-of-N by thumbnail; falls back to first result."""
    if not candidates:
        raise ValueError("no candidates to pick from")
    if len(candidates) == 1 or not os.environ.get("ANTHROPIC_API_KEY"):
        return candidates[0]

    try:
        content = []
        for i, video in enumerate(candidates):
            thumb = fetch_thumbnail(video, Path(workdir) / "thumbs")
            content.append({"type": "text", "text": f"Candidate {i + 1}:"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(thumb.read_bytes()).decode(),
                    },
                }
            )
        content.append(
            {
                "type": "text",
                "text": PICK_PROMPT.format(
                    spoken=shot.spoken, keywords=", ".join(shot.keywords), n=len(candidates)
                ),
            }
        )
        response = _client().messages.create(
            model=VISION_MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": content}],
        )
        cost = (
            response.usage.input_tokens * HAIKU_INPUT_PER_MTOK
            + response.usage.output_tokens * HAIKU_OUTPUT_PER_MTOK
        ) / 1_000_000
        spend.log_spend(
            "claude-vision-pick",
            cost_usd=cost,
            units=f"{response.usage.input_tokens} in / {response.usage.output_tokens} out",
            description=f"pick clip for: {shot.spoken[:50]}",
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return candidates[_parse_choice(text, len(candidates))]
    except Exception as e:  # picker is best-effort; the Reel must not die here
        print(f"  vision pick failed ({e}); using first result")
        return candidates[0]


def _parse_choice(text: str, n: int) -> int:
    """'3' / 'Candidate 3' / '3.' -> index 2. Anything unparseable -> 0."""
    match = re.search(r"\d+", text)
    if match:
        choice = int(match.group())
        if 1 <= choice <= n:
            return choice - 1
    return 0


def resolve_stock_shots(shots: list[Shot], workdir: str | Path) -> dict[int, Path]:
    """For each STOCK shot: search -> vision pick -> download. Returns {shot index: clip path}."""
    clips: dict[int, Path] = {}
    used_ids: set[int] = set()  # never show the same clip twice in one Reel
    cache_dir = Path(workdir) / "stock_cache"
    for i, shot in enumerate(shots):
        if shot.kind != "STOCK":
            continue
        query = " ".join(shot.keywords)
        candidates = search_videos(query)
        fresh = [c for c in candidates if c.id not in used_ids]
        if fresh:
            candidates = fresh  # only fall back to a repeat if nothing else exists
        if not candidates:
            raise RuntimeError(
                f"shot {i}: Pexels returned nothing for {query!r} — "
                "adjust keywords before spending on assembly"
            )
        best = pick_best_clip(shot, candidates, workdir)
        used_ids.add(best.id)
        clips[i] = download_video(best, cache_dir)
        print(f"  shot {i:2d}: {query!r} -> pexels #{best.id} ({best.duration:.0f}s)")
    return clips
