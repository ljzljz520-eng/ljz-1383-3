"""Duplicate detection.

Two uploads are duplicates only when source identity matches OR there is strong
*overlapping* spatial-temporal evidence. Two genuine back-to-back sessions must
not be merged merely because their timestamps are near each other: disjoint
tracks produce a low score even with zero gap between them.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from .geo import haversine_m

# Conservative defaults; exposed in the methodology endpoint.
MAX_CLOCK_SKEW_S = 180
SOURCE_MATCH_WINDOW_S = 30 * 60
START_PROXIMITY_M = 120
MEAN_PATH_PROXIMITY_M = 80
MIN_TIME_OVERLAP = 0.55
SPATIO_SCORE_THRESHOLD = 0.82
SAMPLES = 32


def time_interval(activity: dict) -> tuple:
    return activity["started_dt"], activity.get("ended_dt")


def temporal_overlap(a: dict, b: dict) -> float:
    a0, a1 = time_interval(a)
    b0, b1 = time_interval(b)
    if not a1 or not b1:
        # Point/unknown durations: only count overlap when starts nearly agree.
        delta = abs((a0 - b0).total_seconds())
        return 1.0 if delta <= MAX_CLOCK_SKEW_S else 0.0
    left, right = max(a0, b0), min(a1, b1)
    inter = max(0.0, (right - left).total_seconds())
    union = max(1.0, (max(a1, b1) - min(a0, b0)).total_seconds())
    return inter / union


def resample(points: list[dict], n: int = SAMPLES) -> list[dict]:
    if len(points) <= 1:
        return points
    if len(points) == n:
        return points
    out: list[dict] = []
    for i in range(n):
        pos = i * (len(points) - 1) / (n - 1)
        lo = int(math.floor(pos))
        hi = min(len(points) - 1, math.ceil(pos))
        frac = pos - lo
        if lo == hi:
            out.append(points[lo])
        else:
            out.append({
                "lat": points[lo]["lat"] + (points[hi]["lat"] - points[lo]["lat"]) * frac,
                "lon": points[lo]["lon"] + (points[hi]["lon"] - points[lo]["lon"]) * frac,
            })
    return out


def mean_resampled_distance(points_a: list[dict], points_b: list[dict]) -> float | None:
    if not points_a or not points_b:
        return None
    a, b = resample(points_a), resample(points_b)
    total = 0.0
    for x, y in zip(a, b):
        total += haversine_m(x["lat"], x["lon"], y["lat"], y["lon"])
    return total / len(a)


def compare(a: dict, b: dict) -> dict:
    """Return a match verdict with evidence and basis."""
    same_provider = bool(a.get("source_provider") and a.get("source_provider") == b.get("source_provider"))
    same_source_uid = bool(same_provider and a.get("source_uid") and a.get("source_uid") == b.get("source_uid"))
    same_content = bool(a.get("content_sha256") and a.get("content_sha256") == b.get("content_sha256"))
    start_delta_s = abs((a["started_dt"] - b["started_dt"]).total_seconds())
    overlap = temporal_overlap(a, b)

    pa, pb = a.get("points") or [], b.get("points") or []
    start_distance = None
    end_distance = None
    mean_distance = None
    path_score = 0.0
    if pa and pb:
        start_distance = haversine_m(pa[0]["lat"], pa[0]["lon"], pb[0]["lat"], pb[0]["lon"])
        end_distance = haversine_m(pa[-1]["lat"], pa[-1]["lon"], pb[-1]["lat"], pb[-1]["lon"])
        mean_distance = mean_resampled_distance(pa, pb)
        path_score = math.exp(-(mean_distance or 9999) / MEAN_PATH_PROXIMITY_M)

    time_score = max(0.0, 1.0 - start_delta_s / 3600.0)
    score = round(min(1.0, 0.55 * path_score + 0.45 * overlap + 0.05 * time_score), 4)

    if same_content:
        return {"match": True, "basis": "content_sha256", "score": 1.0, "temporal_overlap": overlap,
                "start_distance_m": start_distance, "mean_path_distance_m": mean_distance,
                "start_delta_s": start_delta_s}

    if same_source_uid and start_delta_s <= SOURCE_MATCH_WINDOW_S:
        return {"match": True, "basis": "provider_source_uid", "score": 1.0, "temporal_overlap": overlap,
                "start_distance_m": start_distance, "mean_path_distance_m": mean_distance,
                "start_delta_s": start_delta_s}

    # Temporal proximity alone can never trigger a merge: require both temporal
    # overlap and geometric agreement of the sampled trajectories.
    spatial_ok = (
        mean_distance is not None
        and mean_distance <= START_PROXIMITY_M
        and (start_distance is None or start_distance <= START_PROXIMITY_M * 3)
    )
    spatio_match = overlap >= MIN_TIME_OVERLAP and spatial_ok and score >= SPATIO_SCORE_THRESHOLD
    if spatio_match:
        return {"match": True, "basis": "spatiotemporal", "score": score, "temporal_overlap": round(overlap, 4),
                "start_distance_m": round(start_distance, 2) if start_distance is not None else None,
                "end_distance_m": round(end_distance, 2) if end_distance is not None else None,
                "mean_path_distance_m": round(mean_distance, 2), "start_delta_s": round(start_delta_s, 1)}

    return {"match": False, "basis": "none", "score": score, "temporal_overlap": round(overlap, 4),
            "start_distance_m": round(start_distance, 2) if start_distance is not None else None,
            "end_distance_m": round(end_distance, 2) if end_distance is not None else None,
            "mean_path_distance_m": round(mean_distance, 2) if mean_distance is not None else None,
            "start_delta_s": round(start_delta_s, 1)}
