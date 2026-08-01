"""Cost-logging wrapper. ALL paid API calls go through here.

Every paid call must be recorded with log_spend() so the run's total cost is
always visible. The log is JSONL, one record per paid call, at the path given
by the SPEND_LOG_PATH env var (default: spend_log.jsonl in the repo root).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.Lock()

DEFAULT_LOG_NAME = "spend_log.jsonl"


def _log_path() -> Path:
    return Path(os.environ.get("SPEND_LOG_PATH", DEFAULT_LOG_NAME))


def log_spend(
    service: str,
    cost_usd: float,
    description: str = "",
    units: str = "",
) -> dict:
    """Record one paid API call. Returns the record that was written.

    service:     e.g. "gemini-tts", "fal-kling", "claude"
    cost_usd:    estimated cost of this single call in USD
    description: what the call was for, e.g. "voiceover for reel demo-001"
    units:       human-readable usage, e.g. "812 chars", "10s video"
    """
    if cost_usd < 0:
        raise ValueError(f"cost_usd must be >= 0, got {cost_usd}")
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "service": service,
        "cost_usd": round(cost_usd, 6),
        "units": units,
        "description": description,
    }
    path = _log_path()
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return record


def read_spend() -> list[dict]:
    """All spend records logged so far (empty list if no log yet)."""
    path = _log_path()
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def total_spend(service: str | None = None) -> float:
    """Total USD spent, optionally filtered to one service."""
    return round(
        sum(r["cost_usd"] for r in read_spend() if service is None or r["service"] == service),
        6,
    )


def total_spend_today(service: str | None = None) -> float:
    """Total USD spent today (UTC) — what the Worker's daily spend ceiling checks."""
    today = datetime.now(UTC).date().isoformat()
    return round(
        sum(
            r["cost_usd"]
            for r in read_spend()
            if r["ts"][:10] == today and (service is None or r["service"] == service)
        ),
        6,
    )
