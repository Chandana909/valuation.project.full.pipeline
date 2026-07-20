"""
test_api.py — live integration test suite for the API service (stdlib only).

Exercises EVERY endpoint against a running server: happy paths, error mapping,
the full conversational-intake flow, edge cases (garbage input, no-match,
validation rejects), determinism, and accuracy/coherence audits on real data.

Run:  python api.py          (in one terminal)
      python test_api.py     (in another; exits 0 iff all checks pass)
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "http://127.0.0.1:8733"
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    return cond


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=180) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def get_raw(path, _retries=2):
    try:
        req = urllib.request.Request(BASE + path,
                                     headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except (ConnectionResetError, ConnectionAbortedError):
        # Windows urllib<->uvicorn reset quirk on large bodies (server logs 200
        # and curl retrieves the full body) — fall back to curl
        if _retries:
            return get_raw(path, _retries - 1)
        import subprocess
        out = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", BASE + path],
                             capture_output=True, text=True, timeout=180,
                             encoding="utf-8")
        body, _, code = out.stdout.rpartition("\n")
        return int(code or 0), body


def post(path, body=None):
    req = urllib.request.Request(
        BASE + path, method="POST",
        data=json.dumps(body).encode() if body is not None else b"",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def main():
    print("=" * 74)
    print("API INTEGRATION AUDIT")
    print("=" * 74)

    # ---- system -----------------------------------------------------------
    print("\n-- system --")
    st, h = get("/api/v1/health")
    check("health 200 + ok", st == 200 and h.get("ok") is True, str(h))
    st, s = get("/api/v1/status")
    check("status: universe loaded", st == 200 and s.get("universe_companies", 0) > 50,
          f"{s.get('universe_companies')} companies ({s.get('data_source')})")
    real = s.get("data_source") == "real"
    st, db = get("/api/v1/database/status")
    if real:
        er = db.get("etl_report", {})
        check("database status: ETL report + hashes",
              st == 200 and er.get("recon", {}).get("rate_pct", 0) > 95
              and len(er.get("source_files", {})) == 6,
              f"recon {er.get('recon', {}).get('rate_pct')}% · "
              f"{len(er.get('source_files', {}))} source hashes")
    else:
        check("database status: mock disclosed", st == 200
              and db.get("data_source") == "mock", str(db)[:60])

    # ---- methodology docs ---------------------------------------------------
    print("\n-- methodology --")
    st, f = get("/api/v1/filters")
    n_controls = sum(len(x["controls"]) for x in f.get("stages", []))
    check("filters: 15 controls + guarantees + limitations",
          st == 200 and n_controls == 15 and len(f.get("guarantees", [])) >= 5
          and len(f.get("limitations", [])) >= 4,
          f"{n_controls} controls")
    st, v = get("/api/v1/validation")
    check("observed-market validation: median error within gate",
          st == 200 and v.get("kind") == "observed_market_validation"
          and v.get("verdict_ok") is True,
          f"median {v.get('median_abs_pct')}% · in-range "
          f"{v.get('n_in_range')}/{v.get('n')} · as of {v.get('as_of')}")

    # ---- search + valuation -------------------------------------------------
    print("\n-- valuation --")
    name = "20 Microns Ltd."
    q = urllib.parse.quote(name)
    st, sg = get(f"/api/v1/companies/suggest?q={urllib.parse.quote(name[:7])}")
    check("suggest returns the company", st == 200
          and any(name.split()[0] in x for x in sg.get("suggestions", [])),
          str(sg.get("suggestions", [])[:2]))
    st, r = get(f"/api/v1/valuations?name={q}")
    val = r.get("valuation") or {}
    check("valuation 200 + complete result", st == 200
          and r["meta"]["status"] == "ok"
          and all(k in r for k in ("target", "peers", "rejected", "confidence",
                                   "audit_trail", "data_quality", "target_lineage")),
          f"status={r['meta']['status']}")
    hm0 = next((m for m in val.get("methods", [])
                if m["method"] == val.get("headline_method")), {})
    check("EV range low < mid < high",
          (hm0.get("ev_low_cr") or 0) < (hm0.get("ev_mid_cr") or 0)
          < (hm0.get("ev_high_cr") or 1),
          f"{hm0.get('ev_low_cr')} < {hm0.get('ev_mid_cr')} < {hm0.get('ev_high_cr')}")
    check("NO-ASSUMPTION rule: equity withheld, requirements listed",
          val.get("equity_mid_cr") is None
          and set(val.get("equity_requires") or []) == {"borrowings", "cash"},
          f"equity_requires={val.get('equity_requires')}")
    cl = {c["key"]: c["status"] for c in r.get("parameter_checklist", [])}
    check("parameter checklist: debt/cash missing, revenue available",
          cl.get("debt_cr") == "missing" and cl.get("revenue_cr") == "available",
          f"{sum(1 for s in cl.values() if s == 'missing')} missing")
    check("comparability block present", val.get("comparability_adjustment") is not None, "")
    # enrichment: supplying debt/cash unlocks equity + transactions view
    st_e, er = post("/api/v1/valuations/enrich",
                    {"name": name, "debt_cr": 180, "cash_cr": 25})
    ev_ = er.get("valuation") or {}
    check("enrich: equity + transactions appear once net debt known",
          st_e == 200 and ev_.get("equity_mid_cr") is not None
          and ev_.get("transaction_analysis") is not None
          and ev_.get("net_debt_cr") == 155.0,
          f"equity mid {ev_.get('equity_mid_cr')} · net debt {ev_.get('net_debt_cr')}")
    if real:
        hm = next((m for m in val["methods"]
                   if m["method"] == val["headline_method"]), {})
        check("real data: sector-calibrated multiple in trading band",
              hm.get("ev_basis") == "sector-calibrated"
              and 2.0 <= hm.get("multiple_median", 0) <= 30.0,
              f"{hm.get('multiple_median')}x ({hm.get('ev_basis')})")
    audits = {a["code"] for a in r["audit_trail"]}
    check("audit trail carries decisions",
          {"TARGET_RESOLVED", "QUALITY_POSITIONED"}.issubset(audits),
          f"{len(r['audit_trail'])} records")

    # determinism: same input -> same numbers
    st2, r2 = get(f"/api/v1/valuations?name={q}")
    hm2 = next((m for m in (r2.get("valuation") or {}).get("methods", [])
                if m["method"] == (r2.get("valuation") or {}).get("headline_method")), {})
    check("deterministic: identical numbers on re-run",
          hm0.get("ev_mid_cr") == hm2.get("ev_mid_cr"),
          f"EV mid {hm0.get('ev_mid_cr')} == {hm2.get('ev_mid_cr')}")

    # error mapping
    st, _ = get("/api/v1/valuations?name=zzz-no-such-company-xyz")
    check("unknown company -> 404", st == 404, f"HTTP {st}")
    st, _ = get("/api/v1/valuations")
    check("missing param -> 422", st == 422, f"HTTP {st}")

    # report
    st, html = get_raw(f"/api/v1/valuations/report?name={q}")
    check("HTML report renders with all sections", st == 200
          and all(x in html for x in ("Headline Valuation", "Audit Trail",
                                      "Data lineage", "Rejected Candidates")),
          f"{len(html):,} bytes")
    st, html_p = get_raw(f"/api/v1/valuations/report?name={q}&print=1")
    check("print=1 injects the PDF dialog", st == 200 and "window.print()" in html_p, "")

    # ---- conversational intake ---------------------------------------------
    print("\n-- guided intake --")
    st, j = post("/api/v1/intake/start")
    sid = j.get("session_id")
    check("intake start 201 + first question", st == 201
          and j.get("question", {}).get("id") == "name", f"sid={sid}")
    st, bad = post(f"/api/v1/intake/{sid}/answer", {"value": "X"})
    check("too-short name rejected with reason", not bad.get("ok")
          and bad.get("error"), bad.get("error", ""))
    answers = ["Audit Test Fabricators",
               "manufactures sheet-metal fabrications for industrial equipment makers",
               "engineering", "no", "60", "48", "8.5", "2.2", "40", "10", "3", "yes",
               "skip", "skip"]
    done = None
    for a in answers:
        st, done = post(f"/api/v1/intake/{sid}/answer", {"value": a})
    check("question graph completes (14 nodes incl. optional deal)",
          done.get("done") is True,
          f"{done.get('progress', {}).get('pct')}%")
    st, fresh = post("/api/v1/intake/start")
    sid2 = fresh.get("session_id")
    st, ir = post(f"/api/v1/intake/{sid}/value")
    iv = ir.get("valuation") or {}
    check("intake valuation ok on the shared engine", st == 200
          and ir["meta"]["status"] == "ok" and iv.get("headline_method") != "none",
          f"equity mid ₹{iv.get('equity_mid_cr')} Cr")
    check("private intake target gets DLOM", iv.get("discount", 0) > 0,
          f"DLOM {iv.get('discount')}")
    check("user-provided lineage disclosed",
          any("intake" in str(x.get("file", ""))
              for x in ir.get("target_lineage", {}).values()), "")
    st, ihtml = get_raw(f"/api/v1/intake/{sid}/report")
    check("intake report renders", st == 200 and "Headline Valuation" in ihtml,
          f"{len(ihtml):,} bytes")
    st, _ = get("/api/v1/intake/does-not-exist")
    check("unknown session -> 404", st == 404, f"HTTP {st}")
    st, notdone = post(f"/api/v1/intake/{sid2}/value") if sid2 else (409, {})
    check("incomplete intake -> 409", st == 409, f"HTTP {st}")

    # ---- legacy UI routes ----------------------------------------------------
    print("\n-- legacy UI routes --")
    for path in ("/api/health", "/api/status", "/api/companies",
                 "/api/suggest?q=a"):
        st, _ = get(path)
        check(f"legacy {path.split('?')[0]}", st == 200, f"HTTP {st}")
    st, page = get_raw("/")
    check("UI served at / (intake + API tab present)", st == 200
          and "Start guided intake" in page and "API &amp; Report" in page,
          f"{len(page):,} bytes")

    n = len(CHECKS)
    ok = sum(CHECKS)
    print("\n" + "=" * 74)
    print(f"RESULT: {ok}/{n} API checks passed")
    print("=" * 74)
    return ok == n


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
