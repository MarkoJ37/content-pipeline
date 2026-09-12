"""Shot list model, validation, and the Claude call that generates one.

`generate_shot_list(script)` asks Claude to segment a ~30s script into 10-15
tagged shots (STOCK / TEXT_CARD / SCREEN_REC / GENERATE), enforced by a JSON
schema (structured outputs) and validated with `validate_shot_list`. Invalid
output is retried once with the validation error as feedback — hard cap of 2
paid attempts, both spend-logged.

Model: claude-sonnet-5, not Opus — CLAUDE.md budgets ~$0.05/Reel for ALL
Claude reasoning (shot list + captions + QC), and an Opus-tier call would
consume most of that on this one step.

DEMO_SCRIPT / DEMO_SHOTS remain as the free, no-API-call baseline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..lib import spend

MIN_SHOTS = 10
MAX_SHOTS = 15

SHOTLIST_MODEL = "claude-sonnet-5"
SONNET_INPUT_PER_MTOK = 3.00
SONNET_OUTPUT_PER_MTOK = 15.00
MAX_ATTEMPTS = 2  # never retry a paid call in a loop without a hard cap


@dataclass
class Shot:
    kind: str  # STOCK | TEXT_CARD | SCREEN_REC | GENERATE
    spoken: str  # the exact words of the script spoken over this shot
    card_text: str = ""  # TEXT_CARD: on-screen copy
    keywords: list[str] = field(default_factory=list)  # STOCK: Pexels search terms
    prompt: str = ""  # GENERATE: video-model prompt


def validate_shot_list(script: str, shots: list[Shot]) -> None:
    """Fail loudly, cheaply — before any TTS or rendering money is spent."""
    if not MIN_SHOTS <= len(shots) <= MAX_SHOTS:
        raise ValueError(
            f"shot list has {len(shots)} shots; a 30s Reel needs {MIN_SHOTS}-{MAX_SHOTS}"
        )
    spoken = " ".join(" ".join(s.spoken.split()) for s in shots)
    flat_script = " ".join(script.split())
    if spoken != flat_script:
        raise ValueError(
            "shots' spoken text does not reconstruct the script exactly.\n"
            f"  script: {flat_script}\n  shots:  {spoken}"
        )
    generated = [s for s in shots if s.kind == "GENERATE"]
    if len(generated) > 2:
        raise ValueError(f"{len(generated)} GENERATE shots; cap is 2 (AI video is the cost sink)")
    for i, shot in enumerate(shots):
        if shot.kind not in {"STOCK", "TEXT_CARD"}:
            raise ValueError(f"shot {i} has unsupported kind {shot.kind}")
        if shot.kind == "TEXT_CARD" and not shot.card_text:
            raise ValueError(f"shot {i} is TEXT_CARD but has no card_text")
        if shot.kind == "STOCK" and not shot.keywords:
            raise ValueError(f"shot {i} is STOCK but has no keywords")


# --- Claude shot-list generation ----------------------------------------------


class ShotListError(Exception):
    """Claude could not produce a valid shot list within the attempt cap."""


SHOT_SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["STOCK", "TEXT_CARD"],
                    },
                    "spoken": {"type": "string"},
                    "card_text": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "prompt": {"type": "string"},
                },
                "required": ["kind", "spoken", "card_text", "keywords", "prompt"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["shots"],
    "additionalProperties": False,
}

SHOTLIST_SYSTEM = f"""\
You segment short-form video scripts into shot lists for a faceless Instagram \
Reel pipeline. Scripts are ~60-90 words (~30 seconds spoken).

Rules — all of them are validated mechanically, so follow them exactly:
1. Produce {MIN_SHOTS}-{MAX_SHOTS} shots. Each shot covers 2-3 seconds of \
speech (roughly 4-9 words).
2. The `spoken` fields must partition the script WORD FOR WORD: concatenating \
them in order must reproduce the script exactly — same words, same \
punctuation, same capitalization, nothing added or dropped.
3. Shot kinds:
   - STOCK (the default, use for most shots): generic b-roll. Provide 3-6 \
lowercase `keywords` for a stock-video search. The footage must be faceless — \
prefer hands-only, over-the-shoulder, object, and screen b-roll. Never \
keywords that imply a visible face (e.g. "woman smiling").
   - TEXT_CARD: for punchy emphasis — hooks, numbered points, stats, the CTA. \
Provide `card_text`: max 4 lines of max 18 characters, line breaks as \\n. \
Punchy fragments, not sentences.
   Only STOCK and TEXT_CARD are supported. Never request generated video or screen recordings.
4. Unused fields must be "" (or [] for keywords).
5. Mix: aim for roughly 60-80% STOCK and 20-40% TEXT_CARD. Open with a \
strong hook shot; end with the CTA."""


def _estimate_and_log(response, description: str) -> None:
    cost = (
        response.usage.input_tokens * SONNET_INPUT_PER_MTOK
        + response.usage.output_tokens * SONNET_OUTPUT_PER_MTOK
    ) / 1_000_000
    spend.log_spend(
        "claude-shotlist",
        cost_usd=cost,
        units=f"{response.usage.input_tokens} in / {response.usage.output_tokens} out",
        description=description,
    )


def _parse_shots(payload: dict) -> list[Shot]:
    return [
        Shot(
            kind=s["kind"],
            spoken=s["spoken"],
            card_text=s.get("card_text", ""),
            keywords=[k for k in s.get("keywords", []) if k],
            prompt=s.get("prompt", ""),
        )
        for s in payload["shots"]
    ]


def generate_shot_list(script: str) -> list[Shot]:
    """Claude segments a script into a validated shot list. Max 2 paid attempts."""
    import anthropic  # lazy: only the generate path needs it

    if not script.strip():
        raise ValueError("script must not be empty")
    client = anthropic.Anthropic()
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user_msg = f"Segment this script into a shot list:\n\n{script}"
        if feedback:
            user_msg += (
                f"\n\nYour previous attempt was rejected by the validator:\n{feedback}\n"
                "Fix exactly that problem and try again."
            )
        response = client.messages.create(
            model=SHOTLIST_MODEL,
            max_tokens=4000,
            system=SHOTLIST_SYSTEM,
            # effort low: segmentation is mechanical and validator-checked, and
            # default-effort adaptive thinking alone cost ~$0.06/call — over the
            # entire ~$0.05/Reel Claude budget
            output_config={
                "format": {"type": "json_schema", "schema": SHOT_SCHEMA},
                "effort": "low",
            },
            messages=[{"role": "user", "content": user_msg}],
        )
        _estimate_and_log(response, f"shot list attempt {attempt}: {script[:50]}...")
        text = next(b.text for b in response.content if b.type == "text")
        shots = _parse_shots(json.loads(text))
        try:
            validate_shot_list(script, shots)
            return shots
        except ValueError as e:
            feedback = str(e)
    raise ShotListError(
        f"no valid shot list after {MAX_ATTEMPTS} attempts; last error:\n{feedback}"
    )


# --- the hardcoded demo -------------------------------------------------------

DEMO_SCRIPT = (
    "Stop scrolling. Your next customer just searched for a product like yours, "
    "and found someone else. Here's the fix. First: post consistently. Accounts "
    "that post daily grow three times faster. Second: hook them in the first "
    "second, or they're gone. Third: let automation do the boring parts, so you "
    "only approve the final cut. This whole video was planned, voiced, and edited "
    "by a pipeline, for six cents. Follow for the build log."
)

# ~70% STOCK / 30% TEXT_CARD — the mix that validates the project's central
# assumption (keyword-matched b-roll reads as a real ad). Keywords steer to
# faceless / hands-only footage per the Pexels license constraint.
DEMO_SHOTS = [
    Shot("STOCK", "Stop scrolling.", keywords=["hand", "scrolling", "phone", "close", "up"]),
    Shot(
        "STOCK",
        "Your next customer just searched for a product like yours,",
        keywords=["typing", "laptop", "keyboard", "hands"],
    ),
    Shot(
        "STOCK",
        "and found someone else.",
        keywords=["online", "shopping", "phone", "hands"],
    ),
    Shot("TEXT_CARD", "Here's the fix.", card_text="Here's the fix"),
    Shot("TEXT_CARD", "First: post consistently.", card_text="1.\nPost consistently"),
    Shot(
        "STOCK",
        "Accounts that post daily grow three times faster.",
        keywords=["growth", "chart", "screen", "data"],
    ),
    Shot(
        "STOCK",
        "Second: hook them in the first second, or they're gone.",
        keywords=["watching", "video", "phone", "hands", "close", "up"],
    ),
    Shot(
        "STOCK",
        "Third: let automation do the boring parts,",
        keywords=["code", "computer", "screen", "dark"],
    ),
    Shot(
        "STOCK",
        "so you only approve the final cut.",
        keywords=["coffee", "relaxing", "hands", "phone"],
    ),
    Shot(
        "TEXT_CARD",
        "This whole video was planned, voiced, and edited by a pipeline, for six cents.",
        card_text="This video\ncost $0.06",
    ),
    Shot(
        "STOCK",
        "Follow for the build log.",
        keywords=["neon", "sign", "night", "city"],
    ),
]
