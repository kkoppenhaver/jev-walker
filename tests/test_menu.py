import json

import pytest

from jev_walker.geo import LocalProjection
from jev_walker.menu import ARRIVE_HOME, MAX_OPTIONS, build_menu, build_state, edge_id

from .conftest import ORIGIN, _add


def by_direction(options):
    return {o.direction: o for o in options}


def test_four_way_from_the_south(grid4):
    # Node 5 is (1, 1); arriving northbound from node 1.
    opts, dropped = build_menu(grid4, 5, (1, 5, 0), {edge_id(1, 5, 0)})
    d = by_direction(opts)
    assert set(d) == {"straight", "left", "right", "back"}
    assert d["straight"].traversal.end == 9
    assert d["left"].traversal.end == 4    # west
    assert d["right"].traversal.end == 6   # east
    assert d["back"].traversal.end == 1 and d["back"].back is True
    assert d["back"].already_walked is True
    assert not any(o.back for o in opts if o.direction != "back")
    assert d["left"].angle_deg == pytest.approx(-90, abs=1)
    assert d["right"].angle_deg == pytest.approx(90, abs=1)
    assert [o.id for o in opts] == list("ABCD")
    assert dropped == []


def test_t_junction_and_dead_end(grid4):
    # Corner node 0 has two exits; cut node 3's west link to make 3 a dead end.
    grid4.remove_edge(2, 3)
    opts, _ = build_menu(grid4, 7, (6, 7, 0), set())
    d = by_direction(opts)
    assert set(d) == {"left", "right", "back"}
    assert d["right"].dead_end is True       # south to node 3, now degree 1
    assert d["left"].dead_end is False


def test_first_corner_uses_absolute_directions(grid4):
    opts, _ = build_menu(grid4, 5, None, set())
    assert sorted(o.direction for o in opts) == ["east", "north", "south", "west"]
    assert all(o.back is None for o in opts)


def test_already_walked_either_direction(grid4):
    walked = {edge_id(6, 5, 0)}  # walked 6 -> 5 earlier
    opts, _ = build_menu(grid4, 5, (1, 5, 0), walked)
    assert by_direction(opts)["right"].already_walked is True
    assert by_direction(opts)["left"].already_walked is False


def test_six_option_cap_keeps_cardinal_exits(grid4):
    # Add three diagonal spokes from node 5 to new nodes: 7 exits total.
    proj = LocalProjection(*ORIGIN)
    for n, (dx, dy) in zip((100, 101, 102), ((60, 60), (-60, 60), (60, -50))):
        lon, lat = proj.to_lonlat(120 + dx, 120 + dy)
        grid4.add_node(n, x=lon, y=lat)
        _add(grid4, 5, n, 85, f"Diag {n}", highway="footway")
    opts, dropped = build_menu(grid4, 5, (1, 5, 0), set())
    assert len(opts) == MAX_OPTIONS
    assert len(dropped) == 1
    assert {"straight", "left", "right", "back"} <= {o.direction for o in opts}


def test_arrive_home_option(grid4):
    opts, _ = build_menu(grid4, 5, (1, 5, 0), set(), offer_arrive_home=True)
    assert opts[-1].id == ARRIVE_HOME and opts[-1].public() == {"id": ARRIVE_HOME, "kind": ARRIVE_HOME}


FORBIDDEN = {"lat", "lon", "x", "y", "coords", "to_node", "edges", "node", "lonlat", "geometry"}


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_public_view_leaks_no_map(grid4):
    opts, _ = build_menu(grid4, 5, (1, 5, 0), set(), offer_arrive_home=True)
    state = build_state(walked_m=100, target_m=8047, repeats=0, last_turns=["left"],
                        elapsed_min=2, mode="compass", here=(-87.65, 41.958),
                        home=(-87.651, 41.957), heading=0.0)
    payload = {"state": state, "options": [o.public() for o in opts]}
    assert not (set(_keys(payload)) & FORBIDDEN)
    # and no raw coordinates hiding in values either
    assert "-87." not in json.dumps(payload) and "41.9" not in json.dumps(payload)


def test_state_compass_and_pure():
    common = dict(walked_m=3410, target_m=8047, repeats=0, last_turns=["straight", "right", "straight", "left"],
                  elapsed_min=41, here=(-87.65, 41.958), heading=0.0)
    # home due south of a northbound walker: directly behind
    home = (-87.65, 41.947)
    s = build_state(mode="compass", home=home, **common)
    assert s["remaining_m"] == 8047 - 3410
    assert s["home_bearing_relative"]["label"] == "behind"
    assert abs(s["home_bearing_relative"]["deg"]) == 180
    assert 1200 < s["home_distance_m"] < 1250
    assert s["last_turns"] == ["right", "straight", "left"]
    p = build_state(mode="pure", home=home, **common)
    assert "home_distance_m" not in p and "home_bearing_relative" not in p
