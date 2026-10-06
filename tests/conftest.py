import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    # service upload dir into tmp
    from app import service
    monkeypatch.setattr(service, "UPLOAD_DIR", tmp_path / "uploads")
    db.init_db(db_path)
    with TestClient(app) as c:
        yield c


def make_gpx(points, name="run"):
    """points: list of (lat, lon, datetime tz-aware[, ele])"""
    pts = []
    for p in points:
        lat, lon, t = p[0], p[1], p[2]
        ele = f"<ele>{p[3]}</ele>" if len(p) > 3 else ""
        pts.append(
            f'<trkpt lat="{lat}" lon="{lon}"><time>{t.isoformat()}</time>{ele}</trkpt>')
    return ('<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<trk><name>{name}</name><trkseg>{''.join(pts)}</trkseg></trk></gpx>").encode()


def walk(lat0, lon0, start, n=20, dt_s=30, step_m=80, bearing_deg=0.0, tz_off=0):
    """Generate a straight-ish track by approximating metres->degrees."""
    import math
    pts = []
    lat, lon = lat0, lon0
    off = timezone(timedelta(minutes=tz_off))
    for i in range(n):
        t = (start + timedelta(seconds=dt_s * i)).astimezone(off)
        pts.append((lat, lon, t))
        d_lat = (step_m * math.cos(math.radians(bearing_deg))) / 111_320
        d_lon = (step_m * math.sin(math.radians(bearing_deg))) / (
            111_320 * max(1e-9, math.cos(math.radians(lat))))
        lat += d_lat
        lon += d_lon
    return pts
