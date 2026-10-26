# Jev Walks Me Home

Harness for the sidewalk-scale cannonball run: Jev picks an exit at every corner of a
five-mile loop from the front door, seeing only the current corner plus a compass home.

Built so far:
- Build-plan steps 2 and 3 (graph + menu builder, simulator, scoring, JSON log) for
  two areas, Buena Park and San Francisco.
- The chooser adapters for step 5, including a Jev adapter written against TypeSafe's
  published API.
- Step 6: the phone page and its backend.

Still to build: the replay renderer and analysis.

## Layout

| Path | What |
| --- | --- |
| `jev_walker/graph.py` | OSM pull, filter, simplify, cache; load as an undirected graph with the door inserted |
| `jev_walker/menu.py` | The decision contract: `build_menu()` (typed options) and `build_state()` |
| `jev_walker/run.py` | `Run`: position, path, clock, end conditions, log. The sim and the phone backend both drive it |
| `jev_walker/choosers.py` | `random`, `heuristic`, `claude`, `jev` |
| `jev_walker/scoring.py` | Per-run scoring table |
| `jev_walker/sim.py` | Run N loops, aggregate, keep a few full logs |
| `jev_walker/live.py` | `LiveWalk`: a real walk, the same `Run` driven by GPS fixes and the real clock (corner detection, overrides, crash resume) |
| `jev_walker/server.py` | FastAPI backend: serves the phone page and a small JSON API over `LiveWalk` |
| `jev_walker/static/` | The phone page (one HTML file, no build step), PWA manifest, service worker, icons |
| `tools/phone_rehearsal.js` | Drives the phone page in an emulated Pixel with fake GPS, for a desk rehearsal |
| `data/<area>_walk.graphml.gz` | Cached street graphs, `buena_park` and `sf` (no map calls on the walk) |
| `config/home.json` | Front doors per area: SF is 335 Powell St (the Westin St. Francis entrance on Powell); Buena Park is a placeholder. `config/home.local.json` (gitignored, same shape) or `JEV_HOME=lat,lon` override them |

## Running

```sh
pip install -e '.[dev,claude]'
python -m pytest

python -m jev_walker.sim --chooser random --runs 1000
python -m jev_walker.sim --chooser heuristic --runs 1000
python -m jev_walker.sim --chooser heuristic --runs 200 --mode pure
python -m jev_walker.sim --chooser claude --runs 20      # needs ANTHROPIC_API_KEY
python -m jev_walker.sim --chooser jev --runs 1000       # needs TYPESAFE_API_KEY or OPENROUTER_API_KEY

# San Francisco
python -m jev_walker.sim --area sf --chooser heuristic --runs 1000
python -m jev_walker.sim --area sf --chooser heuristic --runs 1000 --walk-speed 1.1

# Rebuild a graph cache (Buena Park ~2 min, SF ~5 min; overpass-api.de is often saturated)
OVERPASS_URL=https://maps.mail.ru/osm/tools/overpass/api python -m jev_walker.graph --area sf
```

The sim writes `runs/<stamp>-<area>-<chooser>-<mode>/` with `summary.json`, a
`runs.jsonl` score line per run, and the first `--keep-logs` full logs for the renderer.

## Walk day: the phone and the backend

```sh
pip install -e '.[server]'
export TYPESAFE_API_KEY=...           # or OPENROUTER_API_KEY
python -m jev_walker.server --area sf --chooser jev --port 8000 --token <secret>
cloudflared tunnel --url http://localhost:8000     # prints https://<random>.trycloudflare.com
```

Open `https://<tunnel>/?t=<secret>` in Chrome on the Android phone, then use Add to
Home screen.
- The phone talks to the backend over LTE, so the backend needs a public HTTPS URL.
  The Geolocation API needs HTTPS too. A laptop left awake on hotel wifi plus a quick
  tunnel is enough; a small VPS works the same way.
- `--chooser heuristic` runs the whole flow without a Jev key, for the field test.

**What happens**
1. Tap **Start** at the door. The page keeps the screen awake, averages GPS for 10 s,
   and the backend asks Jev for the first corner, using absolute directions.
2. Tap **Clap** on the three-count. That sets the sync marker for the footage.
3. The screen shows the arrow, street, probability and a bar per option. Mid-block it
   shrinks to miles, the compass home, and the distance to the next corner.
4. Arrival is detected on the server: two consecutive fixes within 15 m of the corner
   being walked to, with accuracy of 30 m or better, and not in the first 5 s after a
   decision. The screen flashes and buzzes, and the next decision is already on it.
   **I'm here** forces an arrival (taps within 3 s of a decision are ignored as double
   taps).
5. **Can't** asks which exit you actually took and why (no legal crossing, sidewalk
   closed, exit missing, other). It's logged as an override and the run continues from
   that exit.
6. If Jev fails twice in a row, the screen shows **Ask again**. The run is not lost.
7. The walk ends on `arrive_home`, at 6.5 mi, at the area's time cap (SF: 3 h), or on
   **End**. The results screen links to the log.

**Logs and crashes**
- Every event rewrites `runs/live/<run_id>.json`: decisions with real timestamps,
  every GPS fix with its nearest corner, UI events, overrides, the phone-to-server clock
  offset, and the sync marker.
- If the backend dies mid-walk, restart it with `--resume latest`. It replays the log
  against the graph and carries on, and the page picks the walk back up on reload.

**Desk rehearsal:** `node tools/phone_rehearsal.js <dir>` walks an emulated phone through
a dozen corners against a local server, including one Can't and one I'm here, and
saves screenshots.

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
- The two carriageways of a divided road leaving a corner side by side are one exit
  (`dropped_for: twin_of_...` in the log). Walking either one marks both as walked.

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
  to Damen, so a walker heading north doesn't hit a fake dead end at Foster. SF is the
  whole city, so any door gets a full loop radius.
- **Divided roads and parallel bike lanes are one street.** Parallel edges between the
  same two corners within 30 m of each other are merged, keeping the street over the
  bike lane. Carriageways that end at different corners are folded at menu time. This
  matters most in SF: Market St corners had up to 11 exits before; now the busiest
  corner has 8, and 7 of 6,656 corners hit the six-exit cap.
- **The cache stores only what the harness reads** (one direction per edge; length,
  name, highway, geometry), gzipped. All of SF is 1.2 MB.
- **Unnamed park paths are dropped by default (`--paths named`).** Named paths stay,
  including the Lakefront Trail and the access paths under Lake Shore Drive. This is
  the biggest call here, so it's a flag. The unnamed paths around Montrose Harbor are
  where walkers get stuck: with them in, the compass heuristic goes from 67% clean to
  0% (table below). Use `--paths all` to put them back.

Corners spot-checked against the real graphs in `tests/test_real_corners.py`:
Montrose & Clarendon, Irving Park & Sheridan, Buena & Hazel (a T); in SF, Valencia &
20th and Haight & Divisadero.

## Simulation results: Buena Park

1,000 runs each, placeholder door, compass mode unless noted, graph `38710436afd5`.

| Chooser | Paths | Finish | Clean | Median closed loop (mi) | Mean repeats | Decisions |
| --- | --- | --- | --- | --- | --- | --- |
| random | named | 30% | 0% | 0.35 | 26.3 | 53 |
| heuristic | named | 100% | 67.2% | 5.12 | 1.35 | 56 |
| heuristic, pure mode (200 runs) | named | 0% | 0% | – | 0.08 | 77 |
| heuristic | all | 75% | 0% | 5.93 | 6.8 | 55 |

- **Random** finishes about 30% of the time, but almost always by stepping back to the
  door and taking `arrive_home` within a few blocks. That is what the rules allow.
- **Heuristic** keeps home at 180°×(1 − walked/target) off the nose: the
  angle-to-start on a circular loop. It uses nothing outside the public state. Its
  near-perfect finish rate shows the compass fields are enough to close a loop. It is
  nearly deterministic, so its spread is narrow.
- **Pure mode** fails, as the spec predicts.
- About 55 decisions per 5 miles, slightly under the spec's 60–90 estimate.

## Simulation results: San Francisco

Compass heuristic, 100 runs per door and turning side, `--paths named`, graph
`e169eaa38274`. The doors are placeholders on public streets, snapped to the nearest
block.

| Door | Finish (side +1 / −1) | Clean (side +1 / −1) | Decisions |
| --- | --- | --- | --- |
| Mission (20th St) | 100% / 100% | 86% / 51% | 67–74 |
| Noe Valley | 100% / 43% | 100% / 12% | 63–77 |
| Bernal Heights | 100% / 100% | 0% / 0% | 74–78 |
| Outer Sunset | 100% / 100% | 100% / 100% | 56–62 |
| Inner Richmond | 100% / 100% | 24% / 67% | 58–59 |
| Lower Haight | 100% / 100% | 100% / 100% | 71–74 |
| Russian Hill | 100% / 100% | 56% / 44% | 68–71 |
| Potrero Hill | 100% / 100% | 100% / 12% | 53–70 |
| Glen Park | 100% / 100% | 64% / 26% | 63–83 |

- The result depends a lot on the door and the first turn, so simulate from the real
  door before the walk.
- SF has more decisions per loop than Buena Park (55–85 vs about 55).
- **Hills are not modelled.** Neither the state nor the sim knows elevation. At
  1.34 m/s loops take 104–120 simulated minutes. At 1.1 m/s, roughly an SF hill pace,
  they take 125–144 minutes, which is why SF's time cap is 3 h instead of the spec's
  2 h 30 min. Under the old cap, some runs ended before home (Bernal 14 of 50, Inner
  Richmond 25 of 50, Russian Hill 5 of 50).
- **From the real door (335 Powell St), 200 runs per turning side:** the compass
  heuristic closes 100% of loops. It finishes clean 70% of the time turning one way
  and 0% the other. Loops take 108–116 min at 1.34 m/s, and at most 151 min at 1.0 m/s,
  within the 3 h cap. A random walker closes 26%, never clean.

## Chooser notes

- `claude` asks `claude-opus-5-5` (`JEV_CLAUDE_MODEL`) at effort `low`
  (`JEV_CLAUDE_EFFORT`, chosen for sidewalk latency). It uses structured output for
  `{choice, probabilities}` and server-side refusal fallbacks. It has only been tested
  against a fake client; no live calls have been made yet.
- `jev` calls TypeSafe's Jev decision model. Each corner is one request with one
  `choice` question:

  ```json
  {"model": "jev-latest",
   "state": { ...the state object above... },
   "questions": {"exit": {"type": "choice", "instructions": { ...the game, goals, field meanings... },
     "criteria": {"A": {"summary": "right (+87 deg) onto N Clarendon Ave, 190 m to the next corner",
                        "direction": "right", "angle_deg": 87, "street": "N Clarendon Ave", ...},
                  "arrive_home": {"summary": "Stop here. ..."}}}}}
  ```

  The response gives `answers.exit.choice`, `probabilities` for every option, and
  `confidence`. The log records the served snapshot (e.g. `jev-1.13.0`), the
  confidence, and `usage.cost`. When there is no cost, it estimates $0.042 per million
  input tokens.
  - `TYPESAFE_API_KEY` → `https://api.typesafe.ai/v1/systemone`, model `jev-latest`.
  - `OPENROUTER_API_KEY` → `https://openrouter.ai/api/alpha/decisions`, model
    `typesafe/jev-1.14`.
  - `JEV_PROVIDER`, `JEV_API_URL`, `JEV_MODEL` and `JEV_API_KEY` override these.
  - 429 and 5xx responses get one retry.
  - Tested against fakes and a local HTTP server, not the live API (no key in this
    environment).

### The spec's open questions, answered from the docs

| Question | Answer |
| --- | --- |
| Free-form JSON state or registered schema? | Free-form: `state` is any string, object or array. Nothing to register |
| Options typed with attributes? | Yes. Choice `criteria` values may be strings, objects or arrays, so each exit carries its attributes |
| Option cap? | 255 per Choice. Our cap of 6 is our own |
| Probability for every option? | Yes. `probabilities` covers every option and sums to 1, plus a `confidence` statistic, (p_max − 1/n)/(1 − 1/n) |
| Latency over LTE? | Not documented by TypeSafe. AIML API's docs say "well under a second" (one example: 96 ms). The backend makes the call, not the phone. Measure it |
| State across calls? | None. Each request carries everything; OpenRouter's `session_id` is observability only. `last_turns` stays |

Two notes from the docs:
- Jev never sees the question id, so everything it needs is in `instructions` and
  `criteria`.
- TypeSafe recommends atomic questions combined in code. A later variant could ask
  separate yes/no questions per exit ("keeps the loop closable?", "new street?")
  instead of one forced choice. That is not the spec's test, though.

Sources: [TypeSafe docs](https://docs.typesafe.ai) ([quickstart](https://docs.typesafe.ai/introduction/quickstart.md),
[Choice](https://docs.typesafe.ai/primitives/choice.md), [confidence](https://docs.typesafe.ai/confidence.md)),
[OpenRouter Jev tutorial](https://openrouter.ai/docs/guides/community/jev-tutorial.md),
[OpenRouter Decisions API reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request.md),
[AIML API Jev page](https://docs.aimlapi.com/api-references/decision-models/typesafe/jev.md).

## Log format (v1)

`header` (run id, mode, chooser, model version, home node, graph hash, config),
`decisions` (state, menu with geometry, choice, probabilities, latency, cost, override,
served model and confidence when the chooser reports them,
and in sims an `oracle` block with each option's true shortest-path distance home and
whether the loop is still closable within 5.5 mi, for the calibration chart), `path`
(one entry per block walked, with geometry and repeat count), `ui_events`, `gps` (empty
in sims), `end`, `score`. Sim timestamps are simulated seconds: 1.34 m/s walking plus
8 s per corner plus chooser latency.
