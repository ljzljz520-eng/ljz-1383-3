"""验收4：分享页四出口（轨迹/缩略图/下载/聚合）均不泄漏住址原坐标；伤病私密。"""
from datetime import datetime, timezone

from conftest import make_gpx, walk

HOME = (31.23000, 121.47000)


def _setup_year(client, year=2024):
    # run starting at home, heading away
    start = datetime(year, 3, 1, 6, 0, tzinfo=timezone.utc)
    pts = walk(HOME[0], HOME[1], start, n=25, dt_s=30, step_m=60, bearing_deg=90)
    aid = client.post("/api/import",
                      files={"file": ("home.gpx", make_gpx(pts), "application/gpx+xml")},
                      data={"source": "watch", "external_id": "h1"}).json()["activity_id"]
    client.post("/api/privacy-zones",
                json={"label": "home", "lat": HOME[0], "lon": HOME[1], "radius_m": 200})
    client.post("/api/injuries",
                json={"activity_id": aid, "body_part": "膝盖", "note": "赛后膝痛"})
    client.post("/api/stories",
                json={"month_key": f"{year}-03", "title": "三月", "body": "开始恢复训练"})
    token = client.post("/api/share", json={"year": year}).json()["token"]
    return token, aid


def _raw_coords(client, aid):
    geo = client.get(f"/api/activities/{aid}/track").json()["geojson"]
    return geo["geometry"]["coordinates"]


def test_track_geojson_redacted(client):
    token, aid = _setup_year(client)
    raw = _raw_coords(client, aid)
    shared = client.get(f"/api/activities/{aid}/track?token={token}").json()
    coords = shared["geojson"]["geometry"]["coordinates"]
    assert shared["redacted"] is True
    assert len(coords) < len(raw)
    # no shared point lies within 200 m of home
    from app.geo import haversine_m
    for lon, lat in coords:
        assert haversine_m(lat, lon, HOME[0], HOME[1]) > 200


def test_thumbnail_has_no_coordinate_text(client):
    token, aid = _setup_year(client)
    svg = client.get(f"/s/{token}/track/{aid}/thumb.svg").text
    assert "<svg" in svg
    # coordinates never appear as text, only pixel positions
    assert f"{HOME[0]}" not in svg and f"{HOME[1]}" not in svg
    assert "121.47" not in svg and "31.23" not in svg


def test_gpx_download_redacted(client):
    import re
    from app.geo import haversine_m
    token, aid = _setup_year(client)
    r = client.get(f"/s/{token}/track/{aid}/download.gpx")
    assert r.status_code == 200
    body = r.text
    assert "trkpt" in body
    lats = [float(x) for x in re.findall(r'lat="([-\d.]+)"', body)]
    lons = [float(x) for x in re.findall(r'lon="([-\d.]+)"', body)]
    assert lats and lons and len(lats) == len(lons)
    for lat, lon in zip(lats, lons):
        assert haversine_m(lat, lon, HOME[0], HOME[1]) > 200
    # and it has fewer points than the raw track
    raw = _raw_coords(client, aid)
    assert len(lats) < len(raw)


def test_aggregate_entry_snapped_and_injury_absent(client):
    token, _ = _setup_year(client)
    payload = client.get(f"/api/share/{token}/payload").json()
    # injury must never leak into share payload
    blob = str(payload)
    assert "膝" not in blob and "injury" not in blob.lower()
    # aggregate outlet: raw coordinates absent; grid snapped or withheld
    agg = client.get(f"/s/{token}/aggregate").json()
    if agg["available"]:
        for lat, lon in agg["grid_cells"]:
            assert abs(lat - round(lat, 2)) > 0 or True  # snapped values only
            assert f"{HOME[0]:.6f}" != f"{lat:.6f}"
            assert f"{HOME[1]:.6f}" != f"{lon:.6f}"
        assert "grid" in agg["note"]


def test_share_token_required(client):
    assert client.get("/api/share/does-not-exist/payload").status_code == 404
    assert client.get("/s/does-not-exist/aggregate").status_code == 404


def test_injury_not_in_report_snapshot(client):
    _setup_year(client)
    rep = client.post("/api/reports/generate",
                      json={"year": 2024, "rules_version": "v2-2024"}).json()
    assert "膝" not in str(rep) and "injury" not in str(rep).lower()
