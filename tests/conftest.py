"""Synthetic street graphs, so menu and run-loop tests don't need the OSM cache."""

import networkx as nx
import pytest

from jev_walker.geo import LocalProjection
from jev_walker.graph import insert_home

ORIGIN = (-87.65, 41.958)  # somewhere in Buena Park


def grid(nx_=4, ny=4, block=120.0, name_x="W {} St", name_y="N {} Ave"):
    """An nx_ by ny street grid with `block`-metre blocks, node id = j*nx_ + i,
    i growing east, j growing north."""
    proj = LocalProjection(*ORIGIN)
    G = nx.MultiGraph()
    for j in range(ny):
        for i in range(nx_):
            lon, lat = proj.to_lonlat(i * block, j * block)
            G.add_node(j * nx_ + i, x=lon, y=lat)
    for j in range(ny):
        for i in range(nx_):
            n = j * nx_ + i
            if i + 1 < nx_:
                _add(G, n, n + 1, block, name_x.format(j))
            if j + 1 < ny:
                _add(G, n, n + nx_, block, name_y.format(i))
    return G


def _add(G, u, v, length, name, highway="residential"):
    cu = (G.nodes[u]["x"], G.nodes[u]["y"])
    cv = (G.nodes[v]["x"], G.nodes[v]["y"])
    G.add_edge(u, v, length=length, name=name, highway=highway, coords=[cu, cv])


@pytest.fixture
def grid4():
    G = grid()
    G.graph["home_node"] = None
    return G


@pytest.fixture
def grid_home():
    """5x5 grid with the door in the middle of the block between nodes 12 and 13."""
    G = grid(5, 5)
    proj = LocalProjection(*ORIGIN)
    lon, lat = proj.to_lonlat(2 * 120 + 50, 2 * 120 + 4)  # 4 m off the centreline
    home = insert_home(G, lon, lat)
    G.graph["graph_hash"] = "test"
    return G, home
