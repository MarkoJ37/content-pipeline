from pathlib import Path

from src.generate import footage
from src.generate.shots import Shot
from src.lib.stock import StockVideo


def _video(vid):
    return StockVideo(
        id=vid, duration=10, width=1080, height=1920,
        thumbnail_url="", file_url=f"https://cdn/{vid}.mp4",
    )


def test_same_clip_is_not_used_for_two_shots(monkeypatch, tmp_path):
    # both searches return the same top result; second shot must take the runner-up
    monkeypatch.setattr(footage, "search_videos", lambda q: [_video(100), _video(200)])
    monkeypatch.setattr(footage, "pick_best_clip", lambda shot, cands, wd: cands[0])
    monkeypatch.setattr(
        footage, "download_video", lambda v, d: Path(d) / f"pexels_{v.id}.mp4"
    )
    shots = [
        Shot("STOCK", "one.", keywords=["a"]),
        Shot("STOCK", "two.", keywords=["a"]),
    ]

    clips = footage.resolve_stock_shots(shots, tmp_path)

    assert clips[0].name == "pexels_100.mp4"
    assert clips[1].name == "pexels_200.mp4"


def test_repeat_allowed_when_no_alternative_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(footage, "search_videos", lambda q: [_video(100)])
    monkeypatch.setattr(footage, "pick_best_clip", lambda shot, cands, wd: cands[0])
    monkeypatch.setattr(
        footage, "download_video", lambda v, d: Path(d) / f"pexels_{v.id}.mp4"
    )
    shots = [
        Shot("STOCK", "one.", keywords=["a"]),
        Shot("STOCK", "two.", keywords=["a"]),
    ]

    clips = footage.resolve_stock_shots(shots, tmp_path)

    assert clips[0].name == clips[1].name == "pexels_100.mp4"
