import pytest

from src.generate.assemble import build_ass
from src.lib.align import check_alignment, substitute_script_words
from src.lib.projects import validate_uploads
from src.review.qc import parse_audio_analysis, timeline_issues


def test_inserted_recognizer_word_does_not_shift_following_caption():
    words = [{"word": w, "start": i, "end": i + .5}
             for i, w in enumerate(["Show", "um", "your", "product."])]
    fixed = substitute_script_words(words, "Show your product.")
    assert fixed[1]["start"] == 2
    assert fixed[2]["start"] == 3


@pytest.mark.parametrize("start,end", [(-1, .2), (0, 0), (float("nan"), 1)])
def test_invalid_first_timestamp_is_rejected(start, end):
    assert not check_alignment([{"word": "Hello", "start": start, "end": end}], "Hello")


def test_wide_caption_requires_correction():
    with pytest.raises(ValueError, match="too wide"):
        build_ass([{"word": "W" * 40, "start": 0, "end": 1}])


def test_silent_audio_and_internal_pauses_are_flagged():
    assert parse_audio_analysis("max_volume: -inf dB", 30)
    assert parse_audio_analysis("silence_end: 8 | silence_duration: 2", 30)
    assert not parse_audio_analysis("max_volume: -1.5 dB", 30)


def test_timeline_rejects_overflow_and_scene_gaps():
    issues = timeline_issues([{"start": 0, "end": 10}], [(0, 2), (3, 4)], 4)
    assert any("past voiceover" in i for i in issues)
    assert any("gap or overlap" in i for i in issues)


def test_uploaded_asset_cannot_reference_arbitrary_public_files():
    with pytest.raises(ValueError):
        validate_uploads([{"key": "projects/demo/voice.wav", "kind": "image"}])
    with pytest.raises(ValueError):
        validate_uploads([{"key": "projects/uploads/" + "a" * 36 + ".mp4", "kind": "image"}])
