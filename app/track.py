"""Track cleaning, distance/moving-time measurement and day allocation.

Rules intentionally live in code rather than only in documentation: changing a
threshold creates a new statistics rule version, never silently changes an old
annual report.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .geo import haversine_m
from .timeutil import get_zone

MAX_JUMP_SPEED_MPS = 85.0
DEFAULT_MOVING_GAP_SECONDS = 30


@dataclass
class NormalizationResult:
    points: list[dict] = field(default_factory=list)
    distance_m: float = 0.0
    elapsed_s: int | None = None
    moving_s: int | None = None
    dropped_points: int = 0
    max_gap_s: int = DEFAULT_MOVING_GAP_SECONDS
    rule: str = "gps-v1"
    issues: list[str] = field(default_factory=list)


def _valid_coord(p: dict[str, Any]) -> bool:
    lat, lon = p.get("lat"), p.get("lon")
    return isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and -90 <= lat <= 90 and -180 <= lon <= 180


def _speed_mps(a: dict, b: dict) -> float | None:
    t = (b["utc"] - a["utc"]).total_seconds()
    if t <= 0:
        return None
    return haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) / t


def normalize_track(raw_points: list[dict], max_gap_s: int = DEFAULT_MOVING_GAP_SECONDS) -> NormalizationResult:
    """Sort, de-duplicate and remove impossible GPS jumps.

    A point is treated as a spike when both links around it are impossibly fast,
    or it leaves and returns to essentially the same position. A merely long
    pause is retained: it affects moving time via the gap threshold, not point
    deletion.
    """
    issues: list[str] = []
    prepared = []
    for p in raw_points:
        if not _valid_coord(p) or not p.get("utc"):
            issues.append("invalid_or_untimestamped_point")
            continue
        q = dict(p)
        q["utc"] = p["utc"] if isinstance(p["utc"], datetime) else p["utc"]
        prepared.append(q)

    prepared.sort(key=lambda x: x["utc"])
    deduped: list[dict] = []
    duplicate_count = 0
    for p in prepared:
        if deduped and p["utc"] == deduped[-1]["utc"]:
            duplicate_count += 1
            continue
        deduped.append(p)
    if duplicate_count:
        issues.append(f"removed_{duplicate_count}_duplicate_timestamps")

    flags = [False] * len(deduped)
    for i in range(1, len(deduped)):
        speed = _speed_mps(deduped[i - 1], deduped[i])
        if speed is not None and speed > MAX_JUMP_SPEED_MPS:
            # Interior teleport: the isolated fast out-and-back point is spike.
            if i + 1 < len(deduped):
                out = speed
                back = _speed_mps(deduped[i], deduped[i + 1])
                direct = haversine_m(
                    deduped[i - 1]["lat"], deduped[i - 1]["lon"],
                    deduped[i + 1]["lat"], deduped[i + 1]["lon"],
                )
                excursion = haversine_m(
                    deduped[i - 1]["lat"], deduped[i - 1]["lon"],
                    deduped[i]["lat"], deduped[i]["lon"],
                )
                if (back is not None and back > MAX_JUMP_SPEED_MPS) or (excursion > 500 and direct < max(100, excursion * 0.25)):
                    flags[i] = True
                    continue
            # If the current point begins a stable new location, the prior edge
            # alone is insufficient evidence; do not delete real travel.
            issues.append("fast_edge_retained_for_review")

    points = [p for p, bad in zip(deduped, flags) if not bad]
    dropped = len(raw_points) - len(points)

    distance = 0.0
    moving = 0
    for i in range(1, len(points)):
        seg = haversine_m(points[i - 1]["lat"], points[i - 1]["lon"], points[i]["lat"], points[i]["lon"])
        gap = (points[i]["utc"] - points[i - 1]["utc"]).total_seconds()
        distance += seg
        if gap <= max_gap_s:
            moving += int(round(gap))

    elapsed = None
    if len(points) >= 2:
        elapsed = int(round((points[-1]["utc"] - points[0]["utc"]).total_seconds()))
    if not points:
        issues.append("no_valid_points")
    return NormalizationResult(
        points=points,
        distance_m=round(distance, 3),
        elapsed_s=elapsed,
        moving_s=moving if points else None,
        dropped_points=max(0, dropped),
        max_gap_s=max_gap_s,
        issues=issues,
    )


def _interpolate(a: dict, b: dict, frac: float) -> tuple[float, float]:
    return (
        a["lat"] + (b["lat"] - a["lat"]) * frac,
        a["lon"] + (b["lon"] - a["lon"]) * frac,
    )


def allocate_by_local_day(
    points: list[dict],
    tz_name: str | None,
    total_distance_m: float | None = None,
    moving_s: int | None = None,
    max_gap_s: int = DEFAULT_MOVING_GAP_SECONDS,
    moving_gap_s: int | None = None,
) -> dict[str, dict[str, float]]:
    """Allocate track distance/moving seconds to local calendar days.

    Each segment crossing local midnight is split at the interpolated midnight
    location. This is the documented "cross-midnight mileage ownership" rule.
    """
    tz = get_zone(tz_name)
    moving_gap_s = max_gap_s if moving_gap_s is None else moving_gap_s
    alloc: dict[str, dict[str, float]] = defaultdict(lambda: {"distance_m": 0.0, "moving_s": 0.0})
    if not points:
        return {}

    gps_segments: list[tuple[dict, dict, float, float, float]] = []
    gps_total = 0.0
    moving_total = 0.0
    for a, b in zip(points, points[1:]):
        seg = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        gap_s = (b["utc"] - a["utc"]).total_seconds()
        moving_part = gap_s if gap_s <= moving_gap_s else 0.0
        gps_segments.append((a, b, seg, float(gap_s), float(moving_part)))
        gps_total += seg
        moving_total += moving_part

    if not gps_segments:
        day = points[0]["utc"].astimezone(tz).date().isoformat()
        if total_distance_m is not None:
            alloc[day]["distance_m"] = float(total_distance_m)
        if moving_s is not None:
            alloc[day]["moving_s"] = float(moving_s)
        return dict(alloc)

    for a, b, seg, gap_s, moving_part in gps_segments:
        start_local = a["utc"].astimezone(tz)
        end_local = b["utc"].astimezone(tz)
        start_day = start_local.date()
        end_day = end_local.date()
        if start_day == end_day:
            alloc[start_day.isoformat()]["distance_m"] += seg
            alloc[start_day.isoformat()]["moving_s"] += float(moving_part)
            continue

        total_seconds = (end_local - start_local).total_seconds()
        cursor = start_local
        cursor_lat, cursor_lon = a["lat"], a["lon"]
        for boundary_day in sorted({start_day, end_day}):
            midnight = datetime.combine(boundary_day, datetime.min.time(), tzinfo=tz)
            if midnight <= start_local or midnight >= end_local:
                continue
            frac = (midnight - start_local).total_seconds() / total_seconds if total_seconds else 0
            mid_lat, mid_lon = _interpolate(a, b, frac)
            first = haversine_m(cursor_lat, cursor_lon, mid_lat, mid_lon)
            first_seconds = (midnight - cursor).total_seconds()
            day_key = cursor.date().isoformat()
            alloc[day_key]["distance_m"] += first
            if moving_part:
                alloc[day_key]["moving_s"] += first_seconds
            cursor = midnight
            cursor_lat, cursor_lon = mid_lat, mid_lon
        last = haversine_m(cursor_lat, cursor_lon, b["lat"], b["lon"])
        last_seconds = (end_local - cursor).total_seconds()
        day_key = cursor.date().isoformat()
        alloc[day_key]["distance_m"] += last
        if moving_part:
            alloc[day_key]["moving_s"] += last_seconds

    # A hand-corrected total is authoritative. Split it proportionally while
    # retaining the GPS-derived day fractions.
    if total_distance_m is not None and gps_total > 0:
        scale = total_distance_m / gps_total
        for row in alloc.values():
            row["distance_m"] *= scale
    if moving_s is not None and moving_total > 0:
        scale = moving_s / moving_total
        for row in alloc.values():
            row["moving_s"] *= scale

    # Remove floating dust.
    return {
        day: {"distance_m": round(row["distance_m"], 3), "moving_s": round(row["moving_s"], 3)}
        for day, row in alloc.items()
    }
