"""Privacy: hide track near home/address before sharing.

脱敏必须同时作用于四条出口，任一遗漏都算泄漏：
1. 页面 GeoJSON 轨迹  2. 缩略图（静态 SVG，不含坐标文本）
3. 下载文件（GPX，仅含裁剪后点）  4. 聚合入口（边界/起终点/聚类网格）
聚合数据做网格四舍五入，点数过少/单聚点时整体拒绝暴露位置。
"""
from __future__ import annotations

import math

from .geo import Point, haversine_m

MIN_POINTS_TO_SHOW = 3
AGG_GRID_DEG = 0.05  # ~5 km 聚合网格
AGG_MIN_GROUPS = 2   # 单聚点时不提供聚合位置（可反推出住址）


def in_zone(p: Point, lat: float, lon: float, radius_m: float) -> bool:
    return haversine_m(p.lat, p.lon, lat, lon) <= radius_m


def redact_track(points: list[Point], zones: list[tuple[float, float, float]]) -> list[Point]:
    """Drop every point inside any privacy zone (four outlets share this cut)."""
    if not zones:
        return list(points)
    return [p for p in points
            if not any(in_zone(p, lat, lon, r) for lat, lon, r in zones)]


def _grid_snap(value: float, cell: float) -> float:
    return round(math.floor(value / cell) * cell + cell / 2, 5)


def aggregate_safe(points_by_activity, zones) -> dict | None:
    """Aggregate entry: snapped grid cells + grid bbox; None if it could reveal a zone.

    Rejects when almost every activity starts at home AND only one grid cell
    results — that cell would identify the address.
    """
    cells: set[tuple[float, float]] = set()
    n_total = 0
    n_starts_near = 0
    for pts in points_by_activity:
        clean = redact_track(pts, zones)
        if len(clean) < MIN_POINTS_TO_SHOW:
            continue
        n_total += 1
        if any(pts and haversine_m(pts[0].lat, pts[0].lon, lat, lon) <= r + 50
               for lat, lon, r in zones):
            n_starts_near += 1
        step = max(1, len(clean) // 10)
        for p in clean[::step]:
            cells.add((_grid_snap(p.lat, AGG_GRID_DEG), _grid_snap(p.lon, AGG_GRID_DEG)))
    if not cells or (n_total and n_starts_near / n_total > 0.8 and len(cells) < AGG_MIN_GROUPS):
        return None
    lats = [c[0] for c in cells]
    lons = [c[1] for c in cells]
    return {
        "grid_cells": sorted(cells),
        "bbox": [_grid_snap(min(lats), AGG_GRID_DEG), _grid_snap(min(lons), AGG_GRID_DEG),
                 _grid_snap(max(lats), AGG_GRID_DEG), _grid_snap(max(lons), AGG_GRID_DEG)],
        "note": "snapped to ~5km grid; raw start/end coordinates withheld",
    }


def track_to_geojson(points: list[Point]) -> dict:
    return {"type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[p.lon, p.lat] for p in points]}}


def render_thumbnail_svg(points: list[Point], size: int = 240) -> str:
    """Static thumbnail with no coordinate text — projected pixels only."""
    if len(points) < 2:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"/>'
    lats = [p.lat for p in points]
    lons = [p.lon for p in points]
    minlat, maxlat, minlon, maxlon = min(lats), max(lats), min(lons), max(lons)
    span_lat = max(1e-9, maxlat - minlat)
    span_lon = max(1e-9, maxlon - minlon)
    pad = size * 0.08

    def xy(p: Point) -> tuple[float, float]:
        x = pad + (p.lon - minlon) / span_lon * (size - 2 * pad)
        y = size - pad - (p.lat - minlat) / span_lat * (size - 2 * pad)
        return x, y

    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in map(xy, points))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
        f'<rect width="100%" height="100%" fill="#f6f8fa"/>'
        f'<polyline points="{coords}" fill="none" stroke="#2563eb" stroke-width="2"/>'
        f"</svg>"
    )


def to_gpx_bytes(points: list[Point], name: str = "shared-track") -> bytes:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
             f"<trk><name>{name}</name><trkseg>"]
    for p in points:
        ele = f"<ele>{p.ele}</ele>" if p.ele is not None else ""
        parts.append(
            f'<trkpt lat="{p.lat:.6f}" lon="{p.lon:.6f}">'
            f"<time>{p.time.isoformat()}</time>{ele}</trkpt>")
    parts.append("</trkseg></trk></gpx>")
    return "\n".join(parts).encode()
