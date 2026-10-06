"""Training-file parsers: GPX and TCX (Garmin/Strava exports)."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

from .geo import Point, parse_ts_full


class ParseError(ValueError):
    pass


NS = {
    "gpx": "http://www.topografix.com/GPX/1/1",
    "tcx": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
}


def parse_gpx(data: bytes) -> list[Point]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError(f"invalid GPX XML: {exc}") from exc
    points: list[Point] = []
    for trkpt in root.iter():
        if not trkpt.tag.endswith("}trkpt"):
            continue
        try:
            lat = float(trkpt.attrib["lat"])
            lon = float(trkpt.attrib["lon"])
        except (KeyError, ValueError):
            continue
        t_el = next((c for c in trkpt if c.tag.endswith("}time")), None)
        if t_el is None or not t_el.text:
            continue
        ele_el = next((c for c in trkpt if c.tag.endswith("}ele")), None)
        ele = float(ele_el.text) if ele_el is not None and ele_el.text else None
        ts, off = parse_ts_full(t_el.text)
        points.append(Point(lat, lon, ts, ele, off))
    if not points:
        raise ParseError("GPX contains no track points")
    return points


def parse_tcx(data: bytes) -> list[Point]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ParseError(f"invalid TCX XML: {exc}") from exc
    points: list[Point] = []
    for tp in root.iter():
        if not tp.tag.endswith("}Trackpoint"):
            continue
        t_el = next((c for c in tp if c.tag.endswith("}Time")), None)
        pos_el = next((c for c in tp if c.tag.endswith("}Position")), None)
        if t_el is None or not t_el.text or pos_el is None:
            continue
        lat_el = next((c for c in pos_el if c.tag.endswith("}LatitudeDegrees")), None)
        lon_el = next((c for c in pos_el if c.tag.endswith("}LongitudeDegrees")), None)
        if lat_el is None or lon_el is None or lat_el.text is None or lon_el.text is None:
            continue
        try:
            lat, lon = float(lat_el.text), float(lon_el.text)
        except ValueError:
            continue
        alt_el = next((c for c in tp if c.tag.endswith("}AltitudeMeters")), None)
        ele = float(alt_el.text) if alt_el is not None and alt_el.text else None
        ts, off = parse_ts_full(t_el.text)
        points.append(Point(lat, lon, ts, ele, off))
    if not points:
        raise ParseError("TCX contains no trackpoints")
    return points


def detect_and_parse(filename: str, data: bytes) -> list[Point]:
    name = filename.lower()
    head = data[:4096].lstrip()
    if name.endswith(".gpx") or b"<gpx" in head:
        return parse_gpx(data)
    if name.endswith(".tcx") or b"TrainingCenterDatabase" in head:
        return parse_tcx(data)
    raise ParseError("unsupported file type (only .gpx/.tcx)")


def file_fingerprint(data: bytes) -> str:
    """Content hash: exact duplicate uploads of the same file are rejected."""
    return hashlib.sha256(data).hexdigest()
