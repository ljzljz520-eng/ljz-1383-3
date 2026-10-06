"""Dependency-free JSON HTTP server for the runner annual report site."""
from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
import re

from . import content, services
from .database import connect, init_db, transaction
from .importers import ImportError_, parse_training_file

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


def parse_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    server_version = "RunnerReport/1.0"

    def _send(self, payload, status=200, content_type="application/json; charset=utf-8", extra_headers=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        else:
            body = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc, status=400):
        self._send({"error": type(exc).__name__.strip("_"), "message": str(exc)}, status=status)

    def _json_body(self):
        length = parse_int(self.headers.get("Content-Length"), 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        conn = connect()
        try:
            if path == "/":
                return self._static("index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._static(path.split("/static/", 1)[1])
            if path == "/api/health":
                return self._send({"ok": True})
            if path == "/api/methodology":
                return self._send(services.methodology())
            if path == "/api/activities":
                return self._send(services.list_activities(conn, query.get("include_duplicates", ["0"])[0] == "1"))
            match = re.fullmatch(r"/api/activities/(\d+)", path)
            if match:
                return self._send(services.get_activity(conn, int(match.group(1))))
            if path == "/api/duplicates":
                return self._send(services.list_duplicate_groups(conn))
            if path == "/api/races":
                return self._send(content.list_races(conn, query.get("start", [None])[0], query.get("end", [None])[0]))
            match = re.fullmatch(r"/api/races/(\d+)", path)
            if match:
                return self._send(content.get_race(conn, int(match.group(1))))
            if path == "/api/stories":
                return self._send(content.list_stories(conn, query.get("start", [None])[0], query.get("end", [None])[0]))
            match = re.fullmatch(r"/api/stories/(\d+)", path)
            if match:
                return self._send(content.get_story(conn, int(match.group(1))))
            if path == "/api/injuries":
                return self._send(content.list_injuries(conn))
            match = re.fullmatch(r"/api/injuries/(\d+)", path)
            if match:
                return self._send(content.get_injury(conn, int(match.group(1))))
            if path == "/api/statistics":
                year = parse_int(query.get("year", [None])[0])
                rule = query.get("rule_version", [services.CURRENT_RULE])[0]
                return self._send(services.compute_statistics(
                    conn, year, query.get("start", [None])[0], query.get("end", [None])[0], rule))
            if path == "/api/reports":
                return self._send(content.list_reports(conn))
            match = re.fullmatch(r"/api/reports/(\d+)", path)
            if match:
                return self._send(content.get_report(conn, int(match.group(1))))
            match = re.fullmatch(r"/api/shares/([^/]+)", path)
            if match:
                if query.get("download") == ["gpx"]:
                    share = content.get_share(conn, unquote(match.group(1)))
                    body = content.render_redacted_gpx(share).encode("utf-8")
                    return self._send(body, content_type="application/gpx+xml; charset=utf-8",
                                      extra_headers={"Content-Disposition": 'attachment; filename="redacted-run.gpx"'})
                if query.get("thumbnail") == ["svg"]:
                    share = content.get_share(conn, unquote(match.group(1)))
                    return self._send(content.render_thumbnail_svg(share).encode("utf-8"),
                                      content_type="image/svg+xml; charset=utf-8")
                if query.get("check") == ["privacy"]:
                    return self._send(content.privacy_check(conn, unquote(match.group(1))))
                return self._send(content.get_share(conn, unquote(match.group(1)), include_checks=True))
            self._send({"error": "not_found", "message": path}, 404)
        except KeyError as exc:
            self._error(exc, 404)
        except (ValueError, ImportError_) as exc:
            self._error(exc, 400)
        except Exception as exc:  # keep API usable during local acceptance
            self._error(exc, 500)
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = connect()
        try:
            if path == "/api/imports":
                payload = self._json_body()
                filename = payload.get("filename", "upload.json")
                # The browser client supplies base64; tests may call services directly.
                import base64
                raw = base64.b64decode(payload["content_base64"])
                parsed_file = parse_training_file(filename, raw)
                if payload.get("tz_name") and not parsed_file.tz_name:
                    parsed_file.tz_name = payload["tz_name"]
                if payload.get("is_race") is not None:
                    parsed_file.is_race = bool(payload["is_race"])
                with transaction(conn):
                    result = services.import_activity(conn, filename, raw, parsed_file, payload.get("default_tz"))
                return self._send(result, 201)

            body = self._json_body()
            with transaction(conn):
                if path == "/api/races":
                    return self._send(content.create_race(conn, body), 201)
                if path == "/api/stories":
                    return self._send(content.create_story(conn, body), 201)
                if path == "/api/injuries":
                    return self._send(content.create_injury(conn, body), 201)
                if path == "/api/reports":
                    return self._send(content.create_report(
                        conn, int(body["year"]), body.get("rule_version", services.CURRENT_RULE),
                        body.get("status", "draft")), 201)
                match = re.fullmatch(r"/api/shares/(activity|report)/(\d+)", path)
                if match:
                    return self._send(content.create_share(conn, match.group(1), int(match.group(2)), body), 201)
            self._send({"error": "not_found", "message": path}, 404)
        except KeyError as exc:
            self._error(exc, 404)
        except (ValueError, ImportError_) as exc:
            self._error(exc, 400)
        except Exception as exc:
            self._error(exc, 500)
        finally:
            conn.close()

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = connect()
        body = self._json_body()
        try:
            with transaction(conn):
                match = re.fullmatch(r"/api/activities/(\d+)/corrections", path)
                if match:
                    return self._send(services.correct_activity(
                        conn, int(match.group(1)), body, body.get("changed_by", "user"), body.get("reason")))
                match = re.fullmatch(r"/api/duplicates/(\d+)/split", path)
                if match:
                    return self._send(services.split_duplicate_group(
                        conn, int(match.group(1)), body.get("reason"), body.get("changed_by", "user")))
                match = re.fullmatch(r"/api/duplicates/(\d+)/restore", path)
                if match:
                    return self._send(services.restore_duplicate_group(
                        conn, int(match.group(1)), body.get("changed_by", "user")))
                match = re.fullmatch(r"/api/races/(\d+)", path)
                if match:
                    return self._send(content.update_race(conn, int(match.group(1)), body))
                match = re.fullmatch(r"/api/stories/(\d+)", path)
                if match:
                    return self._send(content.update_story(conn, int(match.group(1)), body))
                match = re.fullmatch(r"/api/injuries/(\d+)", path)
                if match:
                    return self._send(content.update_injury(conn, int(match.group(1)), body))
                match = re.fullmatch(r"/api/reports/(\d+)/publish", path)
                if match:
                    return self._send(content.publish_report(conn, int(match.group(1))))
                match = re.fullmatch(r"/api/reports/(\d+)/regenerate", path)
                if match:
                    return self._send(content.regenerate_report(conn, int(match.group(1))), 201)
            self._send({"error": "not_found", "message": path}, 404)
        except KeyError as exc:
            self._error(exc, 404)
        except (ValueError, ImportError_) as exc:
            self._error(exc, 400)
        except Exception as exc:
            self._error(exc, 500)
        finally:
            conn.close()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _static(self, name: str, content_type="application/octet-stream"):
        safe = Path(name).name
        file_path = STATIC / safe
        if not file_path.is_file():
            return self._send({"error": "not_found"}, 404)
        data = file_path.read_bytes()
        if safe.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif safe.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        return self._send(data, content_type=content_type)

    def log_message(self, fmt, *args):
        if os.environ.get("HTTP_LOG"):
            super().log_message(fmt, *args)


def main(host=None, port=None):
    conn = connect()
    init_db(conn)
    conn.close()
    server = ThreadingHTTPServer((host or os.environ.get("HOST", "0.0.0.0"),
                                  port or int(os.environ.get("PORT", "8000"))), Handler)
    print(f"Runner report site listening on http://{server.server_address[0]}:{server.server_address[1]}")
    server.serve_forever()


if __name__ == "__main__":
    main()
