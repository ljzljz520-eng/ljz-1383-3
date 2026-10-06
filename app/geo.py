"""Geo / time helpers.

口径公开（见 docs 与 /api/methodology）：
- 距离：WGS84 经纬度按 haversine 公式分段求和（地球半径 6371000 m）。
- GPS 跳点：相邻点瞬时速度超过 SPEED_SPIKE_MPS（默认 50 m/s ≈ 180 km/h）
  判定为设备误差点，从轨迹中剔除，不参与距离/时间统计；剔除会产生修订版本。
- 移动时间：相邻有效点速度 >= STOP_SPEED_MPS（默认 0.5 m/s）的相邻时间段累加；
  停留（红绿灯等）不计入移动时间，elapsed_time 仍为首个到末个有效点的墙钟跨度。
- 时区：解析文件内时间戳（统一转为 UTC 存储），归属日按活动 *开始* 的当地时间，
  跨午夜跑步全部里程计入开始日（v1/v2 口径差异见 rules.py）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

EARTH_RADIUS_M = 6_371_000.0
SPEED_SPIKE_MPS = 50.0
STOP_SPEED_MPS = 0.5


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Point:
    lat: float
    lon: float
    time: datetime  # tz-aware, UTC
    ele: Optional[float] = None
    offset_min: Optional[int] = None  # raw timestamp UTC offset before normalization


def parse_ts(value: str) -> datetime:
    """Parse ISO-8601 / XML-Schema timestamps; naive timestamps are assumed UTC."""
    return parse_ts_full(value)[0]


def parse_ts_full(value: str) -> tuple[datetime, int]:
    """Return (utc datetime, raw UTC offset minutes)."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    offset_min = int(dt.utcoffset().total_seconds() // 60)
    return dt.astimezone(timezone.utc), offset_min


def filter_spikes(points: list[Point], max_speed: float = SPEED_SPIKE_MPS) -> tuple[list[Point], list[int]]:
    """Remove GPS spikes.

    A point is a spike when speed from the previous *kept* point exceeds
    max_speed AND return speed to the following point also exceeds it
    (single-point teleport). A sustained high speed (real movement, e.g. train)
    fails the return check and is kept. Returns (kept_points, removed indices).
    """
    removed: list[int] = []
    if len(points) <= 2:
        return list(points), removed
    kept: list[Point] = []
    removed_set: set[int] = set()
    for i, p in enumerate(points):
        if not kept or i == len(points) - 1:
            kept.append(p)
            continue
        prev = kept[-1]
        nxt = points[i + 1]
        dt_out = (p.time - prev.time).total_seconds()
        dt_back = (nxt.time - p.time).total_seconds()
        if dt_out <= 0 or dt_back <= 0:
            kept.append(p)
            continue
        d_out = haversine_m(prev.lat, prev.lon, p.lat, p.lon)
        d_back = haversine_m(p.lat, p.lon, nxt.lat, nxt.lon)
        if d_out / dt_out > max_speed and d_back / dt_back > max_speed:
            removed_set.add(i)
            removed.append(i)
            continue  # skip p; next point compares against the same prev
        kept.append(p)
    return kept, sorted(removed_set)


@dataclass
class TrackStats:
    distance_m: float
    elapsed_s: float
    moving_s: float
    ascent_m: float
    descent_m: float
    n_points: int
    spikes_removed: int


def compute_stats(
    points: list[Point],
    stop_speed: float = STOP_SPEED_MPS,
) -> TrackStats:
    """Distance/time stats over cleaned, time-ordered points."""
    if not points:
        return TrackStats(0, 0, 0, 0, 0, 0, 0)
    pts = sorted(points, key=lambda p: p.time)
    distance = 0.0
    moving = 0.0
    ascent = descent = 0.0
    for a, b in zip(pts, pts[1:]):
        seg = haversine_m(a.lat, a.lon, b.lat, b.lon)
        distance += seg
        dt = (b.time - a.time).total_seconds()
        if dt > 0 and seg / dt >= stop_speed:
            moving += dt
        if a.ele is not None and b.ele is not None:
            dh = b.ele - a.ele
            if dh > 0:
                ascent += dh
            else:
                descent -= dh
    elapsed = (pts[-1].time - pts[0].time).total_seconds()
    return TrackStats(
        distance_m=distance,
        elapsed_s=max(0.0, elapsed),
        moving_s=moving,
        ascent_m=ascent,
        descent_m=descent,
        n_points=len(pts),
        spikes_removed=0,
    )


def bbox(points: Iterable[Point]) -> Optional[tuple[float, float, float, float]]:
    pts = list(points)
    if not pts:
        return None
    return (
        min(p.lat for p in pts),
        min(p.lon for p in pts),
        max(p.lat for p in pts),
        max(p.lon for p in pts),
    )
