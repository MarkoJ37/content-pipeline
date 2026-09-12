import copy
import json

import pytest

from scripts import render_revision
from src.generate.assemble import build_ass
from src.lib import projects


@pytest.fixture
def project():
    return {"id": "source", "audio_key": "projects/source/audio.wav", "timings": [
        {"word": "Hello", "start": 0, "end": 0.4},
        {"word": "world", "start": 0.5, "end": 1.0}],
        "segments": [{"key": "projects/source/one.mp4", "start": 0, "end": 0.5},
                     {"key": "projects/source/two.mp4", "start": 0.5, "end": 1.1}]}


@pytest.fixture
def recipe():
    return {"words": ["Hello", "friend"], "clips": [1, 0],
            "brand": {"font": "DejaVu Sans", "color": "#D1EE8A", "size": 64, "position": 800}}


@pytest.mark.parametrize("field,value", [
    ("words", ["missing"]), ("words", ["two words", "test"]),
    ("clips", [2, 0]), ("clips", [True, 0]),
    ("brand", {"font": "Arial\nInjected"}),
])
def test_invalid_recipe_rejected(project, recipe, field, value):
    recipe[field] = value
    with pytest.raises(ValueError):
        projects.validate_recipe(project, recipe)


def test_brand_is_applied_to_exported_captions(project, recipe):
    result = build_ass(project["timings"], brand=recipe["brand"])
    assert "Cap,DejaVu Sans,64,&H008AEED1" in result
    assert ",90,90,800,1" in result


def test_render_reuses_media_preserves_timing_and_does_not_modify_original(
    project, recipe, monkeypatch, tmp_path,
):
    original = copy.deepcopy(project)
    fetched = []
    def fetch(domain, key):
        fetched.append(key)
        return b"fixture"
    monkeypatch.setattr(render_revision, "fetch_asset", fetch)
    rendered = []
    monkeypatch.setattr(render_revision, "_render_stock_segment",
                        lambda source, duration, out: rendered.append((source.name, duration)))
    commands = []
    monkeypatch.setattr(render_revision, "run_ffmpeg", lambda args, **kw: commands.append(args))
    out, revised = render_revision.render(project, recipe, "example.test", tmp_path)
    assert project == original
    assert revised["segments"][0]["key"] == "projects/source/two.mp4"
    assert revised["segments"][0]["end"] == 0.5
    assert revised["timings"][1] == {"word": "friend", "start": 0.5, "end": 1.0}
    assert rendered[0] == ("source_1.mp4", 0.5)
    assert out.name == "revision.mp4"
    assert len(commands) == 1
    assert fetched[0] == project["audio_key"]
    assert "friend" in (tmp_path / "captions.ass").read_text()


@pytest.mark.parametrize("key", ["../secret", "projects/../secret", "https://evil.test/a"])
def test_fetch_asset_rejects_external_paths(key):
    with pytest.raises(ValueError):
        render_revision.fetch_asset("example.test", key)


def test_save_project_publishes_manifest_after_media(monkeypatch, tmp_path):
    from types import SimpleNamespace

    uploads = []
    monkeypatch.setattr(projects, "run_ffmpeg", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        projects.storage, "upload", lambda key, data, **kw: uploads.append((key, data)))
    projects.save_project("example", [], [(0, 1)], [SimpleNamespace(spoken="Hello")],
                          tmp_path / "voice.wav", tmp_path)
    assert uploads[-1][0] == "projects/example/project.json"
    assert json.loads(uploads[-1][1])["segments"][0]["key"] == "projects/example/seg_00.mp4"
