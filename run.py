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
METHODOLOGY_VERSION = "1.1.0"
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

def compute_confidence(profile, n_peers, target_ebitda_pos, n_methods):
    score = (0.35 * profile.confidence
             + 0.35 * (min(n_peers, 15) / 15.0)
             + 0.15 * (1.0 if target_ebitda_pos else 0.0)
             + 0.15 * (1.0 if n_methods >= 2 else 0.0))
    if score >= 0.75:
        label = "HIGH"
    elif score >= 0.50:
        label = "MEDIUM"
    else:
        label = "LOW"
    return round(score, 3), label


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
    conf_score, conf_label = compute_confidence(
        tprofile,
        n_peers=min(len(ranked), top_n),
        target_ebitda_pos=(target.ebitda_cr or 0) > 0,
        n_methods=len(valuation.methods),
    )
    audit.info("confidence", "CONFIDENCE_SCORED",
               f"{conf_label} ({conf_score})",
               {"score": conf_score, "label": conf_label})

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
        "confidence": {"score": conf_score, "label": conf_label},
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
        print(f"  {i:2d}. {p['name'][:34]:34s} score {p['score']:.3f} | "
              f"EV/EBITDA {('%.1fx' % evx) if evx else '  n/a':>6s} | "
              f"₹{_fmt(p['revenue_cr'],0)} Cr | {p['city']:>11s} | {tag}")
    print("-" * 78)
    print(f"REJECTED ({len(result['rejected'])})")
    for r in result["rejected"]:
        print(f"  x {r['name'][:34]:34s} -> {r['reason']}")
    print("-" * 78)
    print("VALUATION METHODS (triangulation)")
    for m in val["methods"]:
        print(f"  {m['method']:11s} median {m['multiple_median']:.2f}x "
              f"(P25 {m['multiple_p25']:.2f} / P75 {m['multiple_p75']:.2f}) | "
              f"n={m['n_multiples']} drop={m['n_outliers_dropped']} | "
              f"equity ₹{_fmt(m['equity_low_cr'])}–{_fmt(m['equity_mid_cr'])}–"
              f"{_fmt(m['equity_high_cr'])} Cr")
    print("-" * 78)
    print(f"EV basis : {val['ev_basis']}")
    print(f"Net debt : ₹{_fmt(val['net_debt_cr'])} Cr | discount {val['discount']*100:.0f}% "
          f"({val['discount_reason']})")
    print(f"HEADLINE ({val['headline_method']}) equity value: "
          f"₹{_fmt(val['equity_low_cr'])} – {_fmt(val['equity_mid_cr'])} – "
          f"{_fmt(val['equity_high_cr'])} Cr")
    for w in val["warnings"]:
        print(f"  ! {w}")
    print(f"CONFIDENCE: {result['confidence']['label']} ({result['confidence']['score']})")
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

    for tgt, cluster in (("Woodward", "A"), ("Kirloskar Brothers Pumps", "C")):
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
        # 7 headline range low<mid<high with discount
        check(f"[{tgt}] headline range low<mid<high w/ discount",
              (val.equity_low_cr is not None
               and val.equity_low_cr < val.equity_mid_cr < val.equity_high_cr
               and 0 < val.discount < 1),
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
