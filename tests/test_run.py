from jev_walker.choosers import Choice, Chooser, HeuristicChooser, RandomChooser, normalise
from jev_walker.graph import HOME_NODE
from jev_walker.menu import ARRIVE_HOME
from jev_walker.run import MILE_M, Run, RunConfig, dist_to_home


class Scripted(Chooser):
    """Picks options by direction label from a script, then arrive_home if offered."""
    name = "scripted"

    def __init__(self, script):
        self.script = list(script)

    def choose(self, state, options):
        ids = [o["id"] for o in options]
        if any(o["id"] == ARRIVE_HOME for o in options):
            pick = ARRIVE_HOME
        else:
            want = self.script.pop(0)
            pick = next(o["id"] for o in options if o["kind"] == "exit" and o["direction"] == want)
        return Choice(pick, normalise({pick: 1.0}, ids))


def test_door_is_inserted_mid_block(grid_home):
    G, home = grid_home
    assert home.node == HOME_NODE
    assert sorted(v for _, v in G.edges(HOME_NODE)) == [12, 13]
    assert 3 < home.snap_m < 5


def test_square_loop_closes_clean_and_scores(grid_home):
    G, home = grid_home
    # Leave east, go round one block anticlockwise (left, left, left), come back
    # along the west half of the door's block.
    cfg = RunConfig(target_m=480, clean_lo_m=400, clean_hi_m=600)
    run = Run(G, home, Scripted(["east", "left", "left", "left", "left"]), cfg,
              dist_home=dist_to_home(G, home))
    log = run.run_to_end()
    s = log["score"]
    assert log["end"]["reason"] == "home"
    assert s["loop_closed"] and s["repeated_segments"] == 0 and s["clean_finish"]
    assert abs(log["end"]["walked_m"] - 480) < 1
    assert s["decisions"] == 6  # door, 4 corners, door again
    assert log["decisions"][-1]["choice"] == ARRIVE_HOME
    assert log["decisions"][0]["menu"][0]["back"] is None
    # first menu: two exits along the block, absolute directions
    assert sorted(o["direction"] for o in log["decisions"][0]["menu"]) == ["east", "west"]


def test_out_and_back_counts_repeats(grid_home):
    G, home = grid_home
    run = Run(G, home, Scripted(["east", "back"]), RunConfig())
    log = run.run_to_end()
    assert log["end"]["reason"] == "home"
    assert log["score"]["repeated_segments"] == 1
    assert not log["score"]["clean_finish"]


def test_arrive_home_not_offered_at_start(grid_home):
    G, home = grid_home
    run = Run(G, home, RandomChooser(0))
    _, options, _ = run.corner()
    assert all(o.kind == "exit" for o in options)


def test_distance_cap_ends_run(grid_home):
    G, home = grid_home

    class NeverHome(RandomChooser):
        def choose(self, state, options):
            exits = [o for o in options if o["kind"] == "exit"]
            return super().choose(state, exits)

    cfg = RunConfig()
    log = Run(G, home, NeverHome(1), cfg).run_to_end()
    assert log["end"]["reason"] in ("distance_cap", "time_cap")
    assert log["end"]["walked_m"] >= cfg.cap_m or log["end"]["elapsed_min"] >= cfg.time_cap_min
    assert log["end"]["walked_m"] < cfg.cap_m + 200  # stops within a block of the cap


def test_probabilities_are_full_vectors(grid_home):
    G, home = grid_home
    log = Run(G, home, HeuristicChooser(0), RunConfig(target_m=2000)).run_to_end()
    for d in log["decisions"]:
        assert set(d["probabilities"]) == {o["id"] for o in d["menu"]}
        assert abs(sum(d["probabilities"].values()) - 1) < 1e-3


def test_oracle_marks_closable(grid_home):
    G, home = grid_home
    run = Run(G, home, RandomChooser(0), dist_home=dist_to_home(G, home))
    rec = run.step()
    assert set(rec["oracle"]) == {o["id"] for o in rec["menu"]}
    assert all(v["closable"] for v in rec["oracle"].values())  # 5 mi budget on a tiny grid


def test_mile_constant():
    assert round(5 * MILE_M) == 8047
