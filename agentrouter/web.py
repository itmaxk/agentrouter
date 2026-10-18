"""Local dashboard: static page plus JSON endpoints, bound to localhost."""
import json
import logging
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import report
from .db import DB
from .stats import parse_period

PAGE = Path(__file__).with_name("dashboard.html")
log = logging.getLogger("agentrouter.web")


def serve(cfg, db_path=None):
    db = DB(db_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            # JSON-only and same-origin: a cross-site page can't send this without a CORS preflight
            origin = self.headers.get("Origin")
            if (not (self.headers.get("Content-Type") or "").startswith("application/json")
                    or (origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"))):
                return self.send(403, b"forbidden", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if url.path == "/api/checks":
                    report.set_checks(db, bool(body["enabled"]))
                    data = {"checks_paused": db.get("checks_paused")}
                else:
                    return self.send(404, b"not found", "text/plain")
            except (ValueError, KeyError, TypeError) as e:
                return self.send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            self.send(200, json.dumps({"now": time.time(), "data": data}).encode(), "application/json")

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/status":
                    data = report.status(db, cfg)
                elif url.path == "/api/stats":
                    data = report.stats_table(db, parse_period(qs.get("period", "7d")))
                elif url.path == "/api/history":
                    data = report.history(db, qs.get("model"), parse_period(qs.get("period", "2d")))
                elif url.path == "/api/predict":
                    data = report.predictions(db, cfg, int(qs.get("hours", 24)))
                elif url.path == "/api/switches":
                    data = db.switches(int(qs.get("limit", 50)))
                else:
                    return self.send(404, b"not found", "text/plain")
            except Exception as e:
                log.exception("api error")
                return self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            self.send(200, json.dumps({"now": time.time(), "data": data}, default=str).encode(), "application/json")

    host, port = cfg["web"]["host"], cfg["web"]["port"]
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    log.info("dashboard on http://%s:%d", host, port)
    httpd.serve_forever()
