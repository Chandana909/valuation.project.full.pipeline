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

from mock_api import MockDnBClient
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
METHODOLOGY_VERSION = "1.6.0"
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

def _metadata(name, status):
    return {
        "engine": ENGINE_NAME,
        "methodology_version": METHODOLOGY_VERSION,
        "dnb_schema_version": DNB_SCHEMA_VERSION,
        "run_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_source": "Dun & Bradstreet (dnbhoovers)",
        "currency": "INR",
        "reporting_units": "Crore",
        "source_units": "Thousand",
        "human_in_the_loop": False,
        "query": name,
        "status": status,
    }


def run_pipeline(name, client=None, top_n=15):
    """
    Returns (result_dict, ctx). `ctx` carries the live objects for callers/tests.
    Never raises on bad input — degraded runs return a structured result with a
    non-'ok' `status` and a complete audit trail explaining why.
    """
    audit = AuditTrail()
    client = client or MockDnBClient(audit=audit)
    audit.info("run", "START", f"pipeline start for query '{name}'",
               {"methodology_version": METHODOLOGY_VERSION})
    ctx = {"target": None, "tprofile": None, "ranked": [], "rejected": [],
           "valuation": None, "data_quality": None}

    # 1. resolve --------------------------------------------------------
    duns = _best_match(client, name, audit)
    if duns is None:
        result = {
            "meta": _metadata(name, "no_match"),
            "query": name, "target": None, "target_profile": None,
            "data_quality": None, "peers": [], "peers_ranked_count": 0,
            "rejected": [], "valuation": None,
            "confidence": {"score": 0.0, "label": "LOW"},
            "audit_trail": audit.to_list(),
        }
        return result, ctx

    # 2. target ---------------------------------------------------------
    target = _fetch_company(client, duns, audit, with_mgmt=True)
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
            "meta": _metadata(name, "insufficient_data"),
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

    # 4. build universe (fetch + normalize + profile each DUNS) ----------
    universe = []
    for d in client.universe_duns():
        if d == duns:
            continue
        c = _fetch_company(client, d, audit, with_mgmt=False)
        universe.append((c, build_profile(c)))
    audit.info("universe", "UNIVERSE_LOADED",
               f"loaded {len(universe)} candidate companies from D&B",
               {"count": len(universe)})

    # 5. discover -------------------------------------------------------
    ranked, rejected = discover_peers(target, tprofile, universe, audit)
    ctx["ranked"] = ranked
    ctx["rejected"] = rejected

    # 6. value ----------------------------------------------------------
    valuation = compute_valuation(target, ranked, top_n=top_n, audit=audit)
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

    status = "ok" if valuation.headline_method != "none" else "no_valuation"
    audit.info("run", "COMPLETE", f"pipeline complete: status={status}",
               {"status": status})

    result = {
        "meta": _metadata(name, status),
        "query": name,
        "target": company_to_dict(target),
        "target_profile": profile_to_dict(tprofile),
        "data_quality": dataquality_to_dict(dq),
        "peers": valuation.peers_used,
        "peers_ranked_count": len(ranked),
        "rejected": rejected,
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
    print(f"REJECTED ({len(result['rejected'])})")
    for r in result["rejected"]:
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
    print("\n" + "#" * 78)
    print("# ACCEPTANCE TESTS")
    print("#" * 78)
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {detail}")

    for tgt, cluster in (("Woodward", "A"), ("Kirloskar Brothers Pumps", "C"),
                         ("Bharat Forge Components", "B")):
        print(f"\n-- target: {tgt} (cluster {cluster}) --")
        result, ctx = run_pipeline(tgt)
        target, tprof = ctx["target"], ctx["tprofile"]
        ranked, rejected, val = ctx["ranked"], ctx["rejected"], ctx["valuation"]

        # 1 resolve
        check(f"[{tgt}] resolves to a DUNS", target.duns is not None,
              f"DUNS={target.duns}")
        # 2 financials normalize
        check(f"[{tgt}] financials normalize (rev & EBITDA in Cr, non-zero)",
              (target.revenue_cr or 0) > 0 and (target.ebitda_cr or 0) > 0,
              f"rev={target.revenue_cr} ebitda={target.ebitda_cr}")
        # 3 >=15 peers
        peers_used = result["peers"]
        check(f"[{tgt}] at least 15 peers", len(peers_used) >= 15,
              f"got {len(peers_used)}")
        # 4 all 5 wrong rejected with reasons
        rej_names = {r["name"] for r in rejected}
        wrong = {"Metro Wholesale Distributors", "Prime Industrial Traders",
                 "Sterling Steel Billets", "UrbanMart Retail Stores",
                 "Insight Engineering Consulting"}
        all_reasons = all(r.get("reason") for r in rejected)
        check(f"[{tgt}] all 5 wrong entities rejected w/ reason",
              wrong.issubset(rej_names) and len(rejected) == 5 and all_reasons,
              f"rejected={sorted(rej_names)}")
        # 5 all three methods compute + triangulation band
        method_names = {m["method"] for m in val.methods}
        three = {"EV/EBITDA", "EV/Revenue", "EV/EBIT"}.issubset(method_names)
        mids = [m["equity_mid_cr"] for m in val.methods]
        band_ok = False
        if mids and min(mids) > 0:
            band_ok = (max(mids) / min(mids)) <= 2.5   # sensible triangulation band
        check(f"[{tgt}] all 3 methods compute + triangulate",
              three and band_ok,
              f"methods={sorted(method_names)} mids={[round(m,1) for m in mids]}")
        # 6 IQR trimming ran (reported, may be 0)
        trimmed_reported = all("n_outliers_dropped" in m for m in val.methods)
        check(f"[{tgt}] IQR trimming ran (reported)", trimmed_reported,
              f"dropped={[m['n_outliers_dropped'] for m in val.methods]}")
        # 7 headline range low<mid<high with a valid discount (0 for listed target)
        check(f"[{tgt}] headline range low<mid<high w/ discount",
              (val.equity_low_cr is not None
               and val.equity_low_cr < val.equity_mid_cr < val.equity_high_cr
               and 0 <= val.discount < 1),
              f"{val.equity_low_cr}<{val.equity_mid_cr}<{val.equity_high_cr} "
              f"disc={val.discount}")
        # 8 result.json contents
        keys_ok = all(k in result for k in
                      ("meta", "target", "data_quality", "peers", "rejected",
                       "valuation", "confidence", "audit_trail"))
        check(f"[{tgt}] result has all top-level sections",
              keys_ok and len(result["audit_trail"]) > 0,
              f"audit entries={len(result['audit_trail'])}")
        # 9 structured audit trail (typed records)
        a0 = result["audit_trail"][0]
        audit_typed = all(k in a0 for k in ("seq", "ts", "stage", "level", "code"))
        has_decisions = any(a["level"] == "DECISION" for a in result["audit_trail"])
        check(f"[{tgt}] audit trail is structured + has DECISION records",
              audit_typed and has_decisions,
              f"levels={sorted(set(a['level'] for a in result['audit_trail']))}")
        # 10 provenance metadata + data-quality grade
        meta = result["meta"]
        meta_ok = (meta.get("status") == "ok"
                   and meta.get("methodology_version")
                   and meta.get("currency") == "INR"
                   and meta.get("reporting_units") == "Crore")
        dq_ok = result["data_quality"]["grade"] in ("A", "B", "C", "D") \
            and result["data_quality"]["valuable"] is True
        check(f"[{tgt}] provenance metadata + data-quality grade present",
              meta_ok and dq_ok,
              f"status={meta.get('status')} v={meta.get('methodology_version')} "
              f"dq={result['data_quality']['grade']}")
        # 11 ACCURACY: for a listed target, comps mid equity must land within 25%
        #    of the company's own observed market cap (calibration check).
        xc = val.market_cross_check
        if target.listed and xc:
            check(f"[{tgt}] comps calibrate to own market cap (±25%)",
                  xc["within_25pct"],
                  f"comps {xc['comps_mid_equity_cr']:.0f} vs mktcap "
                  f"{xc['own_market_cap_cr']:.0f} ({xc['delta_pct']:+.1f}%)")
        else:
            check(f"[{tgt}] unlisted target — DLOM applied, no market cross-check",
                  (not target.listed) and val.discount > 0,
                  f"listed={target.listed} DLOM={val.discount}")

    # 12 ANTI-OVERFITTING: backtest the whole universe of listed comps — positioning
    #    must beat the naive flat median on the majority of targets (proves the method
    #    generalizes and the good example calibrations are not cherry-picked luck).
    print("\n-- anti-overfitting backtest (all listed comps) --")
    try:
        from validate import run_backtest
        corr, pos, med, pos_better, _rows = run_backtest()
        import statistics as _st
        pos_mae = _st.mean(abs(x) for x in pos)
        med_mae = _st.mean(abs(x) for x in med)
        check("[backtest] positioning generalizes (beats naive median)",
              corr > 0.3 and pos_mae < med_mae and pos_better > len(pos) / 2,
              f"corr={corr:.2f} pos_MAE={pos_mae*100:.1f}% med_MAE={med_mae*100:.1f}% "
              f"pos_better={pos_better}/{len(pos)}")
    except Exception as e:  # pragma: no cover
        check("[backtest] positioning generalizes (beats naive median)", False, f"error: {e}")

    total = len(checks)
    passed = sum(1 for _, c, _ in checks if c)
    print("\n" + "#" * 78)
    print(f"# RESULT: {passed}/{total} checks passed")
    print("#" * 78)
    return passed == total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "Woodward"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    result, _ctx = run_pipeline(name)

    # Embed the methodology backtest so the dashboard can show honest, aggregate
    # accuracy (guards against reading one lucky calibration as proof).
    if result.get("valuation"):
        try:
            from validate import backtest_summary
            result["validation"] = backtest_summary()
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
