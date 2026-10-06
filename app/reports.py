"""Annual report aggregation with frozen, rule-versioned snapshots.

口径要点：
- 跨午夜活动按 *开始* 的归属日（v1=UTC 日，v2=当地时区日）计入，里程不拆日。
- 训练统计与比赛成绩分开：race 不进训练里程/跑量图表，单独列成绩。
- 报表内所有图表（月度柱、热力、设备对比…）引用同一冻结数据范围 data_range。
- 生成开始即做快照（frozen_activity_ids + 每个活动的 stats 冻结值），
  生成期间新增/订正的记录不影响本次年报；历史年报可按 rules_version 重新生成。
"""
from __future__ import annotations

import json
from datetime import timedelta, timezone

from . import db
from .geo import parse_ts
from .rules import Rules


def attribution_date(act_row, rules: Rules):
    start = parse_ts(act_row["start_time_utc"])
    if rules.date_attribution == "start_local":
        off = timedelta(minutes=act_row["start_tz_offset_min"] or 0)
        dt = start.astimezone(timezone(off))
    else:
        dt = start.astimezone(timezone.utc)
    return dt.date()


def collect(conn, year: int, rules: Rules) -> dict:
    """Single source of truth for *every* chart in one report run."""
    rows = conn.execute("SELECT * FROM activity WHERE canonical=1 ORDER BY start_time_utc").fetchall()
    frozen_ids = []
    monthly = {f"{year}-{m:02d}": {"distance_m": 0.0, "moving_s": 0.0,
                                    "elapsed_s": 0.0, "activities": 0,
                                    "device_distance_m": 0.0,
                                    "confirmed_distance_m": 0.0}
               for m in range(1, 13)}
    training = {"distance_m": 0.0, "moving_s": 0.0, "activities": 0,
                "device_only_activities": 0, "user_confirmed_activities": 0}
    races = []
    race_ids = {r["activity_id"] for r in conn.execute(
        "SELECT activity_id FROM race_result").fetchall()}

    for r in rows:
        d = attribution_date(r, rules)
        if d.year != year:
            continue
        frozen_ids.append(r["id"])
        stats = json.loads(r["stats_json"])
        dist = float(stats.get("distance_m", 0.0))
        moving = float(stats.get("moving_s", 0.0))
        elapsed = float(stats.get("elapsed_s", 0.0))
        origin = stats.get("distance_origin", "device_estimated")
        is_race = r["kind"] == "race" or r["id"] in race_ids

        if is_race:
            rr = conn.execute("SELECT * FROM race_result WHERE activity_id=?",
                              (r["id"],)).fetchone()
            races.append({
                "activity_id": r["id"], "title": r["title"],
                "date": d.isoformat(), "distance_m": dist,
                "chip_time_s": rr["chip_time_s"] if rr else None,
                "gun_time_s": rr["gun_time_s"] if rr else None,
                "pace_sec_per_km": rr["pace_sec_per_km"] if rr else None,
                "overall_rank": rr["overall_rank"] if rr else None,
            })
            continue  # 成绩与训练统计分别处理

        key = f"{year}-{d.month:02d}"
        bucket = monthly[key]
        bucket["distance_m"] += dist
        bucket["moving_s"] += moving
        bucket["elapsed_s"] += elapsed
        bucket["activities"] += 1
        if origin == "user_confirmed":
            bucket["confirmed_distance_m"] += dist
            training["user_confirmed_activities"] += 1
        else:
            bucket["device_distance_m"] += dist
            training["device_only_activities"] += 1
        training["distance_m"] += dist
        training["moving_s"] += moving
        training["activities"] += 1

    return {
        "year": year,
        "rules_version": rules.version,
        "data_range": {
            "frozen_activity_ids": frozen_ids,
            "count": len(frozen_ids),
            "date_attribution": rules.date_attribution,
        },
        "monthly": monthly,
        "training": training,
        "races": sorted(races, key=lambda x: x["date"]),
    }


def generate(conn, year: int, rules_version: str | None = None, title: str | None = None) -> dict:
    from .rules import get_rules
    rules = get_rules(rules_version)
    payload = collect(conn, year, rules)  # snapshot computed in one pass
    title = title or f"{year} 跑者年报"
    conn.execute(
        "INSERT INTO report(year,rules_version,title,created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(year,rules_version) DO UPDATE SET title=excluded.title,"
        "created_at=excluded.created_at",
        (year, rules.version, title, db.now_iso()))
    report_id = conn.execute(
        "SELECT id FROM report WHERE year=? AND rules_version=?",
        (year, rules.version)).fetchone()["id"]
    conn.execute("DELETE FROM report_snapshot WHERE report_id=?", (report_id,))
    conn.execute("INSERT INTO report_snapshot(report_id,payload_json) VALUES (?,?)",
                 (report_id, db.dumps(payload)))
    conn.commit()
    payload["report_id"] = report_id
    return payload


def get_snapshot(conn, year: int, rules_version: str) -> dict | None:
    row = conn.execute(
        "SELECT rs.payload_json FROM report_snapshot rs JOIN report r ON r.id=rs.report_id"
        " WHERE r.year=? AND r.rules_version=?",
        (year, rules_version)).fetchone()
    return json.loads(row["payload_json"]) if row else None
