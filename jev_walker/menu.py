"""The decision contract: what Jev sees at a corner, and the menu it chooses from.

Each option has two halves. The public half (`Option.public()`) is the only thing
that ever goes to a chooser: direction, street, block length, already_walked,
dead_end, back. The harness half (edge ids, destination node, geometry) stays in
the harness for moving the walker, logging and rendering. Coordinates, the street
graph, other streets' names and any lookahead beyond block length must never leak
into the public half; tests/test_menu.py checks that.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from .geo import bearing_deg, compass_label, haversine_m, point_along, wrap180
from .graph import edge_coords, is_decision_point

MAX_OPTIONS = 6
BEARING_SAMPLE_M = 20.0  # look this far along an edge to get its initial bearing
TWIN_ANGLE = 20.0        # same-street exits closer than this in angle...
TWIN_END_M = 60.0        # ...whose next corners are this close are one street
ARRIVE_HOME = "arrive_home"
OPTION_IDS = "ABCDEF"

EdgeId = tuple  # (u, v, k) with u <= v: undirected identity of a street segment


def edge_id(u, v, k) -> EdgeId:
    return (u, v, k) if u <= v else (v, u, k)


@dataclass
class Traversal:
    """A walk from one decision point to the next: one or more edges (more only when
    passing through a degree-2 node, which has nothing to decide)."""
    start: int
    end: int
    hops: list[tuple[int, int, int]]  # directed (u, v, k) in walking order
    length_m: float
    coords: list[tuple[float, float]]
    # The other carriageway of a divided road (or a parallel bike lane) that leaves
    # this corner alongside this one. Walking either counts as walking both.
    twin_edges: list = field(default_factory=list)

    @property
    def edge_ids(self) -> list[EdgeId]:
        return [edge_id(*h) for h in self.hops]

    @property
    def first_hop(self) -> tuple[int, int, int]:
        return self.hops[0]

    @property
    def last_hop(self) -> tuple[int, int, int]:
        return self.hops[-1]


@dataclass
class Option:
    id: str
    kind: str                     # "exit" or "arrive_home"
    direction: str | None = None  # left/right/straight/back, or north/... on the first corner
    angle_deg: float | None = None
    street: str = ""
    block_length_m: float = 0.0
    already_walked: bool = False
    dead_end: bool = False
    back: bool | None = None      # None on the first corner (no arriving edge)
    # harness-only
    traversal: Traversal | None = field(default=None, repr=False)

    def public(self) -> dict:
        if self.kind == ARRIVE_HOME:
            return {"id": self.id, "kind": ARRIVE_HOME}
        return {
            "id": self.id,
            "kind": "exit",
            "direction": self.direction,
            "angle_deg": round(self.angle_deg),
            "street": self.street,
            "block_length_m": round(self.block_length_m),
            "already_walked": self.already_walked,
            "dead_end": self.dead_end,
            "back": self.back,
        }

    def harness(self) -> dict:
        """What the log and renderer get on top of the public view."""
        d = self.public()
        if self.traversal is not None:
            t = self.traversal
            d["to_node"] = t.end
            d["edges"] = [list(e) for e in t.edge_ids]
            if t.twin_edges:
                d["twin_edges"] = [list(e) for e in t.twin_edges]
            d["coords"] = [[round(x, 7), round(y, 7)] for x, y in t.coords]
        return d


def traverse(G: nx.MultiGraph, node, nbr, key) -> Traversal:
    """Follow an exit until the next decision point."""
    hops = [(node, nbr, key)]
    coords = edge_coords(G, node, nbr, key)
    length = G.edges[node, nbr, key]["length"]
    prev, cur, ck = node, nbr, key
    while not is_decision_point(G, cur) and len(hops) < 50:
        nxt = [(v, k) for _, v, k in G.edges(cur, keys=True) if not (v == prev and k == ck)]
        if len(nxt) != 1:
            break
        v, k = nxt[0]
        hops.append((cur, v, k))
        coords += edge_coords(G, cur, v, k)[1:]
        length += G.edges[cur, v, k]["length"]
        prev, cur, ck = cur, v, k
    return Traversal(node, cur, hops, length, coords)


def exit_bearing(G, u, v, k) -> float:
    coords = edge_coords(G, u, v, k)
    x, y = point_along(coords, BEARING_SAMPLE_M)
    return bearing_deg(coords[0][0], coords[0][1], x, y)


def arrival_heading(G, u, v, k) -> float:
    """Heading of travel arriving at v along (u, v, k), from its last few metres."""
    coords = list(reversed(edge_coords(G, u, v, k)))  # starts at v
    x, y = point_along(coords, BEARING_SAMPLE_M)
    return bearing_deg(x, y, coords[0][0], coords[0][1])


def direction_bucket(angle: float) -> str:
    a = abs(angle)
    if a <= 30:
        return "straight"
    if a > 150:
        return "back"
    return "right" if angle > 0 else "left"


def relative_label(angle: float) -> str:
    """8-way label for a bearing relative to heading, e.g. 'left-behind'."""
    a = wrap180(angle)
    labels = ["ahead", "right-ahead", "right", "right-behind", "behind",
              "left-behind", "left", "left-ahead"]
    return labels[int(((a % 360) + 22.5) // 45) % 8]


def build_menu(
    G: nx.MultiGraph,
    node,
    arriving: tuple[int, int, int] | None,
    walked: set[EdgeId],
    *,
    offer_arrive_home: bool = False,
) -> tuple[list[Option], list[dict]]:
    """Typed options at `node`. `arriving` is the directed (u, node, k) hop just
    walked, or None on the first corner. Returns (options, dropped) where dropped
    lists exits removed by the six-option cap, for the log."""
    heading = arrival_heading(G, *arriving) if arriving else None
    arriving_id = edge_id(*arriving) if arriving else None

    options: list[Option] = []
    for _, nbr, key in G.edges(node, keys=True):
        trav = traverse(G, node, nbr, key)
        b = exit_bearing(G, node, nbr, key)
        if heading is None:
            angle, direction, back = b, compass_label(b), None
        else:
            angle = wrap180(b - heading)
            back = edge_id(node, nbr, key) == arriving_id
            direction = direction_bucket(angle)
        options.append(Option(
            id="", kind="exit", direction=direction, angle_deg=angle,
            street=G.edges[node, nbr, key]["name"],
            block_length_m=trav.length_m,
            already_walked=any(e in walked for e in trav.edge_ids),
            dead_end=G.degree(trav.end) == 1,
            back=back, traversal=trav,
        ))

    options, dropped = _merge_twins(G, options)
    if len(options) > MAX_OPTIONS:
        # Keep the six closest to the cardinal directions (relative to heading, or
        # to north on the first corner) and log the rest.
        def off_cardinal(o: Option) -> float:
            return abs(wrap180(o.angle_deg - 90 * round(o.angle_deg / 90)))
        ranked = sorted(options, key=off_cardinal)
        keep = ranked[:MAX_OPTIONS]
        dropped += [{**o.harness(), "dropped_for": "six_option_cap"} for o in ranked[MAX_OPTIONS:]]
        options = keep

    # Stable order: clockwise from straight ahead (or from north).
    options.sort(key=lambda o: (o.angle_deg % 360))
    for i, o in enumerate(options):
        o.id = OPTION_IDS[i]
    if offer_arrive_home:
        options.append(Option(id=ARRIVE_HOME, kind=ARRIVE_HOME))
    return options, dropped


def _merge_twins(G, options: list[Option]) -> tuple[list[Option], list[dict]]:
    """Fold divided-road carriageways that leave this corner side by side into one
    exit. (Twins that rejoin at the same corner are already merged in the graph.)"""
    kept: list[Option] = []
    dropped: list[dict] = []
    for o in sorted(options, key=lambda o: (not o.back, o.block_length_m)):
        twin_of = None
        for k in kept:
            if (o.street and o.street == k.street
                    and abs(wrap180(o.angle_deg - k.angle_deg)) < TWIN_ANGLE
                    and _node_gap(G, o.traversal.end, k.traversal.end) < TWIN_END_M):
                twin_of = k
                break
        if twin_of is None:
            kept.append(o)
            continue
        twin_of.traversal.twin_edges += o.traversal.edge_ids + o.traversal.twin_edges
        twin_of.already_walked = twin_of.already_walked or o.already_walked
        dropped.append({**o.harness(), "dropped_for": f"twin_of_{twin_of.direction}"})
    return kept, dropped


def _node_gap(G, a, b) -> float:
    return haversine_m(G.nodes[a]["x"], G.nodes[a]["y"], G.nodes[b]["x"], G.nodes[b]["y"])


def build_state(
    *,
    walked_m: float,
    target_m: float,
    repeats: int,
    last_turns: list[str],
    elapsed_min: float,
    mode: str,
    here: tuple[float, float],
    home: tuple[float, float],
    heading: float | None,
) -> dict:
    """The state object Jev sees. `here`/`home` are (lon, lat) and are reduced to a
    distance and a heading-relative bearing; they are never passed through."""
    state = {
        "walked_m": round(walked_m),
        "target_m": round(target_m),
        "remaining_m": round(max(0.0, target_m - walked_m)),
    }
    if mode == "compass":
        dist = haversine_m(here[0], here[1], home[0], home[1])
        state["home_distance_m"] = round(dist)
        if dist < 1:
            state["home_bearing_relative"] = None
        else:
            b = bearing_deg(here[0], here[1], home[0], home[1])
            if heading is None:
                state["home_bearing_relative"] = {"label": compass_label(b), "deg": round(b), "absolute": True}
            else:
                rel = wrap180(b - heading)
                state["home_bearing_relative"] = {"label": relative_label(rel), "deg": round(rel)}
    state["repeats_so_far"] = repeats
    state["last_turns"] = last_turns[-3:]
    state["elapsed_min"] = round(elapsed_min, 1)
    return state
