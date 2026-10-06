"""SQLite persistence for source identities, revisions and normalized facts."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    provider TEXT,
    source_uid TEXT,
    device_name TEXT,
    filename TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    raw_metadata TEXT NOT NULL DEFAULT '{}',
    imported_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_content ON sources(content_sha256);
CREATE INDEX IF NOT EXISTS idx_sources_natural ON sources(provider, source_uid);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    activity_type TEXT NOT NULL DEFAULT 'run',
    name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    tz_name TEXT,
    local_start_date TEXT NOT NULL,
    is_race INTEGER NOT NULL DEFAULT 0,
    device_distance_m REAL,
    corrected_distance_m REAL,
    device_moving_s INTEGER,
    corrected_moving_s INTEGER,
    device_elapsed_s INTEGER,
    measured_distance_m REAL NOT NULL DEFAULT 0,
    measured_moving_s INTEGER,
    dropped_points INTEGER NOT NULL DEFAULT 0,
    normalization_rule TEXT NOT NULL DEFAULT 'gps-v1',
    normalization_issues TEXT NOT NULL DEFAULT '[]',
    canonical_activity_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(started_at);
CREATE INDEX IF NOT EXISTS idx_activities_local ON activities(local_start_date);
CREATE INDEX IF NOT EXISTS idx_activities_canonical ON activities(canonical_activity_id);

CREATE TABLE IF NOT EXISTS track_points (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    utc_time TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    elevation_m REAL,
    hr REAL,
    cadence REAL,
    UNIQUE(activity_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_points_activity ON track_points(activity_id, seq);

CREATE TABLE IF NOT EXISTS activity_day_allocations (
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    distance_m REAL NOT NULL,
    moving_s REAL NOT NULL,
    PRIMARY KEY(activity_id, day)
);
CREATE INDEX IF NOT EXISTS idx_alloc_day ON activity_day_allocations(day);

CREATE TABLE IF NOT EXISTS activity_versions (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    changed_by TEXT NOT NULL,
    change_type TEXT NOT NULL,
    reason TEXT,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(activity_id, revision)
);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('active','split')),
    canonical_activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    match_basis TEXT NOT NULL,
    match_score REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS duplicate_members (
    group_id INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'duplicate',
    PRIMARY KEY(group_id, activity_id)
);
CREATE TABLE IF NOT EXISTS duplicate_split_decisions (
    id INTEGER PRIMARY KEY,
    group_id INTEGER REFERENCES duplicate_groups(id) ON DELETE SET NULL,
    activity_a_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    activity_b_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    changed_by TEXT NOT NULL DEFAULT 'user',
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_split_a ON duplicate_split_decisions(activity_a_id, activity_b_id);
CREATE INDEX IF NOT EXISTS idx_split_b ON duplicate_split_decisions(activity_b_id, activity_a_id);

CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER UNIQUE REFERENCES activities(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    race_date TEXT NOT NULL,
    distance_m REAL NOT NULL,
    elapsed_s INTEGER,
    overall_place INTEGER,
    age_group_place TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_races_date ON races(race_date);

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    activity_id INTEGER REFERENCES activities(id) ON DELETE SET NULL,
    happened_on TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    mood TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stories_date ON stories(happened_on);

CREATE TABLE IF NOT EXISTS injuries (
    id INTEGER PRIMARY KEY,
    body_part TEXT NOT NULL,
    occurred_on TEXT,
    severity TEXT NOT NULL CHECK(severity IN ('mild','moderate','severe')),
    is_private INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    year INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','published','archived')),
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_year ON reports(year, rule_version);

CREATE TABLE IF NOT EXISTS share_tokens (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('activity','report')),
    subject_id INTEGER NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS share_redactions (
    id INTEGER PRIMARY KEY,
    share_id INTEGER NOT NULL REFERENCES share_tokens(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    radius_m REAL NOT NULL DEFAULT 150
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(path: str | None = None) -> sqlite3.Connection:
    db_path = path or os.environ.get("RUNNER_DB", "data/runner.sqlite3")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None):
    if value is None:
        return default
    return json.loads(value)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
