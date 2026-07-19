"""
core/calibration.py — sector-level trading-multiple anchors for pricing on data
sources that carry NO market prices (the real Accord extract).

THE PROBLEM THIS SOLVES. Book capital employed (≈ net worth) is not enterprise
value: pricing peers off it yields ~1x-book "multiples", so every valuation
lands 2–4x below where Indian companies actually trade. The peer machinery
(who is comparable, relative positioning, dispersion, weights) is sound — only
the LEVEL is wrong.

THE FIX. When a method falls back to the book pool, its peer-multiple
distribution is RE-LEVELLED so the weighted median equals the sector's
observed trading anchor: every book multiple is scaled by
(anchor / median_book_multiple). Shape, dispersion, outliers and similarity
weights are preserved — the target's quality positioning still reads the same
percentile of the same distribution — but the resulting numbers sit at market
level, not book level. Audited as SECTOR_CALIBRATED with the factor disclosed.

THE ANCHORS. Editable calibration defaults per sector group (the ~20 keyword
groups the real client derives from CD_Industry), set from published India
sector aggregates (Damodaran NYU dataset, January-2025 vintage) sanity-checked
against NSE small/mid-cap trading bands. They skew toward larger listed
companies, so a SIZE FACTOR haircuts the anchor for smaller targets (small-cap
multiple discounts are well documented). These are the honest stand-in until a
real market-price feed is wired in — replace THIS TABLE, nothing else.

Stdlib only. Mock-universe safety: mock Hoovers codes are numeric (no "GRP_"
prefix), so anchor_for returns None and the calibrated mock backtest is
untouched.
"""

# sector group -> (EV/EBITDA anchor, EV/Sales anchor).  EV/EBIT = EV/EBITDA x 1.25
# (typical D&A ~20% of EBITDA for Indian industrials).
#
# VINTAGE: July-2026. Re-calibrated against OBSERVED NSE market caps pulled on
# 2026-07-17 for companies present in this database (FY2025 financials):
#   MINING  — 20 Microns:      implied EV/EBITDA ≈ 9.0x   (anchor set 9.0)
#   MACH    — Kirloskar 38.8x · Roto 21.9x · Mahindra EPC 15.6x · Shakti 12.1x
#             → sector median ≈ 18x                        (anchor set 18.0)
#   CHEM    — Aarti Industries: ≈ 17x                      (anchor set 16.0)
# Unobserved sectors are scaled ~1.25-1.35x from the Jan-2025 base, consistent
# with the broad 2025-26 Indian mid/small-cap re-rating those points evidence.
# THIS TABLE IS THE CALIBRATION SURFACE: refresh it on a schedule (or replace
# with a derived monthly vintage from a price feed) — see PRODUCTION_REVIEW 1.7.
SECTOR_ANCHORS = {
    "AUTO":      (14.0, 1.8),   # auto components / OEM ancillaries
    "PHARMA":    (17.0, 3.6),
    "CHEM":      (16.0, 2.6),   # observed (Aarti)
    "METAL":     (9.0,  1.3),
    "TEXTILE":   (9.5,  1.1),
    "BUILDMAT":  (14.0, 2.0),
    "MACH":      (18.0, 2.5),   # observed (pumps/engineering cluster)
    "ELEC":      (17.0, 2.0),
    "FOOD":      (14.0, 1.6),
    "POLYPAPER": (10.0, 1.2),
    "MINING":    (9.0,  1.9),   # observed (20 Microns)
    "ENERGY":    (8.5,  1.3),
    "CONSTR":    (11.0, 1.6),
    "LOGIST":    (12.0, 1.7),
    "MEDIA":     (11.0, 2.0),
    "HOTEL":     (15.0, 3.1),
    "SERVICES":  (14.0, 2.2),
    "IT":        (17.0, 3.2),
    "RETAIL":    (17.0, 1.5),
    "TRADE":     (11.0, 0.6),
    # "FIN" deliberately absent: EV multiples are not meaningful for financials;
    # those stay on the disclosed book basis until a P/B method is added.
}

_EBIT_UPLIFT = 1.25

# Size factor on the anchor — published sector aggregates skew to larger caps.
# Softened in the Jul-2026 vintage: the observed small caps in the calibration
# set (Roto ₹240 Cr revenue at 21.9x, Mahindra EPC ₹312 Cr at 15.6x) showed no
# small-cap multiple discount in the current market.
_SIZE_FACTORS = ((100.0, 0.90), (500.0, 0.95))   # revenue < threshold -> factor


def size_factor(revenue_cr):
    for threshold, f in _SIZE_FACTORS:
        if (revenue_cr or 0) < threshold:
            return f
    return 1.00


def anchor_for(hoovers_code, method, revenue_cr=None):
    """Sector trading anchor for a method, size-adjusted. None when the sector
    is unknown/unanchored (caller then stays on the disclosed book basis)."""
    if not hoovers_code or not str(hoovers_code).startswith("GRP_"):
        return None
    group = str(hoovers_code)[4:]
    pair = SECTOR_ANCHORS.get(group)
    if pair is None:
        return None
    ev_ebitda, ev_sales = pair
    base = {"EV/EBITDA": ev_ebitda,
            "EV/Revenue": ev_sales,
            "EV/EBIT": ev_ebitda * _EBIT_UPLIFT}.get(method)
    if base is None:
        return None
    return round(base * size_factor(revenue_cr), 3)


def describe(hoovers_code, revenue_cr=None):
    """Human note for reports/audit: which anchor set applies and why."""
    if not hoovers_code or not str(hoovers_code).startswith("GRP_"):
        return None
    group = str(hoovers_code)[4:]
    if group not in SECTOR_ANCHORS:
        return None
    sf = size_factor(revenue_cr)
    ev, sales = SECTOR_ANCHORS[group]
    return (f"sector '{group}' trading anchors: EV/EBITDA {ev}x · EV/Sales {sales}x "
            f"· EV/EBIT {round(ev*_EBIT_UPLIFT,1)}x, size factor ×{sf} "
            f"(Jul-2026 vintage, calibrated to observed NSE sector points)")
