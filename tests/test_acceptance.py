import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.content import (create_race, create_report, create_share, create_story, create_injury,
                          privacy_check, publish_report, regenerate_report, render_redacted_gpx,
                          render_thumbnail_svg, get_share)
from app.database import connect, init_db
from app.importers import parse_gpx, parse_training_file
from app.services import (CURRENT_RULE, compute_statistics, content_hash, correct_activity,
                          import_activity, list_duplicate_groups, split_duplicate_group,
                          restore_duplicate_group)
from app.track import allocate_by_local_day, normalize_track

SH = ZoneInfo("Asia/Shanghai")


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite3"
        self.conn = connect(str(self.db))
        self.conn.isolation_level = None
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def activity_json(self, name, start, points, provider="watch", uid=None, distance=None, race=False):
        return json.dumps({
            "source_provider": provider, "source_uid": uid or name, "name": name,
            "tz_name": "Asia/Shanghai", "is_race": race,
            "device_distance_m": distance,
            "points": [{"time": t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        "lat": lat, "lon": lon} for t, lat, lon in points],
        }).encode()

    def import_json(self, name, content, filename=None):
        parsed = parse_training_file(filename or f"{name}.json", content)
        return import_activity(self.conn, filename or f"{name}.json", content, parsed)

    def straight_points(self, name, start, n=10, step_m=100):
        points = []
        lat, lon = 31.2300, 121.4700
        # ~1/111km per latitude degree
        for i in range(n):
            t = start + timedelta(seconds=30 * i)
            points.append((t, lat + i * step_m / 111_000, lon))
        return points


class TrackTests(TempDB):
    def test_gps_spike_removed_but_real_gap_remains(self):
        start = datetime(2026, 3, 1, tzinfo=SH)
        points = [{"utc": start, "lat": 31.2, "lon": 121.4}]
        points.append({"utc": start + timedelta(seconds=30), "lat": 31.20005, "lon": 121.4})
        points.append({"utc": start + timedelta(seconds=31), "lat": 35.0, "lon": 130.0})
        points.append({"utc": start + timedelta(seconds=60), "lat": 31.20010, "lon": 121.4})
        result = normalize_track(points)
        self.assertEqual(result.dropped_points, 1)
        self.assertEqual(len(result.points), 3)
        self.assertLess(result.distance_m, 100)
        self.assertEqual(result.moving_s, 60)

    def test_cross_midnight_mileage_allocated_to_both_days(self):
        start = datetime(2026, 1, 31, 23, 50, tzinfo=SH)
        points = [
            {"utc": start, "lat": 31.20, "lon": 121.40},
            {"utc": start + timedelta(minutes=20), "lat": 31.21, "lon": 121.40},
        ]
        alloc = allocate_by_local_day(normalize_track(points).points, "Asia/Shanghai", 2000, 1200, moving_gap_s=1200)
        self.assertEqual(set(alloc), {"2026-01-31", "2026-02-01"})
        self.assertAlmostEqual(sum(v["distance_m"] for v in alloc.values()), 2000, places=1)
        self.assertEqual(sum(v["moving_s"] for v in alloc.values()), 1200)

    def test_gpx_parser_retains_source_and_timestamped_points(self):
        gpx = b"""<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><metadata><name>watch-1</name></metadata><trk><trkseg>
        <trkpt lat="31.2" lon="121.4"><time>2026-02-01T23:10:00Z</time></trkpt>
        <trkpt lat="31.21" lon="121.4"><time>2026-02-01T23:20:00Z</time></trkpt>
        </trkseg></trk></gpx>"""
        parsed = parse_gpx(gpx)
        self.assertEqual(parsed.source_uid, "watch-1")
        self.assertEqual(len(parsed.raw_points), 2)
        self.assertEqual(parsed.started_at.year, 2026)


class ImportAndDuplicateTests(TempDB):
    def test_duplicate_file_and_source_not_double_counted(self):
        start = datetime(2026, 4, 1, 8, tzinfo=SH)
        points = self.straight_points("run", start)
        watch = self.activity_json("same", start, points, "garmin", "ext-1")
        phone = watch  # same bytes, different filename in real UI
        first = self.import_json("watch", watch, "watch.gpx.json")
        second = self.import_json("phone", phone, "phone-copy.json")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        stats = compute_statistics(self.conn, 2026, rule_version=CURRENT_RULE)
        self.assertEqual(stats["training"]["activity_count"], 1)

    def test_overlapping_watch_phone_uploads_match_spatiotemporally(self):
        start = datetime(2026, 4, 2, 8, tzinfo=SH)
        points = self.straight_points("same", start, n=12, step_m=90)
        shifted = [(t + timedelta(seconds=2), lat + 0.00002, lon + 0.00002) for t, lat, lon in points]
        a = self.activity_json("watch", start, points, "garmin", "a")
        b = self.activity_json("phone", start, shifted, "apple", "b")
        self.import_json("a", a)
        r = self.import_json("b", b)
        self.assertTrue(r["duplicate"])
        self.assertEqual(r["basis"], "spatiotemporal")

    def test_adjacent_real_sessions_are_not_merged_by_time_alone(self):
        start1 = datetime(2026, 4, 3, 8, tzinfo=SH)
        start2 = start1 + timedelta(minutes=5)
        p1 = self.straight_points("morning-a", start1, n=6)
        p2 = [(start2 + timedelta(seconds=30 * i), 31.30 + i / 111_000, 121.50) for i in range(6)]
        self.import_json("a", self.activity_json("a", start1, p1, "w", "adj-a"))
        r = self.import_json("b", self.activity_json("b", start2, p2, "w", "adj-b"))
        self.assertFalse(r["duplicate"])
        stats = compute_statistics(self.conn, 2026, rule_version=CURRENT_RULE)
        self.assertEqual(stats["training"]["activity_count"], 2)

    def test_manual_split_is_persisted_and_restore_works(self):
        start = datetime(2026, 4, 4, 8, tzinfo=SH)
        points = self.straight_points("same", start)
        shifted = [(t + timedelta(seconds=1), lat + 0.00001, lon) for t, lat, lon in points]
        self.import_json("a", self.activity_json("a", start, points, "w", "m1"))
        dup = self.import_json("b", self.activity_json("b", start, shifted, "w", "m2"))
        gid = dup["group_id"]
        split_duplicate_group(self.conn, gid, "two real sessions")
        stats = compute_statistics(self.conn, 2026, rule_version=CURRENT_RULE)
        self.assertEqual(stats["training"]["activity_count"], 2)
        restore_duplicate_group(self.conn, gid)
        stats = compute_statistics(self.conn, 2026, rule_version=CURRENT_RULE)
        self.assertEqual(stats["training"]["activity_count"], 1)


class CorrectionAndStatsTests(TempDB):
    def _one_run(self, race=False):
        start = datetime(2026, 5, 1, 8, tzinfo=SH)
        points = self.straight_points("race" if race else "run", start, n=11, step_m=100)
        return self.import_json("run", self.activity_json("run", start, points, "w", "x", distance=950, race=race))

    def test_manual_distance_correction_version_and_label(self):
        r = self._one_run()
        updated = correct_activity(self.conn, r["activity"]["id"], {"corrected_distance_m": 1234}, reason="known route")
        self.assertEqual(updated["effective"]["distance_status"], "user_confirmed")
        self.assertAlmostEqual(updated["effective"]["distance_m"], 1234)
        self.assertEqual(len(updated["revisions"]), 2)
        stats = compute_statistics(self.conn, 2026)
        self.assertAlmostEqual(stats["training"]["distance_km"], 1.234)
        self.assertEqual(stats["training"]["distance_by_status"]["user_confirmed"], 1234)

    def test_race_result_separate_from_training_statistics(self):
        self._one_run(race=True)
        create_race(self.conn, {"name": "10K race", "race_date": "2026-05-01", "distance_m": 10000,
                                "elapsed_s": 2700, "activity_id": 1})
        stats = compute_statistics(self.conn, 2026, rule_version=CURRENT_RULE)
        self.assertEqual(stats["training"]["distance_km"], 0)
        self.assertEqual(stats["training"]["activity_count"], 0)
        self.assertEqual(len(stats["races"]), 1)
        self.assertEqual(stats["races"][0]["elapsed_s"], 2700)

    def test_report_regeneration_uses_historical_rule_and_adds_record_without_mutating(self):
        self._one_run()
        report = create_report(self.conn, 2026, "2024-v1", "published")
        published_snapshot = json.dumps(report["snapshot"], sort_keys=True)
        publish_report(self.conn, report["id"])
        # New record arrives before regeneration.
        start = datetime(2026, 6, 1, 8, tzinfo=SH)
        self.import_json("later", self.activity_json("later", start, self.straight_points("later", start), "w", "later"))
        draft = regenerate_report(self.conn, report["id"])
        self.assertNotEqual(draft["id"], report["id"])
        self.assertEqual(draft["rule_version"], "2024-v1")
        self.assertEqual(draft["status"], "draft")
        old = publish_report  # symbol exists; old report remains unchanged
        reread = json.dumps(__import__("app.content", fromlist=["get_report"]).get_report(self.conn, report["id"])["snapshot"], sort_keys=True)
        self.assertEqual(published_snapshot, reread)
        self.assertGreater(draft["snapshot"]["training"]["activity_count"], report["snapshot"]["training"]["activity_count"])


class PrivacyTests(TempDB):
    def test_share_thumbnail_download_aggregate_have_no_private_coordinates_or_injury(self):
        start = datetime(2026, 7, 1, 8, tzinfo=SH)
        points = [(start + timedelta(seconds=30 * i), 31.2300 + i * 0.001, 121.4700) for i in range(8)]
        r = self.import_json("home", self.activity_json("home", start, points, "w", "home"))
        home = points[0]
        create_injury(self.conn, {"body_part": "knee", "occurred_on": "2026-07-02",
                                  "severity": "mild", "notes": "secret symptom", "is_private": True})
        share = create_share(self.conn, "activity", r["activity"]["id"],
                             {"title": "share", "redactions": [{"label": "home", "lat": home[1], "lon": home[2], "radius_m": 200}]})
        token = share["token"]
        check = privacy_check(self.conn, token)
        self.assertTrue(check["pass"], check)
        self.assertGreaterEqual(check["removed_points"], 1)
        public = get_share(self.conn, token)
        text = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("secret symptom", text)
        gpx = render_redacted_gpx(public)
        svg = render_thumbnail_svg(public)
        self.assertNotIn(f"{home[1]:.7f}", gpx)
        self.assertNotIn(f"{home[2]:.7f}", svg)
        self.assertNotIn(f"{home[1]:.7f}", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
