import io
import json

import pytest

from src.generate import footage
from src.generate.shots import DEMO_SHOTS, Shot
from src.lib import stock


@pytest.fixture(autouse=True)
def pexels_key(monkeypatch, tmp_path):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    monkeypatch.setenv("SPEND_LOG_PATH", str(tmp_path / "spend.jsonl"))


def _file(width, height, link="https://cdn/video.mp4"):
    return {"width": width, "height": height, "link": link}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# -- best-file selection -------------------------------------------------------


def test_best_file_prefers_smallest_covering_file():
    files = [_file(2160, 3840), _file(1080, 1920), _file(720, 1280)]
    assert stock._best_file(files) == _file(1080, 1920)


def test_best_file_falls_back_to_largest_portrait():
    files = [_file(720, 1280), _file(540, 960)]
    assert stock._best_file(files) == _file(720, 1280)


def test_best_file_rejects_landscape_only():
    assert stock._best_file([_file(1920, 1080)]) is None


# -- search --------------------------------------------------------------------


def _search_payload():
    return {
        "videos": [
            {
                "id": 101,
                "duration": 12,
                "width": 1080,
                "height": 1920,
                "image": "https://images.pexels.com/101.jpg?fit=crop",
                "video_files": [_file(1080, 1920, "https://cdn/101.mp4")],
            },
            {  # too short — filtered out
                "id": 102,
                "duration": 1,
                "width": 1080,
                "height": 1920,
                "image": "https://images.pexels.com/102.jpg",
                "video_files": [_file(1080, 1920)],
            },
        ]
    }


def test_search_parses_and_filters(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        return FakeResponse(json.dumps(_search_payload()).encode())

    monkeypatch.setattr(stock.urllib.request, "urlopen", fake_urlopen)
    results = stock.search_videos("typing hands")

    assert captured["auth"] == "test-key"
    assert "orientation=portrait" in captured["url"]
    assert len(results) == 1  # the 1s clip was dropped
    assert results[0].id == 101
    assert results[0].file_url == "https://cdn/101.mp4"


def test_search_requires_key(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY")
    with pytest.raises(stock.StockError, match="PEXELS_API_KEY"):
        stock.search_videos("anything")


# -- download caching ----------------------------------------------------------


def test_download_is_cached_by_video_id(monkeypatch, tmp_path):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(1)
        return FakeResponse(b"video-bytes")

    monkeypatch.setattr(stock.urllib.request, "urlopen", fake_urlopen)
    video = stock.StockVideo(
        id=7, duration=10, width=1080, height=1920,
        thumbnail_url="", file_url="https://cdn/7.mp4",
    )

    first = stock.download_video(video, tmp_path)
    second = stock.download_video(video, tmp_path)

    assert first == second
    assert first.read_bytes() == b"video-bytes"
    assert len(calls) == 1  # second call served from cache


# -- vision picker -------------------------------------------------------------


def test_parse_choice():
    assert footage._parse_choice("3", 5) == 2
    assert footage._parse_choice("Candidate 2 is best.", 5) == 1
    assert footage._parse_choice("9", 5) == 0  # out of range -> first
    assert footage._parse_choice("no idea", 5) == 0


def _candidates(n):
    return [
        stock.StockVideo(
            id=i, duration=10, width=1080, height=1920,
            thumbnail_url=f"https://img/{i}.jpg", file_url=f"https://cdn/{i}.mp4",
        )
        for i in range(n)
    ]


def test_pick_falls_back_to_first_without_anthropic_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    shot = Shot("STOCK", "words", keywords=["k"])

    assert footage.pick_best_clip(shot, _candidates(3), tmp_path).id == 0


def test_pick_single_candidate_skips_api(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(
        footage, "_client", lambda: (_ for _ in ()).throw(AssertionError("no API call"))
    )
    shot = Shot("STOCK", "words", keywords=["k"])

    assert footage.pick_best_clip(shot, _candidates(1), tmp_path).id == 0


def test_pick_survives_api_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(
        footage, "fetch_thumbnail", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net"))
    )
    shot = Shot("STOCK", "words", keywords=["k"])

    assert footage.pick_best_clip(shot, _candidates(3), tmp_path).id == 0


# -- demo shot mix -------------------------------------------------------------


def test_demo_is_roughly_70_percent_stock():
    stock_count = sum(1 for s in DEMO_SHOTS if s.kind == "STOCK")
    assert 0.6 <= stock_count / len(DEMO_SHOTS) <= 0.8
