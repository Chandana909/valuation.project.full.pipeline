"""
run.py — orchestrator for the D&B-grounded MSME valuation pipeline.

Flow:
  company_search -> best DUNS -> company_information + company_financials
  -> normalize + profile -> build universe (loop universe_duns, fetch+normalize)
  -> discover_peers -> compute_valuation(top_n=15) -> confidence
  -> write output/result.json -> print readable summary.

The D&B client is INJECTED here (core/ never imports mock_api/). Swapping the
mock for the live client is a one-line change at the `client = ...` seam.

Usage:  python run.py "Woodward"
"""

import sys
import os
import json

# On Windows the default console codec (cp1252) cannot encode ₹/– etc.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# make package importable when run as a script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone

from core import (
    AuditTrail,
    normalize_company, build_profile, validate_company,
    discover_peers, compute_valuation,
    company_to_dict, profile_to_dict, valuation_to_dict, dataquality_to_dict,
)
from dashboard import build_dashboard

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
RESULT_PATH = os.path.join(OUTPUT_DIR, "result.json")
DASHBOARD_PATH = os.path.join(OUTPUT_DIR, "dashboard.html")

# Provenance — versioned so every archived valuation is reproducible/traceable.
# 1.2.0: peer multiples use market enterprise value from LISTED comps
#        (market cap + net debt); book capital-employed is a per-method fallback only.
# 1.3.0: quality positioning — the central multiple is taken at the target's
#        EBITDA-margin percentile within the peer set (not the flat median); DLOM
#        applies only to PRIVATE targets; listed targets are cross-checked against
#        their own market cap.
# 1.4.0: anti-overfitting pass — synthetic multiples are now fundamentals-driven
#        (corr(margin,EV/EBITDA)~0.49), so positioning generalizes (backtested: mean
#        |Δ| 8.1% vs own market cap across 32 listed comps, beats flat-median 10.5%
#        on 24/32). Confidence is now discriminating (blends triangulation agreement
#        + comp dispersion), no longer a flat 0.98. See validate.py.
# 1.5.0: similarity-weighted peer multiples — borderline comps (just outside the
#        ideal range) are down-weighted via a weighted percentile, so a thin/weak
#        peer set can't distort the headline; range widens on the EFFECTIVE (weighted)
#        peer count and confidence uses it too.
# 1.6.0: valuation math UNCHANGED. Added (a) seed-robustness sweep — the calibration
#        backtest re-run on 5 freshly drawn universes (validate.py seed_robustness;
#        positioning beat the naive median on 5/5, mean MAE 8.5% vs 10.7% — not
#        seed-luck), and (b) a live UI (server.py + ui/, stdlib http.server): browser
#        input, tabbed industry-style report, filter-chain documentation, football-
#        field chart, print-to-PDF.
# 2.0.0: REAL-DATA support. Architecture: 9 uploaded Excel extracts -> etl.py ->
#        realdata.db (SQLite, stdlib) with per-row provenance + a P&L reconciliation
#        gate (99.1% of rows reconcile) -> RealDnBClient emits the same D&B envelopes,
#        so the calculation core is UNCHANGED. 13,906 valuation-grade real companies.
#        Honesty on gaps: no market prices / borrowings / cash in the extract ->
#        book-basis valuation with explicit caveats, net-debt-unknown warning, and
#        debt/cash data-quality checks. Per-field lineage (file+row) in result + UI.
#        Universe cached per data source; rejection logging capped (first 20 + summary).
METHODOLOGY_VERSION = "2.0.0"
DNB_SCHEMA_VERSION = "dnbhoovers-2024"
ENGINE_NAME = "dnb-msme-comparable-valuation"


# ---------------------------------------------------------------------------
# D&B fetch helpers
# ---------------------------------------------------------------------------

def _best_match(client, name, audit):
    resp = client.request("company_search",
                          {"name": name, "countryISOAlpha2Code": "IN"})
    cands = resp.get("data", {}).get("matchCandidates", []) or []
    if not cands:
        audit.error("resolve", "NO_MATCH",
                    f"D&B company_search returned no candidates for '{name}'",
                    {"query": name})
        return None
    best = max(cands, key=lambda c: c.get("matchQualityInformation", {})
               .get("confidenceCode", 0))
    org = best.get("organization", {})
    conf = best.get("matchQualityInformation", {}).get("confidenceCode")
    if len(cands) > 1:
        audit.info("resolve", "MATCH_CANDIDATES",
                   f"{len(cands)} candidates; selected highest confidence",
                   {"candidates": len(cands), "selected_confidence": conf})
    audit.decision("resolve", "TARGET_RESOLVED",
                   f"'{name}' -> {org.get('primaryName')} "
                   f"(DUNS {org.get('duns')}, confidence {conf})",
                   {"query": name, "duns": org.get("duns"), "confidence": conf})
    return org.get("duns")


def _fetch_company(client, duns, audit, with_mgmt=False):
    info = client.request("company_information", {"duns": duns})
    fin = client.request("company_financials", {"duns": duns})
    mgmt = client.request("company_management", {"duns": duns}) if with_mgmt else None
    return normalize_company(info, fin, mgmt)


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def compute_confidence(profile, n_peers, target_ebitda_pos, valuation):
    """
    Discriminating confidence: blends input quality (profile, peer coverage, EBITDA,
    method count) with OUTPUT COHERENCE (how tightly the three methods triangulate and
    how dispersed the comparable multiples are). A result where the methods disagree or
    the comps are scattered is genuinely less trustworthy — so this does NOT saturate at
    ~1.0 for every target. Returns (score, label, breakdown).
    """
    methods = valuation.methods
    n_methods = len(methods)

    # -- triangulation agreement: how close the three method mid-equities are --
    mids = [m["equity_mid_cr"] for m in methods
            if m.get("equity_mid_cr") and m["equity_mid_cr"] > 0]
    if len(mids) >= 2 and min(mids) > 0:
        spread = max(mids) / min(mids) - 1.0          # 0 = perfect agreement
        triangulation = 1.0 - _clamp(spread / 0.80, 0.0, 1.0)  # 80%+ spread => 0
    else:
        triangulation = 0.0

    # -- comparable tightness: dispersion (CV) of the headline peer multiples --
    hm = valuation.headline_method
    mults = [p["multiples"].get(hm) for p in valuation.peers_used
             if p.get("ev_basis") == "market" and p["multiples"].get(hm)]
    if len(mults) >= 3:
        mean = sum(mults) / len(mults)
        sd = (sum((x - mean) ** 2 for x in mults) / len(mults)) ** 0.5
        cv = sd / mean if mean else 1.0
        comp_tightness = 1.0 - _clamp(cv / 0.45, 0.0, 1.0)  # CV 45%+ => 0
    else:
        comp_tightness = 0.30

    breakdown = {
        "profile": round(0.20 * profile.confidence, 3),
        "peer_coverage": round(0.20 * (min(n_peers, 15) / 15.0), 3),
        "ebitda_positive": round(0.10 * (1.0 if target_ebitda_pos else 0.0), 3),
        "methods": round(0.10 * (min(n_methods, 3) / 3.0), 3),
        "triangulation": round(0.25 * triangulation, 3),
        "comp_tightness": round(0.15 * comp_tightness, 3),
    }
    score = round(sum(breakdown.values()), 3)
    if score >= 0.75:
        label = "HIGH"
    elif score >= 0.50:
        label = "MEDIUM"
    else:
        label = "LOW"
    return score, label, breakdown


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

def _metadata(name, status, client=None):
    label = getattr(client, "DATA_SOURCE_LABEL", None) or \
        "Dun & Bradstreet (dnbhoovers) — synthetic mock universe"
    return {
        "engine": ENGINE_NAME,
        "methodology_version": METHODOLOGY_VERSION,
        "dnb_schema_version": DNB_SCHEMA_VERSION,
        "run_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_source": label,
        "currency": "INR",
        "reporting_units": "Crore",
        "source_units": "Thousand",
        "human_in_the_loop": False,
        "query": name,
        "status": status,
    }


def make_client(data_source="real", audit=None):
    """Factory for the injected data client. Only 'real' serves realdata.db (run etl.py
    first). Mock API has been disconnected."""
    from realdata import RealDnBClient
    return RealDnBClient(audit=audit)


# Universe cache: building (normalize + profile) 14k real companies takes seconds —
# do it once per process per data source, not on every valuation request.
_UNIVERSE_CACHE = {}


def get_universe(client, audit=None):
    """(Company, EconomicProfile) list for every universe DUNS, cached per client
    cache_key. Per-company D&B calls are audit-MUTED during a bulk build (a 14k-row
    build would drown the trail); the summary line is always logged."""
    key = getattr(client, "cache_key", "mock-default")
    if key in _UNIVERSE_CACHE:
        if audit is not None:
            audit.info("universe", "UNIVERSE_CACHE_HIT",
                       f"universe cache hit ({len(_UNIVERSE_CACHE[key])} companies)",
                       {"count": len(_UNIVERSE_CACHE[key]), "cache_key": key})
        return _UNIVERSE_CACHE[key]
    prev_audit = getattr(client, "_audit", None)
    if hasattr(client, "set_audit"):
        client.set_audit(None)
    try:
        universe = []
        for d in client.universe_duns():
            c = _fetch_company(client, d, audit=None, with_mgmt=False)
            universe.append((c, build_profile(c)))
    finally:
        if hasattr(client, "set_audit"):
            client.set_audit(prev_audit)
    _UNIVERSE_CACHE[key] = universe
    if audit is not None:
        audit.info("universe", "UNIVERSE_LOADED",
                   f"loaded {len(universe)} candidate companies (audit-muted bulk "
                   f"build; per-company provenance available via lineage)",
                   {"count": len(universe), "cache_key": key})
    return universe


def run_pipeline(name, client=None, top_n=15, data_source="real"):
    """
    Returns (result_dict, ctx). `ctx` carries the live objects for callers/tests.
    Never raises on bad input — degraded runs return a structured result with a
    non-'ok' `status` and a complete audit trail explaining why.
    """
    audit = AuditTrail()
    if client is None:
        client = make_client(data_source, audit=audit)
    elif hasattr(client, "set_audit"):
        # bind an externally-provided (e.g. server-shared) client to THIS run's trail
        client.set_audit(audit)
    audit.info("run", "START", f"pipeline start for query '{name}'",
               {"methodology_version": METHODOLOGY_VERSION,
                "data_source": getattr(client, "DATA_SOURCE_LABEL", "mock")})
    ctx = {"target": None, "tprofile": None, "ranked": [], "rejected": [],
           "valuation": None, "data_quality": None}

    # 1. resolve --------------------------------------------------------
    duns = _best_match(client, name, audit)
    if duns is None:
        result = {
            "meta": _metadata(name, "no_match", client),
            "query": name, "target": None, "target_profile": None,
            "data_quality": None, "peers": [], "peers_ranked_count": 0,
            "rejected": [], "valuation": None,
            "confidence": {"score": 0.0, "label": "LOW"},
            "audit_trail": audit.to_list(),
        }
        return result, ctx

    # 2. target ---------------------------------------------------------
    target = _fetch_company(client, duns, audit, with_mgmt=True)
    return _evaluate(name, target, client, audit, ctx, top_n)


def run_pipeline_custom(target, client=None, top_n=15, data_source="real",
                        lineage=None, txn_multiple=None):
    """Value a USER-PROVIDED company (e.g. from the conversational intake)
    against the database universe. Same engine, same audit, same result shape —
    only the resolve step differs (there is nothing to resolve).
    `target` is a core.Company; `lineage` an optional per-field provenance dict.
    """
    audit = AuditTrail()
    if client is None:
        client = make_client(data_source, audit=audit)
    elif hasattr(client, "set_audit"):
        client.set_audit(audit)
    audit.info("run", "START", f"pipeline start for custom target '{target.name}'",
               {"methodology_version": METHODOLOGY_VERSION, "mode": "custom_intake"})
    audit.decision("resolve", "TARGET_CUSTOM",
                   f"target '{target.name}' supplied via guided intake — no D&B "
                   f"resolution; figures are user-provided",
                   {"duns": target.duns})
    ctx = {"target": None, "tprofile": None, "ranked": [], "rejected": [],
           "valuation": None, "data_quality": None}
    return _evaluate(target.name, target, client, audit, ctx, top_n,
                     custom_lineage=lineage, txn_multiple=txn_multiple)


_ENRICHABLE = ("debt_cr", "cash_cr", "depreciation_cr", "revenue_prior_cr")


def run_pipeline_enriched(name, overrides, client=None, top_n=15,
                          data_source="real"):
    """Value a DATABASE company with user-supplied figures for the fields the
    source lacks (debt, cash, depreciation, prior-year revenue). The engine
    re-derives every dependent quantity (EBIT, growth, net debt) and the
    supplied fields carry 'user-provided enrichment' lineage — the peer set
    and multiples stay fully database-driven.
    """
    audit = AuditTrail()
    if client is None:
        client = make_client(data_source, audit=audit)
    elif hasattr(client, "set_audit"):
        client.set_audit(audit)
    audit.info("run", "START", f"pipeline start (enriched) for '{name}'",
               {"methodology_version": METHODOLOGY_VERSION,
                "overrides": {k: v for k, v in overrides.items() if v is not None}})
    ctx = {"target": None, "tprofile": None, "ranked": [], "rejected": [],
           "valuation": None, "data_quality": None}
    duns = _best_match(client, name, audit)
    if duns is None:
        return {"meta": _metadata(name, "no_match", client), "query": name,
                "target": None, "target_profile": None, "data_quality": None,
                "peers": [], "peers_ranked_count": 0, "rejected": [],
                "valuation": None,
                "confidence": {"score": 0.0, "label": "LOW"},
                "audit_trail": audit.to_list()}, ctx

    target = _fetch_company(client, duns, audit, with_mgmt=True)
    applied = []
    for f in _ENRICHABLE:
        v = overrides.get(f)
        if v is None:
            continue
        setattr(target, f, float(v))
        applied.append(f)
    if "debt_cr" in applied:
        target.debt_known = True
    if "cash_cr" in applied:
        target.cash_known = True
    if "depreciation_cr" in applied and target.ebitda_cr is not None:
        target.ebit_cr = round(target.ebitda_cr - target.depreciation_cr, 4)
    if "revenue_prior_cr" in applied and (target.revenue_prior_cr or 0) > 0 \
            and target.revenue_cr is not None:
        target.revenue_growth = round(
            (target.revenue_cr - target.revenue_prior_cr)
            / target.revenue_prior_cr, 4)
    if applied:
        audit.decision("resolve", "TARGET_ENRICHED",
                       f"user supplied {len(applied)} missing figure(s) for "
                       f"'{target.name}': {', '.join(applied)} — dependent "
                       f"quantities re-derived; lineage marks them user-provided",
                       {"fields": applied})
    lineage = dict(client.lineage(duns) or {}) if hasattr(client, "lineage") else {}
    for f in applied:
        lineage[f] = {"file": "user-provided enrichment (popup)",
                      "row": None, "fy": None}
    return _evaluate(name, target, client, audit, ctx, top_n,
                     custom_lineage=lineage)


def _parameter_checklist(target, lineage):
    """Essential-parameter checklist — the honest inventory the UI/report tick
    off: AVAILABLE (from source), USER-PROVIDED (chat/enrichment), or MISSING
    (never assumed; fillable in chat). One entry per figure the valuation
    actually consumes."""
    user_keys = " ".join(k for k, v in (lineage or {}).items()
                         if "user-provided" in str((v or {}).get("file", "")))

    def entry(key, label, value, needed_for):
        if value is not None:
            status = "user_provided" if key in user_keys else "available"
        else:
            status = "missing"
        return {"key": key, "label": label, "status": status,
                "value": value, "needed_for": needed_for}

    return [
        entry("name", "Company name", target.name, "identity"),
        entry("industry", "Industry / sector",
              target.naics_desc or target.hoovers, "peer filters #5-6, sector anchors"),
        entry("description", "Business description",
              (target.activities or None), "economic classifier (filters #3-4, #9)"),
        entry("listed", "Listing status", target.listed, "DLOM decision"),
        entry("revenue_cr", "Revenue (₹ Cr)", target.revenue_cr,
              "EV/Revenue driver · scale similarity · DLOM band"),
        entry("revenue_prior_cr", "Prior-year revenue", target.revenue_prior_cr,
              "revenue growth context"),
        entry("ebitda_cr", "EBITDA", target.ebitda_cr,
              "EV/EBITDA driver (headline) · margin positioning"),
        entry("depreciation_cr", "Depreciation", target.depreciation_cr,
              "EBIT → EV/EBIT method"),
        entry("net_worth_cr", "Net worth / capital employed",
              target.capital_employed_cr, "book multiple base"),
        entry("debt_cr", "Borrowings / debt",
              target.debt_cr if target.debt_known else None,
              "EV → equity bridge (equity withheld until known)"),
        entry("cash_cr", "Cash & equivalents",
              target.cash_cr if target.cash_known else None,
              "EV → equity bridge (equity withheld until known)"),
    ]


def _evaluate(name, target, client, audit, ctx, top_n, custom_lineage=None,
              txn_multiple=None):
    """Shared evaluation path: profile -> data-quality gate -> universe ->
    discover -> value -> confidence -> result. Both entry points route here, so
    any methodology change lands identically for database and custom targets."""
    ctx["target"] = target
    tprofile = build_profile(target)
    ctx["tprofile"] = tprofile
    audit.decision("profile", "TARGET_PROFILED",
                   f"{target.name}: {tprofile.operating_model}/{tprofile.value_chain}/"
                   f"{tprofile.customer_type} (conf {tprofile.confidence})",
                   {"operating_model": tprofile.operating_model,
                    "value_chain": tprofile.value_chain,
                    "customer_type": tprofile.customer_type})

    # 3. data-quality gate (before any valuation) -----------------------
    dq = validate_company(target, audit)
    ctx["data_quality"] = dq

    if not dq.valuable:
        audit.error("validate", "INSUFFICIENT_DATA",
                    "target lacks revenue and a usable EV proxy; cannot value",
                    {"missing": dq.missing_fields})
        result = {
            "meta": _metadata(name, "insufficient_data", client),
            "query": name,
            "target": company_to_dict(target),
            "target_profile": profile_to_dict(tprofile),
            "data_quality": dataquality_to_dict(dq),
            "peers": [], "peers_ranked_count": 0, "rejected": [],
            "valuation": None,
            "confidence": {"score": 0.0, "label": "LOW"},
            "audit_trail": audit.to_list(),
        }
        return result, ctx

    # 4. build universe (cached per data source; audit-muted bulk build) --
    universe_all = get_universe(client, audit)
    universe = [(c, p) for (c, p) in universe_all if c.duns != target.duns]

    # 5. discover -------------------------------------------------------
    ranked, rejected = discover_peers(target, tprofile, universe, audit)
    ctx["ranked"] = ranked
    ctx["rejected"] = rejected

    # 6. value ----------------------------------------------------------
    valuation = compute_valuation(target, ranked, top_n=top_n, audit=audit,
                                  txn_multiple=txn_multiple)
    ctx["valuation"] = valuation
    audit.info("value", "VALUATION_DONE",
               f"headline {valuation.headline_method}; "
               f"{len(valuation.methods)} methods computed",
               {"headline": valuation.headline_method,
                "n_methods": len(valuation.methods)})

    # 7. confidence -----------------------------------------------------
    # Peer coverage uses the EFFECTIVE (similarity-weighted) peer count, so a set
    # padded with borderline comps counts for less than a set of exact matches.
    eff_peers = valuation.effective_peer_count
    if eff_peers is None:
        eff_peers = min(len(ranked), top_n)
    conf_score, conf_label, conf_breakdown = compute_confidence(
        tprofile,
        n_peers=eff_peers,
        target_ebitda_pos=(target.ebitda_cr or 0) > 0,
        valuation=valuation,
    )
    audit.info("confidence", "CONFIDENCE_SCORED",
               f"{conf_label} ({conf_score}) — triangulation "
               f"{conf_breakdown['triangulation']}, comp-tightness "
               f"{conf_breakdown['comp_tightness']}",
               {"score": conf_score, "label": conf_label, "breakdown": conf_breakdown})

    # 8. data-source caveats + per-field lineage (traceability) ----------
    caveats = list(getattr(client, "source_caveats", []) or [])
    if custom_lineage is not None:
        caveats.append(
            "Some or all target figures are user-provided (guided intake or "
            "enrichment popup) and unaudited — the peer set and multiples come "
            "from the database, but user-supplied drivers are only as reliable "
            "as the inputs. Per-field lineage marks exactly which is which.")
    for cv in caveats:
        valuation.notes.append(f"data-source caveat: {cv}")
        audit.warn("run", "SOURCE_CAVEAT", cv)
    lineage = {}
    if custom_lineage is not None:
        lineage = custom_lineage
        audit.info("run", "SOURCE_LINEAGE",
                   "target figures are user-provided via guided intake — lineage "
                   "records that explicitly for every answered field",
                   {"fields": list(lineage.keys())})
    elif hasattr(client, "lineage"):
        lineage = client.lineage(target.duns) or {}
        if lineage:
            audit.info("run", "SOURCE_LINEAGE",
                       "per-field source lineage attached to result "
                       "(file + row for every key figure)",
                       {"fields": list(lineage.keys())})

    status = "ok" if valuation.headline_method != "none" else "no_valuation"
    audit.info("run", "COMPLETE", f"pipeline complete: status={status}",
               {"status": status})

    result = {
        "meta": _metadata(name, status, client),
        "query": name,
        "target": company_to_dict(target),
        "target_profile": profile_to_dict(tprofile),
        "target_lineage": lineage,
        "source_caveats": caveats,
        "data_quality": dataquality_to_dict(dq),
        "parameter_checklist": _parameter_checklist(target, lineage),
        "peers": valuation.peers_used,
        "peers_ranked_count": len(ranked),
        # a real 14k universe can reject thousands — carry a sample + the count
        "rejected": rejected[:50],
        "rejected_total": len(rejected),
        "valuation": valuation_to_dict(valuation),
        "confidence": {"score": conf_score, "label": conf_label,
                       "breakdown": conf_breakdown},
        "audit_trail": audit.to_list(),
    }
    return result, ctx


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _fmt(x, nd=1):
    return "n/a" if x is None else f"{x:,.{nd}f}"


def print_summary(result):
    meta = result.get("meta", {})
    print("=" * 78)
    print(f"{meta.get('engine')}  v{meta.get('methodology_version')}  | "
          f"status={meta.get('status')}  | {meta.get('run_timestamp')}")
    print(f"Source: {meta.get('data_source')} ({meta.get('dnb_schema_version')}) | "
          f"units {meta.get('source_units')}→{meta.get('reporting_units')} "
          f"{meta.get('currency')} | touchless={not meta.get('human_in_the_loop')}")

    if result.get("target") is None:
        print(f"NO VALUATION — status '{meta.get('status')}' for query "
              f"'{result.get('query')}'. See audit trail.")
        print("=" * 78)
        return

    t = result["target"]
    tp = result["target_profile"]
    dq = result.get("data_quality") or {}
    val = result["valuation"]
    print("-" * 78)
    print(f"TARGET  {t['name']}  (DUNS {t['duns']}, CIN {t['cin']})")
    print(f"        {tp['operating_model']}/{tp['value_chain']} | {tp['customer_type']} "
          f"| NAICS {t['naics']} ({tp['naics_subsector']}) | "
          f"{'LISTED' if t['listed'] else 'unlisted'}")
    print(f"        Revenue ₹{_fmt(t['revenue_cr'])} Cr | EBITDA ₹{_fmt(t['ebitda_cr'])} Cr "
          f"({_fmt((t['ebitda_margin'] or 0)*100)}%) | EBIT ₹{_fmt(t['ebit_cr'])} Cr | "
          f"growth {_fmt((t['revenue_growth'] or 0)*100)}%")
    print(f"        Data quality: grade {dq.get('grade')} (score {dq.get('score')})"
          + (f" | missing: {', '.join(dq.get('missing_fields'))}"
             if dq.get('missing_fields') else " | no missing fields"))

    if val is None:
        print("-" * 78)
        print(f"NO VALUATION — status '{meta.get('status')}'. See audit trail.")
        print("=" * 78)
        return

    print("-" * 78)
    print(f"PEERS  (top {len(result['peers'])} of {result['peers_ranked_count']} ranked)")
    for i, p in enumerate(result["peers"], 1):
        evx = p["multiples"].get("EV/EBITDA")
        tag = "LISTED" if p["listed"] else "unlisted"
        bl = " ·borderline" if p.get("borderline") else ""
        print(f"  {i:2d}. {p['name'][:30]:30s} score {p['score']:.3f} w{p.get('weight',1):.2f} | "
              f"EV/EBITDA {('%.1fx' % evx) if evx else '  n/a':>6s} | "
              f"₹{_fmt(p['revenue_cr'],0)} Cr | {tag}{bl}")
    print("-" * 78)
    rej_total = result.get("rejected_total", len(result["rejected"]))
    shown = result["rejected"][:10]
    print(f"REJECTED ({rej_total} total"
          + (f", first {len(shown)} shown" if rej_total > len(shown) else "") + ")")
    for r in shown:
        print(f"  x {r['name'][:34]:34s} -> {r['reason']}")
    print("-" * 78)
    print("VALUATION METHODS (triangulation)")
    for m in val["methods"]:
        print(f"  {m['method']:11s} [{m.get('ev_basis','')}] positioned {m['multiple_median']:.2f}x "
              f"(low {m['multiple_p25']:.2f} / high {m['multiple_p75']:.2f}) | "
              f"n={m['n_multiples']} drop={m['n_outliers_dropped']} | "
              f"equity ₹{_fmt(m['equity_low_cr'])}–{_fmt(m['equity_mid_cr'])}–"
              f"{_fmt(m['equity_high_cr'])} Cr")
    if val.get("positioning"):
        print(f"Positioning: {val['positioning']}")
    print(f"Peer weighting: {val.get('n_borderline',0)} borderline of "
          f"{len(result['peers'])} → effective peer count "
          f"{val.get('effective_peer_count')} (borderline comps down-weighted)")
    print(f"EV basis : {val['ev_basis']}")
    print(f"Net debt : ₹{_fmt(val['net_debt_cr'])} Cr | discount {val['discount']*100:.0f}% "
          f"({val['discount_reason']})")
    print(f"HEADLINE ({val['headline_method']}) equity value: "
          f"₹{_fmt(val['equity_low_cr'])} – {_fmt(val['equity_mid_cr'])} – "
          f"{_fmt(val['equity_high_cr'])} Cr")
    xc = val.get("market_cross_check")
    if xc:
        flag = "OK" if xc["within_25pct"] else "CHECK"
        print(f"ACCURACY [{flag}]: comps mid ₹{_fmt(xc['comps_mid_equity_cr'])} Cr vs own "
              f"market cap ₹{_fmt(xc['own_market_cap_cr'])} Cr ({xc['delta_pct']:+.1f}%)")
    for w in val["warnings"]:
        print(f"  ! {w}")
    cb = result["confidence"].get("breakdown", {})
    print(f"CONFIDENCE: {result['confidence']['label']} ({result['confidence']['score']})"
          + (f"  [profile {cb.get('profile')} + coverage {cb.get('peer_coverage')} + "
             f"ebitda {cb.get('ebitda_positive')} + methods {cb.get('methods')} + "
             f"triangulation {cb.get('triangulation')} + comp-tightness "
             f"{cb.get('comp_tightness')}]" if cb else ""))
    levels = {}
    for a in result["audit_trail"]:
        levels[a["level"]] = levels.get(a["level"], 0) + 1
    print(f"AUDIT: {len(result['audit_trail'])} records "
          f"({', '.join(f'{k} {v}' for k, v in sorted(levels.items()))})")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Acceptance tests (§10)
# ---------------------------------------------------------------------------

def acceptance_tests():
    """REAL-DATA acceptance suite (the mock universe has been removed).
    Verifies: resolution, normalization, peer discovery, the NO-ASSUMPTION rule
    (equity withheld when net debt unknown), the enrichment path, the custom-
    intake path, honest degradation, and the observed-market validation gate."""
    print()
    print("#" * 78)
    print("# ACCEPTANCE TESTS (real data)")
    print("#" * 78)
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

    client = make_client("real")
    for tgt in ("20 Microns Ltd.", "Kirloskar Brothers Ltd.",
                "Odyssey Technologies Ltd."):
        print(f"-- target: {tgt} --")
        result, ctx = run_pipeline(tgt, client=client)
        target, val = ctx["target"], ctx["valuation"]
        check(f"[{tgt}] resolves + normalizes (revenue in Cr)",
              target is not None and (target.revenue_cr or 0) > 0,
              f"rev={target.revenue_cr if target else None}")
        check(f"[{tgt}] >=10 peers, rejected recorded with reasons",
              len(result["peers"]) >= 10 and result["rejected_total"] > 0
              and all(r.get("reason") for r in result["rejected"][:20]),
              f"peers={len(result['peers'])} rejected={result['rejected_total']}")
        hm = next((m for m in val.methods if m["method"] == val.headline_method), None)
        check(f"[{tgt}] methods compute; EV range low<mid<high",
              hm is not None and hm["ev_low_cr"] < hm["ev_mid_cr"] < hm["ev_high_cr"],
              f"EV {hm['ev_low_cr']:.0f}<{hm['ev_mid_cr']:.0f}<{hm['ev_high_cr']:.0f}"
              if hm else "no method")
        # THE NO-ASSUMPTION RULE: source has no borrowings/cash -> equity withheld
        check(f"[{tgt}] equity WITHHELD (net debt unknown, nothing assumed)",
              val.equity_mid_cr is None and val.net_debt_cr is None
              and set(val.equity_requires) == {"borrowings", "cash"},
              f"equity_requires={val.equity_requires}")
        cl = {c["key"]: c["status"] for c in result["parameter_checklist"]}
        check(f"[{tgt}] parameter checklist: debt/cash MISSING, revenue AVAILABLE",
              cl.get("debt_cr") == "missing" and cl.get("cash_cr") == "missing"
              and cl.get("revenue_cr") == "available",
              f"{sum(1 for s in cl.values() if s=='available')} available / "
              f"{sum(1 for s in cl.values() if s=='missing')} missing")
        a0 = result["audit_trail"][0]
        check(f"[{tgt}] structured audit + provenance + DQ valuable",
              all(k in a0 for k in ("seq", "ts", "stage", "level", "code"))
              and result["meta"]["status"] == "ok"
              and result["data_quality"]["valuable"] is True,
              f"audit={len(result['audit_trail'])} dq={result['data_quality']['grade']}")

    # enrichment path: user supplies debt/cash -> equity appears
    print("-- enrichment path (user-supplied debt/cash) --")
    r, c = run_pipeline_enriched("20 Microns Ltd.",
                                 {"debt_cr": 180.0, "cash_cr": 25.0}, client=client)
    v = c["valuation"]
    check("[enrich] equity computed once net debt known",
          v.equity_mid_cr is not None
          and v.equity_low_cr < v.equity_mid_cr < v.equity_high_cr
          and v.net_debt_cr == 155.0,
          f"equity mid={v.equity_mid_cr} net_debt={v.net_debt_cr}")
    check("[enrich] lineage marks user-provided + TARGET_ENRICHED audited",
          any("enrichment" in str(x.get("file", ""))
              for x in r["target_lineage"].values())
          and any(a["code"] == "TARGET_ENRICHED" for a in r["audit_trail"]), "")
    check("[enrich] transactions view present (equity known)",
          v.transaction_analysis is not None, "")

    # custom-intake path: full figures incl. debt/cash -> equity + DLOM
    print("-- custom intake path --")
    from intake import IntakeSession, build_company, intake_lineage
    s = IntakeSession(industry_choices=list(client.industry_catalog().keys()))
    for a in ("Acceptance Test Forgings",
              "manufactures forged steel components for industrial machinery",
              "Forgings", "no", "75", "62", "10.5", "2.8", "48", "12", "4", "yes",
              "150", "11"):        # optional observed deal: 150/11 ≈ 13.6x
        s.submit(a)
    from intake import txn_multiple_from
    r, c = run_pipeline_custom(build_company(s.answers, client), client=client,
                               lineage=intake_lineage(s.answers),
                               txn_multiple=txn_multiple_from(s.answers))
    v = c["valuation"]
    check("[intake] custom company valued with equity + DLOM",
          v is not None and v.equity_mid_cr is not None and v.discount > 0,
          f"equity mid={v.equity_mid_cr} DLOM={v.discount}")
    check("[intake] OBSERVED transaction multiple used (user-provided deal)",
          v.transaction_analysis is not None
          and v.transaction_analysis.get("txn_multiple") is not None,
          f"txn {v.transaction_analysis.get('txn_multiple') if v.transaction_analysis else None}x")

    # honest degradation
    r, _c = run_pipeline("zzz-no-such-company-999", client=client)
    check("[degrade] unknown company -> status no_match, no numbers",
          r["meta"]["status"] == "no_match" and r["valuation"] is None, "")

    # observed-market validation gate (EV mid vs actual market caps, 2026-07-17)
    print("-- observed-market validation gate --")
    try:
        from validate import real_validation_summary
        summ = real_validation_summary(client=client)
        check("[market] median |EV vs market-cap error| within 25%",
              summ["median_abs_pct"] <= 25.0,
              f"median {summ['median_abs_pct']}% · mean {summ['mean_abs_pct']}% · "
              f"in-range {summ['n_in_range']}/{summ['n']}")
    except Exception as e:  # pragma: no cover
        check("[market] observed-market validation runs", False, f"error: {e}")

    total = len(checks)
    passed = sum(1 for _, cnd, _ in checks if cnd)
    print()
    print("#" * 78)
    print(f"# RESULT: {passed}/{total} checks passed")
    print("#" * 78)
    return passed == total



def main():
    argv = sys.argv[1:]
    data_source = os.environ.get("DATA_SOURCE", "real")
    if "--data" in argv:
        i = argv.index("--data")
        if i + 1 < len(argv):
            data_source = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]          # strip the flag + its value
    args = [a for a in argv if not a.startswith("--")]
    default_name = "20 Microns Ltd."
    name = args[0] if args else default_name
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    result, _ctx = run_pipeline(name, data_source=data_source)

    # Embed the observed-market validation so reports show aggregate accuracy.
    if result.get("valuation"):
        try:
            from validate import real_validation_summary
            result["validation"] = real_validation_summary()
        except Exception as e:  # pragma: no cover
            result["validation"] = {"error": str(e)}

    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print_summary(result)
    print(f"\nWrote {RESULT_PATH}")

    build_dashboard(RESULT_PATH, DASHBOARD_PATH)
    print(f"Wrote {DASHBOARD_PATH}")

    ok = acceptance_tests()
    print("\n" + ("PASS: all acceptance tests passed."
                  if ok else "FAIL: some acceptance tests failed."))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
