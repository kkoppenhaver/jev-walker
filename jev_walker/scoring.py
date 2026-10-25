"""Score a run log on what Five Mile Walk optimises, plus the model-only numbers."""

from __future__ import annotations

import statistics

MILE_M = 1609.344
LOW_CONFIDENCE = 0.4


def score(log: dict, cfg) -> dict:
    end = log["end"]
    decisions = log["decisions"]
    path = log["path"]
    walked = end["walked_m"]
    closed = end["reason"] == "home"
    repeats = sum(p["repeat_hops"] for p in path)
    overrides = sum(1 for d in decisions if d.get("override"))
    top_p = [max(d["probabilities"].values()) for d in decisions if d.get("probabilities")]
    latencies = [d["latency_ms"] for d in decisions]
    streets = {p["street"] for p in path if p["street"]}
    in_window = cfg.clean_lo_m <= walked <= cfg.clean_hi_m
    return {
        "loop_closed": closed,
        "clean_finish": closed and in_window and repeats == 0,
        "end_reason": end["reason"],
        "distance_mi": round(walked / MILE_M, 3),
        "distance_error_mi": round((walked - cfg.target_m) / MILE_M, 3),
        "repeated_segments": repeats,
        "unique_streets": len(streets),
        "overrides": overrides,
        "decisions": len(decisions),
        "mean_top_probability": round(statistics.fmean(top_p), 3) if top_p else None,
        "low_confidence_corners": sum(p < LOW_CONFIDENCE for p in top_p),
        "total_cost_usd": round(sum(d.get("cost_usd") or 0 for d in decisions), 4),
        "median_latency_ms": round(statistics.median(latencies), 1) if latencies else None,
        "elapsed_min": end["elapsed_min"],
    }
