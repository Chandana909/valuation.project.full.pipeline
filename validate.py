"""
validate.py — OBSERVED-MARKET validation (the synthetic mock universe and its
backtest have been removed; nothing here is simulated).

Method: for a fixed set of listed companies present in the database, the engine
values each one and its enterprise-value mid is compared against the company's
OWN market capitalisation as observed on the NSE on the as-of date below.

Honesty notes (read before quoting the numbers):
  * The engine's EV is compared to MARKET CAP (an equity value) because the
    source extract has no borrowings/cash for these companies and the engine's
    no-assumption rule withholds equity. For the mostly low-leverage companies
    in this set the gap is small; the bias direction (EV >= equity for net-debt
    positive companies) is known and disclosed.
  * The actual market caps are OBSERVED constants (source + date recorded
    below), not assumptions. Refresh them when re-validating.
  * Two companies are known idiosyncratic outliers (a premium re-rating and a
    policy-risk discount) that sector comps cannot see — they are kept IN the
    set deliberately; hiding them would overstate accuracy.

Run:  python validate.py       (exit 0 iff the gate passes)
"""

import sys
import os
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# Observed NSE market caps, pulled 2026-07-17 (screener.in / stockanalysis.com /
# NSE quotes — URLs in PRODUCTION_REVIEW.md and the commit message of record).
AS_OF = "2026-07-17/20"
OBSERVED = {
    # round 1 (2026-07-17)
    "20 Microns Ltd.":              714.0,
    "Aarti Industries Ltd.":     17485.0,
    "Kirloskar Brothers Ltd.":   13926.0,   # known outlier: ~38.8x re-rating
    "Mahindra EPC Irrigation Ltd.": 336.0,
    "Shakti Pumps (India) Ltd.":  6798.0,   # known outlier: policy-risk discount
    "Roto Pumps Ltd.":            1226.0,
    # round 2 (2026-07-20) — 7 more sectors/sizes
    "Gabriel India Ltd.":        19199.0,   # known outlier: ~60x restructuring re-rating
    "Fiem Industries Ltd.":       5726.0,
    "Aarti Drugs Ltd.":           3854.0,
    "Apollo Pipes Ltd.":          2263.0,   # known limit: category mixes packaging & pipes
    "Vadilal Industries Ltd.":    3949.0,
    "The Anup Engineering Ltd.":  4486.0,
    "Jash Engineering Ltd.":      3206.0,
}

_CACHE = {}


def real_validation(client=None):
    """Value every observed company; return per-row comparison. Deterministic
    given the database + the pinned observation set."""
    from run import run_pipeline, make_client
    client = client or make_client("real")
    rows = []
    for name, actual in OBSERVED.items():
        result, ctx = run_pipeline(name, client=client)
        val = ctx["valuation"]
        if val is None or val.headline_method == "none":
            rows.append({"company": name, "status": "no_valuation",
                         "actual_mcap_cr": actual})
            continue
        hm = next(m for m in val.methods if m["method"] == val.headline_method)
        delta = hm["ev_mid_cr"] / actual - 1.0
        rows.append({
            "company": name,
            "engine_ev_low_cr": round(hm["ev_low_cr"], 1),
            "engine_ev_mid_cr": round(hm["ev_mid_cr"], 1),
            "engine_ev_high_cr": round(hm["ev_high_cr"], 1),
            "actual_mcap_cr": actual,
            "delta_pct": round(delta * 100, 1),
            "in_range": bool(hm["ev_low_cr"] <= actual <= hm["ev_high_cr"]),
            "status": "ok",
        })
    return rows


def real_validation_summary(client=None):
    """Aggregate summary for the API/report; cached per process."""
    if "summary" in _CACHE:
        return _CACHE["summary"]
    rows = real_validation(client=client)
    ok = [r for r in rows if r["status"] == "ok"]
    errs = [abs(r["delta_pct"]) for r in ok]
    summary = {
        "kind": "observed_market_validation",
        "as_of": AS_OF,
        "basis": ("engine EV mid vs the company's own observed NSE market cap; "
                  "EV~equity proxy disclosed (no borrowings/cash in source)"),
        "n": len(ok),
        "rows": rows,
        "median_abs_pct": round(statistics.median(errs), 1) if errs else None,
        "mean_abs_pct": round(statistics.mean(errs), 1) if errs else None,
        "n_in_range": sum(1 for r in ok if r["in_range"]),
        "note": ("Two known idiosyncratic outliers are retained deliberately; "
                 "the ranges disagree with them honestly rather than being "
                 "widened to swallow them."),
        "verdict_ok": bool(errs) and statistics.median(errs) <= 25.0,
    }
    _CACHE["summary"] = summary
    return summary


def main():
    s = real_validation_summary()
    print("=" * 74)
    print(f"OBSERVED-MARKET VALIDATION  (as of {AS_OF})")
    print("=" * 74)
    for r in s["rows"]:
        if r["status"] != "ok":
            print(f"{r['company']:32s}  NO VALUATION")
            continue
        tag = "IN " if r["in_range"] else "out"
        print(f"{r['company']:32s} EV mid ₹{r['engine_ev_mid_cr']:>9,.0f} Cr | "
              f"actual ₹{r['actual_mcap_cr']:>9,.0f} Cr | {r['delta_pct']:>+6.1f}% "
              f"[{tag} range]")
    print("-" * 74)
    print(f"median |err| {s['median_abs_pct']}% · mean {s['mean_abs_pct']}% · "
          f"in-range {s['n_in_range']}/{s['n']}")
    print(f"VERDICT: {'PASS' if s['verdict_ok'] else 'REVIEW'} "
          f"(gate: median ≤ 25%)")
    return s["verdict_ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
