"""
core/pipeline.py — deterministic comparable-company discovery + multi-method
valuation for Indian MSMEs, grounded on the Dun & Bradstreet response schema.

NO LLM calls. NO network. NO non-stdlib imports. This module must NOT import from
`mock_api` — the D&B client is injected by the orchestrator (run.py).

Money: D&B returns INR in Thousand. Normalization converts every monetary field
to INR Crore (÷10,000). All downstream math is in Crore.
"""

from dataclasses import dataclass, field, asdict
from math import log1p, log10
from typing import Optional, List, Dict, Any

from .calibration import anchor_for as _sector_anchor, describe as _sector_describe


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Company:
    duns: str
    name: str
    cin: Optional[str]
    # industry
    naics: Optional[str]
    naics_desc: Optional[str]
    hoovers: Optional[str]
    major_industry: Optional[str]          # code, e.g. "D"
    major_industry_desc: Optional[str]
    activities: str
    is_exporter: bool
    employees: Optional[int]
    incorporated: Optional[str]
    city: Optional[str]
    listed: bool
    # financials (INR Crore)
    revenue_cr: Optional[float]
    revenue_prior_cr: Optional[float]
    revenue_growth: Optional[float]
    ebitda_cr: Optional[float]
    ebitda_margin: Optional[float]
    depreciation_cr: Optional[float]
    ebit_cr: Optional[float]
    gross_profit_cr: Optional[float]
    operating_profit_cr: Optional[float]
    pat_cr: Optional[float]
    net_income_cr: Optional[float]
    cash_cr: float
    debt_cr: float
    capital_employed_cr: Optional[float]
    net_worth_cr: Optional[float]
    total_assets_cr: Optional[float]
    working_capital_cr: Optional[float]
    market_cap_cr: Optional[float] = None  # market capitalisation (listed only)
    market_ev_cr: Optional[float] = None   # market enterprise value (listed only)
    listing_status: str = "unlisted"       # "listed" | "unlisted"
    debt_known: bool = True                # False when the source omitted borrowings
    cash_known: bool = True                # False when the source omitted cash
    directors: List[str] = field(default_factory=list)


@dataclass
class EconomicProfile:
    operating_model: str      # manufacturer | distributor | retailer | service | unknown
    value_chain: str          # finished_goods | raw_material
    customer_type: str        # B2B | B2C | mixed
    naics_subsector: Optional[str]   # first 3 digits of primary NAICS
    major_industry: Optional[str]
    confidence: float


@dataclass
class DataQuality:
    score: float
    grade: str                       # A | B | C | D
    checks: List[Dict[str, Any]]     # {field, status, level, detail}
    missing_fields: List[str]
    valuable: bool                   # is there enough to attempt a valuation at all?


@dataclass
class Valuation:
    headline_method: str
    methods: List[Dict[str, Any]]
    net_debt_cr: Optional[float]           # None = unknown, equity withheld
    discount: float
    discount_reason: str
    equity_low_cr: Optional[float]
    equity_mid_cr: Optional[float]
    equity_high_cr: Optional[float]
    peers_used: List[Dict[str, Any]]
    ev_basis: str
    quality_percentile: Optional[float] = None      # target margin rank within peers
    positioning: Optional[str] = None               # human note on how the multiple was set
    market_cross_check: Optional[Dict[str, Any]] = None  # listed target: comps vs own mkt cap
    effective_peer_count: Optional[float] = None    # similarity-weighted peer count
    n_borderline: int = 0                           # peers below the strong-match line
    comparability_adjustment: Optional[Dict[str, Any]] = None  # scale-mismatch penalty
    transaction_analysis: Optional[Dict[str, Any]] = None      # indicative M&A view
    equity_requires: List[str] = field(default_factory=list)   # figures needed for equity
    notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Small numeric helpers (stdlib only)
# ---------------------------------------------------------------------------

def _to_cr(thousand):
    """Convert an INR-Thousand value to INR Crore (÷10,000). Null-safe."""
    if thousand is None:
        return None
    return round(thousand / 10000.0, 4)


def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _percentile(sorted_vals, p):
    """Linear-interpolation percentile. `sorted_vals` must be sorted ascending."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _pct_rank(value, sorted_vals):
    """Percentile rank of `value` within `sorted_vals` (fraction at-or-below, 0..1)."""
    if not sorted_vals or value is None:
        return 0.5
    below = sum(1 for x in sorted_vals if x < value)
    equal = sum(1 for x in sorted_vals if x == value)
    return (below + 0.5 * equal) / len(sorted_vals)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _split_industry_codes(codes):
    """Split D&B industryCodes[] into (naics, naics_desc, hoovers, major, major_desc)."""
    naics = naics_desc = hoovers = major = major_desc = None
    for c in codes or []:
        td = (c.get("typeDescription") or "")
        if td == "North American Industry Classification System 2022":
            if naics is None:  # first NAICS is primary
                naics = c.get("code")
                naics_desc = c.get("description")
        elif td == "D&B Hoovers Industry Classification":
            if hoovers is None:
                hoovers = c.get("code")
        elif td == "D&B Standard Major Industry Code":
            if major is None:
                major = c.get("code")
                major_desc = c.get("description")
    return naics, naics_desc, hoovers, major, major_desc


def _cin_from_reg(reg_numbers):
    for r in reg_numbers or []:
        if (r.get("typeDescription") or "") == "CIN":
            return r.get("registrationNumber")
    return None


def normalize_company(info_resp, fin_resp, mgmt_resp=None) -> Company:
    """
    Flatten company_information + company_financials into a Company (Crore).
    All monetary fields are converted ÷10,000. Handles null fields gracefully.
    """
    org = (info_resp or {}).get("data", {}).get("organization", {}) or {}
    duns = org.get("duns")
    name = org.get("primaryName")
    naics, naics_desc, hoovers, major, major_desc = _split_industry_codes(
        org.get("industryCodes"))
    activities = " ".join(
        (a.get("description") or "") for a in (org.get("activities") or []))
    is_exporter = bool(org.get("isExporter"))
    emp_list = org.get("numberOfEmployees") or []
    employees = emp_list[0].get("value") if emp_list else None
    incorporated = org.get("incorporatedDate")
    addr = org.get("primaryAddress") or {}
    city = (addr.get("addressLocality") or {}).get("name")
    cin = _cin_from_reg(org.get("registrationNumbers"))
    listed = bool(cin and cin[:1].upper() == "L")

    fin_org = (fin_resp or {}).get("data", {}).get("organization", {}) or {}
    lff = fin_org.get("latestFiscalFinancials") or {}
    ov = lff.get("overview") or {}

    revenue_cr = _to_cr(ov.get("salesRevenue"))
    ebitda_cr = _to_cr(ov.get("ebitda"))
    depreciation_cr = _to_cr(ov.get("depreciation"))
    gross_profit_cr = _to_cr(ov.get("grossProfit"))
    operating_profit_cr = _to_cr(ov.get("operatingProfit"))
    pat_cr = _to_cr(ov.get("profitAfterTax"))
    net_income_cr = _to_cr(ov.get("netIncome"))
    cash_cr = _to_cr(ov.get("cashAndLiquidAssets"))
    debt_cr = _to_cr(ov.get("longTermDebt"))
    capital_employed_cr = _to_cr(ov.get("capitalEmployed"))
    net_worth_cr = _to_cr(ov.get("netWorth"))
    total_assets_cr = _to_cr(ov.get("totalAssets"))
    working_capital_cr = _to_cr(ov.get("workingCapital"))

    # ebit = ebitda - depreciation (null-safe)
    if ebitda_cr is not None and depreciation_cr is not None:
        ebit_cr = round(ebitda_cr - depreciation_cr, 4)
    else:
        ebit_cr = None

    # market data (listed companies only) -> real enterprise value
    market = fin_org.get("marketData") or {}
    market_cap_cr = _to_cr(market.get("marketCapitalization"))
    market_ev_cr = _to_cr(market.get("enterpriseValue"))
    # trust CIN listing flag AND presence of market data
    is_listed = bool(listed or market.get("isPubliclyTraded"))
    listing_status = "listed" if is_listed else "unlisted"

    # prior-year revenue from otherFinancials[1]
    other = fin_org.get("otherFinancials") or []
    revenue_prior_cr = None
    if len(other) > 1:
        revenue_prior_cr = _to_cr(other[1].get("salesRevenue"))

    revenue_growth = None
    if revenue_cr is not None and revenue_prior_cr not in (None, 0):
        revenue_growth = round((revenue_cr - revenue_prior_cr) / revenue_prior_cr, 4)

    ebitda_margin = None
    if ebitda_cr is not None and revenue_cr not in (None, 0):
        ebitda_margin = round(ebitda_cr / revenue_cr, 4)

    directors = []
    if mgmt_resp:
        m_org = (mgmt_resp or {}).get("data", {}).get("organization", {}) or {}
        directors = [p.get("fullName") for p in (m_org.get("currentPrincipals") or [])]

    return Company(
        duns=duns, name=name, cin=cin,
        naics=naics, naics_desc=naics_desc, hoovers=hoovers,
        major_industry=major, major_industry_desc=major_desc,
        activities=activities, is_exporter=is_exporter, employees=employees,
        incorporated=incorporated, city=city, listed=is_listed,
        revenue_cr=revenue_cr, revenue_prior_cr=revenue_prior_cr,
        revenue_growth=revenue_growth, ebitda_cr=ebitda_cr, ebitda_margin=ebitda_margin,
        depreciation_cr=depreciation_cr, ebit_cr=ebit_cr,
        gross_profit_cr=gross_profit_cr, operating_profit_cr=operating_profit_cr,
        pat_cr=pat_cr, net_income_cr=net_income_cr,
        cash_cr=(cash_cr if cash_cr is not None else 0.0),
        debt_cr=(debt_cr if debt_cr is not None else 0.0),
        capital_employed_cr=capital_employed_cr, net_worth_cr=net_worth_cr,
        total_assets_cr=total_assets_cr, working_capital_cr=working_capital_cr,
        market_cap_cr=market_cap_cr, market_ev_cr=market_ev_cr,
        listing_status=listing_status,
        debt_known=(debt_cr is not None), cash_known=(cash_cr is not None),
        directors=directors,
    )


# ---------------------------------------------------------------------------
# Economic-profile classifier (rule-based; LLM-swap seam)
# ---------------------------------------------------------------------------

# NOTE: This is the seam where a future LLM economic-profile classifier could
# drop in. It must remain a pure function (company -> EconomicProfile) so the LLM
# variant is a black-box swap. Until then it is DELIBERATELY rule-based — no LLM.

_MFG_VERBS = ("manufactures", "manufacturer of", "produces", "producing",
              "manufacturing of")
_DISTRIBUTOR_KW = ("wholesale distributor", "distributor of", "sourced from manufacturers",
                   "resold to", "wholesale trader", "wholesale of")
_RETAIL_KW = ("retail store", "stores selling", "direct to consumer", "retail")
_SERVICE_KW = ("consulting", "design services", "provides engineering consulting")
_RAW_KW = ("raw material", "billets", "supplied to downstream")


def build_profile(company: Company) -> EconomicProfile:
    text = (company.activities or "").lower()

    # COLLISION RULE (critical): a manufacturing VERB — not the bare noun
    # "manufacturers" — triggers the manufacturer model. This stops distributor
    # text like "sourced from manufacturers" from being read as a manufacturer.
    has_mfg_verb = any(v in text for v in _MFG_VERBS)

    major_is_mfg = (company.major_industry or "").upper() == "D"

    # ---- operating model (priority order) -------------------------------
    if any(k in text for k in _DISTRIBUTOR_KW) and not has_mfg_verb:
        operating_model = "distributor"
    elif any(k in text for k in _RETAIL_KW) and not has_mfg_verb:
        operating_model = "retailer"
    elif any(k in text for k in _SERVICE_KW) and not has_mfg_verb:
        operating_model = "service"
    elif has_mfg_verb or major_is_mfg:
        operating_model = "manufacturer"
    else:
        operating_model = "unknown"

    # ---- value chain (independent axis) ---------------------------------
    if any(k in text for k in _RAW_KW):
        value_chain = "raw_material"
    else:
        value_chain = "finished_goods"

    # ---- customer type --------------------------------------------------
    b2c_signals = ("consumer", "retail store", "stores selling", "direct to consumer")
    b2b_signals = ("oem", "industrial", "process", "utility", "assemblers",
                   "downstream", "infrastructure", "marine", "irrigation")
    if any(s in text for s in b2c_signals):
        customer_type = "B2C"
    elif any(s in text for s in b2b_signals):
        customer_type = "B2B"
    else:
        customer_type = "mixed"

    naics_subsector = (company.naics or "")[:3] or None

    # ---- confidence -----------------------------------------------------
    conf = 0.5
    if operating_model in ("manufacturer", "distributor", "retailer", "service"):
        conf = 0.75
    if has_mfg_verb and operating_model == "manufacturer":
        conf = 0.90
    if any(k in text for k in _DISTRIBUTOR_KW) and operating_model == "distributor":
        conf = 0.90
    if naics_subsector:
        conf = min(1.0, conf + 0.05)

    return EconomicProfile(
        operating_model=operating_model,
        value_chain=value_chain,
        customer_type=customer_type,
        naics_subsector=naics_subsector,
        major_industry=company.major_industry,
        confidence=round(conf, 3),
    )


# ---------------------------------------------------------------------------
# Data-quality validation gate
# ---------------------------------------------------------------------------

def validate_company(company: Company, audit=None) -> DataQuality:
    """
    Explicit data-quality gate run BEFORE valuation. Produces a graded score, a
    per-field check list, and a `valuable` flag (is there enough to value at all?).
    Every failing/at-risk check is logged to the audit trail.

    Weights reflect materiality: revenue and a positive value driver matter most;
    identity fields are informational.
    """
    checks = []
    missing = []
    penalty = 0.0

    def add(field_name, ok, level, detail, weight=0.0):
        nonlocal penalty
        status = "pass" if ok else ("warn" if level == "WARN" else "fail")
        checks.append({"field": field_name, "status": status,
                       "level": level, "detail": detail})
        if not ok:
            if field_name not in ("prior_revenue",):
                missing.append(field_name)
            penalty += weight
            if audit is not None:
                audit.warn("validate", "DATA_QUALITY_" + status.upper(),
                           f"{field_name}: {detail}",
                           {"field": field_name, "level": level})

    rev_ok = (company.revenue_cr or 0) > 0
    add("revenue", rev_ok, "CRITICAL",
        "sales revenue present and > 0" if rev_ok else "missing sales revenue", 0.50)

    ebitda_ok = (company.ebitda_cr or 0) > 0
    add("ebitda", ebitda_ok, "HIGH",
        "EBITDA present and > 0" if ebitda_ok else "EBITDA missing/non-positive", 0.15)

    ebit_ok = company.ebit_cr is not None
    add("ebit", ebit_ok, "MEDIUM",
        "EBIT computable" if ebit_ok else "EBIT not computable (depreciation missing)",
        0.05)

    ce_ok = (company.capital_employed_cr or 0) > 0
    add("capital_employed", ce_ok, "HIGH",
        "capital employed present (book EV proxy available)" if ce_ok
        else "capital employed missing (book EV proxy unavailable)", 0.15)

    prior_ok = company.revenue_prior_cr is not None
    add("prior_revenue", prior_ok, "LOW",
        "prior-year revenue present (growth computable)" if prior_ok
        else "no prior-year revenue (growth unavailable)", 0.03)

    margin_ok = company.ebitda_margin is None or (0.0 < company.ebitda_margin < 0.60)
    add("ebitda_margin", margin_ok, "MEDIUM",
        "EBITDA margin within plausible band" if margin_ok
        else f"implausible EBITDA margin ({company.ebitda_margin})", 0.07)

    naics_ok = bool(company.naics)
    add("naics", naics_ok, "MEDIUM",
        "NAICS industry code present" if naics_ok else "no NAICS code (industry match weakened)",
        0.05)

    cin_ok = bool(company.cin)
    add("cin", cin_ok, "LOW",
        "CIN present" if cin_ok else "no CIN on record", 0.01)

    add("debt", company.debt_known, "MEDIUM",
        "borrowings present" if company.debt_known
        else "borrowings absent from source (net debt assumes 0)", 0.04)

    add("cash", company.cash_known, "LOW",
        "cash & liquid assets present" if company.cash_known
        else "cash absent from source (net debt assumes 0)", 0.03)

    score = max(0.0, round(1.0 - penalty, 3))
    if score >= 0.85:
        grade = "A"
    elif score >= 0.70:
        grade = "B"
    elif score >= 0.50:
        grade = "C"
    else:
        grade = "D"

    # "valuable" = at minimum revenue + a usable EV proxy exist.
    valuable = rev_ok and (ce_ok or ebitda_ok)

    if audit is not None:
        audit.info("validate", "DATA_QUALITY_GRADE",
                   f"data-quality grade {grade} (score {score}), valuable={valuable}",
                   {"grade": grade, "score": score, "valuable": valuable,
                    "missing": missing})

    return DataQuality(score=score, grade=grade, checks=checks,
                       missing_fields=missing, valuable=valuable)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _similarity(target: Company, tprof: EconomicProfile,
                peer: Company, pprof: EconomicProfile):
    """Return (score, components dict, selected_because[], differences[])."""
    comp = {}
    because = []
    diffs = []

    # industry 0.40
    if tprof.naics_subsector and pprof.naics_subsector and \
            tprof.naics_subsector == pprof.naics_subsector:
        ind = 1.0
        because.append(f"same NAICS subsector {tprof.naics_subsector}")
    elif target.hoovers and peer.hoovers and target.hoovers == peer.hoovers:
        ind = 0.6
        because.append("same D&B Hoovers industry")
    else:
        ind = 0.0
        diffs.append(f"NAICS {peer.naics} vs {target.naics}")
    comp["industry"] = round(ind * 0.40, 4)

    # scale 0.20
    rt = target.revenue_cr or 0.0
    rp = peer.revenue_cr or 0.0
    scale = 1.0 / (1.0 + abs(log1p(rt) - log1p(rp)))
    comp["scale"] = round(scale * 0.20, 4)
    if scale > 0.8:
        because.append("comparable revenue scale")
    else:
        diffs.append(f"revenue ₹{rp:.0f} Cr vs ₹{rt:.0f} Cr")

    # margin 0.15
    mt = target.ebitda_margin
    mp = peer.ebitda_margin
    if mt is not None and mp is not None:
        margin = max(0.0, 1.0 - 5.0 * abs(mt - mp))
    else:
        margin = 0.0
    comp["margin"] = round(margin * 0.15, 4)
    if margin > 0.7:
        because.append("similar EBITDA margin")

    # customer 0.15
    cust = 1.0 if tprof.customer_type == pprof.customer_type else 0.0
    comp["customer"] = round(cust * 0.15, 4)
    if cust == 1.0:
        because.append(f"same customer type ({tprof.customer_type})")
    else:
        diffs.append(f"customer {pprof.customer_type} vs {tprof.customer_type}")

    # export 0.10
    exp = 1.0 if target.is_exporter == peer.is_exporter else 0.3
    comp["export"] = round(exp * 0.10, 4)
    if target.is_exporter == peer.is_exporter:
        because.append("same export profile")

    score = round(sum(comp.values()), 4)
    return score, comp, because, diffs


def discover_peers(target: Company, tprofile: EconomicProfile, universe, audit=None):
    """
    Filter mismatches, then score survivors.
    universe: list of (Company, EconomicProfile) tuples (target excluded upstream).
    Returns (ranked, rejected). ranked is sorted descending by score.
    """
    ranked = []
    rejected = []

    for peer, pprof in universe:
        if peer.duns == target.duns:
            continue

        # ---- mismatch filter (reject BEFORE scoring) --------------------
        reason = None
        if pprof.operating_model != tprofile.operating_model:
            reason = (f"operating_model mismatch "
                      f"({pprof.operating_model} vs {tprofile.operating_model})")
        elif pprof.value_chain != tprofile.value_chain:
            reason = (f"value_chain mismatch "
                      f"({pprof.value_chain} vs {tprofile.value_chain})")
        elif (peer.major_industry or "") != (target.major_industry or ""):
            reason = (f"major_industry mismatch "
                      f"({peer.major_industry} vs {target.major_industry})")

        if reason:
            rejected.append({
                "duns": peer.duns, "name": peer.name, "reason": reason,
                "operating_model": pprof.operating_model,
                "value_chain": pprof.value_chain,
                "major_industry": peer.major_industry,
            })
            # On a large real universe thousands of candidates are rejected — logging
            # each one would drown the audit trail, so detail the first 20 and roll
            # the rest into one summary DECISION (counts by reason kind, below).
            if audit is not None and len(rejected) <= 20:
                audit.decision("discover", "PEER_REJECTED",
                               f"reject {peer.name}: {reason}",
                               {"duns": peer.duns, "reason": reason})
            continue

        score, comp, because, diffs = _similarity(target, tprofile, peer, pprof)
        ranked.append({
            "company": peer,
            "profile": pprof,
            "score": score,
            "components": comp,
            "selected_because": because,
            "differences": diffs,
        })

    ranked.sort(key=lambda r: r["score"], reverse=True)
    if audit is not None:
        if len(rejected) > 20:
            by_kind = {}
            for r in rejected:
                kind = r["reason"].split(" mismatch")[0]
                by_kind[kind] = by_kind.get(kind, 0) + 1
            audit.decision("discover", "PEERS_REJECTED_SUMMARY",
                           f"{len(rejected)} candidates rejected in total "
                           f"(first 20 detailed above); by check: " +
                           ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())),
                           {"total": len(rejected), "by_check": by_kind})
        audit.info("discover", "PEERS_RANKED",
                   f"{len(ranked)} peers passed the mismatch filter and were ranked; "
                   f"{len(rejected)} rejected",
                   {"ranked": len(ranked), "rejected": len(rejected)})
    return ranked, rejected


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------

def _ev_of(company: Company):
    """EV = market_ev_cr if set else capital_employed_cr (book EV proxy)."""
    if company.market_ev_cr is not None and company.market_ev_cr > 0:
        return company.market_ev_cr, "market"
    if company.capital_employed_cr is not None and company.capital_employed_cr > 0:
        return company.capital_employed_cr, "book"
    return None, None


def _tukey_trim(values):
    """Drop Tukey outliers (1.5*IQR). Return (kept, n_dropped). Skip if <4."""
    if len(values) < 4:
        return list(values), 0
    s = sorted(values)
    q1 = _percentile(s, 0.25)
    q3 = _percentile(s, 0.75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    kept = [v for v in s if lo <= v <= hi]
    return kept, len(s) - len(kept)


# --- similarity weighting of peer multiples --------------------------------
# The peer set is rarely all "exact" matches. Rather than treat a borderline
# comparable (just outside the ideal range on scale / margin / industry) the same
# as a perfect one, its multiple is DOWN-WEIGHTED in proportion to how far its
# similarity score sits below the "full match" line. This keeps a thin or weak peer
# set from distorting the headline number — the answer leans on the closest comps.
# The taper is linear between two anchors:
#     score >= 0.85  -> full weight 1.0        (an exact comparable)
#     score  = 0.625 -> ~0.5                    (only half counts)
#     score <= 0.40  -> floor 0.15              (barely counts, but still informs)
_FULL_MATCH = 0.85
_MIN_MATCH = 0.40
_WEIGHT_FLOOR = 0.15


def _match_weight(score):
    if score is None:
        return _WEIGHT_FLOOR
    frac = (score - _MIN_MATCH) / (_FULL_MATCH - _MIN_MATCH)
    return max(_WEIGHT_FLOOR, min(1.0, frac))


def _tukey_trim_pairs(pairs):
    """Tukey-trim a list of (value, weight) pairs on the VALUE. Skip if <4."""
    if len(pairs) < 4:
        return list(pairs), 0
    vals = sorted(v for v, _ in pairs)
    q1 = _percentile(vals, 0.25)
    q3 = _percentile(vals, 0.75)
    iqr = q3 - q1
    lo = q1 - 1.5 * iqr
    hi = q3 + 1.5 * iqr
    kept = [(v, w) for (v, w) in pairs if lo <= v <= hi]
    return kept, len(pairs) - len(kept)


def _weighted_percentile(pairs, p):
    """
    Weighted percentile of (value, weight) pairs (linear interpolation on the
    cumulative-weight midpoints). Falls back to the unweighted percentile if all
    weights are zero. `p` in [0,1].
    """
    if not pairs:
        return None
    s = sorted(pairs, key=lambda t: t[0])
    vals = [v for v, _ in s]
    ws = [max(0.0, w) for _, w in s]
    total = sum(ws)
    if total <= 0:
        return _percentile(vals, p)
    if len(vals) == 1:
        return vals[0]
    # cumulative-weight midpoint for each value, normalized to [0,1]
    cum = []
    running = 0.0
    for w in ws:
        cum.append((running + w / 2.0) / total)
        running += w
    if p <= cum[0]:
        return vals[0]
    if p >= cum[-1]:
        return vals[-1]
    for i in range(1, len(cum)):
        if p <= cum[i]:
            lo_c, hi_c = cum[i - 1], cum[i]
            frac = (p - lo_c) / (hi_c - lo_c) if hi_c > lo_c else 0.0
            return vals[i - 1] + (vals[i] - vals[i - 1]) * frac
    return vals[-1]


_METHOD_SPEC = [
    ("EV/EBITDA", "ebitda_cr"),
    ("EV/Revenue", "revenue_cr"),
    ("EV/EBIT", "ebit_cr"),
]

# Positioning shrinkage on NON-MARKET bases. The margin->multiple relationship
# that justifies full-strength quality positioning is verified only where peer
# multiples are observed market prices (backtest corr ~0.49). On book/sector-
# calibrated bases that link is unverified, and real-market spot checks
# (Jul-2026: Kirloskar priced at a premium DESPITE low margin, Shakti at a
# discount DESPITE high margin) show it can point the wrong way. So on those
# bases the position moves only halfway from the median toward the margin rank
# — a shrinkage estimator: keep the signal, halve its leverage.
_POSITION_SHRINK_NONMARKET = 0.5


def compute_valuation(target: Company, peers, top_n=15, audit=None,
                      txn_multiple=None) -> Valuation:
    used = peers[:top_n]
    notes = []
    warnings = []
    n_used = len(used)

    # --- Quality positioning -------------------------------------------
    # Applying the flat peer *median* multiple to every target is inaccurate: a
    # below-median-quality company should trade below the median, an above-median
    # one above it. We position the target by its EBITDA-margin percentile within
    # the peer set and read the peer multiple at THAT percentile as the central
    # (mid) estimate — this is what closes the gap to a listed target's own market
    # value. The low/high band is a window around that position (±20pts, ±30 when
    # the peer set is thin) reflecting comp dispersion.
    peer_margins = sorted(p["company"].ebitda_margin for p in used
                          if p["company"].ebitda_margin is not None)
    q_rank = _pct_rank(target.ebitda_margin, peer_margins)
    # Effective peer count = sum of similarity weights. A set padded with borderline
    # comps has a low effective count even if nominally 15 — so we widen the range on
    # the EFFECTIVE count, not the raw one. Not enough *exact* peers => wider range.
    effective_peer_count = round(sum(_match_weight(r["score"]) for r in used), 2)
    n_borderline = sum(1 for r in used if r["score"] < _FULL_MATCH)
    thin = effective_peer_count < 10 or n_used < 10
    window = 0.30 if thin else 0.20
    mid_q = _clamp(q_rank, 0.15, 0.85)
    low_q = _clamp(mid_q - window, 0.05, 0.95)
    high_q = _clamp(mid_q + window, 0.05, 0.95)
    range_basis = (f"positioned at margin P{int(round(mid_q*100))} "
                   f"(band P{int(round(low_q*100))}–P{int(round(high_q*100))})")
    _tm = "n/a" if target.ebitda_margin is None else f"{target.ebitda_margin*100:.1f}%"
    positioning = (
        f"target EBITDA margin {_tm} ranks at the "
        f"{int(round(q_rank*100))}th percentile of the peer set → central multiple "
        f"taken at peer P{int(round(mid_q*100))} (not the flat median).")
    if audit is not None:
        audit.decision("value", "QUALITY_POSITIONED", positioning,
                       {"q_rank": round(q_rank, 3), "mid_q": round(mid_q, 3),
                        "window": window})
        if thin:
            audit.decision("value", "RANGE_WIDENED",
                           f"effective peer count {effective_peer_count} (of {n_used}): "
                           f"widening positioning band to ±30pts",
                           {"n_peers": n_used,
                            "effective_peer_count": effective_peer_count, "window": window})

    # --- Comparability (scale-mismatch) adjustment -----------------------
    # The similarity weights already down-weight borderline comps, but when the
    # target's SIZE falls outside the scale band its peers actually span, even a
    # weighted multiple is not fully transferable: size-premium studies show
    # smaller companies systematically trade at lower multiples than larger
    # peers (higher required returns), and vice-versa. So if the target's
    # revenue is below the smallest used peer (with 20% tolerance) the positioned
    # multiples are marked DOWN 7.5% per log10 decade of gap (capped at 15%);
    # above the largest peer (25% tolerance) they are marked UP 5%/decade
    # (capped at 10%). Inside the peer scale band → no adjustment. Always
    # disclosed and audited; this is the explicit penalty for "no exact-size
    # peer exists", separate from (and additive to) similarity weighting.
    peer_revs = [r["company"].revenue_cr for r in used
                 if (r["company"].revenue_cr or 0) > 0]
    comp_adj = {"applied": False, "direction": "none", "pct": 0.0,
                "target_revenue_cr": target.revenue_cr,
                "peer_revenue_min_cr": round(min(peer_revs), 2) if peer_revs else None,
                "peer_revenue_max_cr": round(max(peer_revs), 2) if peer_revs else None,
                "reason": "target size within the peer scale band — no adjustment"}
    scale_factor = 1.0
    if peer_revs and (target.revenue_cr or 0) > 0:
        lo_band, hi_band = min(peer_revs) * 0.8, max(peer_revs) * 1.25
        if target.revenue_cr < lo_band:
            gap_decades = log10(lo_band / target.revenue_cr)
            adj = -min(0.15, 0.075 * gap_decades)
            scale_factor = 1.0 + adj
            comp_adj.update(applied=True, direction="down", pct=round(adj * 100, 1),
                            reason=(f"target revenue ₹{target.revenue_cr:,.0f} Cr is below "
                                    f"the smallest used peer (₹{min(peer_revs):,.0f} Cr): "
                                    f"size-premium penalty {adj*100:.1f}% on all multiples"))
        elif target.revenue_cr > hi_band:
            gap_decades = log10(target.revenue_cr / hi_band)
            adj = min(0.10, 0.05 * gap_decades)
            scale_factor = 1.0 + adj
            comp_adj.update(applied=True, direction="up", pct=round(adj * 100, 1),
                            reason=(f"target revenue ₹{target.revenue_cr:,.0f} Cr exceeds "
                                    f"the largest used peer (₹{max(peer_revs):,.0f} Cr): "
                                    f"scale uplift +{adj*100:.1f}% on all multiples"))
    if comp_adj["applied"]:
        warnings.append(f"comparability adjustment: {comp_adj['reason']}")
        if audit is not None:
            audit.decision("value", "COMPARABILITY_ADJUSTMENT",
                           comp_adj["reason"], comp_adj)

    # Net debt bridges enterprise value to equity value. NO-ASSUMPTION RULE:
    # if borrowings or cash are unknown, the engine does NOT assume 0 and does
    # NOT publish an equity number — it reports the enterprise-value range and
    # lists exactly which figures are required (fillable via the chat /
    # enrichment popup). Nothing is ever imputed.
    net_debt_known = bool(target.debt_known and target.cash_known)
    if net_debt_known:
        net_debt = round((target.debt_cr or 0.0) - (target.cash_cr or 0.0), 4)
        equity_requires = []
    else:
        net_debt = None
        equity_requires = ([] if target.debt_known else ["borrowings"]) + \
                          ([] if target.cash_known else ["cash"])
        warnings.append(
            "equity value withheld — " + " & ".join(equity_requires) + " not in the "
            "data source and nothing is assumed. The enterprise-value range is "
            "reported; supply the missing figures (chat / enrichment) to bridge "
            "EV → equity.")
        if audit is not None:
            audit.warn("value", "NET_DEBT_UNKNOWN",
                       "borrowings/cash absent from source; equity WITHHELD "
                       "(no zero-assumption) — EV range reported instead",
                       {"debt_known": target.debt_known,
                        "cash_known": target.cash_known,
                        "equity_requires": equity_requires})
    # DLOM — Discount for Lack of Marketability — applies ONLY to a PRIVATE target,
    # because it is priced off *listed* peers whose equity is liquid. A listed target
    # is itself liquid, so no DLOM. For private targets the DLOM is size-scaled.
    rev = target.revenue_cr or 0.0
    if target.listed:
        discount, dreason = 0.0, "no DLOM — target is listed/public (liquid equity)"
    elif rev < 100:
        discount, dreason = 0.30, "DLOM 30% — micro/small private co. (revenue < ₹100 Cr)"
    elif rev < 500:
        discount, dreason = 0.25, "DLOM 25% — small-mid private co. (revenue ₹100–500 Cr)"
    else:
        discount, dreason = 0.20, "DLOM 20% — established private co. (revenue ≥ ₹500 Cr)"
    if audit is not None:
        audit.decision("value", "DISCOUNT_APPLIED", f"applied {dreason}",
                       {"discount": discount, "revenue_cr": rev, "listed": target.listed})

    # Collect multiples in TWO pools:
    #   market_multiples — from LISTED comps priced off observed market EV (primary)
    #   book_multiples   — from ALL comps priced off book capital employed (fallback)
    # A trading multiple is only meaningful off a market enterprise value, so the
    # market pool is preferred; the book pool is a documented last resort used per
    # method only when there are fewer than 3 listed comps for that metric.
    # each pool holds (multiple, weight) pairs — weight = similarity match weight
    market_multiples = {name: [] for name, _ in _METHOD_SPEC}
    book_multiples = {name: [] for name, _ in _METHOD_SPEC}
    peers_used_out = []
    n_market = 0
    n_book = 0
    for r in used:
        p = r["company"]
        weight = _match_weight(r["score"])       # effective count computed above
        borderline = r["score"] < _FULL_MATCH
        mkt_ev = p.market_ev_cr if (p.market_ev_cr and p.market_ev_cr > 0) else None
        book_ev = p.capital_employed_cr if (p.capital_employed_cr and
                                            p.capital_employed_cr > 0) else None
        peer_basis = "market" if mkt_ev else ("book" if book_ev else "none")
        if peer_basis == "market":
            n_market += 1
        elif peer_basis == "book":
            n_book += 1
        pm = {}      # multiples shown for this peer (on its own best basis)
        for mname, driver in _METHOD_SPEC:
            dv = getattr(p, driver)
            if dv is None or dv <= 0:
                continue
            if mkt_ev:
                market_multiples[mname].append((mkt_ev / dv, weight))
                pm[mname] = round(mkt_ev / dv, 4)
            if book_ev:
                book_multiples[mname].append((book_ev / dv, weight))
                if not mkt_ev:
                    pm[mname] = round(book_ev / dv, 4)
        peers_used_out.append({
            "duns": p.duns, "name": p.name, "score": r["score"],
            "weight": round(weight, 3), "borderline": borderline,
            "listed": p.listed, "city": p.city,
            "revenue_cr": p.revenue_cr, "ebitda_margin": p.ebitda_margin,
            "revenue_growth": p.revenue_growth,
            "ev_basis": peer_basis,
            "market_ev_cr": mkt_ev, "market_cap_cr": p.market_cap_cr,
            "book_ev_cr": book_ev,
            "multiples": pm,
            "components": r["components"],
            "selected_because": r["selected_because"],
            "differences": r["differences"],
        })
    if audit is not None and n_borderline:
        audit.decision("value", "PEERS_WEIGHTED",
                       f"{n_borderline}/{len(used)} peers are borderline (similarity < "
                       f"{_FULL_MATCH}); multiples similarity-weighted — effective "
                       f"peer count {effective_peer_count} of {len(used)}",
                       {"n_borderline": n_borderline, "n_used": len(used),
                        "effective_peer_count": effective_peer_count})

    ev_basis = (f"Primary basis: market enterprise value (market cap + net debt) of "
                f"{n_market} LISTED comparables. Book capital-employed proxy "
                f"({n_book} unlisted comps) is used only as a per-method fallback when "
                f"fewer than 3 listed comps exist for a metric. Peer multiples are "
                f"similarity-weighted (borderline comps count less).")

    # compute each method
    methods = []
    target_drivers = {
        "EV/EBITDA": target.ebitda_cr,
        "EV/Revenue": target.revenue_cr,
        "EV/EBIT": target.ebit_cr,
    }

    calibrated_any = None
    for mname, _driver in _METHOD_SPEC:
        # market-primary, book-fallback selection of the peer multiple set
        if len(market_multiples[mname]) >= 3:
            raw = market_multiples[mname]          # list of (multiple, weight)
            method_basis = "market"
        else:
            raw = book_multiples[mname]
            method_basis = "book"
            if audit is not None and raw:
                audit.decision("value", "FALLBACK_BOOK_EV",
                               f"{mname}: <3 listed comps; falling back to book "
                               f"capital-employed multiples",
                               {"method": mname,
                                "n_market": len(market_multiples[mname])})
            # SECTOR CALIBRATION (see core/calibration.py): book capital employed
            # is not enterprise value — its "multiples" sit 2–4x below trading
            # levels. Re-level the peer distribution so its weighted median hits
            # the sector's trading anchor; shape/dispersion/weights (and thus the
            # target's quality positioning) are preserved exactly.
            anchor = _sector_anchor(target.hoovers, mname, target.revenue_cr)
            if anchor and raw:
                med = _weighted_percentile(raw, 0.5)
                if med and med > 0:
                    factor = _clamp(anchor / med, 0.25, 25.0)
                    raw = [(v * factor, w) for v, w in raw]
                    method_basis = "sector-calibrated"
                    calibrated_any = _sector_describe(target.hoovers,
                                                      target.revenue_cr)
                    if audit is not None:
                        audit.decision(
                            "value", "SECTOR_CALIBRATED",
                            f"{mname}: book multiples re-levelled ×{factor:.2f} so "
                            f"the peer median meets the sector trading anchor "
                            f"{anchor}x (book median was {med:.2f}x)",
                            {"method": mname, "anchor": anchor,
                             "book_median": round(med, 3),
                             "factor": round(factor, 3)})
        kept, dropped = _tukey_trim_pairs(raw)
        tdriver = target_drivers[mname]
        if tdriver is None or tdriver <= 0:
            if len(kept) >= 3:
                notes.append(f"{mname} skipped: target driver not positive")
                if audit is not None:
                    audit.warn("value", "METHOD_SKIPPED_DRIVER",
                               f"{mname} skipped: target driver not positive",
                               {"method": mname, "driver": tdriver})
            continue
        if len(kept) < 3:
            notes.append(f"{mname} skipped: only {len(kept)} peer multiples after trimming")
            if audit is not None:
                audit.warn("value", "METHOD_SKIPPED_THIN",
                           f"{mname} skipped: only {len(kept)} multiples after trimming",
                           {"method": mname, "n_multiples": len(kept)})
            continue
        eff_n = round(sum(w for _, w in kept), 2)          # effective (weighted) count
        # similarity-weighted positioned multiple + band; shrink the position
        # toward the median on non-market bases (see _POSITION_SHRINK_NONMARKET)
        if method_basis != "market":
            m_mid_q = 0.5 + _POSITION_SHRINK_NONMARKET * (mid_q - 0.5)
            # calibrated bases always use the WIDE band: observed within-sector
            # dispersion (2026 spot checks: 12x-60x inside one group) far exceeds
            # what a +/-20pt band can honestly cover
            w2 = max(window, 0.30)
            m_low_q = _clamp(m_mid_q - w2, 0.05, 0.95)
            m_high_q = _clamp(m_mid_q + w2, 0.05, 0.95)
            shrunk = True
        else:
            m_mid_q, m_low_q, m_high_q = mid_q, low_q, high_q
            shrunk = False
        p_mid = _weighted_percentile(kept, m_mid_q)
        p_low = _weighted_percentile(kept, m_low_q)
        p_high = _weighted_percentile(kept, m_high_q)
        if shrunk and audit is not None:
            audit.info("value", "POSITION_SHRUNK",
                       f"{mname}: non-market basis — margin position P"
                       f"{int(round(mid_q*100))} shrunk halfway to median → "
                       f"P{int(round(m_mid_q*100))}",
                       {"method": mname, "raw_q": round(mid_q, 3),
                        "shrunk_q": round(m_mid_q, 3)})
        # strict ordering guard (flat/degenerate distributions)
        if not (p_low < p_mid < p_high):
            p_low = _weighted_percentile(kept, 0.25)
            p_mid = _weighted_percentile(kept, 0.5)
            p_high = _weighted_percentile(kept, 0.75)
            if not (p_low < p_mid < p_high):
                ks = sorted(v for v, _ in kept)
                lo_v, hi_v = ks[0], ks[-1]
                p_low, p_mid, p_high = lo_v, (lo_v + hi_v) / 2.0, hi_v
        # scale-mismatch penalty/uplift (same positive factor → ordering preserved)
        p_low, p_mid, p_high = (p_low * scale_factor, p_mid * scale_factor,
                                p_high * scale_factor)

        def equity(mult):
            ev_x = mult * tdriver
            if not net_debt_known:
                return None, round(ev_x, 4)          # equity withheld, EV reported
            return round((ev_x - net_debt) * (1.0 - discount), 4), round(ev_x, 4)

        eq_low, ev_low = equity(p_low)
        eq_mid, ev_mid = equity(p_mid)
        eq_high, ev_high = equity(p_high)
        methods.append({
            "method": mname,
            "target_driver": round(tdriver, 4),
            "ev_basis": method_basis,
            "n_peers": len(raw),
            "n_multiples": len(kept),
            "effective_n": eff_n,
            "n_outliers_dropped": dropped,
            "range_basis": range_basis,
            "multiple_p25": round(p_low, 4),
            "multiple_median": round(p_mid, 4),
            "multiple_p75": round(p_high, 4),
            "ev_low_cr": ev_low, "ev_mid_cr": ev_mid, "ev_high_cr": ev_high,
            "equity_low_cr": eq_low, "equity_mid_cr": eq_mid, "equity_high_cr": eq_high,
        })
        if audit is not None:
            audit.info("value", "METHOD_COMPUTED",
                       f"{mname}: positioned {p_mid:.2f}x on {len(kept)} {method_basis} "
                       f"multiples (effective {eff_n}, {dropped} outliers dropped)",
                       {"method": mname, "multiple": round(p_mid, 4),
                        "ev_basis": method_basis, "effective_n": eff_n,
                        "n_multiples": len(kept), "n_outliers_dropped": dropped})

    if calibrated_any:
        note = (f"multiples are SECTOR-CALIBRATED — peer book distributions "
                f"re-levelled to published trading anchors ({calibrated_any}); "
                f"replace the anchor table with a live market feed for exact levels")
        notes.append(note)
        ev_basis += " " + note.capitalize() + "."

    # headline selection: EV/EBITDA -> EV/Revenue -> EV/EBIT
    order = ["EV/EBITDA", "EV/Revenue", "EV/EBIT"]
    computed = {m["method"]: m for m in methods}
    headline_method = "none"
    for cand in order:
        if cand in computed:
            headline_method = cand
            break
    if audit is not None and headline_method != "none":
        if headline_method == order[0]:
            audit.decision("value", "HEADLINE_SELECTED",
                           f"headline = {headline_method} (preferred method available)",
                           {"headline": headline_method})
        else:
            audit.decision("value", "FALLBACK_HEADLINE",
                           f"headline fell back to {headline_method}: "
                           f"preferred method(s) unavailable",
                           {"headline": headline_method,
                            "computed": list(computed.keys())})

    # warnings
    if (target.ebitda_cr or 0) <= 0:
        warnings.append("target EBITDA ≤ 0: headline falls back past EV/EBITDA")
    if len(used) < 10:
        warnings.append(f"only {len(used)} peers used: ranges widened, treat with caution")
    if headline_method == "none":
        warnings.append("no method computable: valuation is 'none'")
        if audit is not None:
            audit.error("value", "NO_METHOD",
                        "no valuation method computable; returning 'none'")
        return Valuation(
            headline_method="none", methods=methods, net_debt_cr=net_debt,
            discount=discount, discount_reason=dreason,
            equity_low_cr=None, equity_mid_cr=None, equity_high_cr=None,
            peers_used=peers_used_out, ev_basis=ev_basis,
            quality_percentile=round(q_rank, 3), positioning=positioning,
            effective_peer_count=effective_peer_count, n_borderline=n_borderline,
            comparability_adjustment=comp_adj, equity_requires=equity_requires,
            notes=notes, warnings=warnings,
        )

    h = computed[headline_method]

    # --- Comparable-transactions view (indicative, honestly derived) ------
    # The data source has no M&A transaction database, so observed precedent-
    # transaction multiples are unavailable. What CAN be stated defensibly:
    # acquisitions of CONTROL price above minority trading value — empirical
    # control-premium studies cluster around 20–30%. We therefore publish an
    # indicative acquisition range = comps equity × (1 + premium band), clearly
    # labelled as derived. When a transaction database is added, replace this
    # with observed deal multiples (the block's shape stays the same).
    if txn_multiple and (target.ebitda_cr or 0) > 0:
        # OBSERVED comparable transaction (user-provided, optional intake): the
        # deal's EV/EBITDA is applied directly — an observed multiple always
        # outranks the derived control-premium view. ±10% band for deal noise.
        ev_acq = txn_multiple * target.ebitda_cr
        transaction_analysis = {
            "basis": "observed comparable transaction (user-provided EV/EBITDA "
                     "applied to the target's EBITDA)",
            "txn_multiple": round(float(txn_multiple), 2),
            "acquisition_ev_low_cr": round(ev_acq * 0.90, 2),
            "acquisition_ev_mid_cr": round(ev_acq, 2),
            "acquisition_ev_high_cr": round(ev_acq * 1.10, 2),
            "caveat": "Based on ONE user-reported transaction — verify the deal's "
                      "terms and comparability; a transactions database would "
                      "replace this with a multiple distribution.",
        }
        if net_debt_known:
            transaction_analysis["acquisition_equity_mid_cr"] = round(
                ev_acq - net_debt, 2)
        if audit is not None:
            audit.decision("value", "TRANSACTION_OBSERVED",
                           f"user-provided comparable transaction at "
                           f"{txn_multiple:.1f}x EV/EBITDA → acquisition EV "
                           f"₹{ev_acq:,.0f} Cr (outranks the derived premium view)",
                           {"txn_multiple": txn_multiple})
    elif h["equity_mid_cr"] is not None:
        transaction_analysis = {
            "basis": "derived: trading-comps equity × control premium (no precedent-"
                     "transaction database in the current data source)",
            "control_premium_low": 0.20, "control_premium_mid": 0.25,
            "control_premium_high": 0.30,
            "acquisition_equity_low_cr": round(h["equity_low_cr"] * 1.20, 2),
            "acquisition_equity_mid_cr": round(h["equity_mid_cr"] * 1.25, 2),
            "acquisition_equity_high_cr": round(h["equity_high_cr"] * 1.30, 2),
            "caveat": "Indicative only — what a CONTROL buyer might pay relative to "
                      "the minority trading value above. Replace with observed "
                      "precedent-transaction multiples when a deal database is "
                      "integrated.",
        }
        if audit is not None:
            audit.info("value", "TRANSACTION_VIEW",
                       f"indicative acquisition range "
                       f"₹{transaction_analysis['acquisition_equity_low_cr']:,.0f}"
                       f"–{transaction_analysis['acquisition_equity_mid_cr']:,.0f}"
                       f"–{transaction_analysis['acquisition_equity_high_cr']:,.0f} Cr "
                       f"(control premium 20–30% over comps equity)",
                       {"premium_band": [0.20, 0.30]})
    else:
        # equity withheld -> no acquisition view is fabricated either
        transaction_analysis = None

    # Market cross-check — the strongest accuracy signal available. When the target
    # is itself listed we can compare our comps-derived equity to its OWN observed
    # market capitalisation. A small delta means the engine is well-calibrated.
    market_cross_check = None
    if target.listed and target.market_cap_cr and target.market_cap_cr > 0:
        own = target.market_cap_cr
        delta = h["equity_mid_cr"] / own - 1.0
        market_cross_check = {
            "own_market_cap_cr": round(own, 2),
            "own_ev_ebitda": (round(target.market_ev_cr / target.ebitda_cr, 2)
                              if target.ebitda_cr else None),
            "comps_mid_equity_cr": h["equity_mid_cr"],
            "delta_pct": round(delta * 100, 1),
            "within_25pct": abs(delta) <= 0.25,
        }
        if audit is not None:
            audit.info("value", "MARKET_CROSSCHECK",
                       f"comps mid equity ₹{h['equity_mid_cr']:.0f} Cr vs own market cap "
                       f"₹{own:.0f} Cr ({delta*100:+.1f}%)", market_cross_check)

    return Valuation(
        headline_method=headline_method, methods=methods, net_debt_cr=net_debt,
        discount=discount, discount_reason=dreason,
        equity_low_cr=h["equity_low_cr"], equity_mid_cr=h["equity_mid_cr"],
        equity_high_cr=h["equity_high_cr"],
        peers_used=peers_used_out, ev_basis=ev_basis,
        quality_percentile=round(q_rank, 3), positioning=positioning,
        market_cross_check=market_cross_check,
        effective_peer_count=effective_peer_count, n_borderline=n_borderline,
        comparability_adjustment=comp_adj,
        transaction_analysis=transaction_analysis,
        equity_requires=equity_requires,
        notes=notes, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def company_to_dict(c: Company) -> dict:
    return asdict(c)


def profile_to_dict(p: EconomicProfile) -> dict:
    return asdict(p)


def valuation_to_dict(v: Valuation) -> dict:
    return asdict(v)


def dataquality_to_dict(d: DataQuality) -> dict:
    return asdict(d)
