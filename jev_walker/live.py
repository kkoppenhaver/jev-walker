"""A real walk: the same `Run` the simulator uses, driven by GPS fixes and a real clock.

The phone only senses and displays. It posts every GPS fix here; this module snaps
fixes to the graph, decides when Keanan has reached the next corner, asks the
chooser, and hands back a `view` for the screen. Nothing here touches HTTP, so the
whole walk can be exercised in tests with a fake clock and a list of fixes.

Arrival rule (spec: "Being at a corner"): two consecutive fixes within 15 m of the
corner he is walking to, or a tap on I'm here. Fixes worse than 30 m accuracy
neither count toward arrival nor reset the count, and nothing counts for the first
5 s after a decision, so a short block can't trigger on the corner he just left.
"""

from __future__ import annotations

import dataclasses
import json
import os
import statistics
import threading
import time
import uuid
from pathlib import Path

import networkx as nx
from shapely.geometry import LineString, Point

from .choosers import Chooser, ChooserError
from .geo import LocalProjection, bearing_deg, haversine_m, wrap180
from .graph import Home, is_decision_point
from .menu import ARRIVE_HOME, arrival_heading
from .run import Run, RunConfig, dist_to_home

ARRIVE_RADIUS_M = 15.0
ARRIVE_FIXES = 2
ARRIVE_MAX_ACCURACY_M = 30.0
MIN_WALK_S = 5.0
MANUAL_DEBOUNCE_S = 3.0     # an I'm here within 3 s of a decision is a double tap
SAVE_EVERY_FIXES = 10
OVERRIDE_REASONS = ("no_legal_crossing", "sidewalk_closed", "exit_missing", "other")


class WalkError(ValueError):
    """A request that doesn't fit the walk's current phase (bad option id, etc.)."""


class NodeIndex:
    """Nearest-decision-point lookup on a 100 m grid, for logging snapped fixes."""

    CELL = 100.0

    def __init__(self, G: nx.MultiGraph):
        lon0, lat0 = next(iter(G.nodes(data=True)))[1]["x"], next(iter(G.nodes(data=True)))[1]["y"]
        self.proj = LocalProjection(lon0, lat0)
        self.cells: dict[tuple[int, int], list] = {}
        self.G = G
        for n, d in G.nodes(data=True):
            if is_decision_point(G, n):
                x, y = self.proj.to_xy(d["x"], d["y"])
                self.cells.setdefault((int(x // self.CELL), int(y // self.CELL)), []).append(n)

    def nearest(self, lon: float, lat: float) -> tuple[int | None, float]:
        x, y = self.proj.to_xy(lon, lat)
        cx, cy = int(x // self.CELL), int(y // self.CELL)
        best, best_d = None, float("inf")
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n in self.cells.get((cx + dx, cy + dy), ()):
                    d = haversine_m(lon, lat, self.G.nodes[n]["x"], self.G.nodes[n]["y"])
                    if d < best_d:
                        best, best_d = n, d
        return best, best_d


class LiveWalk:
    def __init__(self, G: nx.MultiGraph, home: Home, chooser: Chooser, config: RunConfig,
                 *, area: str, log_dir: Path, clock=time.time):
        self.G, self.home, self.chooser, self.cfg = G, home, chooser, config
        self.area = area
        self.log_dir = Path(log_dir)
        self.clock = clock
        self.lock = threading.RLock()
        self.index = NodeIndex(G)
        self.dist_home = dist_to_home(G, home)
        self.run: Run | None = None
        self.phase = "idle"          # idle -> walking <-> chooser_error -> ended
        self.t0 = 0.0
        self.pending: dict | None = None   # the decision being walked
        self.error: str | None = None
        self.last_fix: dict | None = None
        self.near_count = 0
        self.offsets: list[float] = []
        self._fixes_since_save = 0
        self._corner = None          # menu kept across a chooser error for retry

    # ---------------------------------------------------------------- helpers

    def _now(self) -> float:
        return self.clock() - self.t0

    def _client(self, client_t: float | None) -> None:
        """Track phone clock vs server clock (client_t is epoch ms)."""
        if client_t:
            self.offsets.append(self.clock() - client_t / 1000)
            self.offsets = self.offsets[-200:]
            if self.run:
                self.run.header["clock_offset_s"] = round(statistics.median(self.offsets), 3)

    def _event(self, type_: str, **kw) -> None:
        self.run.ui_events.append({"t": round(self._now(), 3), "type": type_, **kw})

    def _target(self):
        """The traversal being walked (Jev's pick, or the override)."""
        return self.run._options[self.pending["taken"]].traversal

    # ---------------------------------------------------------------- lifecycle

    def start(self, *, client_t: float | None = None, fix: dict | None = None,
              mode: str | None = None) -> dict:
        with self.lock:
            if self.phase in ("walking", "chooser_error"):
                raise WalkError("a walk is already running; end it first")
            cfg = dataclasses.replace(self.cfg, mode=mode) if mode else self.cfg
            run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
            self.run = Run(self.G, self.home, self.chooser, cfg, run_id=run_id,
                           dist_home=self.dist_home, sim=False)
            self.t0 = self.clock()
            self.run.header.update({"area": self.area, "started_at": self.t0, "live": True})
            self.offsets, self.pending, self.error, self.last_fix = [], None, None, None
            self._corner, self.near_count = None, 0
            self._client(client_t)
            start = {}
            if fix:
                hn = self.G.nodes[self.home.node]
                start = {"start_fix": fix,
                         "start_fix_to_door_m": round(haversine_m(fix["lon"], fix["lat"], hn["x"], hn["y"]), 1)}
                self.run.header.update(start)
            self._event("start", **start)
            self._decide()
            return self.view()

    def resume(self, path: Path) -> dict:
        """Pick a walk back up from its saved log after a server restart: replay
        every decision against the graph to rebuild the run state."""
        with self.lock:
            log = json.loads(Path(path).read_text())
            h = log["header"]
            if h.get("graph_hash") != self.G.graph.get("graph_hash") or h["home_node"] != self.home.node:
                raise WalkError("that log was made on a different graph or door")
            run = Run(self.G, self.home, self.chooser, RunConfig(**h["config"]),
                      run_id=h["run_id"], dist_home=self.dist_home, sim=False)
            walked = {p["decision"]: p for p in log["path"]}
            self.run, self.t0, self.pending, self._corner = run, h["started_at"], None, None
            self.error, self.near_count, self.offsets = None, 0, []
            for rec in log["decisions"]:
                run.s.elapsed_s = rec["t_arrived"]
                run.corner()                      # rebuilds this corner's options
                run.decisions.append(rec)
                if rec["i"] in walked:
                    taken = rec["override"]["taken"] if rec.get("override") else rec["choice"]
                    run.apply(rec, taken)
                    run.path[-1] = walked[rec["i"]]
                elif rec["choice"] == ARRIVE_HOME:
                    run.apply(rec, ARRIVE_HOME)
                else:
                    saved = log.get("pending") or {}
                    self.pending = {"rec": rec, "taken": saved.get("taken", rec["choice"]),
                                    "override": saved.get("override"),
                                    "t_decided": saved.get("t_decided", rec["t_jev_returned"])}
            run.header, run.ui_events, run.gps = h, log["ui_events"], log["gps"]
            end_reason = log["end"]["reason"]
            if end_reason:
                run.s.ended, self.phase = end_reason, "ended"
            elif self.pending:
                self.phase = "walking"
                self._event("resumed")
            else:
                self._event("resumed")
                self._decide()                    # crashed between arrival and decision
            self.save()
            return self.view()

    def end(self, *, reason: str = "manual", client_t: float | None = None) -> dict:
        with self.lock:
            if self.run and self.phase != "ended":
                self._client(client_t)
                self.run.s.elapsed_s = self._now()
                self.run.s.ended = f"manual:{reason}"
                self._event("run_end", reason=f"manual:{reason}")
                self.phase = "ended"
                self.save()
            return self.view()

    def sync(self, *, client_t: float | None = None, label: str = "clap") -> dict:
        """The three-count clap on camera: a shared zero for footage and log."""
        with self.lock:
            if not self.run:
                raise WalkError("start the walk before the sync clap")
            self._client(client_t)
            self._event("sync_marker", label=label, client_t=client_t)
            self.run.header.setdefault("sync_marker_t", round(self._now(), 3))
            self.save()
            return self.view()

    # ---------------------------------------------------------------- deciding

    def _decide(self) -> None:
        run = self.run
        run.s.elapsed_s = self._now()
        if self._corner is None:
            t_arrived = self._now()
            state, options, dropped = run.corner()
            self._corner = (t_arrived, self._now(), state, options, dropped)
        t_arrived, t_menu, state, options, dropped = self._corner
        public = [o.public() for o in options]
        t_sent = self._now()
        choice, err = None, None
        for _ in range(2):  # one immediate retry before bothering Keanan
            try:
                choice = self.chooser.choose(state, public)
                if choice.choice not in run._options:
                    raise ChooserError(f"chose {choice.choice!r}, not on the menu")
                break
            except ChooserError as e:
                choice, err = None, str(e)
        if choice is None:
            self.phase, self.error = "chooser_error", err
            self._event("chooser_error", error=err)
            self.save()
            return
        times = {"t_arrived": t_arrived, "t_menu_built": t_menu,
                 "t_jev_sent": t_sent, "t_jev_returned": self._now()}
        rec = run.record(state, options, dropped, choice, times)
        self._corner, self.error = None, None
        self.pending = {"rec": rec, "taken": choice.choice, "override": None,
                        "t_decided": self._now()}
        self.near_count = 0
        if choice.choice == ARRIVE_HOME:
            self._event("arrive_home_accepted")
            run.apply(rec, ARRIVE_HOME)
            self.phase = "ended"
        else:
            if ARRIVE_HOME in run._options:
                self._event("arrive_home_declined")
            self.phase = "walking"
        self.save()

    def retry(self, *, client_t: float | None = None) -> dict:
        with self.lock:
            if self.phase != "chooser_error":
                raise WalkError("nothing to retry")
            self._client(client_t)
            self._event("retry")
            self._decide()
            return self.view()

    # ---------------------------------------------------------------- walking

    def gps(self, fixes: list[dict], *, client_t: float | None = None) -> dict:
        with self.lock:
            self._client(client_t)
            arrived = False
            for f in fixes:
                rec = self._log_fix(f)
                if self.phase == "walking" and not arrived:
                    arrived = self._check_arrival(rec)
            if arrived:
                self._arrive(manual=False)
            elif self._fixes_since_save >= SAVE_EVERY_FIXES:
                self.save()
            return self.view()

    def _log_fix(self, f: dict) -> dict:
        node, near_m = self.index.nearest(f["lon"], f["lat"])
        rec = {
            "t": round(self._now(), 3) if self.run else None,
            "t_client": f.get("t"),
            "lat": f["lat"], "lon": f["lon"],
            "accuracy": f.get("accuracy"), "heading": f.get("heading"), "speed": f.get("speed"),
            "near_node": node, "near_m": round(near_m, 1),
        }
        self.last_fix = {**rec, "t_server": self.clock()}
        if self.run and self.phase != "ended":
            self.run.gps.append(rec)
            self._fixes_since_save += 1
        return rec

    def _check_arrival(self, fix: dict) -> bool:
        acc = fix.get("accuracy")
        if acc is not None and acc > ARRIVE_MAX_ACCURACY_M:
            return False
        if self._now() - self.pending["t_decided"] < MIN_WALK_S:
            self.near_count = 0
            return False
        end = self.G.nodes[self._target().end]
        if haversine_m(fix["lon"], fix["lat"], end["x"], end["y"]) <= ARRIVE_RADIUS_M:
            self.near_count += 1
        else:
            self.near_count = 0
        return self.near_count >= ARRIVE_FIXES

    def arrive(self, *, client_t: float | None = None) -> dict:
        """The I'm here button."""
        with self.lock:
            self._client(client_t)
            if self.phase == "walking" and self._now() - self.pending["t_decided"] >= MANUAL_DEBOUNCE_S:
                self._arrive(manual=True)
            return self.view()

    def _arrive(self, manual: bool) -> None:
        p = self.pending
        self.run.s.elapsed_s = self._now()
        self._event("arrival", manual=manual, node=self._target().end)
        self.run.apply(p["rec"], p["taken"], p["override"])
        if self.run.s.ended:
            self.phase = "ended"
            self.save()
            return
        self._decide()

    def override(self, option_id: str, reason: str, *, client_t: float | None = None) -> dict:
        """Can't: the exit Jev picked is unsafe or not there. Keanan picks the exit he
        actually takes; the run continues from that one."""
        with self.lock:
            if self.phase != "walking":
                raise WalkError("overrides only apply while walking a choice")
            opt = self.run._options.get(option_id)
            if opt is None or opt.kind == ARRIVE_HOME:
                raise WalkError(f"{option_id!r} is not an exit at this corner")
            if reason not in OVERRIDE_REASONS:
                raise WalkError(f"reason must be one of {OVERRIDE_REASONS}")
            self._client(client_t)
            p = self.pending
            if option_id == p["rec"]["choice"]:
                p["taken"], p["override"] = option_id, None   # changed his mind back
            else:
                p["taken"] = option_id
                p["override"] = {"jev_choice": p["rec"]["choice"], "taken": option_id,
                                 "reason": reason, "t": round(self._now(), 3)}
            p["t_decided"] = self._now()
            self.near_count = 0
            self._event("override_selected", taken=option_id, reason=reason)
            self.save()
            return self.view()

    # ---------------------------------------------------------------- output

    def save(self) -> Path | None:
        if not self.run:
            return None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{self.run.header['run_id']}.json"
        log = self.run.log()
        log["header"]["log_saved_at"] = self.clock()
        if self.pending and self.phase == "walking":
            log["pending"] = {k: v for k, v in self.pending.items() if k != "rec"}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(log))
        os.replace(tmp, path)
        self._fixes_since_save = 0
        return path

    def log(self) -> dict | None:
        with self.lock:
            return self.run.log() if self.run else None

    def view(self) -> dict:
        """Everything the phone screen needs, nothing it doesn't."""
        with self.lock:
            v = {"phase": self.phase, "area": self.area, "error": self.error,
                 "mode": self.run.cfg.mode if self.run else self.cfg.mode,
                 "run_id": self.run.header["run_id"] if self.run else None,
                 "gps": self._gps_view()}
            if not self.run:
                return v
            run, s = self.run, self.run.s
            elapsed = self._now() if self.phase != "ended" else s.elapsed_s
            progress = 0.0
            walk = None
            if self.phase == "walking":
                trav = self._target()
                progress, to_corner = self._progress(trav)
                walk = {"street": run._options[self.pending["taken"]].street,
                        "block_m": round(trav.length_m), "progress_m": round(progress),
                        "to_corner_m": round(to_corner) if to_corner is not None else None,
                        "near_count": self.near_count}
            home_m, home_rel, home_abs = self._compass()
            v.update({
                "decision": self._decision_view(),
                "walk": walk,
                "hud": {
                    "walked_m": round(s.walked_m + progress),
                    "target_m": round(run.cfg.target_m),
                    "elapsed_s": round(elapsed, 1),
                    "time_cap_s": run.cfg.time_cap_min * 60,
                    "home_m": round(home_m) if home_m is not None else None,
                    "home_rel_deg": round(home_rel) if home_rel is not None else None,
                    "home_abs": home_abs,
                    "calls": len(run.decisions),
                    "cost_usd": round(sum(d.get("cost_usd") or 0 for d in run.decisions), 6),
                    "repeats": s.repeats,
                    "overrides": sum(1 for d in run.decisions if d.get("override")),
                },
            })
            if self.phase == "ended":
                v["end"] = {"reason": s.ended, "score": run.log()["score"]}
            return v

    def _gps_view(self) -> dict | None:
        if not self.last_fix:
            return None
        return {"accuracy_m": self.last_fix.get("accuracy"),
                "age_s": round(self.clock() - self.last_fix["t_server"], 1)}

    def _fresh_fix(self) -> dict | None:
        f = self.last_fix
        if f and self.clock() - f["t_server"] < 30:
            return f
        return None

    def _progress(self, trav) -> tuple[float, float | None]:
        """Metres along the block being walked, and metres left to its corner."""
        f = self._fresh_fix()
        if not f:
            return 0.0, None
        end = self.G.nodes[trav.end]
        proj = LocalProjection(end["x"], end["y"])
        line = LineString([proj.to_xy(*c) for c in trav.coords])
        along = line.project(Point(proj.to_xy(f["lon"], f["lat"])))
        to_corner = haversine_m(f["lon"], f["lat"], end["x"], end["y"])
        return min(along, trav.length_m), to_corner

    def _compass(self) -> tuple[float | None, float | None, bool]:
        """Distance and bearing home from where Keanan is now, the bearing relative
        to the way he's walking (absolute when there's no heading yet)."""
        s = self.run.s
        hn = self.G.nodes[self.home.node]
        f = self._fresh_fix()
        here = (f["lon"], f["lat"]) if f else self.run.here
        dist = haversine_m(here[0], here[1], hn["x"], hn["y"])
        if dist < 1:
            return dist, None, False
        to_home = bearing_deg(here[0], here[1], hn["x"], hn["y"])
        heading = None
        if self.phase == "walking":
            end = self.G.nodes[self._target().end]
            if haversine_m(here[0], here[1], end["x"], end["y"]) > 10:
                heading = bearing_deg(here[0], here[1], end["x"], end["y"])
            else:
                heading = arrival_heading(self.G, *self._target().last_hop)
        elif s.arriving:
            heading = arrival_heading(self.G, *s.arriving)
        if heading is None:
            return dist, to_home, True
        return dist, wrap180(to_home - heading), False

    def _decision_view(self) -> dict | None:
        if not self.run.decisions:
            return None
        rec = self.run.decisions[-1]
        taken = self.pending["taken"] if self.pending and self.pending["rec"] is rec else rec["choice"]
        menu = {o["id"]: o for o in rec["menu"]}
        chosen = menu[taken]
        options = []
        for o in rec["menu"]:
            label = "Arrive home" if o["kind"] == ARRIVE_HOME else _label(o)
            options.append({"id": o["id"], "kind": o["kind"], "label": label,
                            "direction": o.get("direction"), "angle_deg": o.get("angle_deg"),
                            "street": o.get("street", ""), "already_walked": o.get("already_walked", False),
                            "p": rec["probabilities"].get(o["id"], 0.0),
                            "chosen": o["id"] == rec["choice"], "taken": o["id"] == taken})
        return {
            "i": rec["i"], "choice": rec["choice"], "taken": taken,
            "override": taken != rec["choice"],
            "first_corner": rec["menu"][0].get("back", False) is None,
            "kind": chosen["kind"],
            "direction": chosen.get("direction"), "angle_deg": chosen.get("angle_deg"),
            "street": chosen.get("street", ""), "block_length_m": chosen.get("block_length_m"),
            "top_p": rec["probabilities"].get(rec["choice"], 0.0),
            "confidence": rec.get("confidence"),
            "latency_ms": rec["latency_ms"],
            "options": options,
        }


def _label(o: dict) -> str:
    street = o.get("street") or "path"
    return f"{o['direction']} · {street}"

