"""Simulation mode: the run loop with a virtual walker, many times over.

    python -m jev_walker.sim --chooser random --runs 1000
    python -m jev_walker.sim --chooser heuristic --runs 1000 --mode pure
    python -m jev_walker.sim --chooser claude --runs 20 --keep-logs 20
    python -m jev_walker.sim --area sf --chooser heuristic --runs 1000 --walk-speed 1.2

Writes runs/<stamp>-<chooser>-<mode>/ with summary.json (aggregate), runs.jsonl
(one score line per run) and the first --keep-logs full run logs for the renderer.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path

from .choosers import make_chooser
from .graph import AREAS, DEFAULT_AREA, ROOT, load_graph
from .run import MILE_M, Run, RunConfig, dist_to_home


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def summarise(scores: list[dict], logs_repeat_nodes: collections.Counter, G) -> dict:
    n = len(scores)
    dist = [s["distance_mi"] for s in scores]
    closed = [s for s in scores if s["loop_closed"]]

    def where(node):
        nd = G.nodes[node]
        streets = sorted({d["name"] for *_, d in G.edges(node, data=True) if d["name"]})
        return {"node": node, "lonlat": [round(nd["x"], 6), round(nd["y"], 6)], "streets": streets}

    return {
        "runs": n,
        "finish_rate": round(len(closed) / n, 3),
        "clean_rate": round(sum(s["clean_finish"] for s in scores) / n, 3),
        "end_reasons": dict(collections.Counter(s["end_reason"] for s in scores)),
        "distance_mi": {"p10": pct(dist, 0.1), "p50": pct(dist, 0.5), "p90": pct(dist, 0.9)},
        "distance_mi_closed": {"p10": pct([s["distance_mi"] for s in closed], 0.1),
                               "p50": pct([s["distance_mi"] for s in closed], 0.5),
                               "p90": pct([s["distance_mi"] for s in closed], 0.9)},
        "repeats_mean": round(statistics.fmean(s["repeated_segments"] for s in scores), 2),
        "zero_repeat_rate": round(sum(s["repeated_segments"] == 0 for s in scores) / n, 3),
        "decisions_mean": round(statistics.fmean(s["decisions"] for s in scores), 1),
        "unique_streets_mean": round(statistics.fmean(s["unique_streets"] for s in scores), 1),
        "mean_top_probability": round(statistics.fmean(s["mean_top_probability"] or 0 for s in scores), 3),
        "total_cost_usd": round(sum(s["total_cost_usd"] for s in scores), 4),
        "median_latency_ms": statistics.median(s["median_latency_ms"] or 0 for s in scores),
        # Where walkers loop: decision points where the most repeated blocks begin.
        "repeat_hotspots": [{**where(node), "repeats": c} for node, c in logs_repeat_nodes.most_common(10)],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--area", default=DEFAULT_AREA, choices=sorted(AREAS))
    ap.add_argument("--chooser", default="random", choices=["random", "heuristic", "claude", "jev"])
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--mode", default="compass", choices=["compass", "pure"])
    ap.add_argument("--paths", default="named", choices=["named", "all"],
                    help="named: drop unnamed park paths (default); all: keep every footway")
    ap.add_argument("--arrive-radius", type=float, default=25.0,
                    help="offer arrive_home within this many metres of the door")
    ap.add_argument("--walk-speed", type=float, default=RunConfig.walk_speed_mps,
                    help="metres per second for the simulated clock (SF hills: try 1.2)")
    ap.add_argument("--time-cap", type=float, default=None,
                    help="minutes before the run is cut off (default: the area's, 150 or 180 for SF)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--keep-logs", type=int, default=10, help="full logs to keep for the renderer")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    G, home = load_graph(paths=args.paths, area=args.area)
    dist_home = dist_to_home(G, home)
    cfg = RunConfig.for_area(args.area, mode=args.mode, arrive_home_radius_m=args.arrive_radius,
                             walk_speed_mps=args.walk_speed, time_cap_min=args.time_cap)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.out or ROOT / "runs" / f"{stamp}-{args.area}-{args.chooser}-{args.mode}"
    out.mkdir(parents=True, exist_ok=True)

    scores, hotspots = [], collections.Counter()
    t0 = time.time()
    with open(out / "runs.jsonl", "w") as jl:
        for i in range(args.runs):
            seed = args.seed + i
            chooser = make_chooser(args.chooser, seed)
            run = Run(G, home, chooser, cfg, run_id=f"{stamp}-{i:04d}", dist_home=dist_home, seed=seed)
            log = run.run_to_end()
            log["header"]["paths"] = args.paths
            log["header"]["area"] = args.area
            s = log["score"]
            scores.append(s)
            for p in log["path"]:
                if p["repeat_hops"]:
                    hotspots[p["from"]] += p["repeat_hops"]
            jl.write(json.dumps({"run_id": log["header"]["run_id"], "seed": seed, **s}) + "\n")
            if i < args.keep_logs:
                (out / f"run-{i:04d}.json").write_text(json.dumps(log))
            if (i + 1) % max(1, args.runs // 10) == 0:
                print(f"  {i + 1}/{args.runs} runs, {time.time() - t0:.0f}s", file=sys.stderr)

    summary = {
        "area": args.area, "chooser": args.chooser, "mode": args.mode, "paths": args.paths,
        "graph_hash": G.graph["graph_hash"], "home_node": home.node,
        "target_mi": round(cfg.target_m / MILE_M, 2),
        **summarise(scores, hotspots, G),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "repeat_hotspots"}, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
