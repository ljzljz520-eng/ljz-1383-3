"""Training-file parsers.

Supported import formats are GPX, TCX and a documented JSON fixture. Parsers
never make medical inferences; they retain only raw activity/track facts.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import parse_dt


class ImportError_(ValueError):
    pass


@dataclass
class ParsedActivity:
    source_provider: str | None = None
    source_uid: str | None = None
    device_name: str | None = None
    activity_type: str = "run"
    name: str | None = None
    started_at: datetime | None = None
    tz_name: str | None = None
    device_distance_m: float | None = None
    device_elapsed_s: int | None = None
    device_moving_s: int | None = None
    is_race: bool = False
    raw_points: list[dict] = field(default_factory=list)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(node: ET.Element, wanted: str) -> list[ET.Element]:
    return [c for c in list(node) if _local_name(c.tag) == wanted]


def _child(node: ET.Element, wanted: str) -> ET.Element | None:
    for c in list(node):
        if _local_name(c.tag) == wanted:
            return c
    return None


def _num(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _extension_value(point_node: ET.Element, names: set[str]) -> float | None:
    for node in point_node.iter():
        if _local_name(node.tag) in names and node.text:
            value = _num(node.text.strip())
            if value is not None:
                return value
    return None


def parse_gpx(content: bytes) -> ParsedActivity:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ImportError_(f"Invalid GPX XML: {exc}") from exc
    if _local_name(root.tag) != "gpx":
        raise ImportError_("Not a GPX file")

    activity = ParsedActivity()
    metadata = _child(root, "metadata")
    if metadata is not None:
        name = _child(metadata, "name")
        if name is not None and name.text:
            activity.source_uid = name.text.strip()
    trk = _child(root, "trk")
    if trk is None:
        raise ImportError_("GPX contains no track")
    name = _child(trk, "name")
    if name is not None and name.text:
        activity.name = name.text.strip()
        if not activity.source_uid:
            activity.source_uid = activity.name

    for trkseg in _children(trk, "trkseg"):
        for pt in _children(trkseg, "trkpt"):
            lat = _num(pt.attrib.get("lat"))
            lon = _num(pt.attrib.get("lon"))
            t = _child(pt, "time")
            utc = parse_dt(t.text if t is not None else None)
            if lat is None or lon is None or utc is None:
                continue
            ele = _num(_child(pt, "ele").text if _child(pt, "ele") is not None else None)
            hr = _extension_value(pt, {"hr", "heartRate"})
            cadence = _extension_value(pt, {"cad", "cadence", "runCadence"})
            activity.raw_points.append({"lat": lat, "lon": lon, "utc": utc, "elevation_m": ele, "hr": hr, "cadence": cadence})
    if not activity.raw_points:
        raise ImportError_("GPX contains no timestamped track points")
    activity.started_at = activity.raw_points[0]["utc"]
    return activity


def parse_tcx(content: bytes) -> ParsedActivity:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ImportError_(f"Invalid TCX XML: {exc}") from exc
    activity = ParsedActivity()
    act_node = next((n for n in root.iter() if _local_name(n.tag) == "Activity"), None)
    if act_node is None:
        raise ImportError_("TCX contains no Activity")
    activity.activity_type = act_node.attrib.get("Sport", "Run").lower().replace("running", "run")
    laps = [n for n in act_node.iter() if _local_name(n.tag) == "Lap"]
    if not laps:
        raise ImportError_("TCX contains no Lap")

    distances: list[float] = []
    times: list[float] = []
    for lap in laps:
        d = _child(lap, "DistanceMeters")
        t = _child(lap, "TotalTimeSeconds")
        if d is not None:
            value = _num(d.text)
            if value is not None:
                distances.append(value)
        if t is not None:
            value = _num(t.text)
            if value is not None:
                times.append(value)
        for tp in [x for x in lap.iter() if _local_name(x.tag) == "Trackpoint"]:
            pos = _child(tp, "Position")
            tnode = _child(tp, "Time")
            utc = parse_dt(tnode.text if tnode is not None else None)
            if pos is None or utc is None:
                continue
            lat_n = _child(pos, "LatitudeDegrees")
            lon_n = _child(pos, "LongitudeDegrees")
            lat, lon = _num(lat_n.text if lat_n is not None else None), _num(lon_n.text if lon_n is not None else None)
            if lat is None or lon is None:
                continue
            hr_node = _child(tp, "HeartRateBpm")
            hr = _num(hr_node.text if hr_node is not None else None)
            cadence = _num(_child(tp, "Cadence").text if _child(tp, "Cadence") is not None else None)
            activity.raw_points.append({"lat": lat, "lon": lon, "utc": utc, "hr": hr, "cadence": cadence})

    if distances:
        activity.device_distance_m = round(sum(distances), 3)
    if times:
        activity.device_elapsed_s = int(round(sum(times)))
    if not activity.raw_points:
        raise ImportError_("TCX contains no timestamped track points")
    activity.started_at = activity.raw_points[0]["utc"]
    return activity


def parse_json(content: bytes) -> ParsedActivity:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportError_(f"Invalid JSON training file: {exc}") from exc
    if isinstance(data, list):
        raise ImportError_("Expected one activity object, received an array")
    if not isinstance(data, dict):
        raise ImportError_("JSON activity must be an object")
    points_data = data.get("points") or data.get("track") or []
    if not isinstance(points_data, list):
        raise ImportError_("points must be an array")

    activity = ParsedActivity(
        source_provider=data.get("source_provider") or data.get("provider"),
        source_uid=str(data.get("source_uid") or data.get("external_id") or data.get("id") or "").strip() or None,
        device_name=data.get("device_name") or data.get("device"),
        activity_type=(data.get("activity_type") or data.get("type") or "run"),
        name=data.get("name"),
        tz_name=data.get("tz_name") or data.get("timezone"),
        device_distance_m=_num_value(data.get("device_distance_m") or data.get("distance_m")),
        device_elapsed_s=_int_value(data.get("device_elapsed_s") or data.get("elapsed_s")),
        device_moving_s=_int_value(data.get("device_moving_s") or data.get("moving_s")),
        is_race=bool(data.get("is_race", False)),
    )
    for i, p in enumerate(points_data):
        if not isinstance(p, dict):
            continue
        utc = parse_dt(p.get("time") or p.get("utc") or p.get("timestamp"), activity.tz_name)
        lat, lon = _num_value(p.get("lat")), _num_value(p.get("lon"))
        if utc is None or lat is None or lon is None:
            continue
        activity.raw_points.append({
            "lat": lat,
            "lon": lon,
            "utc": utc,
            "elevation_m": _num_value(p.get("elevation_m") or p.get("elevation")),
            "hr": _num_value(p.get("hr") or p.get("heart_rate")),
            "cadence": _num_value(p.get("cadence")),
        })
    if not activity.raw_points:
        raise ImportError_("JSON contains no timestamped track points")
    activity.started_at = parse_dt(data.get("started_at"), activity.tz_name) or activity.raw_points[0]["utc"]
    return activity


def _num_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    num = _num_value(value)
    return None if num is None else int(round(num))


def parse_training_file(filename: str, content: bytes) -> ParsedActivity:
    lower = filename.lower()
    if lower.endswith(".gpx"):
        return parse_gpx(content)
    if lower.endswith(".tcx"):
        return parse_tcx(content)
    if lower.endswith(".json"):
        return parse_json(content)
    raise ImportError_("Unsupported file type; use .gpx, .tcx or .json")
