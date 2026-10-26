"""A whole live walk on the synthetic grid: fake clock, GPS fixes along the blocks."""

import json

import pytest

from jev_walker.choosers import Choice, Chooser, ChooserError, normalise
from jev_walker.geo import point_along
from jev_walker.live import LiveWalk, WalkError
from jev_walker.menu import ARRIVE_HOME
from jev_walker.run import RunConfig


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Script(Chooser):
    """Takes exits by direction from a script; arrive_home whenever offered."""
    name = "script"

    def __init__(self, script, fail_first=0):
        self.script, self.fail = list(script), fail_first

    def choose(self, state, options):
        if self.fail:
            self.fail -= 1
            raise ChooserError("HTTP 503: overloaded")
        ids = [o["id"] for o in options]
        if ARRIVE_HOME in ids:
            pick = ARRIVE_HOME
        else:
            want = self.script.pop(0)
            pick = next(o["id"] for o in options if o.get("direction") == want)
        return Choice(pick, normalise({pick: 0.7, ids[0]: 0.3}, ids), latency_ms=120, cost_usd=0.00002)


@pytest.fixture
def walk(grid_home, tmp_path):
    G, home = grid_home

    def make(script, **kw):
        clock = Clock()
        w = LiveWalk(G, home, Script(script, **kw), RunConfig(target_m=480, clean_lo_m=400, clean_hi_m=600),
                     area="test", log_dir=tmp_path, clock=clock)
        return w, clock
    return make


def walk_block(w, clock, step_m=10.0, accuracy=5.0):
    """Feed fixes every 7 s along the block currently being walked."""
    trav = w._target()
    d, v = 0.0, None
    while d <= trav.length_m + step_m and w.phase == "walking" and w._target() is trav:
        lon, lat = point_along(trav.coords, min(d, trav.length_m))
        clock.t += 7
        v = w.gps([{"lat": lat, "lon": lon, "accuracy": accuracy, "t": clock.t * 1000}],
                  client_t=clock.t * 1000 - 400)
        d += step_m
    return v


def test_full_loop_by_gps(walk, tmp_path):
    w, clock = walk(["east", "left", "left", "left", "left"])
    v = w.start(client_t=clock.t * 1000, fix={"lat": 41.958, "lon": -87.65, "accuracy": 4})
    assert v["phase"] == "walking" and v["decision"]["i"] == 0 and v["decision"]["first_corner"]
    assert v["decision"]["direction"] == "east"
    for _ in range(5):
        v = walk_block(w, clock)
    assert v["phase"] == "ended" and v["end"]["reason"] == "home"
    assert v["end"]["score"]["clean_finish"]
    log = json.loads((tmp_path / f"{v['run_id']}.json").read_text())
    assert len(log["decisions"]) == 6
    assert all(e["manual"] is False for e in log["ui_events"] if e["type"] == "arrival")
    assert log["gps"] and log["gps"][0]["near_node"] is not None
    assert log["header"]["clock_offset_s"] == pytest.approx(0.4)
    d1 = log["decisions"][1]
    assert d1["t_arrived"] <= d1["t_jev_sent"] <= d1["t_jev_returned"]


def test_no_arrival_from_the_corner_just_left(walk):
    w, clock = walk(["east", "left"])
    w.start(client_t=clock.t * 1000)
    trav = w._target()
    start = trav.coords[0]
    for _ in range(3):  # standing at the start, then a fix at the far corner too soon
        clock.t += 1
        w.gps([{"lat": start[1], "lon": start[0], "accuracy": 5}])
    end = trav.coords[-1]
    w.gps([{"lat": end[1], "lon": end[0], "accuracy": 5}])
    assert len(w.run.decisions) == 1


def test_poor_accuracy_does_not_arrive(walk):
    w, clock = walk(["east", "left"])
    w.start()
    v = walk_block(w, clock, accuracy=60)
    assert v["phase"] == "walking" and len(w.run.decisions) == 1


def test_im_here_button(walk):
    w, clock = walk(["east", "left", "left"])
    w.start()
    clock.t += 30
    v = w.arrive(client_t=clock.t * 1000)
    assert v["decision"]["i"] == 1 and v["decision"]["direction"] == "left"
    arrivals = [e for e in w.run.ui_events if e["type"] == "arrival"]
    assert len(arrivals) == 1 and arrivals[0]["manual"] is True
    assert w.arrive()["decision"]["i"] == 1    # double tap: ignored
    clock.t += 40
    assert w.arrive()["decision"]["i"] == 2    # next corner
    assert len([e for e in w.run.ui_events if e["type"] == "arrival"]) == 2


def test_override_is_logged_and_followed(walk):
    w, clock = walk(["east", "left", "left", "left", "left"])
    w.start()
    clock.t += 20
    walk_block(w, clock)                       # at the first corner, Jev says left
    dec = w.view()["decision"]
    straight = next(o for o in dec["options"] if o["direction"] == "straight")
    v = w.override(straight["id"], "sidewalk_closed")
    assert v["decision"]["override"] and v["decision"]["taken"] == straight["id"]
    v = walk_block(w, clock)
    log = w.log()
    assert log["decisions"][1]["override"]["reason"] == "sidewalk_closed"
    assert log["path"][1]["street"] == straight["street"]
    assert log["score"]["overrides"] == 1
    with pytest.raises(WalkError):
        w.override("Z", "other")


def test_chooser_error_then_retry(walk):
    w, clock = walk(["east"], fail_first=2)   # both tries of the first call fail
    v = w.start()
    assert v["phase"] == "chooser_error" and "503" in v["error"]
    v = w.retry()
    assert v["phase"] == "walking" and v["decision"]["i"] == 0


def test_view_hud_and_compass(walk):
    w, clock = walk(["east", "left"])
    w.start()
    clock.t += 10
    trav = w._target()
    lon, lat = point_along(trav.coords, 30)
    v = w.gps([{"lat": lat, "lon": lon, "accuracy": 5}])
    assert v["walk"]["progress_m"] == pytest.approx(30, abs=2)
    assert v["hud"]["walked_m"] == pytest.approx(30, abs=2)
    assert v["hud"]["home_m"] == pytest.approx(30, abs=2)
    assert abs(v["hud"]["home_rel_deg"]) == 180       # walking away: home dead behind
    assert v["hud"]["calls"] == 1 and v["hud"]["cost_usd"] == pytest.approx(0.00002)


def test_sync_and_manual_end(walk):
    w, clock = walk(["east"])
    with pytest.raises(WalkError):
        w.sync()
    w.start()
    clock.t += 3
    w.sync(client_t=clock.t * 1000)
    assert w.run.header["sync_marker_t"] == pytest.approx(3)
    v = w.end(reason="rain")
    assert v["phase"] == "ended" and v["end"]["reason"] == "manual:rain"


def test_resume_after_restart(walk, grid_home, tmp_path):
    w, clock = walk(["east", "left", "left", "left", "left"])
    w.start()
    walk_block(w, clock)
    walk_block(w, clock)                          # two corners done, walking the third block
    path = w.save()
    before = w.view()
    # The server dies. A new process with a fresh chooser picks the walk back up.
    G, home = grid_home
    w2 = LiveWalk(G, home, Script(["left", "left"]), RunConfig(target_m=480, clean_lo_m=400, clean_hi_m=600),
                  area="test", log_dir=tmp_path, clock=clock)
    v = w2.resume(path)
    assert v["phase"] == "walking" and v["decision"]["i"] == before["decision"]["i"]
    assert v["hud"]["walked_m"] == before["hud"]["walked_m"]
    assert w2.run.s.walk_counts == w.run.s.walk_counts
    for _ in range(3):
        v = walk_block(w2, clock)
    assert v["phase"] == "ended" and v["end"]["score"]["clean_finish"]
    assert any(e["type"] == "resumed" for e in w2.run.ui_events)
