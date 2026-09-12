import json
from pathlib import Path

import pytest

from scripts import run_and_publish as rap
from src.lib import spend, storage


@pytest.fixture(autouse=True)
def spend_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEND_LOG_PATH", str(tmp_path / "spend.jsonl"))


@pytest.fixture
def fake_upload(monkeypatch):
    uploads = {}

    def fake(key, source, bucket=storage.DEFAULT_BUCKET, content_type=None):
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        uploads[key] = data
        return key

    monkeypatch.setattr(storage, "upload", fake)
    return uploads


# -- StatusPublisher ---------------------------------------------------------------


def test_status_publisher_accumulates_stages_and_publishes(fake_upload):
    status = rap.StatusPublisher("run123")
    status.stage_done("shotlist", cost_usd=0.015)
    status.stage_done("voice", cost_usd=0.005)

    body = json.loads(fake_upload["runs/run123/status.json"])
    assert body["state"] == "running"
    assert body["stages"]["shotlist"] == {"done": True, "cost_usd": 0.015}
    assert body["stages"]["voice"] == {"done": True, "cost_usd": 0.005}
    assert "align" not in body["stages"]  # not reached yet


def test_status_publisher_done_includes_video_key(fake_upload):
    status = rap.StatusPublisher("run123")
    status.stage_done("shotlist")
    status.done("videos/run123.mp4")

    body = json.loads(fake_upload["runs/run123/status.json"])
    assert body["state"] == "done"
    assert body["video_key"] == "videos/run123.mp4"


def test_status_publisher_failed_includes_stage_and_error(fake_upload):
    status = rap.StatusPublisher("run123")
    status.stage_done("shotlist")
    status.failed("voice", "Gemini TTS failed after 3 attempts")

    body = json.loads(fake_upload["runs/run123/status.json"])
    assert body["state"] == "failed"
    assert body["failed_stage"] == "voice"
    assert "Gemini TTS" in body["error"]


def test_status_publisher_reflects_running_total_spend(fake_upload):
    spend.log_spend("gemini-tts", 0.01)
    status = rap.StatusPublisher("run123")
    status.stage_done("voice", cost_usd=0.01)

    body = json.loads(fake_upload["runs/run123/status.json"])
    assert body["cost_usd"] == pytest.approx(0.01)


# -- gallery merge ------------------------------------------------------------------


def test_update_gallery_prepends_new_entry(fake_upload, monkeypatch):
    existing = [{"title": "old reel", "video_key": "videos/old.mp4", "cost_usd": 0.03}]
    monkeypatch.setattr(
        storage, "download_public", lambda domain, key: json.dumps(existing).encode()
    )

    rap.update_gallery("pub-x.r2.dev", "videos/new.mp4", "new reel", 0.04)

    items = json.loads(fake_upload["gallery.json"])
    assert items[0]["video_key"] == "videos/new.mp4"
    assert items[1]["video_key"] == "videos/old.mp4"


def test_update_gallery_handles_first_ever_run(fake_upload, monkeypatch):
    monkeypatch.setattr(storage, "download_public", lambda domain, key: None)

    rap.update_gallery("pub-x.r2.dev", "videos/first.mp4", "first reel", 0.05)

    items = json.loads(fake_upload["gallery.json"])
    assert items == [{"title": "first reel", "video_key": "videos/first.mp4", "cost_usd": 0.05}]


def test_update_gallery_caps_length(fake_upload, monkeypatch):
    existing = [
        {"title": f"reel {i}", "video_key": f"videos/{i}.mp4", "cost_usd": 0.03} for i in range(12)
    ]
    monkeypatch.setattr(
        storage, "download_public", lambda domain, key: json.dumps(existing).encode()
    )

    rap.update_gallery("pub-x.r2.dev", "videos/new.mp4", "new reel", 0.04, keep=12)

    items = json.loads(fake_upload["gallery.json"])
    assert len(items) == 12
    assert items[0]["video_key"] == "videos/new.mp4"


# -- daily spend publish -------------------------------------------------------------


def test_update_daily_spend_publishes_todays_total(fake_upload):
    spend.log_spend("gemini-tts", 0.02)
    spend.log_spend("claude-shotlist", 0.03)

    rap.update_daily_spend()

    body = json.loads(fake_upload["spend.json"])
    assert body["total_usd"] == pytest.approx(0.05)
    assert body["date"]  # ISO date string present


def test_stage_started_reports_active_stage(fake_upload):
    status = rap.StatusPublisher("run123")
    status.stage_started("voice")
    body = json.loads(fake_upload["runs/run123/status.json"])
    assert body["state"] == "running"
    assert body["stages"]["voice"] == {"done": False}


def test_failed_review_never_publishes_video_or_gallery(tmp_path, monkeypatch, fake_upload):
    from types import SimpleNamespace

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("R2_PUBLIC_DOMAIN", "example.test")
    script = tmp_path / "script.md"
    script.write_text("word " * 60)
    monkeypatch.setattr(rap, "find_ffmpeg", lambda: "ffmpeg")
    provider = SimpleNamespace(name="test", synthesize=lambda text: (b"audio", [{}]))
    monkeypatch.setattr(rap.tts, "get_tts_provider", lambda name: provider)
    monkeypatch.setattr(rap.tts, "wav_duration_seconds", lambda audio: 30)
    monkeypatch.setattr(rap, "generate_shot_list", lambda text: [])
    monkeypatch.setattr(rap, "validate_shot_list", lambda *a: None)
    monkeypatch.setattr(rap.align, "check_alignment", lambda *a: True)
    monkeypatch.setattr(rap, "resolve_stock_shots", lambda *a: {})
    monkeypatch.setattr(rap, "assemble_reel", lambda *a, **kw: tmp_path / "video.mp4")
    monkeypatch.setattr(rap, "shot_boundaries", lambda *a: [(0, 30)])
    monkeypatch.setattr(
        rap, "review_reel", lambda *a: SimpleNamespace(passed=False, issues=["black frames"])
    )
    assert rap.main(["--script", str(script), "--run-id", "test-run"]) == 2
    body = json.loads(fake_upload["runs/test-run/status.json"])
    assert body["state"] == "needs_review"
    assert body["issues"] == ["black frames"]
    assert "gallery.json" not in fake_upload
    assert not any(key.startswith("videos/") for key in fake_upload)
