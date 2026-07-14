"""
validate.py — anti-overfitting backtest.

A single valuation that happens to match its target's market cap proves nothing — it
could be luck. This script treats EVERY listed manufacturer in the universe as a
target, values it purely from its peers, and compares the result to that company's
OWN observed market capitalisation. It reports the calibration-error distribution and
checks that the fundamentals-based "quality positioning" systematically beats the naive
flat-median approach. It also reports corr(margin, EV/EBITDA) as a sanity check that the
synthetic market prices quality realistically (a near-zero correlation would mean
positioning is meaningless).

Run:  python validate.py
"""

import sys
import os
import math
import statistics as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from mock_api import MockDnBClient
from core.pipeline import _percentile
from run import run_pipeline


def _corr(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in xs) / n)
    sy = math.sqrt(sum((b - my) ** 2 for b in ys) / n)
    return cov / (sx * sy) if sx and sy else 0.0


def _summ(deltas):
    a = [abs(d) for d in deltas]
    return {
        "n": len(a),
        "mean_abs_pct": round(st.mean(a) * 100, 1),
        "median_abs_pct": round(st.median(a) * 100, 1),
        "max_abs_pct": round(max(a) * 100, 1),
        "within_15pct": sum(1 for x in a if x <= 0.15),
        "within_25pct": sum(1 for x in a if x <= 0.25),
    }


def run_backtest():
    client = MockDnBClient()

    # 1) sanity: does the synthetic market price margin?  (>0.3 == realistic)
    margins, mults = [], []
    for d in client.universe_duns():
        fin = client.request("company_financials", {"duns": d})["data"]["organization"]
        ov = fin["latestFiscalFinancials"]["overview"]
        mkt = fin.get("marketData")
        if mkt and ov.get("ebitda"):
            margins.append(ov["ebitda"] / ov["salesRevenue"])
            mults.append(mkt["enterpriseValue"] / ov["ebitda"])
    corr = _corr(margins, mults)

    # 2) which universe companies are listed manufacturers?
    names = []
    for d in client.universe_duns():
        org = client.request("company_information", {"duns": d})["data"]["organization"]
        cin = org["registrationNumbers"][0]["registrationNumber"]
        codes = {ic["typeDescription"]: ic["code"] for ic in org["industryCodes"]}
        if cin.startswith("L") and codes.get("D&B Standard Major Industry Code") == "D":
            names.append(org["primaryName"])

    # 3) backtest each as a target
    pos_deltas, med_deltas = [], []
    pos_better = 0
    rows = []
    for nm in names:
        result, ctx = run_pipeline(nm)
        t, v = ctx["target"], ctx["valuation"]
        if not v or v.headline_method != "EV/EBITDA":
            continue
        if not (t.market_cap_cr and t.market_cap_cr > 0):
            continue
        own = t.market_cap_cr
        hm = next(m for m in v.methods if m["method"] == "EV/EBITDA")
        pos_eq = hm["equity_mid_cr"]                       # positioned (DLOM 0, listed)
        pos_d = pos_eq / own - 1.0
        peer_mults = sorted(p["multiples"]["EV/EBITDA"] for p in v.peers_used
                            if p.get("ev_basis") == "market" and p["multiples"].get("EV/EBITDA"))
        med_eq = _percentile(peer_mults, 0.5) * t.ebitda_cr - v.net_debt_cr
        med_d = med_eq / own - 1.0
        pos_deltas.append(pos_d)
        med_deltas.append(med_d)
        if abs(pos_d) < abs(med_d):
            pos_better += 1
        rows.append((nm, t.ebitda_margin, pos_d, med_d))

    return corr, pos_deltas, med_deltas, pos_better, rows


def backtest_summary():
    """Machine-readable backtest summary for embedding in a result/dashboard."""
    corr, pos, med, pos_better, rows = run_backtest()
    ps, ms = _summ(pos), _summ(med)
    verdict_ok = (ps["mean_abs_pct"] < ms["mean_abs_pct"]) and corr > 0.3 \
        and pos_better > len(pos) / 2
    return {
        "n_targets": len(pos),
        "margin_multiple_corr": round(corr, 3),
        "positioned": ps,
        "flat_median": ms,
        "positioning_wins": pos_better,
        "verdict": "PASS" if verdict_ok else "REVIEW",
        "verdict_ok": verdict_ok,
    }


def seed_robustness(n_seeds=5):
    """
    The strongest anti-overfitting test: rebuild the ENTIRE universe with shifted
    RNG seeds (different companies' financials, listings, market noise) and re-run
    the calibration backtest on each. If quality-positioning only beat the naive
    median on the canonical seed, it was seed-luck; if it wins across fresh draws,
    the method is structurally sound. Restores the canonical universe afterwards.
    """
    from mock_api.dnb_mock import rebuild_universe
    results = []
    try:
        for k in range(1, n_seeds + 1):
            rebuild_universe(seed_offset=k * 1000)
            corr, pos, med, pos_better, _rows = run_backtest()
            ps, ms = _summ(pos), _summ(med)
            results.append({
                "seed_offset": k * 1000,
                "n": ps["n"],
                "corr": round(corr, 3),
                "pos_mae_pct": ps["mean_abs_pct"],
                "med_mae_pct": ms["mean_abs_pct"],
                "pos_wins": pos_better,
                "pos_beats_naive": ps["mean_abs_pct"] < ms["mean_abs_pct"],
            })
    finally:
        rebuild_universe(seed_offset=0)   # ALWAYS restore the canonical universe
    n_beat = sum(1 for r in results if r["pos_beats_naive"])
    return {
        "n_seeds": len(results),
        "seeds_where_positioning_beats_naive": n_beat,
        "mean_pos_mae_pct": round(sum(r["pos_mae_pct"] for r in results) / len(results), 1),
        "mean_med_mae_pct": round(sum(r["med_mae_pct"] for r in results) / len(results), 1),
        "per_seed": results,
        "robust": n_beat > len(results) / 2,   # strict majority of fresh universes
    }


def main():
    corr, pos, med, pos_better, rows = run_backtest()
    print("=" * 74)
    print("ANTI-OVERFITTING BACKTEST — comps valuation vs own market cap")
    print("=" * 74)
    print(f"corr(EBITDA margin, EV/EBITDA) in synthetic market = {corr:.3f}")
    print("  (>0.3 => market prices quality realistically; ~0 => positioning would be luck)")
    print()
    ps, ms = _summ(pos), _summ(med)
    print(f"{'':22s} {'mean|Δ|':>8s} {'med|Δ|':>8s} {'max|Δ|':>8s} {'≤15%':>6s} {'≤25%':>6s}")
    print(f"{'POSITIONED (used)':22s} {ps['mean_abs_pct']:7.1f}% {ps['median_abs_pct']:7.1f}% "
          f"{ps['max_abs_pct']:7.1f}% {ps['within_15pct']:>3d}/{ps['n']:<2d} {ps['within_25pct']:>3d}/{ps['n']:<2d}")
    print(f"{'FLAT MEDIAN (naive)':22s} {ms['mean_abs_pct']:7.1f}% {ms['median_abs_pct']:7.1f}% "
          f"{ms['max_abs_pct']:7.1f}% {ms['within_15pct']:>3d}/{ms['n']:<2d} {ms['within_25pct']:>3d}/{ms['n']:<2d}")
    print()
    print(f"Positioning is closer to true market cap on {pos_better}/{len(pos)} targets "
          f"({pos_better/len(pos)*100:.0f}%).")
    verdict_ok = (ps["mean_abs_pct"] < ms["mean_abs_pct"]) and corr > 0.3 and pos_better > len(pos) / 2
    print()
    print("VERDICT:", "PASS — positioning generalizes, not overfit."
          if verdict_ok else "REVIEW — positioning does not clearly beat naive median.")
    print("=" * 74)
    # per-target detail (sorted worst-first)
    print("\nPer-target (worst positioned error first):")
    for nm, mgn, pd, md in sorted(rows, key=lambda r: -abs(r[2]))[:12]:
        print(f"  {nm[:30]:30s} margin {mgn*100:4.1f}%  positioned {pd*100:+6.1f}%  "
              f"(naive median {md*100:+6.1f}%)")

    # ---- seed-robustness sweep (was it just this universe?) --------------
    print("\n" + "=" * 74)
    print("SEED-ROBUSTNESS SWEEP — same test on 5 freshly drawn universes")
    print("=" * 74)
    sw = seed_robustness(n_seeds=5)
    print(f"{'seed':>6s} {'n':>4s} {'corr':>6s} {'pos MAE':>9s} {'naive MAE':>10s} {'wins':>7s}  beats naive?")
    for r in sw["per_seed"]:
        print(f"{r['seed_offset']:>6d} {r['n']:>4d} {r['corr']:>6.2f} "
              f"{r['pos_mae_pct']:>8.1f}% {r['med_mae_pct']:>9.1f}% "
              f"{r['pos_wins']:>4d}/{r['n']:<3d} {'YES' if r['pos_beats_naive'] else 'no'}")
    print(f"\nPositioning beats naive on {sw['seeds_where_positioning_beats_naive']}/"
          f"{sw['n_seeds']} fresh universes "
          f"(mean MAE {sw['mean_pos_mae_pct']}% vs {sw['mean_med_mae_pct']}%).")
    print("ROBUSTNESS:", "PASS — not seed-luck." if sw["robust"] else "REVIEW — seed-dependent.")
    return 0 if (verdict_ok and sw["robust"]) else 1


if __name__ == "__main__":
    sys.exit(main())
