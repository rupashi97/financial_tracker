#!/usr/bin/env python3
"""Financial Tracker - local web app.

  .venv/bin/python app/server.py            -> http://127.0.0.1:8765

Listens on 127.0.0.1 only and rejects requests from other sites. The page loads nothing from the internet.
"""
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts  # noqa: E402
import claude  # noqa: E402
import household  # noqa: E402
import parsers  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
from store import db, file_hash, months_back, uploads_dir  # noqa: E402

WEB = Path(__file__).resolve().parent.parent / "web"
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
MAX_UPLOAD = 50 * 1024 * 1024
RUN_LOCK = threading.Lock()


def valid_month(m):
    return bool(MONTH_RE.match(m or "")) and m in months_back(61)


def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", Path(name).name).strip() or "upload"
    return name[:120]


def months_with_data():
    """Months with any transactions (incl. leftovers from neighbouring statements), and months you uploaded for."""
    conn = db()
    uploaded = {r[0] for r in conn.execute("SELECT DISTINCT month FROM files WHERE status = 'read'")}
    ms = uploaded | {r[0] for r in conn.execute("SELECT DISTINCT month FROM transactions")}
    return sorted(ms, reverse=True), sorted(uploaded, reverse=True)


def run_job(month):
    with RUN_LOCK:  # one month at a time
        pipeline.process(month)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def _origin_ok(self):
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._origin_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/months":
                if not claude.available():  # cheap while logged out; picks up a fresh login without a restart
                    claude.check()
                with_data, uploaded = months_with_data()
                return self._send(200, {"with_data": with_data, "uploaded": uploaded, "claude": claude.available(),
                                        "title": household.title(),
                                        "members": [{"id": k, "name": v} for k, v in household.member_names().items()]})
            if u.path == "/api/status":
                return self._send(200, pipeline.JOBS.get(q.get("month"), {"stage": "idle"}))
            if u.path == "/api/report":
                if not valid_month(q.get("month")):
                    return self._send(400, {"error": "bad month"})
                view = q.get("view") or "household"
                if view != "household" and view not in household.member_names():
                    return self._send(400, {"error": "unknown view"})
                return self._send(200, report.build(q["month"], view))
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._origin_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/api/owner":
            # One-time answers: whose income / whose account. Remembered for all future months.
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            owner, key = body.get("owner"), str(body.get("key", ""))[:200]
            valid = set(household.member_names()) | ({"joint"} if body.get("type") == "account" else set())
            if owner not in valid or not key:
                return self._send(400, {"error": "bad input"})
            if body.get("type") == "income":
                household.set_income_owner(key, owner)
            elif body.get("type") == "account":
                accts = accounts.load()
                if key not in {a["id"] for a in accts}:
                    return self._send(400, {"error": "unknown account"})
                for a in accts:
                    if a["id"] == key:
                        a["owner"] = owner
                from store import save_json
                save_json(accounts.REGISTRY, {"accounts": accts})
            else:
                return self._send(400, {"error": "bad type"})
            return self._send(200, {"ok": True})
        month = q.get("month")
        if not valid_month(month):
            return self._send(400, {"error": "pick a month within the last 5 years"})
        try:
            if u.path == "/api/upload":
                name = safe_name(q.get("name", ""))
                if Path(name).suffix.lower() not in parsers.ALLOWED_EXT:
                    return self._send(400, {"error": f"unsupported file type: {name}"})
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_UPLOAD:
                    return self._send(413, {"error": "file too large"})
                data = self.rfile.read(n)
                d = uploads_dir(month)
                d.mkdir(parents=True, exist_ok=True)
                h = file_hash(data)
                for f in d.glob("*"):  # identical file already uploaded for this month
                    if f.is_file() and f.stat().st_size == len(data) and file_hash(f.read_bytes()) == h:
                        return self._send(200, {"ok": True, "duplicate": True})
                dest, i = d / name, 2
                while dest.exists():
                    dest = d / f"{Path(name).stem} ({i}){Path(name).suffix}"
                    i += 1
                dest.write_bytes(data)
                return self._send(200, {"ok": True})
            if u.path == "/api/process":
                if pipeline.JOBS.get(month, {}).get("stage") in ("processing", "creating_report"):
                    return self._send(200, pipeline.JOBS[month])
                pipeline.JOBS[month] = {"month": month, "stage": "processing"}
                threading.Thread(target=run_job, args=(month,), daemon=True).start()
                return self._send(200, pipeline.JOBS[month])
        except Exception as e:
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})


def main():
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"{household.title()}: http://127.0.0.1:{port}  (local only, Ctrl+C to stop)")
    st = claude.status()
    print("Claude:", "connected" if st["ok"] else f"{st['reason']} - PDFs/screenshots may be skipped", f"({st['path']})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
