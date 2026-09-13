import pytest

from src.generate import assemble, shots
from src.generate.shots import DEMO_SCRIPT, DEMO_SHOTS, Shot, validate_shot_list


def _timings_for(script: str, step: float = 0.3):
    words = script.split()
    return [
        {"word": w, "start": round(i * step, 3), "end": round(i * step + 0.25, 3)}
        for i, w in enumerate(words)
    ]


# -- shot list validation ------------------------------------------------------


def test_demo_shot_list_is_valid():
    validate_shot_list(DEMO_SCRIPT, DEMO_SHOTS)


def test_too_few_shots_rejected():
    with pytest.raises(ValueError, match="needs 10-15"):
        validate_shot_list("one two.", [Shot("TEXT_CARD", "one two.", card_text="x")])


def test_spoken_text_must_reconstruct_script():
    bad = [Shot("TEXT_CARD", f"word{i}", card_text="x") for i in range(10)]
    with pytest.raises(ValueError, match="does not reconstruct"):
        validate_shot_list("something else entirely", bad)


def test_generate_shots_capped_at_two():
    words = [f"w{i}." for i in range(10)]
    shot_list = [Shot("GENERATE", w, prompt="p") for w in words[:3]] + [
        Shot("TEXT_CARD", w, card_text="x") for w in words[3:]
    ]
    with pytest.raises(ValueError, match="cap is 2"):
        validate_shot_list(" ".join(words), shot_list)


def test_text_card_requires_copy():
    words = [f"w{i}." for i in range(10)]
    shot_list = [Shot("TEXT_CARD", w) for w in words]
    with pytest.raises(ValueError, match="no card_text"):
        validate_shot_list(" ".join(words), shot_list)


# -- shot boundaries -----------------------------------------------------------


def test_boundaries_cover_full_audio_contiguously():
    timings = _timings_for(DEMO_SCRIPT)
    audio_duration = timings[-1]["end"] + 0.2
    bounds = assemble.shot_boundaries(DEMO_SCRIPT, DEMO_SHOTS, timings, audio_duration)

    assert len(bounds) == len(DEMO_SHOTS)
    assert bounds[0][0] == 0.0
    for (_, prev_end), (next_start, _) in zip(bounds, bounds[1:], strict=False):
        assert prev_end == next_start  # no gaps, no overlaps
    assert bounds[-1][1] == pytest.approx(audio_duration + assemble.TAIL_SECONDS)


def test_boundaries_start_at_first_word_of_each_shot():
    timings = _timings_for(DEMO_SCRIPT)
    bounds = assemble.shot_boundaries(DEMO_SCRIPT, DEMO_SHOTS, timings, timings[-1]["end"])

    words_before_second_shot = len(DEMO_SHOTS[0].spoken.split())
    assert bounds[1][0] == timings[words_before_second_shot]["start"]


def test_boundaries_reject_missing_words():
    timings = _timings_for(DEMO_SCRIPT)[:-3]
    with pytest.raises(ValueError, match="must match"):
        assemble.shot_boundaries(DEMO_SCRIPT, DEMO_SHOTS, timings, timings[-1]["end"])


def test_boundaries_reject_overlapping_word_times():
    timings = [{"word": w, "start": 0.0, "end": 0.05} for w in DEMO_SCRIPT.split()]
    with pytest.raises(ValueError, match="must match"):
        assemble.shot_boundaries(DEMO_SCRIPT, DEMO_SHOTS, timings, 0.1)


def test_boundaries_reject_mismatched_partition():
    with pytest.raises(ValueError, match="partition"):
        assemble.shot_boundaries("only three words", DEMO_SHOTS, _timings_for("x y z"), 1.0)


# -- captions -------------------------------------------------------------------


def test_ass_chunks_words_in_threes():
    ass = assemble.build_ass(_timings_for("one two three four five"))
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    assert len(lines) == 2
    assert lines[0].endswith(",one two three")
    assert lines[1].endswith(",four five")


def test_ass_chunk_holds_until_next_chunk_starts():
    timings = _timings_for("one two three four five six")
    ass = assemble.build_ass(timings)
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    first_end = lines[0].split(",")[2]
    second_start = lines[1].split(",")[1]
    assert first_end == second_start  # no caption flicker between chunks


def test_ass_time_format():
    assert assemble._ass_time(0) == "0:00:00.00"
    assert assemble._ass_time(75.5) == "0:01:15.50"
    assert assemble._ass_time(3661.25) == "1:01:01.25"


def test_ass_escapes_override_braces():
    ass = assemble.build_ass([{"word": "{\\b1}hack", "start": 0.0, "end": 0.3}])
    assert "{" not in ass.split("Dialogue:")[1]


def test_demo_script_word_counts_match_shots():
    assert sum(len(s.spoken.split()) for s in DEMO_SHOTS) == len(DEMO_SCRIPT.split())
    assert shots.MIN_SHOTS <= len(DEMO_SHOTS) <= shots.MAX_SHOTS


def test_captions_do_not_cross_sentence_boundaries():
    ass = assemble.build_ass(_timings_for("Start now. Stay focused."))
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert lines[0].endswith(",Start now.")
    assert lines[1].endswith(",Stay focused.")


def test_captions_clear_during_silence():
    timings = [
        {"word": "Wait", "start": 0.0, "end": 0.4},
        {"word": "Go", "start": 3.0, "end": 3.4},
    ]
    lines = [s for s in assemble.build_ass(timings).splitlines() if s.startswith("Dialogue:")]
    assert lines[0].split(",")[2] == "0:00:00.70"
    assert lines[1].split(",")[1] == "0:00:03.00"


def test_long_caption_phrases_split_before_overflow():
    ass = assemble.build_ass(_timings_for("Extraordinary opportunities ahead"))
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(lines) == 2


def test_caption_backslashes_cannot_inject_line_breaks():
    ass = assemble.build_ass(_timings_for(r"hello\Nworld"))
    assert r"hello\Nworld" not in ass
