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
from run import run_pipeline, make_client, get_universe

BASE = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(BASE, "ui", "index.html")
PORT = int(os.environ.get("PORT", "8733"))

# ---- data source: real (realdata.db) when available, else mock -------------
DATA_SOURCE = os.environ.get("DATA_SOURCE")
if "--data" in sys.argv:
    DATA_SOURCE = sys.argv[sys.argv.index("--data") + 1]
if DATA_SOURCE is None:
    DATA_SOURCE = "real" if os.path.exists(os.path.join(BASE, "realdata.db")) else "mock"

# RLock: _companies() calls _client() while holding the lock — a plain Lock
# would self-deadlock (observed: whole server froze on the first page load).
_lock = threading.RLock()
_cache = {"validation": None, "robustness": None, "companies": None,
          "client": None, "warm": False}


def _client():
    with _lock:
        if _cache["client"] is None:
            _cache["client"] = make_client(DATA_SOURCE)
    return _cache["client"]


def _warm():
    """Build the (cached) universe once in the background so the first user
    valuation doesn't pay the 14k-company normalization cost."""
    try:
        get_universe(_client())
        _cache["warm"] = True
        print(f"[warm] universe ready ({DATA_SOURCE})", flush=True)
    except Exception as e:                       # pragma: no cover
        print(f"[warm] failed: {e}", flush=True)


def _companies():
    """Company-name list for the search box (mock: full 59; real: count only —
    the UI uses /api/suggest for the 14k-name universe)."""
    with _lock:
        if _cache["companies"] is None:
            client = _client()
            if DATA_SOURCE == "real":
                _cache["companies"] = {"count": len(client.universe_duns()),
                                       "companies": []}
            else:
                names = []
                for duns in client.universe_duns():
                    org = client.request("company_information", {"duns": duns})
                    nm = org.get("data", {}).get("organization", {}).get("primaryName")
                    if nm:
                        names.append(nm)
                _cache["companies"] = {"count": len(names),
                                       "companies": sorted(names)}
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

        if path == "/api/status":
            return self._json({"data_source": DATA_SOURCE,
                               "data_source_label": getattr(
                                   _client(), "DATA_SOURCE_LABEL", DATA_SOURCE),
                               "universe": len(_client().universe_duns()),
                               "warm": _cache["warm"]})

        if path == "/api/companies":
            return self._json(_companies())

        if path == "/api/suggest":
            qs = parse_qs(parsed.query)
            q = unquote(qs.get("q", [""])[0])
            client = _client()
            if hasattr(client, "search_names"):
                return self._json({"suggestions": client.search_names(q, limit=15)})
            comp = _companies().get("companies", [])
            ql = q.strip().lower()
            return self._json({"suggestions":
                               [n for n in comp if ql in n.lower()][:15]})

        if path == "/api/value":
            qs = parse_qs(parsed.query)
            name = unquote(qs.get("name", [""])[0]).strip()
            if not name:
                return self._json({"error": "missing ?name="}, 400)
            try:
                result, _ctx = run_pipeline(name, client=_client())
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
    print(f"data source: {DATA_SOURCE}")
    threading.Thread(target=_warm, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
