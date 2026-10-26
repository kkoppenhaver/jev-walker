"""The run loop. One `Run` owns the walker's position, the path so far, the clock and
the log. The simulator drives it with a virtual walker; the phone backend will drive
the same object with real arrivals and real time.
"""

from __future__ import annotations

import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field

import networkx as nx

from .choosers import Chooser, Choice, ChooserError
from .geo import haversine_m
from .graph import Home
from .menu import ARRIVE_HOME, Option, arrival_heading, build_menu, build_state

MILE_M = 1609.344
LOG_VERSION = 1


@dataclass
class RunConfig:
    mode: str = "compass"                     # "compass" or "pure"
    target_m: float = 5.0 * MILE_M
    cap_ratio: float = 1.30                   # stop at 130% of target (6.5 mi)
    time_cap_min: float = 150.0               # 2 h 30 min
    clean_lo_m: float = 4.5 * MILE_M
    clean_hi_m: float = 5.5 * MILE_M
    arrive_home_radius_m: float = 25.0        # see open question on 25 m vs 400 m
    walk_speed_mps: float = 1.34              # ~3 mph
    corner_dwell_s: float = 8.0               # crossing wait, reading the screen
    max_decisions: int = 1000

    @property
    def cap_m(self) -> float:
        return self.target_m * self.cap_ratio


@dataclass
class RunState:
    node: int
    arriving: tuple | None = None             # directed (u, node, k) hop just walked
    walked_m: float = 0.0
    elapsed_s: float = 0.0
    repeats: int = 0
    walk_counts: Counter = field(default_factory=Counter)  # EdgeId -> times walked
    last_turns: list = field(default_factory=list)
    ended: str | None = None


class Run:
    def __init__(self, G: nx.MultiGraph, home: Home, chooser: Chooser,
                 config: RunConfig | None = None, *, run_id: str | None = None,
                 dist_home: dict | None = None, sim: bool = True, seed: int | None = None):
        self.G, self.home, self.chooser = G, home, chooser
        self.cfg = config or RunConfig()
        self.sim = sim
        self.dist_home = dist_home  # harness-only oracle for calibration; never shown to Jev
        self.s = RunState(node=home.node)
        self.decisions: list[dict] = []
        self.path: list[dict] = []
        self.ui_events: list[dict] = []
        self.gps: list[dict] = []
        self._options: dict[str, Option] = {}
        hn = G.nodes[home.node]
        self.header = {
            "log_version": LOG_VERSION,
            "run_id": run_id or uuid.uuid4().hex[:12],
            "sim": sim,
            "seed": seed,
            "mode": self.cfg.mode,
            "chooser": chooser.name,
            "model_version": chooser.model_version,
            "home_node": home.node,
            "home": {"lon": hn["x"], "lat": hn["y"], "door_snap_m": round(home.snap_m, 1)},
            "graph_hash": G.graph.get("graph_hash"),
            "config": asdict(self.cfg),
            "started_at": time.time(),
            "clock_offset_s": 0.0 if sim else None,
        }
        self.ui_events.append({"t": 0.0, "type": "start"})

    # ---------------------------------------------------------------- corner

    @property
    def here(self) -> tuple[float, float]:
        n = self.G.nodes[self.s.node]
        return (n["x"], n["y"])

    @property
    def home_lonlat(self) -> tuple[float, float]:
        n = self.G.nodes[self.home.node]
        return (n["x"], n["y"])

    def _offer_arrive_home(self) -> bool:
        if self.s.walked_m <= 0:
            return False
        if self.s.node == self.home.node:
            return True
        return haversine_m(*self.here, *self.home_lonlat) <= self.cfg.arrive_home_radius_m

    def corner(self) -> tuple[dict, list[Option], list[dict]]:
        s = self.s
        options, dropped = build_menu(self.G, s.node, s.arriving, set(s.walk_counts),
                                      offer_arrive_home=self._offer_arrive_home())
        self._options = {o.id: o for o in options}
        heading = arrival_heading(self.G, *s.arriving) if s.arriving else None
        state = build_state(
            walked_m=s.walked_m, target_m=self.cfg.target_m, repeats=s.repeats,
            last_turns=s.last_turns, elapsed_min=s.elapsed_s / 60, mode=self.cfg.mode,
            here=self.here, home=self.home_lonlat, heading=heading,
        )
        return state, options, dropped

    # ---------------------------------------------------------------- decide / move

    def step(self) -> dict:
        """One simulated corner: build the menu, ask the chooser, walk the choice."""
        t_arrived = self.s.elapsed_s
        state, options, dropped = self.corner()
        public = [o.public() for o in options]
        try:
            choice = self.chooser.choose(state, public)
            if choice.choice not in self._options:
                raise ChooserError(f"chose {choice.choice!r}, not on the menu")
        except ChooserError as e:
            self.s.ended = "chooser_error"
            self.ui_events.append({"t": self.s.elapsed_s, "type": "chooser_error", "error": str(e)})
            return {}
        latency_s = choice.latency_ms / 1000
        rec = self._record(t_arrived, state, options, dropped, choice, latency_s)
        self.s.elapsed_s += latency_s
        self.apply(rec, choice.choice)
        return rec

    def _record(self, t_arrived, state, options, dropped, choice: Choice, latency_s) -> dict:
        rec = {
            "i": len(self.decisions),
            "node": self.s.node,
            "lonlat": list(self.here),
            "t_arrived": round(t_arrived, 3),
            "t_menu_built": round(t_arrived, 3),
            "t_jev_sent": round(t_arrived, 3),
            "t_jev_returned": round(t_arrived + latency_s, 3),
            "state": state,
            "menu": [o.harness() for o in options],
            "dropped": dropped,
            "choice": choice.choice,
            "probabilities": choice.probabilities,
            "latency_ms": round(choice.latency_ms, 1),
            "cost_usd": choice.cost_usd,
            "override": None,
        }
        if choice.note:
            rec["note"] = choice.note
        if choice.model:
            rec["model"] = choice.model
        if choice.confidence is not None:
            rec["confidence"] = choice.confidence
        if self.dist_home is not None:
            rec["oracle"] = self._oracle(options)
        self.decisions.append(rec)
        return rec

    def _oracle(self, options: list[Option]) -> dict:
        """For the calibration chart: true shortest-path distance home after each
        option, and whether the loop is still closable within the clean window."""
        out = {}
        for o in options:
            if o.kind == ARRIVE_HOME:
                total = self.s.walked_m + self.dist_home[self.s.node]
                out[o.id] = {"sp_home_after_m": round(self.dist_home[self.s.node]),
                             "closable": True,
                             "lands_in_window": self.cfg.clean_lo_m <= total <= self.cfg.clean_hi_m}
            else:
                t = o.traversal
                sp = self.dist_home[t.end]
                total = self.s.walked_m + t.length_m + sp
                out[o.id] = {"sp_home_after_m": round(sp),
                             "closable": total <= self.cfg.clean_hi_m,
                             "lands_in_window": total <= self.cfg.clean_hi_m}
        return out

    def apply(self, rec: dict, chosen_id: str, override: dict | None = None) -> None:
        """Walk the chosen exit (or the overridden one). In the simulator the walker
        arrives instantly; time advances by block length / walking speed."""
        s, cfg = self.s, self.cfg
        if override:
            rec["override"] = override
            self.ui_events.append({"t": s.elapsed_s, "type": "override", **override})
        if chosen_id == ARRIVE_HOME:
            finish = 0.0
            if s.node != self.home.node:
                finish = nx.shortest_path_length(self.G, s.node, self.home.node, weight="length")
                s.walked_m += finish
                s.elapsed_s += finish / cfg.walk_speed_mps
            rec["finish_on_foot_m"] = round(finish, 1)
            s.node, s.ended = self.home.node, "home"
            self.ui_events.append({"t": s.elapsed_s, "type": "run_end", "reason": "home"})
            return

        opt = self._options[chosen_id]
        trav = opt.traversal
        repeat_hops = sum(1 for e in trav.edge_ids if s.walk_counts[e] > 0)
        if not repeat_hops and any(s.walk_counts[e] > 0 for e in trav.twin_edges):
            repeat_hops = 1  # same street, other carriageway
        for e in trav.edge_ids + trav.twin_edges:
            s.walk_counts[e] += 1
        s.repeats += repeat_hops
        s.walked_m += trav.length_m
        s.elapsed_s += trav.length_m / cfg.walk_speed_mps + cfg.corner_dwell_s
        s.last_turns.append(opt.direction)
        self.path.append({
            "decision": rec["i"], "from": s.node, "to": trav.end,
            "edges": [list(e) for e in trav.edge_ids], "street": opt.street,
            "length_m": round(trav.length_m, 1), "repeat_hops": repeat_hops,
            "coords": [[round(x, 7), round(y, 7)] for x, y in trav.coords],
        })
        s.node, s.arriving = trav.end, trav.last_hop
        self._check_end()

    def _check_end(self) -> None:
        s, cfg = self.s, self.cfg
        reason = None
        if s.walked_m >= cfg.cap_m:
            reason = "distance_cap"
        elif s.elapsed_s >= cfg.time_cap_min * 60:
            reason = "time_cap"
        elif len(self.decisions) >= cfg.max_decisions:
            reason = "decision_cap"
        if reason:
            s.ended = reason
            self.ui_events.append({"t": s.elapsed_s, "type": "run_end", "reason": reason})

    # ---------------------------------------------------------------- whole run

    def run_to_end(self) -> dict:
        while not self.s.ended:
            self.step()
        return self.log()

    def log(self) -> dict:
        from .scoring import score

        s = self.s
        out = {
            "header": self.header,
            "decisions": self.decisions,
            "path": self.path,
            "ui_events": self.ui_events,
            "gps": self.gps,
            "end": {"reason": s.ended, "walked_m": round(s.walked_m, 1),
                    "elapsed_min": round(s.elapsed_s / 60, 2), "final_node": s.node},
        }
        out["score"] = score(out, self.cfg)
        return out


def dist_to_home(G: nx.MultiGraph, home: Home) -> dict:
    return nx.single_source_dijkstra_path_length(G, home.node, weight="length")

