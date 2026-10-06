"""验收3：跨午夜归属、时区；距离手工订正版本；设备估算vs用户确认。"""
from datetime import datetime, timedelta, timezone

from conftest import make_gpx, walk


def test_cross_midnight_attributed_to_start_day_local(client):
    # 2024-01-31 23:50 local UTC+8 == 15:50 UTC, run crosses midnight local & UTC
    off = timezone(timedelta(hours=8))
    start = datetime(2024, 1, 31, 23, 50, tzinfo=off)
    pts = walk(31.23, 121.47, start, n=8, dt_s=600, step_m=100, tz_off=480)
    r = client.post("/api/import",
                    files={"file": ("xm.gpx", make_gpx(pts), "application/gpx+xml")},
                    data={"source": "watch", "external_id": "xm1",
                          "rules_version": "v2-2024"})
    assert r.status_code == 200
    rep = client.post("/api/reports/generate",
                      json={"year": 2024, "rules_version": "v2-2024"}).json()
    # v2: local start date -> January bucket; whole distance, no split
    jan = rep["monthly"]["2024-01"]
    feb = rep["monthly"]["2024-02"]
    assert jan["activities"] == 1 and feb["activities"] == 0
    assert jan["distance_m"] > 0
    # v1 historical rule: UTC start is 2024-01-31 15:50 -> still January here,
    # construct a case where rules actually differ:
    rep1 = client.post("/api/reports/generate",
                       json={"year": 2024, "rules_version": "v1-2023"}).json()
    assert rep1["rules_version"] == "v1-2023"


def test_utc_vs_local_rule_differs_for_evdening_run(client):
    # local UTC-8: 2024-03-09 18:00 local == 2024-03-10 02:00 UTC
    off = timezone(timedelta(hours=-8))
    start = datetime(2024, 3, 9, 18, 0, tzinfo=off)
    pts = walk(34.0, -118.24, start, n=5, dt_s=600, step_m=100, tz_off=-480)
    client.post("/api/import",
                files={"file": ("la.gpx", make_gpx(pts), "application/gpx+xml")},
                data={"source": "watch", "external_id": "la1"})
    v2 = client.post("/api/reports/generate",
                     json={"year": 2024, "rules_version": "v2-2024"}).json()
    v1 = client.get("/api/reports/2024?rules_version=v1-2023").status_code
    # v1 not generated yet
    assert v1 == 404
    v1 = client.post("/api/reports/generate",
                     json={"year": 2024, "rules_version": "v1-2023"}).json()
    assert v2["monthly"]["2024-03"]["activities"] == 1          # local March 9
    assert v1["monthly"]["2024-03"]["activities"] == 1          # UTC March 10 -> still March
    # Year boundary: UTC-8 evening Dec 31 local == Jan 1 next year UTC
    off2 = timezone(timedelta(hours=-8))
    start2 = datetime(2024, 12, 31, 18, 30, tzinfo=off2)
    pts2 = walk(34.0, -118.24, start2, n=5, dt_s=600, step_m=100, tz_off=-480)
    client.post("/api/import",
                files={"file": ("nye.gpx", make_gpx(pts2), "application/gpx+xml")},
                data={"source": "watch", "external_id": "nye1"})
    v2b = client.post("/api/reports/2024/regenerate",
                      json={"rules_version": "v2-2024"}).json()
    v1b = client.post("/api/reports/2024/regenerate",
                      json={"rules_version": "v1-2023"}).json()
    # local Dec 31 -> December; UTC Jan 1 2025 -> drops out of 2024 entirely under v1
    assert v2b["monthly"]["2024-12"]["activities"] >= 1
    v1_dec = sum(m["activities"] for m in v1b["monthly"].values())
    v2_dec = sum(m["activities"] for m in v2b["monthly"].values())
    assert v2_dec == v1_dec + 1


def test_manual_distance_correction_keeps_versions(client):
    start = datetime(2024, 5, 5, 7, 0, tzinfo=timezone.utc)
    gpx = make_gpx(walk(31.23, 121.47, start, n=10, step_m=100))
    imp = client.post("/api/import", files={"file": ("r.gpx", gpx, "application/gpx+xml")},
                      data={"source": "watch"}).json()
    aid = imp["activity_id"]
    detail0 = client.get(f"/api/activities/{aid}").json()
    assert detail0["distance_origin"] == "device_estimated"
    old = imp["stats"]["distance_m"]
    r = client.post(f"/api/activities/{aid}/correct",
                    json={"distance_m": 5000, "reason": "隧道丢点按路标订正"})
    assert r.status_code == 200
    assert r.json()["old_distance_m"] == old
    detail = client.get(f"/api/activities/{aid}").json()
    assert detail["distance_m"] == 5000.0
    assert detail["distance_origin"] == "user_confirmed"
    assert detail["corrections"][0]["origin"] == "user_confirmed"


def test_methodology_public(client):
    m = client.get("/api/methodology").json()
    for k in ("distance", "moving_time", "cross_midnight", "dedup",
              "races_vs_training", "estimation"):
        assert m[k]
