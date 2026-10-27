"""Adapter plumbing, without network: fake Anthropic client, mock Jev transport."""

import json
import time
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


EXITS = [
    {"id": "A", "kind": "exit", "direction": "right", "angle_deg": 87, "street": "N Clarendon Ave",
     "block_length_m": 190, "already_walked": False, "dead_end": False, "back": False},
    {"id": "B", "kind": "exit", "direction": "back", "angle_deg": -180, "street": "",
     "block_length_m": 120, "already_walked": True, "dead_end": False, "back": True},
    {"id": "arrive_home", "kind": "arrive_home"},
]


def jev(response, monkeypatch, provider="typesafe"):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    for var in ("JEV_API_URL", "JEV_API_KEY", "JEV_MODEL", "JEV_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    ch = JevChooser(provider=provider)
    ch.sent = []

    def post(payload):
        ch.sent.append(payload)
        if isinstance(response, Exception):
            raise response
        return response

    ch._post = post
    return ch


def answer(choice, probs, **extra):
    return {"model": "jev-1.13.0", "answers": {"exit": {"type": "choice", "choice": choice,
            "confidence": 0.5, "probabilities": probs}}, **extra}


def test_jev_request_shape(monkeypatch):
    ch = jev(answer("A", {"A": 0.7, "B": 0.1, "arrive_home": 0.2}), monkeypatch)
    ch.choose(STATE, EXITS)
    req = ch.sent[0]
    assert req["model"] == "jev-latest" and req["state"] == STATE
    q = req["questions"]["exit"]
    assert q["type"] == "choice" and isinstance(q["instructions"], dict)
    assert list(q["criteria"]) == ["A", "B", "arrive_home"]
    a = q["criteria"]["A"]
    assert a["street"] == "N Clarendon Ave" and a["block_length_m"] == 190 and "id" not in a
    assert a["summary"] == "right (+87 deg) onto N Clarendon Ave, 190 m to the next corner"
    assert "U-TURN" in q["criteria"]["B"]["summary"] and "repeat" in q["criteria"]["B"]["summary"]
    home = q["criteria"]["arrive_home"]
    assert home["summary"].startswith("Stop here and end the walk at 0.06 of 5.0 miles.")
    assert "FAILED" in home["summary"] and home["clean_finish"] is False


def test_jev_parses_answer_and_cost(monkeypatch):
    ch = jev(answer("A", {"A": 0.7, "B": 0.1, "arrive_home": 0.2},
                    usage={"input_tokens": 500, "output_tokens": 70, "cost": 0.00002}), monkeypatch)
    c = ch.choose(STATE, EXITS)
    assert c.choice == "A" and c.probabilities == {"A": 0.7, "B": 0.1, "arrive_home": 0.2}
    assert c.cost_usd == 0.00002 and c.model == "jev-1.13.0" and c.confidence == 0.5


def test_jev_cost_estimated_without_usage_cost(monkeypatch):
    ch = jev(answer("B", {"A": 0.4, "B": 0.6, "arrive_home": 0}, usage={"input_tokens": 1_000_000}), monkeypatch)
    assert ch.choose(STATE, EXITS).cost_usd == pytest.approx(0.042)


def test_jev_openrouter_defaults(monkeypatch):
    ch = jev(answer("A", {"A": 1, "B": 0, "arrive_home": 0}), monkeypatch, provider="openrouter")
    assert ch.url == "https://openrouter.ai/api/alpha/decisions"
    assert ch.key == "or-key" and ch.model == "typesafe/jev-1.14"


def test_jev_without_vector_is_flagged(monkeypatch):
    c = jev({"answers": {"exit": {"type": "choice", "choice": "B"}}}, monkeypatch).choose(STATE, EXITS)
    assert c.probabilities["B"] == 1.0 and c.note == "no probability vector returned"


def test_jev_bad_choice_and_transport_error(monkeypatch):
    with pytest.raises(ChooserError):
        jev(answer("Z", {}), monkeypatch).choose(STATE, EXITS)
    with pytest.raises(ChooserError):
        jev(ChooserError("HTTP 503"), monkeypatch).choose(STATE, EXITS)


def test_jev_needs_a_key(monkeypatch):
    for var in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "JEV_API_KEY", "JEV_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ChooserError):
        JevChooser()


def test_jev_http_round_trip_with_retry(monkeypatch):
    """Exercise the real urllib path against a local server that fails once with 503."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.headers["Authorization"], body))
            if len(seen) == 1:
                self.send_response(503)
                self.end_headers()
                return
            out = json.dumps(answer("A", {"A": 0.9, "B": 0.1, "arrive_home": 0})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
        monkeypatch.setenv("JEV_API_URL", f"http://127.0.0.1:{srv.server_port}/v1/systemone")
        monkeypatch.delenv("JEV_PROVIDER", raising=False)
        c = JevChooser(provider="typesafe").choose(STATE, EXITS)
    finally:
        srv.shutdown()
    assert c.choice == "A" and len(seen) == 2
    assert seen[1][0] == "Bearer ts-key" and seen[1][1]["questions"]["exit"]["type"] == "choice"


def test_jev_post_retries_once_on_socket_reset(monkeypatch):
    """A reset mid-read is an OSError, not a URLError: one retry, then ChooserError."""
    import io
    import urllib.request

    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    ch = JevChooser(provider="typesafe")
    monkeypatch.setattr(time, "sleep", lambda s: None)
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        calls.append(req)
        if len(calls) == 1:
            raise ConnectionResetError(104, "Connection reset by peer")
        return Resp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert ch._post({"x": 1}) == {"ok": True}
    assert len(calls) == 2

    calls.clear()

    def always_reset(req, timeout):
        calls.append(req)
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", always_reset)
    with pytest.raises(ChooserError, match="reset"):
        ch._post({"x": 1})
    assert len(calls) == 2


def test_arrive_home_criterion_states_the_cost():
    from jev_walker.choosers import arrive_home_criterion

    short = arrive_home_criterion({"walked_m": 208, "target_m": 8047})
    assert "0.13 of 5.0 miles" in short["summary"] and "4.4 miles short" in short["summary"]
    assert short["clean_finish"] is False and short["walked_m"] == 208

    clean = arrive_home_criterion({"walked_m": 7900, "target_m": 8047})
    assert "clean finish" in clean["summary"] and clean["clean_finish"] is True

    over = arrive_home_criterion({"walked_m": 9200, "target_m": 8047})
    assert "past the 5.5 mile window" in over["summary"] and over["clean_finish"] is False
