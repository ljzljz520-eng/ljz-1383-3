"""Geodesic helpers used by track normalization and duplicate matching."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_000.0


def hav(a: float) -> float:
    return math.sin(a / 2.0) ** 2


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = hav(dphi) + math.cos(phi1) * math.cos(phi2) * hav(dlambda)
    return 2.0 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def path_distance_m(points: Sequence[dict]) -> float:
    total = 0.0
    prev = None
    for p in points:
        if p.get("lat") is None or p.get("lon") is None:
            prev = None
            continue
        if prev is not None:
            total += haversine_m(prev["lat"], prev["lon"], p["lat"], p["lon"])
        prev = p
    return total


def bbox(points: Iterable[dict]) -> tuple[float | None, float | None, float | None, float | None]:
    lats = [p["lat"] for p in points if p.get("lat") is not None and p.get("lon") is not None]
    lons = [p["lon"] for p in points if p.get("lat") is not None and p.get("lon") is not None]
    if not lats:
        return None, None, None, None
    return min(lats), min(lons), max(lats), max(lons)
