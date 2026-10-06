"""Runner year-report site — FastAPI application."""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, privacy, reports, service
from .geo import parse_ts
from .parsers import ParseError
from .rules import CURRENT_RULES, RULES, get_rules
from .service import DuplicateFileError

BASE = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Runner Year Report", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

conn_factory = db.init_db


def conn():
    return db.get_conn()


# ---------------------------------------------------------------- methodology
@app.get("/api/methodology")
def methodology():
    return {
        "distance": "haversine(WGS84), GPS spikes >50 m/s removed (teleport test)",
        "moving_time": "segments >=0.5 m/s; elapsed = wall clock first->last valid fix",
        "cross_midnight": "full distance attributed to activity START date; "
                          "v1 uses UTC date, v2 uses local-timezone date",
        "dedup": "same source external id + time overlap, OR (time similar AND "
                 "space similar); proximity in only one dimension never merges; "
                 "manual split is sticky and reversible only by explicit merge",
        "races_vs_training": "races counted in results only, excluded from training mileage",
        "estimation": "device_estimated vs user_confirmed kept distinct; site gives no "
                      "training or injury-treatment advice",
        "rules_versions": {k: v.to_dict() for k, v in RULES.items()},
        "current": CURRENT_RULES,
        "privacy": "shared pages cut points inside privacy zones on track, thumbnail, "
                   "download and aggregate outlets; injury notes are always private",
    }


# ---------------------------------------------------------------- import
@app.post("/api/import")
async def import_file(
    file: UploadFile = File(...),
    source: str = Form("unknown"),
    external_id: str | None = Form(None),
    kind: str = Form("training"),
    title: str | None = Form(None),
    rules_version: str | None = Form(None),
):
    data = await file.read()
    try:
        result = service.create_activity_from_upload(
            conn(), filename=file.filename or "upload", data=data, source=source,
            external_id=external_id, kind=kind, title=title, rules_version=rules_version)
    except DuplicateFileError as exc:
        raise HTTPException(409, f"duplicate file not double-counted: {exc}")
    except ParseError as exc:
        raise HTTPException(422, f"cannot parse file: {exc}")
    return result


# ---------------------------------------------------------------- activities
def _activity_brief(r) -> dict:
    stats = json.loads(r["stats_json"])
    return {
        "id": r["id"], "kind": r["kind"], "title": r["title"],
        "start_time_utc": r["start_time_utc"],
        "tz_offset_min": r["start_tz_offset_min"],
        "canonical": bool(r["canonical"]),
        "distance_m": round(stats.get("distance_m", 0.0), 1),
        "moving_s": round(stats.get("moving_s", 0.0), 1),
        "elapsed_s": round(stats.get("elapsed_s", 0.0), 1),
        "spikes_removed": stats.get("spikes_removed", 0),
        "distance_origin": stats.get("distance_origin", "device_estimated"),
    }


@app.get("/api/activities")
def list_activities(canonical_only: bool = True):
    sql = "SELECT * FROM activity"
    if canonical_only:
        sql += " WHERE canonical=1"
    sql += " ORDER BY start_time_utc"
    return [_activity_brief(r) for r in conn().execute(sql).fetchall()]


@app.get("/api/activities/{activity_id}")
def activity_detail(activity_id: int):
    c = conn()
    r = c.execute("SELECT * FROM activity WHERE id=?", (activity_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    stats = json.loads(r["stats_json"])
    sources = [dict(s) for s in c.execute(
        "SELECT s.id,s.filename,s.source,s.external_id,s.fingerprint,s.uploaded_at "
        "FROM source_file s JOIN activity_source a ON s.id=a.source_id "
        "WHERE a.activity_id=?", (activity_id,)).fetchall()]
    corrections = [dict(x) for x in c.execute(
        "SELECT field,old_value,new_value,origin,reason,created_at FROM correction "
        "WHERE activity_id=? ORDER BY id", (activity_id,)).fetchall()]
    links = [dict(x) for x in c.execute(
        "SELECT activity_a,activity_b,decision,decided_by FROM activity_link "
        "WHERE activity_a=? OR activity_b=?", (activity_id, activity_id)).fetchall()]
    return {**_activity_brief(r), "stats": stats, "sources": sources,
            "corrections": corrections, "links": links}


@app.post("/api/activities/{activity_id}/correct")
def correct_distance(activity_id: int, body: dict):
    if "distance_m" not in body:
        raise HTTPException(422, "distance_m required")
    try:
        return service.apply_distance_correction(
            conn(), activity_id, float(body["distance_m"]),
            reason=body.get("reason", ""),
            origin=body.get("origin", "user_confirmed"))
    except KeyError:
        raise HTTPException(404)


@app.get("/api/activities/{activity_id}/track")
def activity_track(activity_id: int, token: str | None = None):
    c = conn()
    pts = service.load_points(c, activity_id)
    if not pts:
        raise HTTPException(404)
    zones = _zones(c)
    if token:
        _require_token(c, token)
        pts = privacy.redact_track(pts, zones)
        return {"redacted": True, "geojson": privacy.track_to_geojson(pts)}
    return {"redacted": False, "geojson": privacy.track_to_geojson(pts)}


# ---------------------------------------------------------------- dedup links
@app.post("/api/links/duplicate")
def link_duplicate(body: dict):
    ids = body.get("dup_id"), body.get("keep_id")
    if not all(ids):
        raise HTTPException(422, "dup_id and keep_id required")
    return service.mark_duplicate(conn(), ids[0], ids[1],
                                  body.get("reason", "manual"), decided_by="user")


@app.post("/api/links/split")
def link_split(body: dict):
    try:
        return service.split_activities(conn(), int(body["a"]), int(body["b"]),
                                        body.get("reason", "user split"))
    except Exception:
        raise HTTPException(422, "a and b required")


@app.get("/api/links")
def list_links():
    return [dict(r) for r in conn().execute(
        "SELECT * FROM activity_link ORDER BY id").fetchall()]


# ---------------------------------------------------------------- stories / races / injuries
@app.post("/api/stories")
def add_story(body: dict):
    if not body.get("body"):
        raise HTTPException(422, "body required")
    c = conn()
    cur = c.execute(
        "INSERT INTO story(activity_id,month_key,title,body,created_at) VALUES (?,?,?,?,?)",
        (body.get("activity_id"), body.get("month_key"), body.get("title"),
         body["body"], db.now_iso()))
    c.commit()
    return {"id": cur.lastrowid}


@app.get("/api/stories")
def list_stories(year: int | None = None):
    sql = "SELECT * FROM story"
    if year:
        sql += " WHERE month_key LIKE ? OR activity_id IN (SELECT id FROM activity WHERE strftime('%Y',start_time_utc)=?)"
        rows = conn().execute(sql, (f"{year}-%", str(year))).fetchall()
    else:
        rows = conn().execute(sql).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/race-results")
def add_race_result(body: dict):
    c = conn()
    if not c.execute("SELECT 1 FROM activity WHERE id=?", (body["activity_id"],)).fetchone():
        raise HTTPException(404)
    c.execute("UPDATE activity SET kind='race' WHERE id=?", (body["activity_id"],))
    c.execute(
        "INSERT INTO race_result(activity_id,race_name,chip_time_s,gun_time_s,distance_m,"
        "pace_sec_per_km,overall_rank,age_group_rank) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(activity_id) DO UPDATE SET race_name=excluded.race_name,"
        "chip_time_s=excluded.chip_time_s,gun_time_s=excluded.gun_time_s,"
        "distance_m=excluded.distance_m,pace_sec_per_km=excluded.pace_sec_per_km,"
        "overall_rank=excluded.overall_rank,age_group_rank=excluded.age_group_rank",
        (body["activity_id"], body["race_name"], body.get("chip_time_s"),
         body.get("gun_time_s"), body.get("distance_m"), body.get("pace_sec_per_km"),
         body.get("overall_rank"), body.get("age_group_rank")))
    c.commit()
    return {"ok": True}


@app.post("/api/injuries")
def add_injury(body: dict):
    c = conn()
    cur = c.execute(
        "INSERT INTO injury_note(activity_id,body_part,note,private,occurred_on,created_at)"
        " VALUES (?,?,?,1,?,?)",
        (body.get("activity_id"), body["body_part"], body["note"],
         body.get("occurred_on"), db.now_iso()))
    c.commit()
    return {"id": cur.lastrowid, "private": True,
            "notice": "injury notes are never shown on shared pages or in reports; "
                      "this site provides no treatment advice"}


@app.get("/api/injuries")
def list_injuries():  # owner-only API; never serialized into share payloads
    return [dict(r) for r in conn().execute(
        "SELECT id,activity_id,body_part,note,occurred_on,created_at FROM injury_note").fetchall()]


# ---------------------------------------------------------------- privacy zones
def _zones(c) -> list[tuple[float, float, float]]:
    return [(r["lat"], r["lon"], r["radius_m"])
            for r in c.execute("SELECT * FROM privacy_zone").fetchall()]


@app.post("/api/privacy-zones")
def add_privacy_zone(body: dict):
    c = conn()
    cur = c.execute("INSERT INTO privacy_zone(label,lat,lon,radius_m) VALUES (?,?,?,?)",
                    (body.get("label", "home"), body["lat"], body["lon"],
                     float(body.get("radius_m", 200))))
    c.commit()
    return {"id": cur.lastrowid}


@app.get("/api/privacy-zones")
def get_privacy_zones():
    return [dict(r) for r in conn().execute("SELECT * FROM privacy_zone").fetchall()]


# ---------------------------------------------------------------- share
@app.post("/api/share")
def create_share(body: dict):
    token = secrets.token_urlsafe(12)
    c = conn()
    c.execute("INSERT INTO share_link(token,year,created_at) VALUES (?,?,?)",
              (token, int(body["year"]), db.now_iso()))
    c.commit()
    return {"token": token, "url": f"/s/{token}"}


def _require_token(c, token: str):
    row = c.execute("SELECT * FROM share_link WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(404, "share link not found")
    return row


@app.get("/s/{token}")
def share_page(token: str):
    return HTMLResponse((BASE / "static" / "share.html").read_text(encoding="utf-8"))


@app.get("/api/share/{token}/payload")
def share_payload(token: str):
    c = conn()
    link = _require_token(c, token)
    year, zones = link["year"], _zones(c)
    acts = []
    tracks = []
    for r in c.execute(
            "SELECT * FROM activity WHERE canonical=1 "
            "AND strftime('%Y',start_time_utc)=? ORDER BY start_time_utc", (str(year),)):
        pts = privacy.redact_track(service.load_points(c, r["id"]), zones)
        if len(pts) < privacy.MIN_POINTS_TO_SHOW:
            continue  # whole track near home: withhold instead of leaking the door
        brief = _activity_brief(r)
        brief.pop("canonical", None)
        acts.append(brief)
        tracks.append(pts)
    agg = privacy.aggregate_safe(tracks, zones)
    stories = [dict(t) for t in c.execute(
        "SELECT id,month_key,title,body FROM story WHERE month_key LIKE ?",
        (f"{year}-%",)).fetchall()]
    # 聚合入口也不含原坐标
    return {"year": year, "activities": acts, "stories": stories,
            "aggregate": agg,
            "track_outlets": [f"/s/{token}/track/{{id}}/thumb.svg",
                              f"/s/{token}/track/{{id}}/download.gpx"]}


@app.get("/s/{token}/track/{activity_id}/thumb.svg")
def share_thumb(token: str, activity_id: int):
    c = conn()
    _require_token(c, token)
    pts = privacy.redact_track(service.load_points(c, activity_id), _zones(c))
    return Response(privacy.render_thumbnail_svg(pts), media_type="image/svg+xml")


@app.get("/s/{token}/track/{activity_id}/download.gpx")
def share_download(token: str, activity_id: int):
    c = conn()
    _require_token(c, token)
    pts = privacy.redact_track(service.load_points(c, activity_id), _zones(c))
    if len(pts) < privacy.MIN_POINTS_TO_SHOW:
        raise HTTPException(409, "track withheld: entirely within privacy zone")
    return Response(privacy.to_gpx_bytes(pts, f"run-{activity_id}"),
                    media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="run-{activity_id}.gpx"'})


@app.get("/s/{token}/aggregate")
def share_aggregate(token: str):
    c = conn()
    link = _require_token(c, token)
    zones = _zones(c)
    tracks = [privacy.redact_track(service.load_points(c, r["id"]), zones)
              for r in c.execute(
                  "SELECT id FROM activity WHERE canonical=1 AND strftime('%Y',start_time_utc)=?",
                  (str(link["year"]),)).fetchall()]
    result = privacy.aggregate_safe(tracks, zones)
    if result is None:
        return {"available": False, "reason": "aggregation withheld to protect address"}
    return {"available": True, **result}


# ---------------------------------------------------------------- reports
@app.post("/api/reports/generate")
def generate_report(body: dict):
    year = int(body["year"])
    rv = body.get("rules_version", CURRENT_RULES)
    if rv not in RULES:
        raise HTTPException(422, f"unknown rules version; choose {list(RULES)}")
    return reports.generate(conn(), year, rv, body.get("title"))


@app.get("/api/reports")
def list_reports():
    return [dict(r) for r in conn().execute(
        "SELECT id,year,rules_version,title,created_at FROM report ORDER BY year").fetchall()]


@app.get("/api/reports/{year}")
def get_report(year: int, rules_version: str = CURRENT_RULES):
    snap = reports.get_snapshot(conn(), year, rules_version)
    if snap is None:
        raise HTTPException(404, "no snapshot; generate with this rules version first")
    return snap


@app.post("/api/reports/{year}/regenerate")
def regenerate_report(year: int, body: dict):
    rv = body.get("rules_version", CURRENT_RULES)
    if rv not in RULES:
        raise HTTPException(422, f"unknown rules version; choose {list(RULES)}")
    return reports.generate(conn(), year, rv, body.get("title"))


@app.get("/")
def index():
    return HTMLResponse((BASE / "static" / "index.html").read_text(encoding="utf-8"))
