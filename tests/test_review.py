import json
import sys
from types import SimpleNamespace

import pytest

from src.review import qc


@pytest.fixture(autouse=True)
def spend_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEND_LOG_PATH", str(tmp_path / "spend.jsonl"))


# -- technical checks ------------------------------------------------------------


def _probe(width=1080, height=1920, duration=30.0, with_audio=True):
    streams = [
        {"codec_type": "video", "width": width, "height": height,
         "avg_frame_rate": "30/1", "duration": str(duration)},
    ]
    if with_audio:
        streams.append({"codec_type": "audio", "duration": str(duration)})
    return {"streams": streams}


@pytest.fixture
def clean_analysis(monkeypatch):
    monkeypatch.setattr(qc, "run_ffmpeg_analysis", lambda args: "no black frames here")


def test_technical_pass(monkeypatch, clean_analysis):
    monkeypatch.setattr(qc, "probe_streams", lambda p: _probe())
    assert qc.technical_issues("x.mp4", expected_duration=30.0) == []


def test_wrong_resolution_flagged(monkeypatch, clean_analysis):
    monkeypatch.setattr(qc, "probe_streams", lambda p: _probe(width=720, height=1280))
    issues = qc.technical_issues("x.mp4", 30.0)
    assert any("resolution" in i for i in issues)


def test_missing_audio_flagged(monkeypatch, clean_analysis):
    monkeypatch.setattr(qc, "probe_streams", lambda p: _probe(with_audio=False))
    issues = qc.technical_issues("x.mp4", 30.0)
    assert any("audio" in i for i in issues)


def test_duration_drift_flagged(monkeypatch, clean_analysis):
    monkeypatch.setattr(qc, "probe_streams", lambda p: _probe(duration=25.0))
    issues = qc.technical_issues("x.mp4", 30.0)
    assert any("desync" in i for i in issues)


def test_no_video_stream_short_circuits(monkeypatch, clean_analysis):
    monkeypatch.setattr(qc, "probe_streams", lambda p: {"streams": []})
    assert qc.technical_issues("x.mp4", 30.0) == ["no video stream"]


def test_parse_blackdetect():
    stderr = (
        "[blackdetect @ 0x1] black_start:3.2 black_end:4.1 black_duration:0.9\n"
        "[blackdetect @ 0x1] black_start:10 black_end:10.5 black_duration:0.5\n"
    )
    issues = qc.parse_blackdetect(stderr)
    assert issues == [
        "black frames from 3.2s to 4.1s",
        "black frames from 10s to 10.5s",
    ]


def test_parse_blackdetect_clean():
    assert qc.parse_blackdetect("frame=  100 fps= 30") == []


# -- frame sampling ---------------------------------------------------------------


def test_frame_timestamps_are_even_and_interior():
    ts = qc.frame_timestamps(28.0, count=6)
    assert len(ts) == 6
    assert ts[0] == 4.0
    assert ts[-1] == 24.0
    assert all(0 < t < 28.0 for t in ts)


# -- vision review ----------------------------------------------------------------


class FakeClient:
    def __init__(self, verdict):
        self.verdict = verdict
        self.requests = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.verdict))],
            usage=SimpleNamespace(input_tokens=5000, output_tokens=100),
        )


@pytest.fixture
def fake_anthropic(monkeypatch):
    holder = SimpleNamespace(client=None)
    monkeypatch.setitem(
        sys.modules, "anthropic", SimpleNamespace(Anthropic=lambda: holder.client)
    )
    return holder


def _frames(tmp_path, n=2):
    frames = []
    for i in range(n):
        f = tmp_path / f"f{i}.jpg"
        f.write_bytes(b"\xff\xd8jpegdata")
        frames.append(f)
    return frames


def test_vision_review_pass(fake_anthropic, tmp_path):
    fake_anthropic.client = FakeClient({"passed": True, "issues": []})
    result = qc.vision_review(_frames(tmp_path), "script text")

    assert result.passed and result.issues == []
    from src.lib import spend

    records = spend.read_spend()
    assert records[0]["service"] == "claude-qc"
    assert records[0]["cost_usd"] == pytest.approx((5000 * 1 + 100 * 5) / 1_000_000)


def test_vision_review_failure_carries_issues(fake_anthropic, tmp_path):
    fake_anthropic.client = FakeClient(
        {"passed": False, "issues": ["caption cut off in frame 3"]}
    )
    result = qc.vision_review(_frames(tmp_path), "script text")

    assert not result.passed
    assert result.issues == ["caption cut off in frame 3"]


def test_review_reel_skips_paid_vision_when_technical_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(qc, "technical_issues", lambda *a, **k: ["no audio stream"])
    monkeypatch.setattr(
        qc, "extract_frames", lambda *a: (_ for _ in ()).throw(AssertionError("paid path hit"))
    )
    result = qc.review_reel("x.mp4", "script", 30.0, tmp_path)

    assert not result.passed
    assert result.issues == ["no audio stream"]
