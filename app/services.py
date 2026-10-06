"""Application services: import, identity, corrections, statistics and privacy."""
from __future__ import annotations

import hashlib
import math
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import json_dumps, json_loads, utc_now_iso
from .geo import bbox, haversine_m, path_distance_m
from .importers import ParsedActivity
from .matching import compare
from .timeutil import iso_utc, local_date, parse_dt
from .track import allocate_by_local_day, normalize_track

RULES = {
    "2024-v1": {
        "label": "2024 legacy rule",
        "mileage_attribution": "whole activity assigned to local start date",
        "races_in_training_total": True,
        "distance_priority": ["corrected", "device", "gps_measured"],
        "moving_time": "device elapsed/moving value where available; otherwise sum gaps <=30s",
        "timezone": "activity IANA timezone; UTC values are stored and displayed",
    },
    "2025-v1": {
        "label": "2025 rule",
        "mileage_attribution": "whole activity assigned to local start date",
        "races_in_training_total": False,
        "distance_priority": ["corrected", "device", "gps_measured"],
        "moving_time": "pauses longer than 30 seconds are excluded from GPS moving time",
        "timezone": "activity IANA timezone; UTC values are stored and displayed",
    },
    "2026-v1": {
        "label": "2026 current rule",
        "mileage_attribution": "segments crossing local midnight are split at the interpolated midnight position",
        "races_in_training_total": False,
        "distance_priority": ["user_confirmed", "device_estimated", "gps_measured"],
        "moving_time": "pauses longer than 30 seconds are excluded; user confirmation is separately labelled",
        "timezone": "UTC storage, IANA local aggregation; clock offset does not move day ownership",
    },
}
CURRENT_RULE = "2026-v1"


def content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _activity_effective(row: dict, rule: str = CURRENT_RULE) -> dict:
    corrected_d = row.get("corrected_distance_m")
    device_d = row.get("device_distance_m")
    measured_d = row.get("measured_distance_m") or 0.0
    distance = corrected_d if corrected_d is not None else (device_d if device_d is not None else measured_d)
    distance_status = (
        "user_confirmed" if corrected_d is not None else ("device_estimated" if device_d is not None else "gps_measured")
    )

    corrected_t = row.get("corrected_moving_s")
    device_t = row.get("device_moving_s")
    if device_t is None and rule == "2024-v1":
        device_t = row.get("device_elapsed_s")
    measured_t = row.get("measured_moving_s")
    moving = corrected_t if corrected_t is not None else (device_t if device_t is not None else measured_t)
    moving_status = (
        "user_confirmed" if corrected_t is not None else ("device_estimated" if device_t is not None else "gps_measured")
    )
    return {"distance_m": float(distance or 0.0), "distance_status": distance_status,
            "moving_s": int(moving or 0), "moving_status": moving_status}


def _load_points(conn, activity_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT utc_time, lat, lon, elevation_m, hr, cadence FROM track_points WHERE activity_id=? ORDER BY seq",
        (activity_id,),
    ).fetchall()
    points = []
    for r in rows:
        p = dict(r)
        p["utc"] = parse_dt(p.pop("utc_time"))
        points.append(p)
    return points


def _load_sources_by_activity(conn, activity_ids: list[int]) -> dict[int, list[dict]]:
    if not activity_ids:
        return {}
    q = ",".join("?" for _ in activity_ids)
    rows = conn.execute(f"SELECT * FROM sources WHERE activity_id IN ({q})", activity_ids).fetchall()
    result = defaultdict(list)
    for r in rows:
        result[r["activity_id"]].append(dict(r))
    return result


def _has_split_decision(conn, a: int, b: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM duplicate_split_decisions
        WHERE (activity_a_id=? AND activity_b_id=?) OR (activity_a_id=? AND activity_b_id=?)
        LIMIT 1
        """,
        (a, b, b, a),
    ).fetchone()
    return row is not None


def _candidate_activities(conn, started_at: datetime) -> list[dict]:
    lo, hi = started_at - timedelta(hours=6), started_at + timedelta(hours=6)
    rows = conn.execute(
        """
        SELECT a.*, s.provider AS source_provider, s.source_uid, s.content_sha256
        FROM activities a
        LEFT JOIN sources s ON s.activity_id=a.id
        WHERE a.started_at BETWEEN ? AND ?
        ORDER BY a.started_at
        """,
        (iso_utc(lo), iso_utc(hi)),
    ).fetchall()
    acts = []
    for r in rows:
        d = dict(r)
        d["started_dt"] = parse_dt(d["started_at"])
        d["ended_dt"] = parse_dt(d["ended_at"]) if d.get("ended_at") else d["started_dt"]
        d["points"] = _load_points(conn, d["id"])
        acts.append(d)
    return acts


def _find_exact_content(conn, sha: str) -> dict | None:
    row = conn.execute("SELECT activity_id FROM sources WHERE content_sha256=?", (sha,)).fetchone()
    if not row:
        return None
    return get_activity(conn, row["activity_id"])


def import_activity(conn, filename: str, content: bytes, parsed: ParsedActivity, default_tz: str | None = None) -> dict:
    """Import a normalized activity, then detect duplicates transactionally."""
    sha = content_hash(content)
    duplicate_of = _find_exact_content(conn, sha)
    if duplicate_of:
        return {"activity": duplicate_of, "duplicate": True, "basis": "content_sha256", "group_id": None,
                "warning": "identical file already imported"}

    tz = parsed.tz_name or default_tz
    norm = normalize_track(parsed.raw_points)
    started = parsed.started_at or (norm.points[0]["utc"] if norm.points else None)
    if started is None:
        raise ValueError("activity has no start timestamp")
    ended = norm.points[-1]["utc"] if norm.points else started
    local_start = local_date(started, tz).isoformat()

    now = utc_now_iso()
    effective_device_distance = parsed.device_distance_m
    if effective_device_distance is None and norm.points:
        # Parser may have no device total. The GPS computation remains measured,
        # never promoted into a device claim.
        pass
    cur = conn.execute(
        """
        INSERT INTO activities(
            activity_type,name,started_at,ended_at,tz_name,local_start_date,is_race,
            device_distance_m,device_moving_s,device_elapsed_s,
            measured_distance_m,measured_moving_s,dropped_points,normalization_rule,
            normalization_issues,canonical_activity_id,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            parsed.activity_type, parsed.name, iso_utc(started), iso_utc(ended), tz, local_start,
            int(parsed.is_race), effective_device_distance, parsed.device_moving_s, parsed.device_elapsed_s,
            norm.distance_m, norm.moving_s, norm.dropped_points, norm.rule, json_dumps(norm.issues),
            0, now, now,
        ),
    )
    activity_id = int(cur.lastrowid)
    conn.execute("UPDATE activities SET canonical_activity_id=id WHERE id=?", (activity_id,))
    conn.execute(
        """INSERT INTO sources(activity_id,provider,source_uid,device_name,filename,content_sha256,
           raw_metadata,imported_at) VALUES (?,?,?,?,?,?,?,?)""",
        (activity_id, parsed.source_provider, parsed.source_uid, parsed.device_name, filename, sha,
         json_dumps({"point_count": len(norm.points), "raw_point_count": len(parsed.raw_points)}), now),
    )
    for seq, p in enumerate(norm.points):
        conn.execute(
            """INSERT INTO track_points(activity_id,seq,utc_time,lat,lon,elevation_m,hr,cadence)
               VALUES (?,?,?,?,?,?,?,?)""",
            (activity_id, seq, iso_utc(p["utc"]), p["lat"], p["lon"], p.get("elevation_m"), p.get("hr"), p.get("cadence")),
        )
    _replace_allocations(conn, activity_id, allocate_by_local_day(norm.points, tz, None, None))
    activity_row = get_activity(conn, activity_id)
    conn.execute(
        """INSERT INTO activity_versions(activity_id,revision,changed_by,change_type,reason,before_json,after_json,created_at)
           VALUES (?,1,'system','import','normalized track import','{}',? ,?)""",
        (activity_id, json_dumps(_version_snapshot(activity_row)), now),
    )

    match_info = _auto_group_duplicate(conn, activity_row)
    # Reload after canonical/group changes.
    return {"activity": get_activity(conn, activity_id), "duplicate": match_info is not None,
            "basis": match_info["basis"] if match_info else None,
            "group_id": match_info["group_id"] if match_info else None,
            "dropped_points": norm.dropped_points, "issues": norm.issues}


def _new_match_record(conn, activity_row: dict) -> dict | None:
    current = dict(activity_row)
    current["started_dt"] = parse_dt(current["started_at"])
    current["ended_dt"] = parse_dt(current["ended_at"]) if current.get("ended_at") else current["started_dt"]
    current["points"] = _load_points(conn, current["id"])
    # Provider/UID are represented as a single source in this implementation.
    src = conn.execute("SELECT * FROM sources WHERE activity_id=?", (current["id"],)).fetchone()
    current["source_provider"] = src["provider"] if src else None
    current["source_uid"] = src["source_uid"] if src else None
    current["content_sha256"] = src["content_sha256"] if src else None

    best: dict | None = None
    for candidate in _candidate_activities(conn, current["started_dt"]):
        if candidate["id"] == current["id"] or _has_split_decision(conn, current["id"], candidate["id"]):
            continue
        verdict = compare(current, candidate)
        if verdict["match"] and (best is None or verdict["score"] > best["verdict"]["score"]):
            best = {"candidate": candidate, "verdict": verdict}
    return best


def _auto_group_duplicate(conn, activity_row: dict) -> dict | None:
    found = _new_match_record(conn, activity_row)
    if not found:
        return None
    candidate = found["candidate"]
    verdict = found["verdict"]
    canonical_id = candidate["canonical_activity_id"]
    existing = conn.execute(
        "SELECT id FROM duplicate_members dm JOIN duplicate_groups g ON g.id=dm.group_id WHERE dm.activity_id=? AND g.status='active'",
        (canonical_id,),
    ).fetchone()
    now = utc_now_iso()
    if existing:
        group_id = existing["id"]
        conn.execute("UPDATE duplicate_groups SET updated_at=?, match_score=? WHERE id=?",
                     (now, verdict["score"], group_id))
    else:
        cur = conn.execute(
            """INSERT INTO duplicate_groups(status,canonical_activity_id,match_basis,match_score,created_at,updated_at)
               VALUES ('active',?,?,?,?,?)""",
            (canonical_id, verdict["basis"], verdict["score"], now, now),
        )
        group_id = int(cur.lastrowid)
        conn.execute("INSERT INTO duplicate_members(group_id,activity_id,role) VALUES (?,?,'primary')",
                     (group_id, canonical_id))
    conn.execute("INSERT OR IGNORE INTO duplicate_members(group_id,activity_id,role) VALUES (?,?,'duplicate')",
                 (group_id, activity_row["id"]))
    conn.execute("UPDATE activities SET canonical_activity_id=?, updated_at=? WHERE id=?",
                 (canonical_id, now, activity_row["id"]))
    return {"group_id": group_id, "basis": verdict["basis"], "score": verdict["score"]}


def _replace_allocations(conn, activity_id: int, alloc: dict[str, dict]) -> None:
    conn.execute("DELETE FROM activity_day_allocations WHERE activity_id=?", (activity_id,))
    for day, row in sorted(alloc.items()):
        conn.execute(
            "INSERT INTO activity_day_allocations(activity_id,day,distance_m,moving_s) VALUES (?,?,?,?)",
            (activity_id, day, row["distance_m"], row["moving_s"]),
        )


def _version_snapshot(activity: dict) -> dict:
    keys = ["corrected_distance_m", "corrected_moving_s", "measured_distance_m", "measured_moving_s",
            "device_distance_m", "device_moving_s", "canonical_activity_id", "name", "is_race"]
    return {k: activity.get(k) for k in keys}


def get_activity(conn, activity_id: int) -> dict:
    row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
    if not row:
        raise KeyError(f"activity {activity_id} not found")
    d = dict(row)
    d["issues"] = json_loads(d.pop("normalization_issues"), [])
    d["effective"] = _activity_effective(d)
    d["sources"] = [dict(s) for s in conn.execute("SELECT * FROM sources WHERE activity_id=?", (activity_id,))]
    d["allocations"] = [dict(r) for r in conn.execute(
        "SELECT day,distance_m,moving_s FROM activity_day_allocations WHERE activity_id=? ORDER BY day", (activity_id,))]
    d["revisions"] = [dict(r) for r in conn.execute(
        "SELECT id,revision,changed_by,change_type,reason,created_at FROM activity_versions WHERE activity_id=? ORDER BY revision",
        (activity_id,))]
    return d


def list_activities(conn, include_duplicates: bool = False) -> list[dict]:
    rows = conn.execute("SELECT id FROM activities ORDER BY started_at").fetchall()
    activities = [get_activity(conn, r["id"]) for r in rows]
    if include_duplicates:
        return activities
    return [a for a in activities if a["canonical_activity_id"] == a["id"]]


def correct_activity(conn, activity_id: int, changes: dict, changed_by: str = "user", reason: str | None = None) -> dict:
    before = get_activity(conn, activity_id)
    fields = {}
    if "corrected_distance_m" in changes:
        value = changes["corrected_distance_m"]
        fields["corrected_distance_m"] = None if value is None else round(float(value), 3)
        if fields["corrected_distance_m"] is not None and fields["corrected_distance_m"] < 0:
            raise ValueError("distance cannot be negative")
    if "corrected_moving_s" in changes:
        value = changes["corrected_moving_s"]
        fields["corrected_moving_s"] = None if value is None else int(round(float(value)))
        if fields["corrected_moving_s"] is not None and fields["corrected_moving_s"] < 0:
            raise ValueError("moving seconds cannot be negative")
    if "name" in changes:
        fields["name"] = changes["name"]
    if "is_race" in changes:
        fields["is_race"] = int(bool(changes["is_race"]))
    if not fields:
        raise ValueError("no supported fields to correct")

    sets = ", ".join(f"{k}=?" for k in fields)
    params = list(fields.values()) + [utc_now_iso(), activity_id]
    conn.execute(f"UPDATE activities SET {sets}, updated_at=? WHERE id=?", params)

    points = _load_points(conn, activity_id)
    after = get_activity(conn, activity_id)
    alloc = allocate_by_local_day(
        points, after["tz_name"], after["corrected_distance_m"], after["corrected_moving_s"]
    ) if points else {after["local_start_date"]: {
        "distance_m": after["effective"]["distance_m"], "moving_s": after["effective"]["moving_s"]}}
    _replace_allocations(conn, activity_id, alloc)

    rev = conn.execute("SELECT COALESCE(MAX(revision),0)+? AS r FROM activity_versions WHERE activity_id=?",
                       (1, activity_id)).fetchone()["r"]
    after = get_activity(conn, activity_id)
    conn.execute(
        """INSERT INTO activity_versions(activity_id,revision,changed_by,change_type,reason,before_json,after_json,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (activity_id, rev, changed_by, "manual_correction", reason or "manual correction",
         json_dumps(_version_snapshot(before)), json_dumps(_version_snapshot(after)), utc_now_iso()),
    )
    return get_activity(conn, activity_id)


def list_duplicate_groups(conn) -> list[dict]:
    groups = [dict(r) for r in conn.execute("SELECT * FROM duplicate_groups ORDER BY created_at")]
    for g in groups:
        members = []
        for r in conn.execute(
            """SELECT a.*, dm.role FROM duplicate_members dm JOIN activities a ON a.id=dm.activity_id
               WHERE dm.group_id=? ORDER BY a.started_at""", (g["id"],)):
            d = dict(r)
            d.pop("role", None)
            members.append(get_activity(conn, d["id"]))
        g["members"] = members
    return groups


def split_duplicate_group(conn, group_id: int, reason: str | None = None, changed_by: str = "user") -> dict:
    group = conn.execute("SELECT * FROM duplicate_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise KeyError("duplicate group not found")
    member_ids = [r["id"] for r in conn.execute("SELECT activity_id id FROM duplicate_members WHERE group_id=?", (group_id,))]
    if len(member_ids) < 2:
        raise ValueError("group has fewer than two members")
    now = utc_now_iso()
    conn.execute("UPDATE duplicate_groups SET status='split', updated_at=? WHERE id=?", (now, group_id))
    for aid in member_ids:
        conn.execute("UPDATE activities SET canonical_activity_id=id, updated_at=? WHERE id=?", (now, aid))
    for i, a in enumerate(member_ids):
        for b in member_ids[i + 1:]:
            conn.execute(
                """INSERT INTO duplicate_split_decisions(group_id,activity_a_id,activity_b_id,changed_by,reason,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (group_id, a, b, changed_by, reason or "manual split", now),
            )
    return {"id": group_id, "status": "split", "members": member_ids}


def restore_duplicate_group(conn, group_id: int, changed_by: str = "user") -> dict:
    group = conn.execute("SELECT * FROM duplicate_groups WHERE id=?", (group_id,)).fetchone()
    if not group:
        raise KeyError("duplicate group not found")
    members = conn.execute(
        "SELECT activity_id id FROM duplicate_members WHERE group_id=? ORDER BY activity_id", (group_id,)).fetchall()
    ids = [r["id"] for r in members]
    if len(ids) < 2:
        raise ValueError("group has fewer than two members")
    canonical_id = min(ids)
    now = utc_now_iso()
    conn.execute("DELETE FROM duplicate_split_decisions WHERE group_id=?", (group_id,))
    conn.execute("UPDATE duplicate_groups SET status='active', canonical_activity_id=?, updated_at=? WHERE id=?",
                 (canonical_id, now, group_id))
    for aid in ids:
        conn.execute("UPDATE activities SET canonical_activity_id=?, updated_at=? WHERE id=?", (canonical_id, now, aid))
    conn.execute("UPDATE duplicate_members SET role=CASE WHEN activity_id=? THEN 'primary' ELSE 'duplicate' END WHERE group_id=?",
                 (canonical_id, group_id))
    return {"id": group_id, "status": "active", "canonical_activity_id": canonical_id, "members": ids}


def _canonical_activities(conn, start: str, end: str, include_duplicates: bool = False) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM activities
        WHERE local_start_date BETWEEN ? AND ?
        ORDER BY started_at
        """,
        (start, end),
    ).fetchall()
    acts = []
    seen = set()
    for r in rows:
        d = dict(r)
        if d["canonical_activity_id"] != d["id"]:
            # In v2026 cross-midnight allocation is used, so canonical members
            # may start outside range. Allocations handle that below.
            if not include_duplicates:
                continue
        if d["id"] != d["canonical_activity_id"]:
            canonical = d["canonical_activity_id"]
            if canonical in seen:
                continue
            seen.add(canonical)
            d = dict(conn.execute("SELECT * FROM activities WHERE id=?", (canonical,)).fetchone())
        acts.append(d)
    return acts


def _allocations_in_range(conn, activity_id: int, start: str, end: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT day,distance_m,moving_s FROM activity_day_allocations
           WHERE activity_id=? AND day BETWEEN ? AND ? ORDER BY day""",
        (activity_id, start, end),
    )]


def _races_in_range(conn, start: str, end: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM races WHERE race_date BETWEEN ? AND ? ORDER BY race_date", (start, end))]


def _legacy_distance(row: dict, rule: str) -> tuple[float, str, int, str]:
    eff = _activity_effective(row, rule)
    return eff["distance_m"], eff["distance_status"], eff["moving_s"], eff["moving_status"]


def compute_statistics(conn, year: int | None = None, start: str | None = None, end: str | None = None,
                       rule_version: str = CURRENT_RULE) -> dict:
    """Compute training and race statistics from one explicit date range.

    The returned object includes ``data_range``; report charts must all use the
    monthly/activity series from this same object instead of making divergent
    ad-hoc queries.
    """
    if rule_version not in RULES:
        raise ValueError(f"unknown rule version {rule_version}")
    if start is None or end is None:
        if year is None:
            raise ValueError("year or start/end date range is required")
        start = f"{year:04d}-01-01"
        end = f"{year:04d}-12-31"

    # Build exactly the months covered by the explicit range so every chart
    # shares this series and cannot invent a different scope.
    from datetime import date as date_cls
    s = date_cls.fromisoformat(start)
    e = date_cls.fromisoformat(end)
    months = []
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    monthly = {
        month: {"distance_m": 0.0, "moving_s": 0, "activities": 0, "confirmed_distance_m": 0.0,
                "estimated_distance_m": 0.0, "gps_distance_m": 0.0}
        for month in months
    }

    canonical_rows = conn.execute("SELECT * FROM activities WHERE id=canonical_activity_id ORDER BY started_at").fetchall()
    rows = []
    for candidate in canonical_rows:
        has_alloc = conn.execute(
            "SELECT 1 FROM activity_day_allocations WHERE activity_id=? AND day BETWEEN ? AND ? LIMIT 1",
            (candidate["id"], start, end),
        ).fetchone()
        if has_alloc or (candidate["local_start_date"] >= start and candidate["local_start_date"] <= end):
            rows.append(candidate)
    total_d = 0.0
    total_moving = 0
    activity_count = 0
    distance_statuses = defaultdict(float)
    longest = None
    activities_payload = []

    for r in rows:
        row = dict(r)
        is_race = bool(row["is_race"])
        if rule_version in ("2025-v1", "2026-v1") and is_race:
            # Race performance remains in /races; it is not double-counted as training.
            continue
        eff = _activity_effective(row, rule_version)
        if rule_version == "2024-v1":
            if row["local_start_date"] < start or row["local_start_date"] > end:
                continue
            activity_count += 1
            distance, dstatus, moving, mstatus = _legacy_distance(row, rule_version)
            month = row["local_start_date"][:7]
            bucket = monthly.get(month)
            if bucket is not None:
                bucket["distance_m"] += distance
                bucket["moving_s"] += moving
                bucket["activities"] += 1
                bucket[{"user_confirmed": "confirmed_distance_m",
                        "device_estimated": "estimated_distance_m",
                        "gps_measured": "gps_distance_m"}[dstatus]] += distance
            total_d += distance
            total_moving += moving
            distance_statuses[dstatus] += distance
        else:
            # Device and user-confirmed totals are authoritative, but retain the
            # GPS-derived local-day fractions for cross-midnight allocation.
            authoritative_total = row["corrected_distance_m"]
            if authoritative_total is None:
                authoritative_total = row["device_distance_m"]
            authoritative_moving = row["corrected_moving_s"]
            if authoritative_moving is None and rule_version != "2026-v1":
                authoritative_moving = row["device_moving_s"]
            # Under 2026-v1, a missing device moving-time claim stays missing
            # and the GPS-derived allocation (30s gap rule) is retained.
            allocations = _allocations_in_range(conn, row["id"], start, end)
            if not allocations and rule_version == "2025-v1" and start <= row["local_start_date"] <= end:
                # 2025 legacy allocation is whole-activity-to-start-date, even
                # when no normalized GPS segment exists.
                allocations = [{"day": row["local_start_date"], "distance_m": 0.0, "moving_s": 0.0}]
            if authoritative_total is not None and allocations:
                alloc_gps_total = sum(al["distance_m"] for al in allocations)
                if alloc_gps_total > 0:
                    scale = authoritative_total / alloc_gps_total
                    allocations = [{**al, "distance_m": al["distance_m"] * scale} for al in allocations]
                else:
                    allocations[0]["distance_m"] = authoritative_total
            if authoritative_moving is not None and allocations:
                alloc_time_total = sum(al["moving_s"] for al in allocations)
                if alloc_time_total > 0:
                    scale = authoritative_moving / alloc_time_total
                    allocations = [{**al, "moving_s": al["moving_s"] * scale} for al in allocations]
                else:
                    allocations[0]["moving_s"] = authoritative_moving
            activity_touched = False
            for al in allocations:
                month = al["day"][:7]
                bucket = monthly.get(month)
                if bucket is None:
                    continue
                d = al["distance_m"]
                t = int(round(al["moving_s"]))
                bucket["distance_m"] += d
                bucket["moving_s"] += t
                if not activity_touched:
                    bucket["activities"] += 1
                total_d += d
                total_moving += t
                bucket[{
                    "user_confirmed": "confirmed_distance_m",
                    "device_estimated": "estimated_distance_m",
                    "gps_measured": "gps_distance_m",
                }[eff["distance_status"]]] += d
                distance_statuses[eff["distance_status"]] += d
                activity_touched = True
            if activity_touched:
                activity_count += 1

        included_in_range = False
        if rule_version == "2024-v1":
            included_in_range = start <= row["local_start_date"] <= end
        elif not is_race:
            included_in_range = bool(_allocations_in_range(conn, row["id"], start, end))
        if included_in_range:
            distance = eff["distance_m"]
            if longest is None or distance > longest["distance_m"]:
                longest = {
                    "activity_id": row["id"], "name": row["name"], "date": row["local_start_date"],
                    "distance_m": round(distance, 3), "status": eff["distance_status"],
                }
            activities_payload.append({
                "id": row["id"], "date": row["local_start_date"], "name": row["name"],
                "distance_m": round(eff["distance_m"], 3), "distance_status": eff["distance_status"],
                "moving_s": eff["moving_s"], "moving_status": eff["moving_status"], "is_race": is_race,
            })

    races = _races_in_range(conn, start, end)
    race_wins_or_best = _race_records(conn, start, end)
    training_records = {
        "longest_run": longest,
        "largest_weekly_distance_m": _largest_week(conn, start, end, rule_version),
    }
    total_hours = total_moving / 3600
    avg_pace_s_per_km = (total_moving / (total_d / 1000.0)) if total_d > 0 else None

    return {
        "data_range": {"start": start, "end": end, "timezone_basis": "activity IANA timezone", "rule_version": rule_version},
        "methodology_version": rule_version,
        "training": {
            "activity_count": activity_count,
            "distance_m": round(total_d, 3),
            "distance_km": round(total_d / 1000.0, 3),
            "moving_s": int(total_moving),
            "moving_h": round(total_hours, 3),
            "average_pace_s_per_km": round(avg_pace_s_per_km, 1) if avg_pace_s_per_km is not None else None,
            "distance_by_status": {k: round(v, 3) for k, v in distance_statuses.items()},
            "monthly": [{"month": k, **{kk: (round(vv, 3) if isinstance(vv, float) else vv)
                                        for kk, vv in v.items()}} for k, v in monthly.items()],
            "records": training_records,
        },
        "activities": activities_payload,
        # Race results are a separately defined performance domain.
        "races": races,
        "race_records": race_wins_or_best,
        "separation_notice": "Races are listed separately. Current training totals exclude race activities; legacy 2024-v1 totals follow that year's recorded rule.",
    }


def _largest_week(conn, start: str, end: str, rule: str) -> dict | None:
    weeks = defaultdict(float)
    rows = conn.execute(
        """SELECT a.* FROM activities a WHERE a.id=a.canonical_activity_id
           AND (a.is_race=0 OR ?)""",
        (int(rule == "2024-v1"),),
    ).fetchall()
    for r in rows:
        row = dict(r)
        allocations = _allocations_in_range(conn, row["id"], start, end)
        if rule == "2024-v1":
            if not allocations or row["local_start_date"] < start or row["local_start_date"] > end:
                continue
            from datetime import date as date_cls
            d = date_cls.fromisoformat(row["local_start_date"])
            monday = (d - timedelta(days=d.weekday())).isoformat()
            weeks[monday] += _activity_effective(row, rule)["distance_m"]
            continue
        authoritative_total = row["corrected_distance_m"]
        if authoritative_total is None:
            authoritative_total = row["device_distance_m"]
        all_allocations = _allocations_in_range(conn, row["id"], start, end)
        gps_total = sum(al["distance_m"] for al in all_allocations)
        scale = 1.0 if authoritative_total is None or gps_total <= 0 else authoritative_total / gps_total
        for al in allocations:
            from datetime import date as date_cls
            d = date_cls.fromisoformat(al["day"])
            monday = (d - timedelta(days=d.weekday())).isoformat()
            weeks[monday] += al["distance_m"] * scale
    if not weeks:
        return None
    monday, dist = max(weeks.items(), key=lambda kv: kv[1])
    return {"week_start": monday, "distance_m": round(dist, 3)}


def _race_records(conn, start: str, end: str) -> dict:
    rows = _races_in_range(conn, start, end)
    distances = defaultdict(list)
    for r in rows:
        if r.get("elapsed_s"):
            distances[round(r["distance_m"] / 1000.0)].append(r)
    best = {}
    for km, items in distances.items():
        winner = min(items, key=lambda x: x["elapsed_s"])
        best[f"{km:g}k"] = {"race_id": winner["id"], "name": winner["name"], "elapsed_s": winner["elapsed_s"]}
    return {"best_known_distances": best}


def methodology() -> dict:
    return {
        "current_rule": CURRENT_RULE,
        "rules": RULES,
        "gps": {
            "spike_rule": "point removed only when surrounding links exceed 85 m/s and show an isolated out-and-back excursion",
            "moving_gap_seconds": 30,
            "distance": "great-circle haversine sum of retained points; user corrections never overwrite raw points",
        },
        "duplicate_matching": {
            "identical_file": "SHA-256 exact match is one activity",
            "source_identity": "same provider/source ID within 30 minutes is one activity",
            "spatiotemporal": "requires >=55% time interval overlap, <=120m mean resampled track distance, and score >=0.82",
            "adjacent_activities": "timestamp closeness alone never merges two activities",
            "manual_split": "split decisions are retained and block later automatic merges unless explicitly restored",
        },
        "privacy": {
            "share_redaction": "all points inside a private radius are removed before thumbnail, GPX/download and aggregate geometry are generated",
            "injuries": "injury notes are private by default and are never embedded in public report snapshots",
        },
        "labels": {
            "device_estimated": "device-provided estimate, not user confirmed",
            "user_confirmed": "manually entered or corrected by the runner",
            "gps_measured": "computed from retained GPS points",
        },
        "non_medical_notice": "This product displays training history only and does not provide training prescriptions or injury treatment advice.",
    }
