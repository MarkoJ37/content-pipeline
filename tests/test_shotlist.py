import json
import sys
from types import SimpleNamespace

import pytest

from src.generate import shots as shots_mod
from src.lib import spend

SCRIPT = (
    "One two three four five. Six seven eight nine ten. Eleven twelve thirteen "
    "fourteen fifteen. Sixteen seventeen eighteen nineteen twenty. Twenty-one "
    "twenty-two twenty-three twenty-four. Twenty-five twenty-six twenty-seven "
    "twenty-eight. Twenty-nine thirty thirty-one thirty-two. Thirty-three "
    "thirty-four thirty-five thirty-six. Thirty-seven thirty-eight thirty-nine "
    "forty. Forty-one forty-two forty-three forty-four."
)


def _valid_payload():
    words = SCRIPT.split()
    shots, i = [], 0
    while i < len(words):
        chunk = " ".join(words[i : i + 4])
        shots.append(
            {"kind": "STOCK", "spoken": chunk, "card_text": "", "keywords": ["b", "roll"],
             "prompt": ""}
        )
        i += 4
    return {"shots": shots}


def _response(payload, in_tokens=800, out_tokens=600):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def spend_log(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEND_LOG_PATH", str(tmp_path / "spend.jsonl"))


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Install a fake `anthropic` module; test sets .client before use."""
    holder = SimpleNamespace(client=None)
    module = SimpleNamespace(Anthropic=lambda: holder.client)
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return holder


def test_generates_valid_shot_list_first_try(fake_anthropic):
    fake_anthropic.client = FakeClient([_response(_valid_payload())])

    result = shots_mod.generate_shot_list(SCRIPT)

    assert " ".join(s.spoken for s in result) == SCRIPT
    assert all(s.kind == "STOCK" for s in result)
    assert len(fake_anthropic.client.requests) == 1
    request = fake_anthropic.client.requests[0]
    assert request["model"] == shots_mod.SHOTLIST_MODEL
    assert request["output_config"]["format"]["type"] == "json_schema"


def test_retries_once_with_validator_feedback(fake_anthropic):
    bad = {"shots": [{"kind": "STOCK", "spoken": "wrong words entirely",
                      "card_text": "", "keywords": ["x"], "prompt": ""}]}
    fake_anthropic.client = FakeClient([_response(bad), _response(_valid_payload())])

    result = shots_mod.generate_shot_list(SCRIPT)

    assert len(fake_anthropic.client.requests) == 2
    retry_msg = fake_anthropic.client.requests[1]["messages"][0]["content"]
    assert "rejected by the validator" in retry_msg
    assert " ".join(s.spoken for s in result) == SCRIPT


def test_gives_up_after_attempt_cap(fake_anthropic):
    bad = {"shots": [{"kind": "STOCK", "spoken": "nope", "card_text": "",
                      "keywords": ["x"], "prompt": ""}]}
    fake_anthropic.client = FakeClient([_response(bad), _response(bad)])

    with pytest.raises(shots_mod.ShotListError, match="after 2 attempts"):
        shots_mod.generate_shot_list(SCRIPT)

    assert len(fake_anthropic.client.requests) == shots_mod.MAX_ATTEMPTS


def test_every_attempt_is_spend_logged(fake_anthropic):
    bad = {"shots": [{"kind": "STOCK", "spoken": "nope", "card_text": "",
                      "keywords": ["x"], "prompt": ""}]}
    fake_anthropic.client = FakeClient([_response(bad), _response(_valid_payload())])

    shots_mod.generate_shot_list(SCRIPT)

    records = spend.read_spend()
    assert len(records) == 2
    assert all(r["service"] == "claude-shotlist" for r in records)
    expected = (800 * 3.00 + 600 * 15.00) / 1_000_000
    assert records[0]["cost_usd"] == pytest.approx(expected)


def test_empty_script_rejected_before_any_call(fake_anthropic):
    fake_anthropic.client = FakeClient([])
    with pytest.raises(ValueError, match="empty"):
        shots_mod.generate_shot_list("   ")
    assert fake_anthropic.client.requests == []


def test_parse_shots_drops_empty_keywords():
    parsed = shots_mod._parse_shots(
        {"shots": [{"kind": "STOCK", "spoken": "a b", "card_text": "",
                    "keywords": ["", "hands", ""], "prompt": ""}]}
    )
    assert parsed[0].keywords == ["hands"]


@pytest.mark.parametrize("kind", ["GENERATE", "SCREEN_REC", "UNKNOWN"])
def test_unsupported_shot_rejected(kind):
    shots = shots_mod._parse_shots(_valid_payload())
    shots[0].kind = kind
    with pytest.raises(ValueError, match="unsupported kind"):
        shots_mod.validate_shot_list(SCRIPT, shots)
