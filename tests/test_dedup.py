"""验收2：重复上传不双算；相邻真实训练不被时间接近误合并；人工拆分恢复。"""
from datetime import datetime, timedelta, timezone

from conftest import make_gpx, walk


def _upload(client, gpx, source, ext):
    return client.post("/api/import",
                       files={"file": (f"{ext}.gpx", gpx, "application/gpx+xml")},
                       data={"source": source, "external_id": ext}).json()


def test_watch_and_phone_same_run_not_double_counted(client):
    start = datetime(2024, 4, 10, 6, 30, tzinfo=timezone.utc)
    base = walk(31.2300, 121.4700, start, n=20, dt_s=20, step_m=70, bearing_deg=45)
    # phone: tiny GPS jitter, same window
    phone = [(lat + 0.00005, lon - 0.00004, t) for lat, lon, t in base]
    a = _upload(client, make_gpx(base), "watch", "garmin:9001")
    b = _upload(client, make_gpx(phone), "phone", "apple:555")
    assert b["match"]["duplicate"] is True
    acts = client.get("/api/activities").json()
    assert len(acts) == 1  # only canonical counted
    # both sources preserved under one activity identity
    detail = client.get(f"/api/activities/{a['activity_id']}").json()
    assert len(detail["sources"]) == 2


def test_exact_duplicate_file_rejected(client):
    start = datetime(2024, 4, 11, 7, 0, tzinfo=timezone.utc)
    gpx = make_gpx(walk(31.23, 121.47, start, n=10))
    _upload(client, gpx, "watch", "w100")
    r = client.post("/api/import",
                    files={"file": ("same.gpx", gpx, "application/gpx+xml")},
                    data={"source": "watch", "external_id": "w100"})
    assert r.status_code == 409


def test_back_to_back_real_runs_not_merged_by_time(client):
    # morning and evening segment on the same track, disjoint time windows
    s1 = datetime(2024, 4, 12, 6, 0, tzinfo=timezone.utc)
    s2 = datetime(2024, 4, 12, 18, 0, tzinfo=timezone.utc)
    a = _upload(client, make_gpx(walk(31.230, 121.470, s1, n=15)), "watch", "run-am")
    b = _upload(client, make_gpx(walk(31.230, 121.470, s2, n=15)), "watch", "run-pm")
    assert b["match"]["duplicate"] is False
    assert "time_disjoint" in b["match"]["reason"]
    assert len(client.get("/api/activities").json()) == 2


def test_time_close_but_elsewhere_not_merged(client):
    s1 = datetime(2024, 4, 13, 6, 0, tzinfo=timezone.utc)
    s2 = s1 + timedelta(seconds=60)  # nearly identical time window start
    _upload(client, make_gpx(walk(31.230, 121.470, s1, n=15)), "phone", "city-a")
    b = _upload(client, make_gpx(walk(39.900, 116.400, s2, n=15)), "watch", "city-b")
    assert b["match"]["duplicate"] is False
    assert "space_differs" in b["match"]["reason"]
    assert len(client.get("/api/activities").json()) == 2


def test_manual_split_recovers_and_is_sticky(client):
    start = datetime(2024, 4, 14, 6, 30, tzinfo=timezone.utc)
    base = walk(31.2300, 121.4700, start, n=20, dt_s=20, step_m=70, bearing_deg=45)
    phone = [(lat + 0.00003, lon + 0.00003, t) for lat, lon, t in base]
    a = _upload(client, make_gpx(base), "watch", "same-1")
    b = _upload(client, make_gpx(phone), "phone", "same-2")
    assert b["match"]["duplicate"] is True
    dropped = b["activity_id"]
    # user knows these are two loops wrongly identical-looking; split them
    r = client.post("/api/links/split", json={"a": a["activity_id"], "b": dropped})
    assert r.status_code == 200
    acts = client.get("/api/activities").json()
    assert len(acts) == 2  # recovered
    links = client.get("/api/links").json()
    assert any(l["decision"] == "split" for l in links)
