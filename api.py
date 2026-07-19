"""
api.py — production API service for the D&B-grounded MSME valuation engine.

Layering (each layer only talks to the one below it):

    HTTP (this file, FastAPI)  ->  orchestrator (run.py)  ->  core/ (stdlib)
                                     |                          ^
                                     v                          |
                              clients (realdata/ | mock_api/) --+   [same D&B envelopes]

The deterministic core is untouched: this file is a delivery layer only. It adds
what an API consumer needs — versioned routes, OpenAPI docs (/docs), proper HTTP
status codes, structured errors, CORS, health/readiness, and an HTML report
endpoint — and keeps the legacy routes ui/index.html already calls.

Endpoints
  UI          GET /                            single-file report app
  v1 API      GET /api/v1/health               liveness + readiness (universe warm?)
              GET /api/v1/status               data source, universe size, versions
              GET /api/v1/companies/suggest    autocomplete  ?q=&limit=
              GET /api/v1/valuations           full valuation JSON  ?name=
              GET /api/v1/valuations/report    self-contained HTML report  ?name=
              GET /api/v1/validation           methodology backtest (mock universe)
              GET /api/v1/robustness           5-seed robustness sweep (cached)
              GET /api/v1/database/status      ETL report of realdata.db
  legacy      /api/health /api/status /api/companies /api/suggest /api/value
              /api/robustness                  (consumed by ui/index.html — kept 1:1)

Status mapping (v1): ok / no_valuation -> 200, no_match -> 404,
insufficient_data -> 422. The legacy /api/value always returns 200 with the
structured result, which is what the UI expects.

Config (env): PORT (8733) · DATA_SOURCE (real|mock; default auto — real when
realdata.db exists) · CORS_ORIGINS (comma-separated; default *).

Run:  python api.py            (or: uvicorn api:app --host 0.0.0.0 --port 8733)
"""

import json
import logging
import os
import sqlite3
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from run import (run_pipeline, run_pipeline_custom, run_pipeline_enriched,
                 make_client, get_universe, METHODOLOGY_VERSION, ENGINE_NAME)
from intake import IntakeSession, build_company, intake_lineage
from dashboard import render_dashboard

BASE = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(BASE, "ui", "index.html")
DB_PATH = os.path.join(BASE, "realdata.db")
PORT = int(os.environ.get("PORT", "8733"))
API_VERSION = "1"

DATA_SOURCE = os.environ.get("DATA_SOURCE") or (
    "real" if os.path.exists(DB_PATH) else "mock")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("valuation.api")

# ---------------------------------------------------------------------------
# Application state — one shared client + caches, guarded by an RLock.
# run_pipeline() rebinds the shared client's audit trail per run, so valuation
# calls are serialized; a single valuation is ~2s on the warm 14k universe.
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_state = {"client": None, "warm": False, "companies": None,
          "validation": None, "robustness": None}


def _client():
    with _lock:
        if _state["client"] is None:
            _state["client"] = make_client(DATA_SOURCE)
        return _state["client"]


def _warm():
    """Normalize + profile the whole universe once so the first valuation
    request doesn't pay the 14k-company build cost."""
    try:
        get_universe(_client())
        _state["warm"] = True
        log.info("universe warm (%s)", DATA_SOURCE)
    except Exception:                                  # pragma: no cover
        log.exception("universe warm-up failed")


def _companies():
    with _lock:
        if _state["companies"] is None:
            client = _client()
            if DATA_SOURCE == "real":
                _state["companies"] = {"count": len(client.universe_duns()),
                                       "companies": []}
            else:
                names = []
                for duns in client.universe_duns():
                    org = client.request("company_information", {"duns": duns})
                    nm = org.get("data", {}).get("organization", {}).get("primaryName")
                    if nm:
                        names.append(nm)
                _state["companies"] = {"count": len(names),
                                       "companies": sorted(names)}
        return _state["companies"]


def _validation():
    """Methodology backtest — always runs on the MOCK universe (it has the
    synthetic market caps needed for a calibration target). Cached."""
    with _lock:
        if _state["validation"] is None:
            from validate import backtest_summary
            _state["validation"] = backtest_summary()
        return _state["validation"]


def _robustness():
    with _lock:
        if _state["robustness"] is None:
            from validate import seed_robustness
            _state["robustness"] = seed_robustness(n_seeds=5)
        return _state["robustness"]


def _suggest(q, limit):
    client = _client()
    if hasattr(client, "search_names"):
        return client.search_names(q, limit=limit)
    ql = (q or "").strip().lower()
    comp = _companies().get("companies", [])
    return [n for n in comp if ql and ql in n.lower()][:limit]


def _value(name):
    """Run one valuation on the shared client (serialized — see note above)."""
    with _lock:
        result, _ctx = run_pipeline(name, client=_client())
    if result.get("valuation"):
        try:
            result["validation"] = _validation()
        except Exception:                              # pragma: no cover
            log.exception("backtest attach failed")
    status = (result.get("meta") or {}).get("status")
    if status in ("no_match", "insufficient_data"):
        # actionable fallback: the data source can't support this valuation,
        # but the user can supply the figures through the guided intake agent
        result["hint"] = {
            "action": "guided_intake",
            "detail": ("Not enough source data to value this company. Provide "
                       "the figures yourself through the guided intake — the "
                       "engine values user-described companies against the "
                       "same database peers, with user-provided lineage."),
            "start": "POST /api/v1/intake/start",
        }
    return result

_HTTP_BY_STATUS = {"ok": 200, "no_valuation": 200,
                   "no_match": 404, "insufficient_data": 422}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app):
    log.info("starting: data source=%s, engine=%s v%s",
             DATA_SOURCE, ENGINE_NAME, METHODOLOGY_VERSION)
    threading.Thread(target=_warm, daemon=True).start()
    yield


app = FastAPI(
    title="MSME Comparable Valuation API",
    version=f"{METHODOLOGY_VERSION}+api{API_VERSION}",
    description="Deterministic, touchless comparable-company valuation for "
                "Indian MSMEs. JSON results carry a full audit trail, data-quality "
                "grade, confidence breakdown and per-field source lineage.",
    lifespan=lifespan,
)

_cors = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")]
app.add_middleware(CORSMiddleware, allow_origins=_cors,
                   allow_methods=["GET"], allow_headers=["*"])


def _err(code, message):
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=code)


# ---- UI -------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def ui_root():
    if not os.path.exists(UI_PATH):
        return _err(404, "ui/index.html not found")
    return FileResponse(UI_PATH, media_type="text/html")


@app.get("/architecture", tags=["system"], response_class=HTMLResponse)
def architecture():
    """All-in-one architecture presentation: the five layers, the data pipeline
    from the Excel extracts, and the full 15-control filter chain as a
    data-flow chart."""
    path = os.path.join(BASE, "architecture.html")
    if not os.path.exists(path):
        return _err(404, "architecture.html not found")
    return FileResponse(path, media_type="text/html")


# ---- v1 API ----------------------------------------------------------------

@app.get("/api/v1/health", tags=["system"])
def v1_health():
    """Liveness + readiness. `ready` flips true once the universe cache is warm."""
    return {"ok": True, "ready": _state["warm"]}


@app.get("/api/v1/status", tags=["system"])
def v1_status():
    client = _client()
    return {
        "engine": ENGINE_NAME,
        "methodology_version": METHODOLOGY_VERSION,
        "api_version": API_VERSION,
        "data_source": DATA_SOURCE,
        "data_source_label": getattr(client, "DATA_SOURCE_LABEL", DATA_SOURCE),
        "universe_companies": len(client.universe_duns()),
        "universe_warm": _state["warm"],
        "source_caveats": list(getattr(client, "source_caveats", []) or []),
    }


@app.get("/api/v1/companies/suggest", tags=["companies"])
def v1_suggest(q: str = Query(..., min_length=1, description="name prefix/substring"),
               limit: int = Query(15, ge=1, le=50)):
    return {"query": q, "suggestions": _suggest(q, limit)}


@app.get("/api/v1/valuations", tags=["valuations"])
def v1_valuation(name: str = Query(..., min_length=1,
                                   description="company name to value")):
    """Full valuation result: target, peers, rejected sample, all methods,
    confidence breakdown, data quality, source lineage, audit trail."""
    result = _value(name)
    status = (result.get("meta") or {}).get("status", "ok")
    return JSONResponse(result, status_code=_HTTP_BY_STATUS.get(status, 200))


_PRINT_JS = ("<script>window.addEventListener('load',"
             "()=>setTimeout(()=>window.print(),400));</script>")


def _report_html(result, auto_print):
    html = render_dashboard(result)
    if auto_print:
        html = html.replace("</body>", _PRINT_JS + "</body>")
    return HTMLResponse(html)


@app.get("/api/v1/valuations/report", tags=["valuations"],
         response_class=HTMLResponse)
def v1_report(name: str = Query(..., min_length=1),
              print: bool = Query(False, alias="print",
                                  description="open the browser print-to-PDF "
                                              "dialog automatically")):
    """Self-contained HTML report (the full theory-annotated dashboard) for one
    company. `?print=1` auto-opens the print dialog — save as PDF from there."""
    return _report_html(_value(name), print)


# ---- guided intake (conversational agent) -----------------------------------

_intake_lock = threading.Lock()
_INTAKE = {}                 # session_id -> {"session": IntakeSession, "result": dict}
_INTAKE_MAX = 500            # bound memory: drop oldest sessions beyond this


class AnswerBody(BaseModel):
    value: str


class EnrichBody(BaseModel):
    name: str
    debt_cr: float | None = None
    cash_cr: float | None = None
    depreciation_cr: float | None = None
    revenue_prior_cr: float | None = None


def _intake_get(sid):
    with _intake_lock:
        rec = _INTAKE.get(sid)
    return rec


def _industry_choices():
    catalog = getattr(_client(), "industry_catalog", None)
    return sorted(catalog().keys()) if callable(catalog) else None


@app.get("/api/v1/industries", tags=["companies"])
def v1_industries():
    """The data source's own industry catalog (the categories the intake's
    industry SELECTION must resolve to) with each category's sector group."""
    from realdata.client import _group_of
    choices = _industry_choices() or []
    return {"count": len(choices),
            "industries": [{"category": c, "group": _group_of(c)}
                           for c in choices]}


@app.post("/api/v1/intake/start", tags=["intake"], status_code=201)
def intake_start():
    """Open a guided-intake conversation. The agent walks a deterministic
    LangGraph question graph (each question ships its own 'why we ask' theory);
    the industry question is a SELECTION over the live catalog, so the
    sub-sector classification is exact. Answers build a target company that is
    then valued against the database universe."""
    s = IntakeSession(industry_choices=_industry_choices())
    with _intake_lock:
        if len(_INTAKE) >= _INTAKE_MAX:
            _INTAKE.pop(next(iter(_INTAKE)))
        _INTAKE[s.session_id] = {"session": s, "result": None}
    return {"session_id": s.session_id, "question": s.current(),
            "progress": s.progress()}


@app.post("/api/v1/intake/{sid}/answer", tags=["intake"])
def intake_answer(sid: str, body: AnswerBody):
    """Answer the current question (send 'skip' for optional ones). Returns a
    validation error + the same question, or the next question, or done=true."""
    rec = _intake_get(sid)
    if rec is None:
        return _err(404, "unknown intake session")
    return rec["session"].submit(body.value)


@app.get("/api/v1/intake/{sid}", tags=["intake"])
def intake_state(sid: str):
    rec = _intake_get(sid)
    if rec is None:
        return _err(404, "unknown intake session")
    return rec["session"].to_dict()


@app.post("/api/v1/intake/{sid}/value", tags=["intake"])
def intake_value(sid: str):
    """Build the company from the completed intake and value it against the
    database peers — full result with audit trail, confidence, adjustments."""
    rec = _intake_get(sid)
    if rec is None:
        return _err(404, "unknown intake session")
    s = rec["session"]
    if not s.done:
        return _err(409, f"intake incomplete — {s.progress()['answered']}/"
                         f"{s.progress()['total']} answered")
    company = build_company(s.answers, _client())
    with _lock:
        result, _ctx = run_pipeline_custom(company, client=_client(),
                                           lineage=intake_lineage(s.answers))
    if result.get("valuation"):
        try:
            result["validation"] = _validation()
        except Exception:                          # pragma: no cover
            log.exception("backtest attach failed")
    rec["result"] = result
    status = (result.get("meta") or {}).get("status", "ok")
    return JSONResponse(result, status_code=_HTTP_BY_STATUS.get(status, 200))


@app.get("/api/v1/intake/{sid}/report", tags=["intake"],
         response_class=HTMLResponse)
def intake_report(sid: str,
                  print: bool = Query(False, alias="print")):
    """HTML report for a completed intake valuation (`?print=1` → PDF dialog)."""
    rec = _intake_get(sid)
    if rec is None:
        return _err(404, "unknown intake session")
    if rec["result"] is None:
        return _err(409, "no valuation run yet — POST /value first")
    return _report_html(rec["result"], print)


@app.post("/api/v1/valuations/enrich", tags=["valuations"])
def v1_enrich(body: EnrichBody):
    """Re-value a DATABASE company with user-supplied figures for the fields
    the source lacks (debt / cash / depreciation / prior revenue). Dependent
    quantities (net debt, EBIT, growth) are re-derived; supplied fields carry
    'user-provided enrichment' lineage and an audited TARGET_ENRICHED decision."""
    overrides = {k: getattr(body, k) for k in
                 ("debt_cr", "cash_cr", "depreciation_cr", "revenue_prior_cr")}
    if all(v is None for v in overrides.values()):
        return _err(422, "no override figures supplied")
    with _lock:
        result, _ctx = run_pipeline_enriched(body.name.strip(), overrides,
                                             client=_client())
    if result.get("valuation"):
        try:
            result["validation"] = _validation()
        except Exception:                          # pragma: no cover
            log.exception("backtest attach failed")
    status = (result.get("meta") or {}).get("status", "ok")
    return JSONResponse(result, status_code=_HTTP_BY_STATUS.get(status, 200))


# ---- filter-chain documentation ---------------------------------------------

_FILTER_DOC = {
    "stages": [
        {"stage": "A — Universe & eligibility", "controls": [
            {"n": 1, "name": "Geography", "rule": "country = IN at search"},
            {"n": 2, "name": "Self-exclusion", "rule": "target's own record removed"}]},
        {"stage": "B — Hard knock-outs (rejected BEFORE scoring, each with a reason)",
         "controls": [
            {"n": 3, "name": "Operating model", "rule": "manufacturer/distributor/"
             "retailer/service must equal the target's"},
            {"n": 4, "name": "Value chain", "rule": "finished_goods vs raw_material must match"},
            {"n": 5, "name": "Major industry", "rule": "D&B major letter must match"}]},
        {"stage": "C — Weighted similarity (0–1)", "controls": [
            {"n": 6, "name": "Industry proximity", "rule": "same subsector 1.0 / same "
             "sector group 0.6 / else 0", "weight": 0.40},
            {"n": 7, "name": "Scale proximity", "rule": "1/(1+|Δ log revenue|)", "weight": 0.20},
            {"n": 8, "name": "Margin proximity", "rule": "max(0, 1−5·|Δ margin|)", "weight": 0.15},
            {"n": 9, "name": "Customer type", "rule": "B2B/B2C/mixed equality", "weight": 0.15},
            {"n": 10, "name": "Export profile", "rule": "same flag 1.0 else 0.3", "weight": 0.10}]},
        {"stage": "D — Post-scoring quality controls", "controls": [
            {"n": 11, "name": "Top-N cut", "rule": "top 15 by score enter valuation"},
            {"n": 12, "name": "Similarity weighting", "rule": "score ≥0.85 weight 1.0, "
             "tapering to a 0.15 floor — borderline comps cannot distort the answer"},
            {"n": 13, "name": "Multiple eligibility", "rule": "market EV primary; book "
             "fallback if <3 listed comps; driver must be > 0"},
            {"n": 14, "name": "Outlier trim", "rule": "Tukey 1.5×IQR fence per method"},
            {"n": 15, "name": "Scale-mismatch penalty", "rule": "target outside the peer "
             "revenue band → multiples adjusted down/up (−7.5%/decade below, capped 15%; "
             "+5%/decade above, capped 10%), disclosed as COMPARABILITY_ADJUSTMENT"}]},
    ],
    "guarantees": [
        "No peer of a different operating model, value chain or major industry can "
        "reach the valuation — stage B rejects it with a recorded reason.",
        "No single outlier multiple can move the answer — the Tukey fence drops it.",
        "Borderline comps cannot dominate — their weight tapers to a 0.15 floor and "
        "the range widens on the EFFECTIVE (weighted) peer count.",
        "A size-mismatched peer set cannot silently inflate a small company's value — "
        "the scale penalty adjusts the multiples and says so.",
        "If nothing survives, the answer is 'none' with an audit trail — never a "
        "fabricated number.",
    ],
    "limitations": [
        "Filters compare what the data contains: if the source misdescribes a "
        "company's activities, the classifier inherits that error (visible in the "
        "audit trail, not silently corrected).",
        "Industry proximity uses classification codes, not product-level overlap — two "
        "'engineering' companies may still serve different end markets.",
        "The real extract has no market prices, borrowings or cash: multiples are "
        "book-basis with disclosed caveats until market data is added.",
        "Precedent transactions are derived (control premium over comps), not "
        "observed — a deal database would replace them.",
    ],
}


@app.get("/api/v1/filters", tags=["methodology"])
def v1_filters():
    """The complete peer-selection filter chain, with explicit guarantees (what
    can never happen) and limitations (what the filters cannot promise)."""
    return _FILTER_DOC


@app.get("/api/v1/validation", tags=["methodology"])
def v1_validation():
    """Anti-overfitting backtest: every listed mock company valued from its
    peers vs its own market cap; positioning must beat the naive median."""
    return _validation()


@app.get("/api/v1/robustness", tags=["methodology"])
def v1_robustness():
    """Backtest re-run on 5 freshly seeded universes (slow first call, cached)."""
    return _robustness()


@app.get("/api/v1/database/status", tags=["system"])
def v1_db_status():
    """ETL provenance of realdata.db: row counts, join coverage, P&L
    reconciliation rate, source-file hashes, build timestamp and age."""
    if DATA_SOURCE != "real":
        return {"data_source": "mock",
                "detail": "synthetic in-code universe; no database"}
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT value FROM meta WHERE key='etl_report'").fetchone()
    con.close()
    report = json.loads(row[0]) if row else {}
    age_h = None
    if report.get("finished"):
        age_h = round((datetime.now(timezone.utc)
                       - datetime.fromisoformat(report["finished"])
                       ).total_seconds() / 3600, 1)
    return {"data_source": "real", "db_path": DB_PATH,
            "db_size_mb": round(os.path.getsize(DB_PATH) / 1e6, 1),
            "age_hours": age_h, "etl_report": report}


# ---- legacy routes (exact shapes ui/index.html consumes) --------------------

@app.get("/api/health", include_in_schema=False)
def health():
    return {"ok": True}


@app.get("/api/status", include_in_schema=False)
def status():
    client = _client()
    return {"data_source": DATA_SOURCE,
            "data_source_label": getattr(client, "DATA_SOURCE_LABEL", DATA_SOURCE),
            "universe": len(client.universe_duns()),
            "warm": _state["warm"]}


@app.get("/api/companies", include_in_schema=False)
def companies():
    return _companies()


@app.get("/api/suggest", include_in_schema=False)
def suggest(q: str = ""):
    return {"suggestions": _suggest(q, 15)}


@app.get("/api/value", include_in_schema=False)
def value(name: str = ""):
    if not name.strip():
        return JSONResponse({"error": "missing ?name="}, status_code=400)
    try:
        return _value(name.strip())
    except Exception as e:                             # never crash the UI
        log.exception("valuation failed for %r", name)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/robustness", include_in_schema=False)
def robustness():
    return _robustness()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=PORT, log_level="info")
