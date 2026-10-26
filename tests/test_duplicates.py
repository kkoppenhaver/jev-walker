"""Divided roads and separately mapped bike lanes must not show up as two exits."""

from jev_walker.geo import LocalProjection
from jev_walker.graph import _merge_parallel_edges
from jev_walker.menu import build_menu

from .conftest import ORIGIN, grid


def _offset_edge(G, u, v, dy, name, highway, bulge=0.0):
    """A u-v edge running `dy` metres north of the straight line (a parallel lane)."""
    proj = LocalProjection(*ORIGIN)
    ux, uy = proj.to_xy(G.nodes[u]["x"], G.nodes[u]["y"])
    vx, vy = proj.to_xy(G.nodes[v]["x"], G.nodes[v]["y"])
    pts = [(ux, uy), (ux + 15, uy + dy + bulge), (vx - 15, vy + dy + bulge), (vx, vy)]
    coords = [proj.to_lonlat(x, y) for x, y in pts]
    length = sum(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 for (x1, y1), (x2, y2) in zip(pts, pts[1:]))
    G.add_edge(u, v, length=length, name=name, highway=highway, coords=coords)


def test_parallel_bike_lane_merges_into_street():
    G = grid()
    _offset_edge(G, 5, 6, 8, "W 1 St", "cycleway")
    assert G.number_of_edges(5, 6) == 2
    assert _merge_parallel_edges(G) == 1
    (d,) = G[5][6].values()
    assert d["highway"] == "residential"


def test_distinct_route_between_same_corners_stays():
    G = grid()
    _offset_edge(G, 5, 6, 0, "Crescent", "residential", bulge=80)  # loops 80 m out
    assert _merge_parallel_edges(G) == 0
    assert G.number_of_edges(5, 6) == 2


def test_divided_road_twins_fold_into_one_exit():
    # Node 5 gets a second eastbound carriageway ending at a new node 2 m from node 6.
    G = grid()
    proj = LocalProjection(*ORIGIN)
    lon, lat = proj.to_lonlat(240, 122)
    G.add_node(50, x=lon, y=lat)
    _offset_edge(G, 5, 50, 4, "W 1 St", "residential")
    _offset_edge(G, 50, 7, 0, "W 1 St", "residential")
    _offset_edge(G, 50, 10, 0, "N 2 Ave", "residential")
    opts, dropped = build_menu(G, 5, (1, 5, 0), set())
    rights = [o for o in opts if o.direction == "right"]
    assert len(rights) == 1 and rights[0].street == "W 1 St"
    assert len(dropped) == 1 and dropped[0]["dropped_for"] == "twin_of_right"
    twin = rights[0].traversal.twin_edges
    assert twin
    # Having walked the twin carriageway, this exit counts as already walked.
    opts2, _ = build_menu(G, 5, (1, 5, 0), set(twin))
    assert next(o for o in opts2 if o.direction == "right").already_walked
