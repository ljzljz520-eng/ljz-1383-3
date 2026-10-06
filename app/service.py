"""Import pipeline and activity/statistics services."""
from __future__ import annotations

import shutil
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .geo import Point, bbox, compute_stats, filter_spikes, parse_ts
from .matching import evaluate
from .parsers import ParseError, detect_and_parse, file_fingerprint
from .rules import get_rules

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "data" / "uploads"


class DuplicateFileError(ValueError):
    pass


def _save_points(conn, activity_id: int, kept: list[Point], removed_idx: set[int]):
    rows = []
    for i, p in enumerate(kept):
        rows.append((activity_id, p.lat, p.lon, p.time.isoformat(), p.ele, 0, "device_estimated"))
    # removed spikes are stored for audit but flagged and never used in stats/export
    conn.executemany(
        "INSERT INTO track_point(activity_id,lat,lon,t_utc,ele,is_spike,version_tag)"
        " VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()


def load_points(conn, activity_id: int, include_spikes: bool = False) -> list[Point]:
    sql = "SELECT * FROM track_point WHERE activity_id=? AND t_utc IS NOT NULL"
    if not include_spikes:
        sql += " AND is_spike=0"
    sql += " ORDER BY t_utc"
    pts = []
    for r in conn.execute(sql, (activity_id,)):
        pts.append(Point(r["lat"], r["lon"], parse_ts(r["t_utc"]), r["ele"]))
    return pts


def create_activity_from_upload(
    conn,
    *,
    filename: str,
    data: bytes,
    source: str = "unknown",
    external_id: str | None = None,
    kind: str = "training",
    title: str | None = None,
    rules_version: str | None = None,
    save_raw: bool = True,
) -> dict:
    rules = get_rules(rules_version)
    fp = file_fingerprint(data)

    dup = conn.execute("SELECT id FROM source_file WHERE fingerprint=?", (fp,)).fetchone()
    if dup:
        raise DuplicateFileError(f"file already uploaded (source_file id={dup['id']})")

    raw_points = detect_and_parse(filename, data)  # may raise ParseError
    kept, removed = filter_spikes(raw_points, rules.spike_speed_mps)
    removed_set = set(removed)
    stats = compute_stats(kept, rules.stop_speed_mps)
    stats_dict = asdict(stats)
    stats_dict["spikes_removed"] = len(removed_set)

    # timezone offset: preserve the file's original timestamp offset (local tz rules)
    tz_offset_min = raw_points[0].offset_min or 0
    start_utc, end_utc = kept[0].time, kept[-1].time

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = None
    if save_raw:
        raw_path = str(UPLOAD_DIR / f"{fp[:16]}_{Path(filename).name}")
        Path(raw_path).write_bytes(data)

    cur = conn.execute(
        "INSERT INTO source_file(filename,fingerprint,source,external_id,byte_size,"
        "uploaded_at,raw_path) VALUES (?,?,?,?,?,?,?)",
        (filename, fp, source, external_id, len(data), db.now_iso(), raw_path))
    source_id = cur.lastrowid

    cur = conn.execute(
        "INSERT INTO activity(kind,title,start_time_utc,end_time_utc,start_tz_offset_min,"
        "stats_json,bbox_json,canonical,created_at) VALUES (?,?,?,?,?,?,?,1,?)",
        (kind, title, start_utc.isoformat(), end_utc.isoformat(), tz_offset_min,
         db.dumps(stats_dict), db.dumps(bbox(kept)), db.now_iso()))
    activity_id = cur.lastrowid
    conn.execute("INSERT INTO activity_source(activity_id,source_id) VALUES (?,?)",
                 (activity_id, source_id))
    # store kept points (spikes themselves are dropped; only count retained for audit)
    _save_points(conn, activity_id, kept, removed_set)
    conn.commit()

    # match against existing canonical activities
    match = match_against_existing(conn, activity_id, rules)
    return {
        "activity_id": activity_id,
        "source_id": source_id,
        "fingerprint": fp,
        "stats": stats_dict,
        "tz_offset_min": tz_offset_min,
        "match": match,
    }


def _source_meta(conn, activity_id: int):
    row = conn.execute(
        "SELECT s.source, s.external_id FROM source_file s JOIN activity_source asp"
        " ON s.id=asp.source_id WHERE asp.activity_id=? ORDER BY s.id LIMIT 1",
        (activity_id,)).fetchone()
    return (row["source"], row["external_id"]) if row else ("unknown", None)


def match_against_existing(conn, activity_id: int, rules=None) -> dict | None:
    rules = rules or get_rules()
    act = conn.execute("SELECT * FROM activity WHERE id=?", (activity_id,)).fetchone()
    if act is None:
        return None
    a_start, a_end = parse_ts(act["start_time_utc"]), parse_ts(act["end_time_utc"])
    pa = load_points(conn, activity_id)
    src_a, ext_a = _source_meta(conn, activity_id)

    rows = conn.execute(
        "SELECT * FROM activity WHERE id != ? AND canonical=1 ORDER BY start_time_utc",
        (activity_id,)).fetchall()
    best: dict | None = None
    for r in rows:
        b_start, b_end = parse_ts(r["start_time_utc"]), parse_ts(r["end_time_utc"])
        pb = load_points(conn, r["id"])
        src_b, ext_b = _source_meta(conn, r["id"])
        res = evaluate(
            source_a=src_a, source_b=src_b, external_id_a=ext_a, external_id_b=ext_b,
            a_start=a_start, a_end=a_end, b_start=b_start, b_end=b_end,
            pa=pa, pb=pb, rules=rules)
        if best is None or res.time_overlap > best["metrics"]["overlap_ratio"]:
            best = {"other_activity_id": r["id"], "duplicate": res.is_duplicate,
                    "reason": res.reason,
                    "metrics": {"overlap_ratio": round(res.time_overlap, 3),
                                "start_gap_s": res.start_gap_s,
                                "endpoint_m": round(res.endpoint_m, 1),
                                "hausdorff_m": round(res.hausdorff_m, 1)}}
        if res.is_duplicate:
            mark_duplicate(conn, activity_id, r["id"], res.reason,
                           evidence=best["metrics"], decided_by="auto")
            best["duplicate"] = True
            return best
    return best


def mark_duplicate(conn, dup_id: int, keep_id: int, reason: str,
                   evidence: dict | None = None, decided_by: str = "user"):
    a, b = sorted((dup_id, keep_id))
    conn.execute(
        "INSERT INTO activity_link(activity_a,activity_b,decision,decided_by,"
        "evidence_json,created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(activity_a,activity_b) DO UPDATE SET decision=excluded.decision,"
        "decided_by=excluded.decided_by,evidence_json=excluded.evidence_json,"
        "created_at=excluded.created_at",
        (a, b, "confirmed_duplicate", decided_by, db.dumps(evidence or {"reason": reason}),
         db.now_iso()))
    # keep the earlier-created identity canonical; merge source membership
    keep, drop = sorted((dup_id, keep_id))
    if keep != keep_id:
        keep, drop = keep_id, dup_id
    for sid in conn.execute("SELECT source_id FROM activity_source WHERE activity_id=?",
                            (drop,)).fetchall():
        conn.execute("INSERT OR IGNORE INTO activity_source(activity_id,source_id) VALUES (?,?)",
                     (keep, sid["source_id"]))
    conn.execute("UPDATE activity SET canonical=0 WHERE id=?", (drop,))
    conn.commit()
    return {"kept": keep, "dropped": drop}


def split_activities(conn, act_a: int, act_b: int, reason: str = "user split"):
    """Manual override: two activities are genuinely separate. Sticky across re-imports."""
    a, b = sorted((act_a, act_b))
    conn.execute(
        "INSERT INTO activity_link(activity_a,activity_b,decision,decided_by,"
        "evidence_json,created_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(activity_a,activity_b) DO UPDATE SET decision='split',"
        "decided_by='user',evidence_json=excluded.evidence_json,created_at=excluded.created_at",
        (a, b, "split", "user", db.dumps({"reason": reason}), db.now_iso()))
    # restore both to canonical (recovery)
    conn.execute("UPDATE activity SET canonical=1 WHERE id IN (?,?)", (a, b))
    conn.commit()
    return {"split": [a, b]}


def is_split(conn, act_a: int, act_b: int) -> bool:
    a, b = sorted((act_a, act_b))
    row = conn.execute(
        "SELECT decision FROM activity_link WHERE activity_a=? AND activity_b=?",
        (a, b)).fetchone()
    return bool(row and row["decision"] == "split")


def apply_distance_correction(conn, activity_id: int, new_distance_m: float,
                              reason: str = "", origin: str = "user_confirmed"):
    """Manual distance correction -> new user_confirmed version.

    device_estimated 与 user_confirmed 始终区分显示；原值保留在 correction 历史。
    """
    act = conn.execute("SELECT stats_json FROM activity WHERE id=?", (activity_id,)).fetchone()
    if act is None:
        raise KeyError("activity not found")
    stats = __import__("json").loads(act["stats_json"])
    old = stats.get("distance_m")
    conn.execute(
        "INSERT INTO correction(activity_id,field,old_value,new_value,origin,reason,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (activity_id, "distance_m", old, float(new_distance_m), origin, reason, db.now_iso()))
    stats["distance_m"] = float(new_distance_m)
    stats["distance_origin"] = origin
    conn.execute("UPDATE activity SET stats_json=? WHERE id=?", (db.dumps(stats), activity_id))
    conn.commit()
    return {"activity_id": activity_id, "old_distance_m": old,
            "new_distance_m": float(new_distance_m), "origin": origin}


def effective_stats(act_row) -> dict:
    import json
    return json.loads(act_row["stats_json"])
