"""Adapter plumbing, without network: fake Anthropic client, mock Jev transport."""

import json
from types import SimpleNamespace

import pytest

from jev_walker.choosers import ChooserError, ClaudeChooser, JevChooser, normalise

STATE = {"walked_m": 100, "target_m": 8047, "remaining_m": 7947, "repeats_so_far": 0,
         "last_turns": [], "elapsed_min": 1.0}
OPTIONS = [{"id": "A", "kind": "exit"}, {"id": "B", "kind": "exit"}, {"id": "arrive_home", "kind": "arrive_home"}]


def test_normalise_fills_and_rescales():
    assert normalise({"A": 2, "B": 2}, ["A", "B", "C"]) == {"A": 0.5, "B": 0.5, "C": 0.0}
    assert normalise({}, ["A", "B"]) == {"A": 0.5, "B": 0.5}


def fake_claude(text, stop_reason="end_turn", model="claude-opus-5-5"):
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(
            stop_reason=stop_reason, stop_details=None, model=model,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
        )

    ch = ClaudeChooser.__new__(ClaudeChooser)
    ch.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    ch.model, ch.effort, ch.model_version = "claude-opus-5-5", "low", "claude-opus-5-5@low"
    return ch, calls


def test_claude_parses_choice_and_cost():
    ch, calls = fake_claude(json.dumps({"choice": "B", "probabilities": {"A": 0.2, "B": 0.6, "arrive_home": 0.2}}))
    c = ch.choose(STATE, OPTIONS)
    assert c.choice == "B" and c.probabilities == {"A": 0.2, "B": 0.6, "arrive_home": 0.2}
    assert c.cost_usd == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    schema = calls[0]["output_config"]["format"]["schema"]
    assert schema["properties"]["choice"]["enum"] == ["A", "B", "arrive_home"]
    # the model sees only the public state and options
    assert json.loads(calls[0]["messages"][0]["content"]) == {"state": STATE, "options": OPTIONS}


def test_claude_refusal_is_a_chooser_error():
    ch, _ = fake_claude("", stop_reason="refusal")
    with pytest.raises(ChooserError):
        ch.choose(STATE, OPTIONS)


def jev(response):
    ch = JevChooser(url="https://jev.invalid/choose", key="k")
    ch.sent = []

    def post(payload):
        ch.sent.append(payload)
        if isinstance(response, Exception):
            raise response
        return response

    ch._post = post
    return ch


def test_jev_round_trip():
    ch = jev({"choice": "A", "probabilities": {"A": 3, "B": 1}})
    c = ch.choose(STATE, OPTIONS)
    assert c.choice == "A" and c.probabilities == {"A": 0.75, "B": 0.25, "arrive_home": 0.0}
    assert ch.sent == [{"state": STATE, "options": OPTIONS}]


def test_jev_without_vector_is_flagged():
    c = jev({"choice": "B"}).choose(STATE, OPTIONS)
    assert c.probabilities["B"] == 1.0 and c.note == "no probability vector returned"


def test_jev_bad_choice_and_transport_error():
    with pytest.raises(ChooserError):
        jev({"choice": "Z"}).choose(STATE, OPTIONS)
    with pytest.raises(ChooserError):
        jev(ChooserError("HTTP 503")).choose(STATE, OPTIONS)
