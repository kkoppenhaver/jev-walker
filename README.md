# Jev Walks Me Home

Harness for the sidewalk-scale cannonball run: Jev picks an exit at every corner of a
five-mile loop from the front door, seeing only the current corner plus a compass home.

This covers build-plan steps 2 and 3 (graph + menu builder, simulator, scoring, JSON
log), plus the chooser adapters for step 5. Still to build: the replay renderer, the
phone page and its backend, and analysis.

## Layout

| Path | What |
| --- | --- |
| `jev_walker/graph.py` | OSM pull, filter, simplify, cache; load as an undirected graph with the door inserted |
| `jev_walker/menu.py` | The decision contract: `build_menu()` (typed options) and `build_state()` |
| `jev_walker/run.py` | `Run`: position, path, clock, end conditions, log. The sim and the phone backend both drive it |
| `jev_walker/choosers.py` | `random`, `heuristic`, `claude`, `jev` |
| `jev_walker/scoring.py` | Per-run scoring table |
| `jev_walker/sim.py` | Run N loops, aggregate, keep a few full logs |
| `data/buena_park_walk.graphml` | Cached street graph (no map calls on the walk) |
| `config/home.json` | Placeholder door on W Buena Ave. Put the real one in `config/home.local.json` (gitignored) or `JEV_HOME=lat,lon` |

## Running

```sh
pip install -e '.[dev,claude]'
python -m pytest

python -m jev_walker.sim --chooser random --runs 1000
python -m jev_walker.sim --chooser heuristic --runs 1000
python -m jev_walker.sim --chooser heuristic --runs 200 --mode pure
python -m jev_walker.sim --chooser claude --runs 20      # needs ANTHROPIC_API_KEY
python -m jev_walker.sim --chooser jev --runs 1000       # needs JEV_API_URL / JEV_API_KEY

# Rebuild the graph cache (about 2 min; overpass-api.de is often saturated)
OVERPASS_URL=https://maps.mail.ru/osm/tools/overpass/api python -m jev_walker.graph
```

The sim writes `runs/<stamp>-<chooser>-<mode>/` with `summary.json`, a `runs.jsonl`
score line per run, and the first `--keep-logs` full logs for the renderer.

## The contract as built

Each corner sends a chooser `{"state": {...}, "options": [...]}` and nothing else.

```json
{"state": {"walked_m": 462, "target_m": 8047, "remaining_m": 7585, "home_distance_m": 264,
           "home_bearing_relative": {"label": "behind", "deg": -161},
           "repeats_so_far": 0, "last_turns": ["right", "left", "straight"], "elapsed_min": 6.4},
 "options": [{"id": "A", "kind": "exit", "direction": "straight", "angle_deg": 10, "street": "",
              "block_length_m": 227, "already_walked": false, "dead_end": false, "back": false}, ...]}
```

The chooser returns one id and a probability for every id. `Option.public()` is the
only thing that leaves the harness; the log also gets the harness half (destination
node, edge ids, geometry) for rendering. `tests/test_menu.py` checks that no
coordinates, node ids or geometry reach the public view.

- Angles are signed, positive to the right. `direction` buckets: straight within
  ±30°, back beyond ±150°, otherwise left or right. `back` is true only for the U-turn
  exit; a sharp diagonal turn can be labelled `back` without being the U-turn.
- First corner: absolute compass directions, `back` is null, and
  `home_bearing_relative` is null because the walker is standing on the door.
- In pure mode `home_distance_m` and `home_bearing_relative` are left out.
- `arrive_home` is offered at the door once the walker has left it, or within
  `--arrive-radius` metres (default 25). If accepted away from the door, the harness
  adds the walk to the door.
- If a corner has more than 6 exits, the 6 nearest to straight, left, right and back
  are kept and the rest are logged under `dropped`.

## Graph decisions (some differ from the spec)

- **Custom Overpass filter.** OSMnx's stock `walk` network drops streets tagged
  `sidewalk=separate`, which covers most Chicago streets. So the harness lists the
  highway classes itself: the spec's list, plus `path`, `steps`, `cycleway` (where foot
  is not banned) and `unclassified`.
- **Sidewalks, crossings and traffic islands are dropped.** Chicago maps sidewalks as
  separate footways (10k+ edges here). If they stayed, every corner would become a
  cluster of nodes. The street centreline stands in for its sidewalks.
- **Short edges are contracted.** Corners joined by an edge under 25 m (trail forks
  mapped as small triangles, street jogs) are merged into one decision point.
  Without this, the walker could circle a 20 m triangle on the Lakefront Trail.
- **The door is a node.** The nearest block is split at the door, so the loop starts
  and finishes exactly there. Coming home along the block you left on counts as a repeat.
- **The fetch area is larger than the spec's.** It runs Belmont to Bryn Mawr and out
  to Damen, so a walker heading north doesn't hit a fake dead end at Foster.
- **Unnamed park paths are dropped by default (`--paths named`).** Named paths stay,
  including the Lakefront Trail and the access paths under Lake Shore Drive. This is
  the biggest call here, so it's a flag. The unnamed paths around Montrose Harbor are
  where walkers get stuck: with them in, the compass heuristic goes from 67% clean to
  0% (table below). Use `--paths all` to put them back.

Corners spot-checked against the real graph in `tests/test_real_corners.py`:
Montrose & Clarendon, Irving Park & Sheridan, Buena & Hazel (a T).

## First simulation results

1,000 runs each, placeholder door, compass mode unless noted, graph `dde19f66e26b`.

| Chooser | Paths | Finish | Clean | Median closed loop (mi) | Mean repeats | Decisions |
| --- | --- | --- | --- | --- | --- | --- |
| random | named | 29.8% | 0% | 0.34 | 26.2 | 53 |
| heuristic | named | 100% | 67.2% | 5.11 | 1.35 | 56 |
| heuristic, pure mode (200 runs) | named | 0% | 0% | – | 0.08 | 76 |
| heuristic | all | 39% | 0% | 5.82 | 8.0 | 55 |

- **Random** finishes about 30% of the time, but almost always by stepping back to the
  door and taking `arrive_home` within a few blocks. That is what the rules allow.
- **Heuristic** keeps home at 180°×(1 − walked/target) off the nose: the
  angle-to-start on a circular loop. It uses nothing outside the public state. Its
  near-perfect finish rate shows the compass fields are enough to close a loop. It is
  nearly deterministic, so its spread is narrow.
- **Pure mode** fails, as the spec predicts.
- About 55 decisions per 5 miles, slightly under the spec's 60–90 estimate.

## Chooser notes

- `claude` asks `claude-opus-5-5` (`JEV_CLAUDE_MODEL`) at effort `low`
  (`JEV_CLAUDE_EFFORT`, chosen for sidewalk latency). It uses structured output for
  `{choice, probabilities}` and server-side refusal fallbacks. It has only been tested
  against a fake client; no live calls have been made yet.
- `jev` is an assumed contract: POST `{state, options}`, expect
  `{choice, probabilities, cost_usd?}`. It is the only code that changes once Jev's
  docs settle the open questions. A response with no probability vector is accepted
  and flagged in the log, since the calibration chart needs the full vector.

## Log format (v1)

`header` (run id, mode, chooser, model version, home node, graph hash, config),
`decisions` (state, menu with geometry, choice, probabilities, latency, cost, override,
and in sims an `oracle` block with each option's true shortest-path distance home and
whether the loop is still closable within 5.5 mi, for the calibration chart), `path`
(one entry per block walked, with geometry and repeat count), `ui_events`, `gps` (empty
in sims), `end`, `score`. Sim timestamps are simulated seconds: 1.34 m/s walking plus
8 s per corner plus chooser latency.
