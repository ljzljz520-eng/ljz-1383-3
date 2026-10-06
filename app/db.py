"""SQLite storage.

身份模型：
- activity        一次“真实跑步”的规范身份（去重组的代表），统计只计 canonical
- source_file     每个上传文件（手表/手机），含文件指纹、来源、外部ID
- activity_source 上传 -> 活动身份 的归属（同一次跑步可有多个 source）
- track_point     规范化轨迹点（保留 spike 标记，支持版本审计）
- correction      订正版本：device_estimated（系统估算）/ user_confirmed（用户确认）
- activity_link   去重组人工决定：confirmed_duplicate / split
- story           个人故事；race_result 成绩与训练统计分开；injury 伤病（私密）
- privacy_zone    住址脱敏区；share_link 分享；report/snapshot 年报冻结数据
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "app.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_file (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    fingerprint TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL DEFAULT 'unknown',
    external_id TEXT,
    byte_size INTEGER,
    uploaded_at TEXT NOT NULL,
    raw_path TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'training',   -- training | race
    title TEXT,
    start_time_utc TEXT NOT NULL,
    end_time_utc TEXT NOT NULL,
    start_tz_offset_min INTEGER DEFAULT 0,
    stats_json TEXT NOT NULL,                -- 冻结的设备估算统计（规范化后）
    bbox_json TEXT,
    canonical INTEGER NOT NULL DEFAULT 1,    -- 0 = 被判重后隐藏，统计不计
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_source (
    activity_id INTEGER NOT NULL REFERENCES activity(id),
    source_id INTEGER NOT NULL REFERENCES source_file(id),
    PRIMARY KEY (activity_id, source_id)
);
CREATE TABLE IF NOT EXISTS track_point (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activity(id),
    lat REAL NOT NULL, lon REAL NOT NULL,
    t_utc TEXT NOT NULL, ele REAL,
    is_spike INTEGER NOT NULL DEFAULT 0,
    version_tag TEXT NOT NULL DEFAULT 'device_estimated'
);
CREATE TABLE IF NOT EXISTS correction (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activity(id),
    field TEXT NOT NULL,                    -- distance_m / moving_s ...
    old_value REAL, new_value REAL NOT NULL,
    origin TEXT NOT NULL,                   -- device_estimated | user_confirmed
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_link (
    id INTEGER PRIMARY KEY,
    activity_a INTEGER NOT NULL REFERENCES activity(id),
    activity_b INTEGER NOT NULL REFERENCES activity(id),
    decision TEXT NOT NULL,                 -- confirmed_duplicate | split
    decided_by TEXT NOT NULL DEFAULT 'user',-- user | auto
    evidence_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(activity_a, activity_b)
);
CREATE TABLE IF NOT EXISTS story (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER REFERENCES activity(id),
    month_key TEXT,                         -- yyyy-mm，允许月度故事
    title TEXT, body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS race_result (
    activity_id INTEGER PRIMARY KEY REFERENCES activity(id),
    race_name TEXT NOT NULL,
    chip_time_s INTEGER, gun_time_s INTEGER,
    distance_m REAL,
    pace_sec_per_km REAL,
    overall_rank INTEGER, age_group_rank INTEGER
);
CREATE TABLE IF NOT EXISTS injury_note (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER REFERENCES activity(id),
    body_part TEXT NOT NULL,
    note TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 1,     -- 永远私密，不入分享/年报
    occurred_on TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS privacy_zone (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 200
);
CREATE TABLE IF NOT EXISTS share_link (
    token TEXT PRIMARY KEY,
    year INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report (
    id INTEGER PRIMARY KEY,
    year INTEGER NOT NULL,
    rules_version TEXT NOT NULL,
    title TEXT, created_at TEXT NOT NULL,
    UNIQUE(year, rules_version)
);
CREATE TABLE IF NOT EXISTS report_snapshot (
    report_id INTEGER PRIMARY KEY REFERENCES report(id),
    payload_json TEXT NOT NULL              -- 生成时刻冻结的数据范围
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str | None = None) -> sqlite3.Connection:
    conn = get_conn(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
