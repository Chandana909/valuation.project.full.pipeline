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
SECTOR_ANCHORS = {
    "AUTO":      (11.0, 1.4),   # auto components / OEM ancillaries
    "PHARMA":    (14.0, 3.0),
    "CHEM":      (12.0, 2.0),
    "METAL":     (7.0,  1.0),
    "TEXTILE":   (8.0,  0.9),
    "BUILDMAT":  (11.0, 1.6),
    "MACH":      (13.0, 1.8),   # engineering / capital goods
    "ELEC":      (13.0, 1.5),
    "FOOD":      (12.0, 1.3),
    "POLYPAPER": (8.5,  1.0),
    "MINING":    (6.5,  1.4),
    "ENERGY":    (7.0,  1.1),
    "CONSTR":    (9.0,  1.3),
    "LOGIST":    (10.0, 1.4),
    "MEDIA":     (10.0, 1.8),
    "HOTEL":     (12.0, 2.5),
    "SERVICES":  (12.0, 1.8),
    "IT":        (15.0, 2.8),
    "RETAIL":    (14.0, 1.2),
    "TRADE":     (9.0,  0.5),
    # "FIN" deliberately absent: EV multiples are not meaningful for financials;
    # those stay on the disclosed book basis until a P/B method is added.
}

_EBIT_UPLIFT = 1.25

# Size factor on the anchor — published sector aggregates skew to larger caps;
# small companies trade at a documented multiple discount.
_SIZE_FACTORS = ((100.0, 0.80), (500.0, 0.90))   # revenue < threshold -> factor


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
            f"(India sector aggregates, Jan-2025 calibration defaults)")
