"""Street graph: fetch, filter, simplify, cache, load.

The harness knows the whole graph; Jev never sees it. The graph exists only so the
harness can tell when the walker is at a fork and what the exits are.

Build pipeline (run once, before any walk; no live map calls on the walk):
  1. Overpass pull of walkable ways in the Buena Park bbox with a custom filter.
     OSMnx's stock `walk` filter drops streets tagged sidewalk=separate, which in
     Chicago is most of them, so we name the highway classes ourselves.
  2. Separately-mapped sidewalks, crossings and traffic islands are excluded: the
     street centreline stands in for its sidewalks, otherwise every corner turns
     into a cluster of nodes and "one decision per intersection" breaks.
  3. Largest connected component, simplify (degree-2 nodes merged), prune short
     footway stubs, consolidate nodes within a few metres (divided carriageways,
     paths that meet a street just shy of the corner).
  4. Save as GraphML. Loading converts to an undirected MultiGraph, inserts the
     front door as a node, and breaks self-loops.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
from shapely.geometry import LineString, Point

from .geo import LocalProjection, haversine_m

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME_PATH = ROOT / "config" / "home.json"
LOCAL_HOME_PATH = ROOT / "config" / "home.local.json"


@dataclass(frozen=True)
class Area:
    bbox: tuple[float, float, float, float]  # (west, south, east, north)
    graph_file: str

    @property
    def graph_path(self) -> Path:
        return ROOT / "data" / self.graph_file


AREAS = {
    # The spec's area is Irving Park (south) to Foster (north), the lake (east) to
    # Clark (west). The fetch runs Belmont to Bryn Mawr and out to Damen so a
    # wandering walker does not hit the edge of the map, where truncated streets
    # look like dead ends.
    "buena_park": Area((-87.6800, 41.9390, -87.6280, 41.9840), "buena_park_walk.graphml.gz"),
    # San Francisco, coast to bay, Fisherman's Wharf to the county line, so any
    # door in the city has the full 2-3 km of loop radius around it.
    "sf": Area((-122.5160, 37.7070, -122.3550, 37.8120), "sf_walk.graphml.gz"),
}
DEFAULT_AREA = "buena_park"
DEFAULT_GRAPH_PATH = AREAS[DEFAULT_AREA].graph_path
BBOX = AREAS[DEFAULT_AREA].bbox

KEEP_HIGHWAYS = (
    "residential", "tertiary", "secondary", "primary", "unclassified",
    "living_street", "pedestrian", "footway", "path", "steps", "cycleway",
)
# Service ways and alleys are deliberately absent: they would triple the option
# count and make the walk a backyard tour.
OVERPASS_FILTER = (
    f'["highway"~"^({"|".join(KEEP_HIGHWAYS)})$"]'
    '["area"!~"yes"]'
    '["access"!~"^(private|no)$"]'
    '["foot"!~"^no$"]'
    '["footway"!~"^(sidewalk|crossing|traffic_island)$"]'
)

STUB_PRUNE_M = 40.0        # dead-end footway/path stubs shorter than this are dropped
CONSOLIDATE_M = 8.0        # nodes closer than this (radius) are merged into one corner
CONTRACT_M = 25.0          # corners joined by a shorter edge count as one intersection
PARALLEL_M = 30.0          # parallel edges between the same corners this close are one street
HOME_NODE = -1             # node id for the inserted front door
SNAP_TO_NODE_M = 12.0      # door this close to an existing node just uses that node

STREET_HIGHWAYS = {"residential", "tertiary", "secondary", "primary", "unclassified", "living_street"}


# --------------------------------------------------------------------------- build

def build_graph(out_path: Path = DEFAULT_GRAPH_PATH, bbox=BBOX) -> nx.MultiDiGraph:
    import osmnx as ox

    ox.settings.http_user_agent = "jev-walker/0.1 (sidewalk cannonball run harness)"
    ox.settings.requests_timeout = 300
    # overpass-api.de is often saturated; OVERPASS_URL points at a mirror, e.g.
    # https://maps.mail.ru/osm/tools/overpass/api
    if os.environ.get("OVERPASS_URL"):
        ox.settings.overpass_url = os.environ["OVERPASS_URL"]
    ox.settings.useful_tags_way = sorted(
        set(ox.settings.useful_tags_way) | {"footway", "tunnel", "bridge", "access", "foot", "sidewalk"}
    )

    # network_type="walk" only matters here for bidirectionality: one-way streets
    # are two-way on foot.
    G = ox.graph_from_bbox(bbox, network_type="walk", custom_filter=OVERPASS_FILTER,
                           simplify=False, retain_all=True)
    log.info("raw: %d nodes, %d edges", len(G), G.number_of_edges())
    G = ox.truncate.largest_component(G, strongly=False)
    G = ox.simplify_graph(G, edge_attrs_differ=None)
    Gp = ox.project_graph(G)
    Gp = ox.consolidate_intersections(Gp, tolerance=CONSOLIDATE_M, rebuild_graph=True,
                                      dead_ends=False, reconnect_edges=True)
    G = ox.project_graph(Gp, to_latlong=True)
    log.info("final: %d nodes, %d edges", len(G), G.number_of_edges())

    G = slim_for_cache(G)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(G, out_path)
    return G


def slim_for_cache(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """Keep only what load_graph reads (one direction per edge; length, name,
    highway, geometry rounded to ~1 cm) so a whole city fits in a small file."""
    from shapely.geometry import LineString as LS

    out = nx.MultiDiGraph()
    out.graph.update({k: v for k, v in G.graph.items() if k in ("crs", "created_date", "created_with")})
    for n, d in G.nodes(data=True):
        out.add_node(n, x=round(float(d["x"]), 7), y=round(float(d["y"]), 7))
    seen = set()
    for u, v, k, d in G.edges(keys=True, data=True):
        sig = (min(u, v), max(u, v), round(float(d["length"]), 1))
        if sig in seen:
            continue
        seen.add(sig)
        attrs = {"length": round(float(d["length"]), 2), "highway": d.get("highway", "")}
        if d.get("name") is not None:
            attrs["name"] = d["name"]
        if d.get("geometry") is not None:
            attrs["geometry"] = LS([(round(x, 7), round(y, 7)) for x, y in d["geometry"].coords])
        out.add_edge(u, v, **attrs)
    return out


def _prune_and_merge(G: nx.MultiGraph) -> None:
    """Drop short dead-end footway stubs (remnants of dropped crossings and courtyard
    paths), then merge the degree-2 nodes that leaves behind, until stable. Real
    dead-end streets stay."""
    while True:
        doomed = []
        for n in G.nodes:
            if G.degree(n) != 1:
                continue
            (_, _, data), = G.edges(n, data=True)
            if data["length"] < STUB_PRUNE_M and data["highway"] not in STREET_HIGHWAYS:
                doomed.append(n)
        G.remove_nodes_from(doomed)
        merged = _merge_degree2(G)
        if not doomed and not merged:
            return


def _merge_degree2(G: nx.MultiGraph) -> int:
    count = 0
    for n in list(G.nodes):
        if G.degree(n) != 2 or n == G.graph.get("home_node"):
            continue
        (_, a, ka, da), (_, b, kb, db) = list(G.edges(n, keys=True, data=True))
        if a == n or b == n:
            continue  # self-loop; handled by _break_self_loops
        ca = list(reversed(edge_coords(G, n, a, ka)))   # a -> n
        cb = edge_coords(G, n, b, kb)                   # n -> b
        names = dict.fromkeys(x for x in (da["name"].split(" / ") + db["name"].split(" / ")) if x)
        longer = da if da["length"] >= db["length"] else db
        G.remove_node(n)
        G.add_edge(a, b, length=da["length"] + db["length"], name=" / ".join(names),
                   highway=longer["highway"], coords=ca + cb[1:])
        count += 1
    return count


def _contract_short_edges(G: nx.MultiGraph, max_len: float = CONTRACT_M) -> None:
    """Collapse clusters of corners joined by very short edges (trail forks mapped
    as little triangles, street jogs) into one decision point at their centroid.
    Otherwise the walker gets a decision every 20 m and can circle a triangle."""
    short = nx.Graph()
    short.add_edges_from((u, v) for u, v, d in G.edges(data=True) if u != v and d["length"] < max_len)
    next_id = _next_node_id(G)
    for cluster in list(nx.connected_components(short)):
        xs = [G.nodes[n]["x"] for n in cluster]
        ys = [G.nodes[n]["y"] for n in cluster]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        new = next_id
        next_id += 1
        G.add_node(new, x=cx, y=cy, merged=sorted(cluster))
        for n in cluster:
            for _, v, k, d in list(G.edges(n, keys=True, data=True)):
                if v in cluster:
                    continue  # internal edge: disappears into the new corner
                coords = edge_coords(G, n, v, k)
                gap = haversine_m(cx, cy, coords[0][0], coords[0][1])
                G.add_edge(new, v, length=d["length"] + gap, name=d["name"],
                           highway=d["highway"], coords=[(cx, cy)] + coords)
        # Edges between two members that are not short (a longer loop inside the
        # cluster) become self-loops on the new corner.
        for u, v, k, d in list(G.subgraph(cluster).edges(keys=True, data=True)):
            if d["length"] >= max_len:
                cu = edge_coords(G, u, v, k)
                G.add_edge(new, new, length=d["length"], name=d["name"], highway=d["highway"],
                           coords=[(cx, cy)] + cu + [(cx, cy)])
        G.remove_nodes_from(cluster)


_HIGHWAY_RANK = {h: 0 for h in STREET_HIGHWAYS} | {
    "pedestrian": 1, "footway": 2, "path": 2, "steps": 3, "cycleway": 4}


def _merge_parallel_edges(G: nx.MultiGraph, max_gap: float = PARALLEL_M) -> int:
    """Two edges between the same pair of corners that run side by side are one
    street to a walker: the two carriageways of a divided road, or a bike lane
    mapped separately from its street (SF does both a lot). Keep the street."""
    removed = 0
    pairs = {(min(u, v), max(u, v)) for u, v, k in G.edges(keys=True)
             if u != v and G.number_of_edges(u, v) > 1}
    for u, v in pairs:
        proj = LocalProjection(G.nodes[u]["x"], G.nodes[u]["y"])
        edges = sorted(G[u][v].items(), key=lambda kd: (
            _HIGHWAY_RANK.get(kd[1]["highway"], 5), not kd[1]["name"], kd[1]["length"]))
        lines = {k: LineString([proj.to_xy(*c) for c in edge_coords(G, u, v, k)]) for k, _ in edges}
        kept: list = []
        for k, d in edges:
            twin = any(abs(d["length"] - G[u][v][j]["length"]) <= max(max_gap, 0.2 * d["length"])
                       and lines[k].hausdorff_distance(lines[j]) <= max_gap for j in kept)
            if twin:
                G.remove_edge(u, v, k)
                removed += 1
            else:
                kept.append(k)
    return removed


def _highways(data) -> set[str]:
    h = data.get("highway")
    if isinstance(h, list):
        return set(h)
    if isinstance(h, str) and h.startswith("["):  # list round-tripped through GraphML
        return {s.strip(" '\"") for s in h.strip("[]").split(",")}
    return {h} if h else set()


# --------------------------------------------------------------------------- load

@dataclass
class Home:
    lon: float
    lat: float
    node: int
    snap_m: float  # how far the door is from the street centreline


def graph_hash(path: Path = DEFAULT_GRAPH_PATH) -> str:
    """Hash of the graph content (decompressed, so re-gzipping doesn't change it)."""
    import gzip

    path = Path(path)
    data = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    return hashlib.sha256(data).hexdigest()[:12]


def load_home_config(area: str = DEFAULT_AREA) -> dict:
    """Front door coordinates for `area`. config/home.local.json (gitignored) wins
    over the checked-in placeholders in config/home.json; JEV_HOME="lat,lon" wins
    over both. Both files are keyed by area name."""
    env = os.environ.get("JEV_HOME")
    if env:
        lat, lon = (float(s) for s in env.split(","))
        return {"lat": lat, "lon": lon, "source": "env"}
    for path in (LOCAL_HOME_PATH, DEFAULT_HOME_PATH):
        if path.exists():
            cfg = json.loads(path.read_text())
            if area in cfg:
                return {**cfg[area], "source": f"{path.name}:{area}"}
    raise KeyError(f"no front door for area {area!r} in {LOCAL_HOME_PATH.name} or {DEFAULT_HOME_PATH.name}")


def load_graph(path: Path | None = None, home: dict | None = None,
               paths: str = "all", area: str = DEFAULT_AREA) -> tuple[nx.MultiGraph, Home]:
    """Load the cached graph as an undirected MultiGraph with the door inserted.

    paths="named" drops unnamed footways/paths (park path tangles around Montrose
    Harbor and the lakefront) while keeping named ones: Lakefront Trail and the
    named access paths under Lake Shore Drive.
    """
    import osmnx as ox

    path = Path(path) if path else AREAS[area].graph_path
    D = ox.load_graphml(path)
    G = nx.MultiGraph()
    G.graph.update(D.graph)
    G.graph["graph_hash"] = graph_hash(path)
    for n, d in D.nodes(data=True):
        G.add_node(n, x=float(d["x"]), y=float(d["y"]))
    # The directed walk graph has every edge twice; keep one per (u, v, osmid set).
    seen = set()
    for u, v, k, d in D.edges(keys=True, data=True):
        coords = _edge_coords_from_data(D, u, v, d)
        sig = (min(u, v), max(u, v), round(float(d["length"]), 1))
        if sig in seen:
            continue
        seen.add(sig)
        G.add_edge(u, v, length=float(d["length"]), name=_clean_name(d.get("name")),
                   highway=sorted(_highways(d))[0] if _highways(d) else "",
                   coords=coords)
    if paths == "named":
        G.remove_edges_from([(u, v, k) for u, v, k, d in G.edges(keys=True, data=True)
                             if d["highway"] not in STREET_HIGHWAYS and not d["name"]])
        G.remove_nodes_from([n for n in list(G.nodes) if G.degree(n) == 0])
        biggest = max(nx.connected_components(G), key=len)
        G.remove_nodes_from([n for n in list(G.nodes) if n not in biggest])
    elif paths != "all":
        raise ValueError(f"paths must be 'all' or 'named', not {paths!r}")
    G.graph["paths"] = paths
    _prune_and_merge(G)
    _contract_short_edges(G)
    _prune_and_merge(G)
    _merge_parallel_edges(G)
    _prune_and_merge(G)
    _break_self_loops(G)
    G.graph["area"] = area
    home_cfg = home or load_home_config(area)
    H = insert_home(G, home_cfg["lon"], home_cfg["lat"])
    return G, H


def _edge_coords_from_data(D, u, v, d) -> list[tuple[float, float]]:
    geom = d.get("geometry")
    if geom is None:
        return [(D.nodes[u]["x"], D.nodes[u]["y"]), (D.nodes[v]["x"], D.nodes[v]["y"])]
    coords = [tuple(map(float, c)) for c in geom.coords]
    if _dist(coords[0], D.nodes[u]) > _dist(coords[-1], D.nodes[u]):
        coords.reverse()
    return coords


def _dist(c, nd) -> float:
    return haversine_m(c[0], c[1], nd["x"], nd["y"])


def edge_coords(G: nx.MultiGraph, u, v, k) -> list[tuple[float, float]]:
    """Edge geometry oriented from u to v (undirected edges store one orientation)."""
    coords = G.edges[u, v, k]["coords"]
    if u == v:
        return coords
    if _dist(coords[0], G.nodes[u]) <= _dist(coords[-1], G.nodes[u]):
        return list(coords)
    return list(reversed(coords))


def _next_node_id(G) -> int:
    return max(n for n in G.nodes if isinstance(n, int)) + 1


def _break_self_loops(G: nx.MultiGraph) -> None:
    """A self-loop is two exits from one corner; split it with a pass-through node so
    every option is a distinct (u, v, k) edge."""
    for u, _, k, d in list(G.edges(keys=True, data=True)):
        if _ != u:
            continue
        coords = d["coords"]
        line = LineString(coords)
        mid = line.interpolate(0.5, normalized=True)
        i = _split_index(coords, (mid.x, mid.y))
        a = coords[:i] + [(mid.x, mid.y)]
        b = [(mid.x, mid.y)] + coords[i:]
        m = _next_node_id(G)
        G.add_node(m, x=mid.x, y=mid.y)
        G.remove_edge(u, u, k)
        for part, (s, t) in ((a, (u, m)), (b, (m, u))):
            G.add_edge(s, t, length=d["length"] / 2, name=d["name"], highway=d["highway"], coords=part)


def _split_index(coords, pt) -> int:
    best, best_i = math.inf, 1
    for i in range(1, len(coords)):
        seg = LineString([coords[i - 1], coords[i]])
        dd = seg.distance(Point(pt))
        if dd < best:
            best, best_i = dd, i
    return best_i


def insert_home(G: nx.MultiGraph, lon: float, lat: float) -> Home:
    """Split the nearest edge at the front door so the loop starts and ends exactly
    there. The door node is always a decision point (the arrive_home check lives
    there) even though it has degree 2."""
    proj = LocalProjection(lon, lat)
    px, py = proj.to_xy(lon, lat)
    best = None
    for u, v, k, d in G.edges(keys=True, data=True):
        line = LineString([proj.to_xy(*c) for c in d["coords"]])
        dd = line.distance(Point(px, py))
        if best is None or dd < best[0]:
            best = (dd, u, v, k, d, line)
    dist, u, v, k, d, line = best
    along = line.project(Point(px, py))
    snapped = line.interpolate(along)
    slon, slat = proj.to_lonlat(snapped.x, snapped.y)

    if along < SNAP_TO_NODE_M or line.length - along < SNAP_TO_NODE_M:
        # Door is effectively at a corner. Use the nearer end.
        end = d["coords"][0] if along < SNAP_TO_NODE_M else d["coords"][-1]
        node = min((u, v), key=lambda n: _dist(end, G.nodes[n]))
        G.graph["home_node"] = node
        return Home(lon, lat, node, dist)

    coords = d["coords"]
    i = _split_index(coords, (slon, slat))
    a = coords[:i] + [(slon, slat)]
    b = [(slon, slat)] + coords[i:]
    frac = along / line.length
    # coords run from whichever of u/v is nearest coords[0]
    first, last = (u, v) if _dist(coords[0], G.nodes[u]) <= _dist(coords[0], G.nodes[v]) else (v, u)
    G.remove_edge(u, v, k)
    G.add_node(HOME_NODE, x=slon, y=slat)
    G.add_edge(first, HOME_NODE, length=d["length"] * frac, name=d["name"], highway=d["highway"], coords=a)
    G.add_edge(HOME_NODE, last, length=d["length"] * (1 - frac), name=d["name"], highway=d["highway"], coords=b)
    G.graph["home_node"] = HOME_NODE
    return Home(lon, lat, HOME_NODE, dist)


# --------------------------------------------------------------------------- names

_ABBREV = {
    "North": "N", "South": "S", "East": "E", "West": "W",
    "Avenue": "Ave", "Street": "St", "Road": "Rd", "Drive": "Dr", "Place": "Pl",
    "Terrace": "Ter", "Boulevard": "Blvd", "Court": "Ct", "Parkway": "Pkwy",
}


def _clean_name(raw) -> str:
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return ""
    if isinstance(raw, str) and raw.startswith("["):
        raw = [s.strip(" '\"") for s in raw.strip("[]").split(",")]
    if isinstance(raw, list):
        names = sorted({_clean_name(r) for r in raw if r})
        return " / ".join(n for n in names if n)
    return " ".join(_ABBREV.get(w, w) for w in str(raw).split())


def is_decision_point(G: nx.MultiGraph, n) -> bool:
    deg = G.degree(n)
    return deg >= 3 or deg == 1 or n == G.graph.get("home_node")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Fetch and cache the walk graph for an area.")
    ap.add_argument("--area", default=DEFAULT_AREA, choices=sorted(AREAS))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    a = AREAS[args.area]
    G = build_graph(a.graph_path, a.bbox)
    print(f"wrote {a.graph_path} ({len(G)} nodes, {G.number_of_edges()} edges), "
          f"hash {graph_hash(a.graph_path)}")
