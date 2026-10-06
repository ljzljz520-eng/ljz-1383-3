"""Race results, stories, injury metadata, reports and privacy-safe sharing."""
from __future__ import annotations

import html
import math
import secrets
from datetime import date
from typing import Any

from .database import json_dumps, json_loads, utc_now_iso
from .geo import bbox, haversine_m
from .services import CURRENT_RULE, RULES, compute_statistics
from .timeutil import iso_utc


# ---------- Race results (performance domain, separate from training stats) --

def create_race(conn, payload: dict) -> dict:
    required = ["name", "race_date", "distance_m"]
    missing = [k for k in required if payload.get(k) in (None, "")]
    if missing:
        raise ValueError(f"missing race fields: {', '.join(missing)}")
    now = utc_now_iso()
    cur = conn.execute(
        """INSERT INTO races(activity_id,name,race_date,distance_m,elapsed_s,overall_place,age_group_place,source,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (payload.get("activity_id"), payload["name"], payload["race_date"], float(payload["distance_m"]),
         payload.get("elapsed_s"), payload.get("overall_place"), payload.get("age_group_place"),
         payload.get("source", "manual"), now, now),
    )
    return get_race(conn, int(cur.lastrowid))


def get_race(conn, race_id: int) -> dict:
    row = conn.execute("SELECT * FROM races WHERE id=?", (race_id,)).fetchone()
    if not row:
        raise KeyError("race not found")
    return dict(row)


def list_races(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    if start and end:
        rows = conn.execute("SELECT * FROM races WHERE race_date BETWEEN ? AND ? ORDER BY race_date", (start, end)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM races ORDER BY race_date").fetchall()
    return [dict(r) for r in rows]


def update_race(conn, race_id: int, payload: dict) -> dict:
    current = get_race(conn, race_id)
    for key in ["name", "race_date", "distance_m", "elapsed_s", "overall_place", "age_group_place", "source", "activity_id"]:
        if key in payload:
            current[key] = payload[key]
    conn.execute(
        """UPDATE races SET activity_id=?,name=?,race_date=?,distance_m=?,elapsed_s=?,overall_place=?,
           age_group_place=?,source=?,updated_at=? WHERE id=?""",
        (current.get("activity_id"), current["name"], current["race_date"], float(current["distance_m"]),
         current.get("elapsed_s"), current.get("overall_place"), current.get("age_group_place"),
         current.get("source"), utc_now_iso(), race_id),
    )
    return get_race(conn, race_id)


# ---------- Stories ----------

def create_story(conn, payload: dict) -> dict:
    if not payload.get("title") or not payload.get("happened_on"):
        raise ValueError("story title and happened_on are required")
    now = utc_now_iso()
    cur = conn.execute(
        """INSERT INTO stories(activity_id,happened_on,title,body,mood,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (payload.get("activity_id"), payload["happened_on"], payload["title"], payload.get("body", ""),
         payload.get("mood"), now, now),
    )
    return get_story(conn, int(cur.lastrowid))


def get_story(conn, story_id: int) -> dict:
    row = conn.execute("SELECT * FROM stories WHERE id=?", (story_id,)).fetchone()
    if not row:
        raise KeyError("story not found")
    return dict(row)


def list_stories(conn, start: str | None = None, end: str | None = None) -> list[dict]:
    if start and end:
        rows = conn.execute("SELECT * FROM stories WHERE happened_on BETWEEN ? AND ? ORDER BY happened_on", (start, end)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM stories ORDER BY happened_on").fetchall()
    return [dict(r) for r in rows]


def update_story(conn, story_id: int, payload: dict) -> dict:
    current = get_story(conn, story_id)
    for key in ["activity_id", "happened_on", "title", "body", "mood"]:
        if key in payload:
            current[key] = payload[key]
    conn.execute(
        "UPDATE stories SET activity_id=?,happened_on=?,title=?,body=?,mood=?,updated_at=? WHERE id=?",
        (current.get("activity_id"), current["happened_on"], current["title"], current.get("body", ""),
         current.get("mood"), utc_now_iso(), story_id),
    )
    return get_story(conn, story_id)


# ---------- Injuries: explicitly private ----------

def create_injury(conn, payload: dict) -> dict:
    if not payload.get("body_part") or payload.get("severity") not in ("mild", "moderate", "severe"):
        raise ValueError("body_part and valid severity are required")
    now = utc_now_iso()
    cur = conn.execute(
        """INSERT INTO injuries(body_part,occurred_on,severity,is_private,notes,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (payload["body_part"], payload.get("occurred_on"), payload["severity"],
         int(payload.get("is_private", True)), payload.get("notes", ""), now, now),
    )
    return get_injury(conn, int(cur.lastrowid))


def get_injury(conn, injury_id: int) -> dict:
    row = conn.execute("SELECT * FROM injuries WHERE id=?", (injury_id,)).fetchone()
    if not row:
        raise KeyError("injury not found")
    d = dict(row)
    # UI/API must be unambiguous: even private notes are never copied to shares.
    d["visibility"] = "private" if d["is_private"] else "account_only"
    return d


def list_injuries(conn) -> list[dict]:
    return [get_injury(conn, r["id"]) for r in conn.execute("SELECT id FROM injuries ORDER BY occurred_on")]


def update_injury(conn, injury_id: int, payload: dict) -> dict:
    current = get_injury(conn, injury_id)
    for key in ["body_part", "occurred_on", "severity", "notes"]:
        if key in payload:
            current[key] = payload[key]
    if "is_private" in payload:
        current["is_private"] = int(bool(payload["is_private"]))
    if current["severity"] not in ("mild", "moderate", "severe"):
        raise ValueError("invalid severity")
    conn.execute(
        "UPDATE injuries SET body_part=?,occurred_on=?,severity=?,is_private=?,notes=?,updated_at=? WHERE id=?",
        (current["body_part"], current.get("occurred_on"), current["severity"], int(current["is_private"]),
         current.get("notes", ""), utc_now_iso(), injury_id),
    )
    return get_injury(conn, injury_id)


# ---------- Annual reports: snapshots + historical rule versions ----------

def create_report(conn, year: int, rule_version: str = CURRENT_RULE, status: str = "draft") -> dict:
    if rule_version not in RULES:
        raise ValueError("unknown rule version")
    if status not in ("draft", "published"):
        raise ValueError("report status must be draft or published")
    start, end = f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"
    snapshot = compute_statistics(conn, int(year), start, end, rule_version)
    snapshot["stories"] = [{"id": s["id"], "happened_on": s["happened_on"], "title": s["title"], "body": s["body"],
                            "mood": s["mood"]} for s in list_stories(conn, start, end)]
    # Deliberately no injuries key: private medical-adjacent notes cannot leak.
    snapshot["medical_notice"] = "Injury details are private and excluded from reports. No treatment advice is generated."
    now = utc_now_iso()
    cur = conn.execute(
        """INSERT INTO reports(year,rule_version,status,range_start,range_end,snapshot_json,created_at,published_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (int(year), rule_version, status, start, end, json_dumps(snapshot), now, now if status == "published" else None),
    )
    return get_report(conn, int(cur.lastrowid))


def get_report(conn, report_id: int) -> dict:
    row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        raise KeyError("report not found")
    d = dict(row)
    d["snapshot"] = json_loads(d.pop("snapshot_json"))
    return d


def list_reports(conn) -> list[dict]:
    rows = conn.execute("SELECT id,year,rule_version,status,range_start,range_end,created_at,published_at FROM reports ORDER BY year,id")
    return [dict(r) for r in rows]


def publish_report(conn, report_id: int) -> dict:
    report = get_report(conn, report_id)
    if report["status"] == "published":
        return report
    conn.execute("UPDATE reports SET status='published', published_at=? WHERE id=?", (utc_now_iso(), report_id))
    return get_report(conn, report_id)


def regenerate_report(conn, report_id: int) -> dict:
    """Create a new draft under the same historical rule.

    The existing report remains untouched, preserving the report that members
    viewed and enabling side-by-side regeneration.
    """
    old = get_report(conn, report_id)
    return create_report(conn, old["year"], old["rule_version"], "draft")


# ---------- Privacy-safe shares ----------

def _points_for_activity(conn, activity_id: int) -> list[dict]:
    rows = conn.execute("SELECT utc_time,lat,lon FROM track_points WHERE activity_id=? ORDER BY seq", (activity_id,)).fetchall()
    return [{"time": r["utc_time"], "lat": r["lat"], "lon": r["lon"]} for r in rows]


def create_share(conn, subject_type: str, subject_id: int, payload: dict) -> dict:
    if subject_type not in ("activity", "report"):
        raise ValueError("invalid share subject")
    token = secrets.token_urlsafe(18)
    now = utc_now_iso()
    cur = conn.execute(
        "INSERT INTO share_tokens(token,subject_type,subject_id,title,created_at,expires_at) VALUES (?,?,?,?,?,?)",
        (token, subject_type, subject_id, payload.get("title"), now, payload.get("expires_at")),
    )
    share_id = int(cur.lastrowid)
    for zone in payload.get("redactions", []):
        conn.execute(
            "INSERT INTO share_redactions(share_id,label,lat,lon,radius_m) VALUES (?,?,?,?,?)",
            (share_id, zone.get("label", "private place"), float(zone["lat"]), float(zone["lon"]),
             float(zone.get("radius_m", 150))),
        )
    return get_share(conn, token, include_checks=True)


def get_share_token_row(conn, token: str) -> dict:
    row = conn.execute("SELECT * FROM share_tokens WHERE token=?", (token,)).fetchone()
    if not row:
        raise KeyError("share not found")
    d = dict(row)
    if d.get("expires_at") and d["expires_at"] < utc_now_iso():
        raise KeyError("share expired")
    return d


def _redaction_zones(conn, share_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT label,lat,lon,radius_m FROM share_redactions WHERE share_id=?", (share_id,))]


def redact_points(points: list[dict], zones: list[dict]) -> list[dict]:
    """Remove every private-zone point and break segments around the gap."""
    out: list[dict] = []
    segment = 0
    prev_hidden = False
    for p in points:
        hidden = False
        for z in zones:
            if haversine_m(p["lat"], p["lon"], z["lat"], z["lon"]) <= z["radius_m"]:
                hidden = True
                break
        if hidden:
            if not prev_hidden and out:
                segment += 1
            prev_hidden = True
            continue
        q = dict(p)
        q["segment"] = segment
        if prev_hidden and out:
            q["segment_start"] = True
        out.append(q)
        prev_hidden = False
    return out


def get_share(conn, token: str, include_checks: bool = False) -> dict:
    token_row = get_share_token_row(conn, token)
    zones = _redaction_zones(conn, token_row["id"])
    result = {"token": token, "subject_type": token_row["subject_type"], "title": token_row["title"],
              "expires_at": token_row["expires_at"], "redaction_count": len(zones)}
    if token_row["subject_type"] == "activity":
        activity = conn.execute("SELECT * FROM activities WHERE id=?", (token_row["subject_id"],)).fetchone()
        if not activity:
            raise KeyError("shared activity missing")
        a = dict(activity)
        raw_points = _points_for_activity(conn, a["id"])
        points = redact_points(raw_points, zones)
        result["activity"] = {
            "id": a["id"], "name": a["name"], "start_date": a["local_start_date"],
            "timezone": a["tz_name"], "distance_m": a["corrected_distance_m"] if a["corrected_distance_m"] is not None
            else (a["device_distance_m"] if a["device_distance_m"] is not None else a["measured_distance_m"]),
            "point_count": len(points),
            "points": points,
        }
        result["bbox"] = bbox(points) if points else None
        result["download_format"] = "redacted GPX only"
    else:
        report = get_report(conn, token_row["subject_id"])
        # Snapshot was already privacy-reviewed; strip any defensive coordinate key.
        result["report"] = report
        result["aggregate_geometry"] = None
    if include_checks:
        result["privacy_check"] = privacy_check(conn, token)
    return result


def render_thumbnail_svg(share: dict) -> str:
    if share["subject_type"] != "activity":
        return ("<svg xmlns='http://www.w3.org/2000/svg' width='640' height='240'>"
                "<text x='20' y='120'>Annual report</text></svg>")
    points = share["activity"]["points"]
    if not points:
        return ("<svg xmlns='http://www.w3.org/2000/svg' width='640' height='240'>"
                "<rect width='640' height='240' fill='%23f4f6f8'/>"
                "<text x='24' y='124' fill='%23334'>轨迹因隐私区域隐藏</text></svg>")
    min_lat, min_lon, max_lat, max_lon = bbox(points)
    width, height, pad = 600, 200, 20
    span_lat = max(1e-9, (max_lat or 0) - (min_lat or 0))
    span_lon = max(1e-9, (max_lon or 0) - (min_lon or 0))
    def xy(p):
        x = pad + (p["lon"] - min_lon) / span_lon * (width - 2 * pad)
        y = height - pad - (p["lat"] - min_lat) / span_lat * (height - 2 * pad)
        return f"{x:.1f},{y:.1f}"
    segments = {}
    for p in points:
        segments.setdefault(p["segment"], []).append(p)
    polylines = "".join(
        f"<polyline fill='none' stroke='#2563eb' stroke-width='4' stroke-linecap='round' stroke-linejoin='round' points='{' '.join(xy(p) for p in seg)}'/>"
        for seg in segments.values()
    )
    title = html.escape(share["activity"].get("name") or "Redacted run")
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='640' height='240' viewBox='0 0 640 240'>
<rect width='640' height='240' fill='#f8fafc'/>
<text x='20' y='28' font-family='sans-serif' font-size='18' fill='#0f172a'>{title}</text>
<g transform='translate(20,20)'>{polylines}</g>
</svg>"""


def render_redacted_gpx(share: dict) -> str:
    if share["subject_type"] != "activity":
        raise ValueError("subject has no GPX track")
    a = share["activity"]
    segments = {}
    for p in a["points"]:
        segments.setdefault(p["segment"], []).append(p)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="runner-annual-report/redacted" xmlns="http://www.topografix.com/GPX/1/1">',
             f"<name>{html.escape(a.get('name') or 'Redacted run')}</name><trk>"]
    for seg in segments.values():
        parts.append("<trkseg>")
        for p in seg:
            parts.append(f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}"><time>{html.escape(p["time"])}</time></trkpt>')
        parts.append("</trkseg>")
    parts.append("</trk></gpx>")
    return "\n".join(parts)


def privacy_check(conn, token: str) -> dict:
    share = get_share(conn, token)
    token_row = get_share_token_row(conn, token)
    zones = _redaction_zones(conn, token_row["id"])
    checks = {"thumbnail": "skipped", "download": "skipped", "aggregate": "pass", "removed_points": 0, "leaks": []}
    if share["subject_type"] == "activity":
        activity_id = share["activity"]["id"]
        raw = _points_for_activity(conn, activity_id)
        removed = [p for p in raw if any(haversine_m(p["lat"], p["lon"], z["lat"], z["lon"]) <= z["radius_m"] for z in zones)]
        checks["removed_points"] = len(removed)
        thumb = render_thumbnail_svg(share)
        gpx = render_redacted_gpx(share)
        checks["thumbnail"] = "pass"
        checks["download"] = "pass" if "<trkpt" in gpx else "no_geometry"
        public_points = share["activity"]["points"]
        for p in removed:
            # Compare full coordinate pairs rather than latitude/longitude in
            # isolation: legitimate visible points may share one axis value.
            if any(abs(q["lat"] - p["lat"]) < 1e-8 and abs(q["lon"] - p["lon"]) < 1e-8 for q in public_points):
                checks["leaks"].append({"lat": p["lat"], "lon": p["lon"], "artifact": "public point"})
            if f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}"' in gpx:
                checks["leaks"].append({"lat": p["lat"], "lon": p["lon"], "artifact": "download point"})
        # Zone center itself is sensitive: never expose labels/centers to public share.
        public_text = json_dumps(share)
        for z in zones:
            if f'{z["lat"]:.7f}' in public_text or f'{z["lon"]:.7f}' in public_text:
                checks["leaks"].append({"artifact": "redaction_center_exposed"})
    else:
        public_text = json_dumps(share)
        if '"lat"' in public_text or '"lon"' in public_text:
            checks["leaks"].append({"artifact": "aggregate_or_snapshot_coordinate"})
            checks["aggregate"] = "fail"
        if any(word in public_text for word in ("injury", "treatment", "medical advice")):
            # The medical notice is allowed; actual injury details are not.
            if '"body_part"' in public_text or '"notes"' in public_text and 'injury' in public_text:
                checks["leaks"].append({"artifact": "injury_detail"})
    checks["pass"] = not checks["leaks"]
    return checks
