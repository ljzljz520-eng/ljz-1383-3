"""验收5：年报快照、新增记录不入报、图表同数据范围、成绩与训练分开、历史规则重算。"""
from datetime import datetime, timedelta, timezone

from conftest import make_gpx, walk


def _run(client, day, ext, lat=31.23, lon=121.47, n=8):
    start = datetime(2024, day[0], day[1], 7, 0, tzinfo=timezone.utc)
    gpx = make_gpx(walk(lat, lon, start, n=n, step_m=100))
    return client.post("/api/import",
                       files={"file": (f"{ext}.gpx", gpx, "application/gpx+xml")},
                       data={"source": "watch", "external_id": ext}).json()["activity_id"]


def test_report_snapshot_isolates_new_records(client):
    _run(client, (1, 10), "a1")
    _run(client, (2, 10), "a2")
    rep = client.post("/api/reports/generate",
                      json={"year": 2024, "rules_version": "v2-2024"}).json()
    assert rep["data_range"]["count"] == 2
    assert len(rep["data_range"]["frozen_activity_ids"]) == 2

    # new upload arrives DURING/AFTER generation — snapshot must not change
    _run(client, (3, 10), "a3")
    fetched = client.get("/api/reports/2024?rules_version=v2-2024").json()
    assert fetched["data_range"]["count"] == 2
    assert fetched["monthly"]["2024-03"]["activities"] == 0
    # regenerate picks up the new record
    regen = client.post("/api/reports/2024/regenerate",
                        json={"rules_version": "v2-2024"}).json()
    assert regen["data_range"]["count"] == 3
    assert regen["monthly"]["2024-03"]["activities"] == 1


def test_all_charts_share_same_data_range(client):
    _run(client, (1, 5), "c1")
    aid = _run(client, (1, 20), "c2")
    rep = client.post("/api/reports/generate",
                      json={"year": 2024, "rules_version": "v2-2024"}).json()
    # monthly chart count and training totals must reconcile to frozen ids
    month_total = sum(m["activities"] for m in rep["monthly"].values())
    assert month_total == rep["training"]["activities"] == rep["data_range"]["count"] == 2
    # convert one training activity into a race result
    client.post("/api/race-results",
                json={"activity_id": aid, "race_name": "上海半马",
                      "chip_time_s": 5400, "distance_m": 21097.5, "overall_rank": 120})
    rep2 = client.post("/api/reports/2024/regenerate",
                       json={"rules_version": "v2-2024"}).json()
    assert rep2["training"]["activities"] == 1            # race excluded from training
    assert len(rep2["races"]) == 1
    assert rep2["races"][0]["chip_time_s"] == 5400
    # reconciliation still holds: trainings + races == frozen ids
    assert rep2["training"]["activities"] + len(rep2["races"]) == rep2["data_range"]["count"]


def test_device_vs_confirmed_separated(client):
    aid = _run(client, (4, 1), "d1")
    client.post(f"/api/activities/{aid}/correct", json={"distance_m": 1000})
    rep = client.post("/api/reports/generate",
                      json={"year": 2024, "rules_version": "v2-2024"}).json()
    assert rep["training"]["user_confirmed_activities"] == 1
    assert rep["training"]["device_only_activities"] == 0
    m = rep["monthly"]["2024-04"]
    assert m["confirmed_distance_m"] == 1000 and m["device_distance_m"] == 0


def test_historical_report_regenerated_with_original_rules(client):
    # UTC-8 evening Dec 31 2023 local == 2024-01-01 UTC
    start = datetime(2023, 12, 31, 18, 30, tzinfo=timezone(timedelta(hours=-8)))
    pts = walk(34.0, -118.24, start, n=5, tz_off=-480, step_m=100)
    client.post("/api/import",
                files={"file": ("old.gpx", make_gpx(pts), "application/gpx+xml")},
                data={"source": "watch", "external_id": "old1"})
    # generated at the time under v1: UTC Jan 1 2024 -> not in 2023 report
    old = client.post("/api/reports/generate",
                      json={"year": 2023, "rules_version": "v1-2023"}).json()
    assert old["data_range"]["count"] == 0
    # years later, regenerating the 2023 report must still reproduce v1 result
    again = client.post("/api/reports/2023/regenerate",
                        json={"rules_version": "v1-2023"}).json()
    assert again["rules_version"] == "v1-2023"
    assert again["data_range"]["count"] == 0
    # but the same run viewed under v2 local rule belongs to 2023 Dec
    with_v2 = client.post("/api/reports/generate",
                          json={"year": 2023, "rules_version": "v2-2024"}).json()
    assert with_v2["data_range"]["count"] == 1
