from types import SimpleNamespace

import pytest

from src.lib import align


class FakeModel:
    """Stands in for faster_whisper.WhisperModel.

    unprompted_segments, if given, is returned when initial_prompt is None
    (the retry path); otherwise segments is returned for every call.
    """

    def __init__(self, segments, unprompted_segments=None):
        self.segments = segments
        self.unprompted_segments = unprompted_segments
        self.calls = []

    def transcribe(self, path, **kwargs):
        self.calls.append((path, kwargs))
        segments = self.segments
        if kwargs.get("initial_prompt") is None and self.unprompted_segments is not None:
            segments = self.unprompted_segments
        return iter(segments), SimpleNamespace(language="en")


def _word(word, start, end):
    return SimpleNamespace(word=word, start=start, end=end)


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "vo.wav"
    path.write_bytes(b"RIFF fake wav")
    return path


@pytest.fixture
def fake_model(monkeypatch):
    model = FakeModel(
        [
            SimpleNamespace(words=[_word(" Stop", 0.0, 0.31), _word(" scrolling.", 0.31, 0.82)]),
            SimpleNamespace(words=[_word(" This", 1.02, 1.2), _word(" works.", 1.2, 1.65)]),
        ]
    )
    monkeypatch.setattr(align, "_load_model", lambda size: model)
    return model


def test_align_flattens_segments_and_strips_words(audio_file, fake_model):
    timings = align.align(audio_file, "Stop scrolling. This works.")

    assert timings == [
        {"word": "Stop", "start": 0.0, "end": 0.31},
        {"word": "scrolling.", "start": 0.31, "end": 0.82},
        {"word": "This", "start": 1.02, "end": 1.2},
        {"word": "works.", "start": 1.2, "end": 1.65},
    ]


def test_align_passes_script_as_initial_prompt(audio_file, fake_model):
    align.align(audio_file, "Stop scrolling. This works.")

    _path, kwargs = fake_model.calls[0]
    assert kwargs["initial_prompt"] == "Stop scrolling. This works."
    assert kwargs["word_timestamps"] is True


def test_align_missing_audio_fails_before_loading_model(fake_model):
    with pytest.raises(FileNotFoundError):
        align.align("does/not/exist.wav", "script")
    assert fake_model.calls == []


def test_align_empty_script_rejected(audio_file, fake_model):
    with pytest.raises(ValueError):
        align.align(audio_file, "   ")


def test_align_no_words_fails_loudly(audio_file, monkeypatch):
    monkeypatch.setattr(align, "_load_model", lambda size: FakeModel([SimpleNamespace(words=[])]))
    with pytest.raises(RuntimeError, match="no words"):
        align.align(audio_file, "some script")


def test_align_retries_without_prompt_when_conditioning_collapses(audio_file, monkeypatch):
    # prompted attempt decodes a fraction of the words (known whisper failure
    # mode when conditioned on the full transcript); unprompted attempt is full
    collapsed = [SimpleNamespace(words=[_word(" Stop", 0.0, 0.3)])]
    full = [
        SimpleNamespace(
            words=[
                _word(" Stop", 0.0, 0.3),
                _word(" scrolling.", 0.3, 0.8),
                _word(" This", 1.0, 1.2),
                _word(" works.", 1.2, 1.6),
            ]
        )
    ]
    model = FakeModel(collapsed, unprompted_segments=full)
    monkeypatch.setattr(align, "_load_model", lambda size: model)

    timings = align.align(audio_file, "Stop scrolling. This works.")

    assert len(model.calls) == 2
    assert model.calls[1][1]["initial_prompt"] is None
    assert len(timings) == 4


def test_align_rejects_both_incomplete_attempts(audio_file, monkeypatch):
    # prompted attempt is off but the unprompted retry is even worse
    close = [SimpleNamespace(words=[_word(" one", 0.0, 0.2), _word(" two", 0.2, 0.4)])]
    worse = [SimpleNamespace(words=[_word(" x", 0.0, 0.1)])]
    model = FakeModel(close, unprompted_segments=worse)
    monkeypatch.setattr(align, "_load_model", lambda size: model)

    with pytest.raises(RuntimeError, match="needs review"):
        align.align(audio_file, "one two three four five six")


# -- substitute_script_words ---------------------------------------------------


def test_substitution_uses_exact_script_words_when_counts_match():
    timings = [
        {"word": "build", "start": 0.0, "end": 0.3},
        {"word": "blog.", "start": 0.3, "end": 0.7},  # whisper misheard "log"
    ]
    fixed = align.substitute_script_words(timings, "build log.")

    assert [t["word"] for t in fixed] == ["build", "log."]
    assert fixed[0]["start"] == 0.0 and fixed[1]["end"] == 0.7


def test_substitution_is_skipped_on_count_mismatch():
    timings = [{"word": "hello", "start": 0.0, "end": 0.3}]
    with pytest.raises(ValueError, match="cannot locate"):
        align.substitute_script_words(timings, "hello there friend")


# -- check_alignment ----------------------------------------------------------


def _timings(*words, step=0.3):
    return [
        {"word": w, "start": round(i * step, 2), "end": round((i + 1) * step, 2)}
        for i, w in enumerate(words)
    ]


def test_check_alignment_accepts_close_match():
    assert align.check_alignment(_timings("stop", "scrolling", "now"), "Stop scrolling now")


def test_check_alignment_rejects_big_word_count_gap():
    assert not align.check_alignment(_timings("one"), "a script with many more words than that")


def test_check_alignment_rejects_non_monotonic_starts():
    bad = [
        {"word": "b", "start": 5.0, "end": 5.2},
        {"word": "a", "start": 0.0, "end": 0.2},
    ]
    assert not align.check_alignment(bad, "b a")


def test_check_alignment_rejects_empty_script():
    assert not align.check_alignment(_timings("word"), "")
