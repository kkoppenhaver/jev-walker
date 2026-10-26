"""Three corners checked by eye against the real Buena Park graph."""

import pytest

from jev_walker.geo import bearing_deg
from jev_walker.graph import AREAS, DEFAULT_GRAPH_PATH, load_graph
from jev_walker.menu import build_menu, exit_bearing

pytestmark = pytest.mark.skipif(not DEFAULT_GRAPH_PATH.exists(), reason="graph cache not built")


@pytest.fixture(scope="module")
def G():
    G, _ = load_graph(paths="all")
    return G


@pytest.fixture(scope="module")
def SF():
    if not AREAS["sf"].graph_path.exists():
        pytest.skip("SF graph cache not built")
    G, _ = load_graph(paths="named", area="sf")
    return G


def corner(G, a, b):
    hits = [n for n in G if {a, b} <= {name for *_, d in G.edges(n, data=True)
                                        for name in d["name"].split(" / ")}]
    assert len(hits) == 1, (a, b, hits)
    return hits[0]


def arriving_from(G, node, street, heading):
    """The incident edge on `street` whose far end lies opposite `heading`, i.e. the
    edge you walk in on when travelling `heading` (0 = north, 90 = east)."""
    nd = G.nodes[node]
    for _, v, k, d in G.edges(node, keys=True, data=True):
        if street in d["name"].split(" / "):
            b = bearing_deg(G.nodes[v]["x"], G.nodes[v]["y"], nd["x"], nd["y"])
            if abs((b - heading + 180) % 360 - 180) < 30:
                return (v, node, k)
    raise AssertionError(f"no {street} edge arriving heading {heading}")


def check_four_way(G, node, along, cross, heading):
    opts, _ = build_menu(G, node, arriving_from(G, node, along, heading), set())
    d = {o.direction: o for o in opts}
    assert set(d) == {"straight", "left", "right", "back"}
    assert d["straight"].street == along and d["back"].street == along and d["back"].back
    assert d["left"].street == cross and d["right"].street == cross
    left_b = exit_bearing(G, *d["left"].traversal.first_hop)
    assert abs((left_b - (heading - 90) + 180) % 360 - 180) < 30


def test_montrose_and_clarendon(G):
    n = corner(G, "W Montrose Ave", "N Clarendon Ave")
    check_four_way(G, n, "N Clarendon Ave", "W Montrose Ave", heading=0)    # northbound
    check_four_way(G, n, "W Montrose Ave", "N Clarendon Ave", heading=270)  # westbound


def test_irving_park_and_sheridan(G):
    n = corner(G, "W Irving Park Rd", "N Sheridan Rd")
    check_four_way(G, n, "N Sheridan Rd", "W Irving Park Rd", heading=0)
    check_four_way(G, n, "W Irving Park Rd", "N Sheridan Rd", heading=90)


def test_buena_and_hazel_t_junction(G):
    # Hazel runs north from Buena: eastbound on Buena it's a left turn, no right.
    n = corner(G, "W Buena Ave", "N Hazel St")
    opts, _ = build_menu(G, n, arriving_from(G, n, "W Buena Ave", heading=90), set())
    d = {o.direction: o for o in opts}
    assert set(d) == {"straight", "left", "back"}
    assert d["left"].street == "N Hazel St" and d["straight"].street == "W Buena Ave"


def test_sf_valencia_and_20th(SF):
    n = corner(SF, "Valencia St", "20th St")
    check_four_way(SF, n, "Valencia St", "20th St", heading=0)
    check_four_way(SF, n, "20th St", "Valencia St", heading=90)


def test_sf_haight_and_divisadero(SF):
    n = corner(SF, "Haight St", "Divisadero St")
    check_four_way(SF, n, "Divisadero St", "Haight St", heading=0)
    check_four_way(SF, n, "Haight St", "Divisadero St", heading=270)
