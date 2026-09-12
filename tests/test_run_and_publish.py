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


def test_gallery_writes_independent_records_without_reading_shared_index(fake_upload, monkeypatch):
    def no_read(*args):
        raise AssertionError("Gallery writes must not read the shared index")
    monkeypatch.setattr(storage, "download_public", no_read)
    rap.update_gallery("example.test", "videos/one.mp4", "one", 0.03)
    rap.update_gallery("example.test", "videos/two.mp4", "two", 0.04)
    assert json.loads(fake_upload["gallery/one.json"])["title"] == "one"
    assert json.loads(fake_upload["gallery/two.json"])["title"] == "two"
    assert "gallery.json" not in fake_upload


def test_spend_records_do_not_overwrite_other_runs(fake_upload):
    spend.log_spend("tts", 0.02)
    rap.update_daily_spend("first")
    rap.update_daily_spend("second")
    records = [json.loads(v) for k, v in fake_upload.items() if k.startswith("spend/")]
    assert len(records) == 2
    assert {r["run_id"] for r in records} == {"first", "second"}
    assert all(r["total_usd"] == 0.02 for r in records)


def test_spend_republication_is_idempotent_and_workflow_retries_are_separate(
    fake_upload, monkeypatch,
):
    spend.log_spend("tts", 0.02)
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    rap.update_daily_spend("run")
    rap.update_daily_spend("run")
    assert len(fake_upload) == 1
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    rap.update_daily_spend("run")
    assert len(fake_upload) == 2


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
    monkeypatch.setattr(rap, "save_project", lambda *args: None)
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
