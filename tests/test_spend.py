import json
from datetime import UTC, datetime, timedelta

import pytest

from src.lib import spend


@pytest.fixture(autouse=True)
def spend_log(tmp_path, monkeypatch):
    path = tmp_path / "spend_log.jsonl"
    monkeypatch.setenv("SPEND_LOG_PATH", str(path))
    return path


def test_log_spend_writes_jsonl_record(spend_log):
    record = spend.log_spend("gemini-tts", 0.0098, description="demo VO", units="812 chars")

    assert record["service"] == "gemini-tts"
    assert record["cost_usd"] == 0.0098
    lines = spend_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    assert on_disk["service"] == "gemini-tts"
    assert on_disk["units"] == "812 chars"
    assert "ts" in on_disk


def test_log_spend_appends(spend_log):
    spend.log_spend("gemini-tts", 0.01)
    spend.log_spend("fal-kling", 0.35)

    assert len(spend.read_spend()) == 2


def test_negative_cost_rejected():
    with pytest.raises(ValueError):
        spend.log_spend("gemini-tts", -0.01)


def test_total_spend_sums_and_filters():
    spend.log_spend("gemini-tts", 0.01)
    spend.log_spend("gemini-tts", 0.02)
    spend.log_spend("fal-kling", 0.35)

    assert spend.total_spend() == pytest.approx(0.38)
    assert spend.total_spend("gemini-tts") == pytest.approx(0.03)
    assert spend.total_spend("nothing") == 0


def test_total_spend_empty_log():
    assert spend.read_spend() == []
    assert spend.total_spend() == 0


def _write_record(path, ts, cost, service="gemini-tts"):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "service": service, "cost_usd": cost}) + "\n")


def test_total_spend_today_excludes_older_days(spend_log):
    today = datetime.now(UTC).date().isoformat()
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    _write_record(spend_log, f"{today}T10:00:00+00:00", 0.05)
    _write_record(spend_log, f"{today}T11:00:00+00:00", 0.02)
    _write_record(spend_log, f"{yesterday}T10:00:00+00:00", 1.00)

    assert spend.total_spend_today() == pytest.approx(0.07)


def test_total_spend_today_filters_by_service(spend_log):
    today = datetime.now(UTC).date().isoformat()
    _write_record(spend_log, f"{today}T10:00:00+00:00", 0.05, service="gemini-tts")
    _write_record(spend_log, f"{today}T10:00:00+00:00", 0.03, service="claude-shotlist")

    assert spend.total_spend_today("gemini-tts") == pytest.approx(0.05)


def test_total_spend_today_empty_log():
    assert spend.total_spend_today() == 0
