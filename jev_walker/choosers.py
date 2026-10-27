"""Choosers: anything that takes (state, options) and returns one option id plus a
probability per option. Every chooser sees exactly the public state and menu, never
the graph.

- random     baseline walker under the same rules
- heuristic  a hand-written compass policy; proves the state carries enough to close
             a loop, and gives the sim a sane non-model reference
- claude     Claude making the identical forced choice from the identical menu
- jev        TypeSafe's Jev decision model: one Choice question per corner
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
    model: str | None = None        # the snapshot that actually served the call
    confidence: float | None = None  # chooser's own spread statistic, if it has one


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
                      note=None if resp.model == self.model else f"served by {resp.model}",
                      model=resp.model)


# --------------------------------------------------------------------------- jev

JEV_INSTRUCTIONS = {
    "question": "Which exit should the walker take at this corner?",
    "situation": (
        "A person is walking a loop that starts and ends at their front door, target "
        "5.0 miles (8,047 m). At every corner you pick one exit; they walk it to the "
        "next corner. You see only this corner and the numbers in the state. There is "
        "no map."
    ),
    "goals_in_order": [
        "Get back to the front door. A loop that never closes fails.",
        "Finish between 4.5 and 5.5 miles (7,242 to 8,851 m).",
        "Never walk the same block twice: already_walked = true is a repeat.",
    ],
    "limits": "The walk is cut off at 6.5 miles or 150 minutes.",
    "state_fields": {
        "home_distance_m": "straight-line distance to the door, not walking distance",
        "home_bearing_relative": (
            "where the door is relative to the way the walker faces; deg is signed, "
            "positive = right, 0 = dead ahead, +/-180 = behind. Absolute compass "
            "bearing on the first corner."
        ),
        "remaining_m": "distance left to the 5.0 mile target",
    },
    "option_fields": {
        "angle_deg": "signed turn angle, positive = right",
        "block_length_m": "distance to the next corner",
        "back": "the U-turn exit",
        "dead_end": "the street ends at the next corner",
    },
}

ARRIVE_HOME_CRITERION = {
    "summary": "Stop here. The walker is at the front door; the walk ends now.",
}

# OpenRouter lists Jev at $0.042 per million input tokens, output free. Used only when
# the response carries no usage.cost (TypeSafe direct).
JEV_PRICE_PER_MTOK = float(os.environ.get("JEV_PRICE_PER_MTOK", "0.042"))

JEV_ENDPOINTS = {
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "TYPESAFE_API_KEY", "jev-latest"),
    "openrouter": ("https://openrouter.ai/api/alpha/decisions", "OPENROUTER_API_KEY", "typesafe/jev-1.14"),
}
RETRY_STATUS = {429, 500, 502, 503, 524, 529}


def jev_criteria(options: list[dict]) -> dict:
    """One Choice criterion per option, keyed by option id. TypeSafe allows object
    descriptions, so each exit keeps its typed attributes, plus a one-line summary."""
    out = {}
    for o in options:
        if o["kind"] == "arrive_home":
            out[o["id"]] = ARRIVE_HOME_CRITERION
            continue
        attrs = {k: v for k, v in o.items() if k not in ("id", "kind")}
        where = f"onto {o['street']}" if o["street"] else "along an unnamed path"
        flags = [f for f in ("already_walked", "dead_end", "back") if o.get(f)]
        summary = f"{o['direction']} ({o['angle_deg']:+d} deg) {where}, {o['block_length_m']} m to the next corner"
        if flags:
            summary += "; " + ", ".join(flags).replace("_", " ")
        out[o["id"]] = {"summary": summary, **attrs}
    return out


class JevChooser(Chooser):
    """TypeSafe Jev via the System One / Decisions API.

        POST https://api.typesafe.ai/v1/systemone           (TYPESAFE_API_KEY)
          or https://openrouter.ai/api/alpha/decisions      (OPENROUTER_API_KEY)
        {"model": ..., "state": {...},
         "questions": {"exit": {"type": "choice", "instructions": ..., "criteria": {"A": {...}, ...}}}}
        -> {"model": "jev-1.13.0", "answers": {"exit": {"type": "choice", "choice": "A",
            "confidence": 0.4, "probabilities": {"A": 0.7, "B": 0.3}}},
            "usage": {"input_tokens": ..., "output_tokens": ..., "cost": ...}}

    Every call is independent: the API keeps no state between requests, which is why
    the state carries last_turns. JEV_API_URL / JEV_API_KEY / JEV_MODEL override the
    defaults.
    """

    name = "jev"
    QUESTION = "exit"

    def __init__(self, provider: str | None = None, timeout_s: float = 10.0):
        provider = provider or os.environ.get("JEV_PROVIDER") or (
            "typesafe" if os.environ.get("TYPESAFE_API_KEY") else "openrouter")
        if provider not in JEV_ENDPOINTS:
            raise ChooserError(f"unknown JEV_PROVIDER {provider!r}")
        url, key_var, model = JEV_ENDPOINTS[provider]
        self.provider = provider
        self.url = os.environ.get("JEV_API_URL", url)
        self.key = os.environ.get("JEV_API_KEY") or os.environ.get(key_var)
        if not self.key:
            raise ChooserError(f"set {key_var} (or JEV_API_KEY) to use the jev chooser")
        self.model = os.environ.get("JEV_MODEL", model)
        self.model_version = f"{provider}:{self.model}"
        self.timeout_s = timeout_s

    def request(self, state: dict, options: list[dict]) -> dict:
        return {
            "model": self.model,
            "state": state,
            "questions": {self.QUESTION: {
                "type": "choice",
                "instructions": JEV_INSTRUCTIONS,
                "criteria": jev_criteria(options),
            }},
        }

    def _post(self, payload: dict) -> dict:
        import urllib.error
        import urllib.request

        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"}
        for attempt in (0, 1):
            req = urllib.request.Request(self.url, data=body, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code in RETRY_STATUS and attempt == 0:
                    time.sleep(0.5)
                    continue
                raise ChooserError(f"HTTP {e.code}: {detail}") from e
            except (OSError, ValueError) as e:
                # URLError, TimeoutError and a reset or dropped socket mid-read are all
                # OSError. One retry: a single reset must not end a run, let alone a
                # 1,000-loop sim.
                if attempt == 0 and not isinstance(e, ValueError):
                    time.sleep(0.5)
                    continue
                raise ChooserError(str(e)) from e
        raise AssertionError("unreachable")

    def choose(self, state, options):
        ids = [o["id"] for o in options]
        t0 = time.perf_counter()
        data = self._post(self.request(state, options))
        latency_ms = (time.perf_counter() - t0) * 1000
        answer = (data.get("answers") or {}).get(self.QUESTION) or {}
        if answer.get("choice") not in ids:
            raise ChooserError(f"jev chose {answer.get('choice')!r}, not one of {ids}: {str(data)[:300]}")
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        if cost is None:
            cost = usage.get("input_tokens", 0) * JEV_PRICE_PER_MTOK / 1e6
        probs = answer.get("probabilities")
        return Choice(answer["choice"], normalise(probs or {answer["choice"]: 1.0}, ids),
                      latency_ms=latency_ms, cost_usd=round(float(cost), 8),
                      note=None if probs else "no probability vector returned",
                      model=data.get("model"), confidence=answer.get("confidence"))


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
