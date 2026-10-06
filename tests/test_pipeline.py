"""验收1：GPS 跳点剔除 + 规范化统计（距离/移动时间/停留）。"""
from datetime import datetime, timedelta, timezone

from conftest import make_gpx, walk
from app.geo import SPEED_SPIKE_MPS


def test_gps_spike_is_removed(client):
    start = datetime(2024, 6, 1, 23, 0, tzinfo=timezone.utc)
    pts = walk(31.230, 121.470, start, n=12, dt_s=20, step_m=60)
    # teleport away and back: classic single-point GPS spike
    spike_idx = 6
    pts[spike_idx] = (35.0, 130.0, pts[spike_idx][2])
    gpx = make_gpx(pts)
    r = client.post("/api/import",
                    files={"file": ("spike.gpx", gpx, "application/gpx+xml")},
                    data={"source": "watch", "external_id": "w1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["stats"]["spikes_removed"] == 1
    # distance stays ~11*60m, not inflated by the teleport
    assert j["stats"]["distance_m"] < 2000
    acts = client.get("/api/activities").json()
    assert acts[0]["spikes_removed"] == 1


def test_moving_time_excludes_stop(client):
    start = datetime(2024, 5, 1, 0, 0, tzinfo=timezone.utc)
    pts = walk(31.20, 121.40, start, n=10, dt_s=30, step_m=90)
    # 5 minute full stop in the middle (same coordinate)
    pause_t = pts[4][2]
    for k in range(1, 11):
        pts.insert(4 + k, (pts[4][0], pts[4][1], pause_t + timedelta(seconds=30 * k)))
    gpx = make_gpx(pts)
    r = client.post("/api/import",
                    files={"file": ("pause.gpx", gpx, "application/gpx+xml")},
                    data={"source": "watch"})
    j = r.json()
    st = j["stats"]
    assert st["moving_s"] < st["elapsed_s"]
    assert st["elapsed_s"] >= 300  # wall clock includes the pause


def test_tcx_supported(client):
    start = datetime(2024, 5, 2, tzinfo=timezone.utc)
    pts = walk(31.20, 121.40, start, n=8)
    body = ['<?xml version="1.0"?><TrainingCenterDatabase xmlns='
            '"http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"><Activities>'
            '<Activity><Lap><Track>']
    for lat, lon, t in pts:
        body.append(f'<Trackpoint><Time>{t.isoformat()}</Time>'
                    f'<Position><LatitudeDegrees>{lat}</LatitudeDegrees>'
                    f'<LongitudeDegrees>{lon}</LongitudeDegrees></Position></Trackpoint>')
    body.append("</Track></Lap></Activity></Activities></TrainingCenterDatabase>")
    r = client.post("/api/import",
                    files={"file": ("a.tcx", "".join(body).encode(), "text/xml")},
                    data={"source": "watch"})
    assert r.status_code == 200
    assert r.json()["stats"]["n_points"] == 8


def test_bad_file_rejected(client):
    r = client.post("/api/import",
                    files={"file": ("x.gpx", b"not xml at all", "text/plain")})
    assert r.status_code == 422
