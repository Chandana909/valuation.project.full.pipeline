"""
server.py — live-demo UI server for the valuation engine.

Pure Python stdlib (http.server + json): NO flask/fastapi, consistent with the
project's dependency budget. Serves:

    GET /                → ui/index.html (single-file report app)
    GET /api/companies   → ["Company A", ...]      (autocomplete list)
    GET /api/value?name= → full valuation result JSON (same shape as result.json)
    GET /api/robustness  → seed-robustness sweep (computed once, then cached)
    GET /api/health      → {"ok": true}

Run:  python server.py   →  http://localhost:8733
"""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from mock_api import MockDnBClient
from run import run_pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(BASE, "ui", "index.html")
PORT = int(os.environ.get("PORT", "8733"))

_lock = threading.Lock()
_cache = {"validation": None, "robustness": None, "companies": None}


def _companies():
    """Sorted list of company names in the universe (for the search box)."""
    with _lock:
        if _cache["companies"] is None:
            client = MockDnBClient()
            names = []
            for duns in client.universe_duns():
                org = client.request("company_information", {"duns": duns})
                nm = org.get("data", {}).get("organization", {}).get("primaryName")
                if nm:
                    names.append(nm)
            _cache["companies"] = sorted(names)
    return _cache["companies"]


def _validation():
    """Canonical calibration backtest — computed once per server process."""
    with _lock:
        if _cache["validation"] is None:
            from validate import backtest_summary
            _cache["validation"] = backtest_summary()
    return _cache["validation"]


def _robustness():
    """Seed-robustness sweep (5 fresh universes) — computed once, then cached."""
    with _lock:
        if _cache["robustness"] is None:
            from validate import seed_robustness
            _cache["robustness"] = seed_robustness(n_seeds=5)
    return _cache["robustness"]


class Handler(BaseHTTPRequestHandler):

    # ---- helpers ---------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, fmt, *args):          # quieter console
        sys.stderr.write("[ui] %s\n" % (fmt % args))

    def do_HEAD(self):                          # health probes send HEAD
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    # ---- routes ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "ui/index.html not found", "text/plain")
            return

        if path == "/api/health":
            return self._json({"ok": True})

        if path == "/api/companies":
            return self._json({"companies": _companies()})

        if path == "/api/value":
            qs = parse_qs(parsed.query)
            name = unquote(qs.get("name", [""])[0]).strip()
            if not name:
                return self._json({"error": "missing ?name="}, 400)
            try:
                result, _ctx = run_pipeline(name)
                if result.get("valuation"):
                    result["validation"] = _validation()
                return self._json(result)
            except Exception as e:                    # never crash the demo
                return self._json({"error": str(e)}, 500)

        if path == "/api/robustness":
            try:
                return self._json(_robustness())
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        self._send(404, "not found", "text/plain")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"valuation UI on http://localhost:{PORT}  (Ctrl+C to stop)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
