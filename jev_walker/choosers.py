"""Choosers: anything that takes (state, options) and returns one option id plus a
probability per option. Every chooser sees exactly the public state and menu, never
the graph.

- random     baseline walker under the same rules
- heuristic  a hand-written compass policy; proves the state carries enough to close
             a loop, and gives the sim a sane non-model reference
- claude     Claude making the identical forced choice from the identical menu
- jev        HTTP adapter for Jev; the request/response shape is an assumption until
             the open questions in the spec are settled against Jev's docs
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass

from .geo import wrap180


class ChooserError(RuntimeError):
    pass


@dataclass
class Choice:
    choice: str
    probabilities: dict[str, float]
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    note: str | None = None


class Chooser:
    name = "base"
    model_version: str | None = None

    def choose(self, state: dict, options: list[dict]) -> Choice:
        raise NotImplementedError


def normalise(probs: dict[str, float], ids: list[str]) -> dict[str, float]:
    """Full probability vector over `ids`, summing to 1. Missing ids get 0."""
    p = {i: max(0.0, float(probs.get(i, 0.0) or 0.0)) for i in ids}
    total = sum(p.values())
    if total <= 0:
        return {i: 1 / len(ids) for i in ids}
    return {i: round(v / total, 4) for i, v in p.items()}


def softmax(scores: dict[str, float], temperature: float) -> dict[str, float]:
    m = max(scores.values())
    e = {k: math.exp((v - m) / temperature) for k, v in scores.items()}
    z = sum(e.values())
    return {k: v / z for k, v in e.items()}


# --------------------------------------------------------------------------- random

class RandomChooser(Chooser):
    name = "random"

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)

    def choose(self, state, options):
        ids = [o["id"] for o in options]
        return Choice(self.rng.choice(ids), {i: round(1 / len(ids), 4) for i in ids})


# --------------------------------------------------------------------------- heuristic

class HeuristicChooser(Chooser):
    """Steer along an imaginary loop using only the compass.

    On a circle that starts and ends at home, the angle between your heading and the
    direction home falls linearly from 180 degrees (home directly behind) to 0 (home
    dead ahead) as you go round. So: aim to keep home at 180*(1 - walked/target)
    degrees off the nose, on a fixed side, nudged toward home when farther out than a
    loop of this length should take you. Repeats, dead ends and U-turns are penalised.
    Pure mode has no compass, so it just avoids repeats and drifts straight.
    """

    name = "heuristic"

    def __init__(self, seed: int | None = None, side: int = 1, temperature: float = 12.0):
        self.rng = random.Random(seed)
        self.side = side
        self.temperature = temperature

    def choose(self, state, options):
        scores: dict[str, float] = {}
        walked, target = state["walked_m"], state["target_m"]
        frac = min(1.0, walked / target)
        compass = state.get("home_bearing_relative")
        home_d = state.get("home_distance_m")

        desired_rel = None
        if compass and not compass.get("absolute"):
            # Grid loops of length L reach at most ~L/4 from home.
            ideal_d = (target / 4) * math.sin(math.pi * frac)
            mag = 180 * (1 - frac) - 0.06 * (home_d - ideal_d)
            desired_rel = self.side * max(0.0, min(180.0, mag))
            # Not enough budget left to get home any other way: go straight for it.
            if state["remaining_m"] < home_d * 1.25:
                desired_rel = 0.0

        for o in options:
            if o["kind"] == "arrive_home":
                scores[o["id"]] = 50.0 if walked >= 0.9 * target else -500.0
                continue
            s = 0.0
            if desired_rel is not None:
                new_rel = wrap180(compass["deg"] - o["angle_deg"])
                s -= abs(wrap180(new_rel - desired_rel))
            elif o["back"] is None:
                # First corner: no heading, home is right here. Leave along the
                # longer block.
                s += o["block_length_m"] / 20
            else:
                s -= abs(o["angle_deg"]) / 4  # pure mode: prefer straight
            if o["already_walked"]:
                s -= 150
            if o["dead_end"]:
                s -= 150
            if o["back"]:
                s -= 90
            s += self.rng.uniform(0, 5)
            scores[o["id"]] = s

        probs = softmax(scores, self.temperature)
        best = max(scores, key=scores.get)
        return Choice(best, normalise(probs, list(scores)))


# --------------------------------------------------------------------------- claude

CLAUDE_SYSTEM = """\
You are steering a person on a five-mile walking loop that starts and ends at their \
front door. At every street corner you choose one exit; they walk it to the next \
corner and you choose again. You only ever see the current corner: the exits here \
and a few numbers about the walk so far. You have no map.

Goal, in order of importance:
1. Get back to the front door. A loop that never closes fails.
2. Land the total between 4.5 and 5.5 miles (target 5.0 mi = 8,047 m).
3. Never walk the same street segment twice (already_walked = true means a repeat).

The run is cut off at 6.5 miles or 2 h 30 min, whichever comes first.

Fields:
- walked_m / target_m / remaining_m: distance so far, the goal, and what is left.
- home_distance_m: straight-line distance to the door (not walking distance).
- home_bearing_relative: where the door is relative to the way the walker is facing; \
deg is signed, positive = to the right, 0 = dead ahead, 180 = directly behind. On the \
first corner it is absolute (compass bearing) instead.
- repeats_so_far, last_turns, elapsed_min.
Each exit has: direction (relative turn, or compass direction on the first corner), \
angle_deg (signed turn angle, + = right), street, block_length_m (distance to the next \
corner), already_walked, dead_end, back (the U-turn exit).
An arrive_home option appears only when the walker is at the door. Picking it ends \
the walk; you may decline and keep walking.

Return the id you choose and a probability for every option id, summing to 1, \
reflecting how strongly you prefer each."""

# $ per million tokens (input, output)
CLAUDE_PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}


class ClaudeChooser(Chooser):
    name = "claude"

    def __init__(self, model: str | None = None, effort: str | None = None):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("JEV_CLAUDE_MODEL", "claude-opus-5-5")
        # Per-corner latency matters on the sidewalk, so default to low effort.
        self.effort = effort or os.environ.get("JEV_CLAUDE_EFFORT", "low")
        self.model_version = f"{self.model}@{self.effort}"

    def choose(self, state, options):
        import anthropic

        ids = [o["id"] for o in options]
        schema = {
            "type": "object",
            "properties": {
                "choice": {"type": "string", "enum": ids},
                "probabilities": {
                    "type": "object",
                    "properties": {i: {"type": "number"} for i in ids},
                    "required": ids,
                    "additionalProperties": False,
                },
            },
            "required": ["choice", "probabilities"],
            "additionalProperties": False,
        }
        prompt = json.dumps({"state": state, "options": options}, separators=(",", ":"))
        t0 = time.perf_counter()
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4096,
                system=CLAUDE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": schema}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIConnectionError as e:
            raise ChooserError(f"connection: {e}") from e
        except anthropic.APIStatusError as e:
            raise ChooserError(f"{e.status_code}: {e.message}") from e
        latency_ms = (time.perf_counter() - t0) * 1000
        if resp.stop_reason == "refusal":
            raise ChooserError(f"refusal: {getattr(resp.stop_details, 'category', None)}")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise ChooserError(f"no text block (stop_reason={resp.stop_reason})")
        data = json.loads(text)
        pin, pout = CLAUDE_PRICES.get(resp.model, CLAUDE_PRICES.get(self.model, (0, 0)))
        cost = (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
        return Choice(data["choice"], normalise(data["probabilities"], ids),
                      latency_ms=latency_ms, cost_usd=round(cost, 6),
                      note=None if resp.model == self.model else f"served by {resp.model}")


# --------------------------------------------------------------------------- jev

class JevChooser(Chooser):
    """Assumed contract, to be checked against Jev's docs (spec: Open questions):

        POST $JEV_API_URL   Authorization: Bearer $JEV_API_KEY
        {"state": {...}, "options": [{"id": "A", ...}, ...]}
        -> {"choice": "A", "probabilities": {"A": 0.62, "B": 0.38}, "cost_usd": 0.0001}

    Only this class should change once the real API shape is known.
    """

    name = "jev"

    def __init__(self, url: str | None = None, key: str | None = None, timeout_s: float = 10.0):
        self.url = url or os.environ.get("JEV_API_URL")
        if not self.url:
            raise ChooserError("JEV_API_URL is not set")
        self.key = key or os.environ.get("JEV_API_KEY")
        self.timeout_s = timeout_s
        self.model_version = os.environ.get("JEV_MODEL_VERSION")

    def _post(self, payload: dict) -> dict:
        import urllib.error
        import urllib.request

        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise ChooserError(str(e)) from e

    def choose(self, state, options):
        ids = [o["id"] for o in options]
        t0 = time.perf_counter()
        data = self._post({"state": state, "options": options})
        latency_ms = (time.perf_counter() - t0) * 1000
        if data.get("choice") not in ids:
            raise ChooserError(f"jev chose {data.get('choice')!r}, not one of {ids}")
        probs = data.get("probabilities") or {data["choice"]: 1.0}
        return Choice(data["choice"], normalise(probs, ids), latency_ms=latency_ms,
                      cost_usd=float(data.get("cost_usd", 0.0)),
                      note=None if data.get("probabilities") else "no probability vector returned")


def make_chooser(name: str, seed: int | None = None) -> Chooser:
    if name == "random":
        return RandomChooser(seed)
    if name == "heuristic":
        return HeuristicChooser(seed)
    if name == "claude":
        return ClaudeChooser()
    if name == "jev":
        return JevChooser()
    raise ValueError(f"unknown chooser {name!r}")
