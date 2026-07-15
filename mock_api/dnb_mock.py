"""
MockDnBClient — returns the REAL Dun & Bradstreet `dnbhoovers` response schema.

This is a drop-in stand-in for the live D&B CorpIntel API. It serves the exact
nested response shape (`data.organization....`, `data.matchCandidates[]....`) so
that the ONLY change required to go live is swapping `request()` for a single
`httpx.post` (see the LIVE SWAP note in `MockDnBClient.request`).

All monetary values are emitted in **INR Thousand** (D&B convention). The core
pipeline converts to INR Crore (÷10,000) at the normalization boundary.

The universe is deliberately LARGE (59 entities) so that any main-cluster target
returns a genuine 15-20 peer set — never padded:

  * Cluster A (18) — engine / turbine controls & precision engine equipment (NAICS 333611)
  * Cluster B (18) — auto components / motor-vehicle parts (NAICS 336390)
  * Cluster C (18) — industrial pumps, valves & fluid machinery (NAICS 333914 / 332911)
  * 5 "wrong" entities the economic filter MUST reject
        - 2 wholesale distributors
        - 1 raw-material supplier (steel billets)
        - 1 retailer
        - 1 services firm
"""

import random

# ---------------------------------------------------------------------------
# Cluster definitions
# ---------------------------------------------------------------------------

_CLUSTER_NAMES = {
    "A": [
        "Woodward India Controls", "Wartsila Turbine Systems", "Cummins Governor Controls",
        "Bharat Turbine Controls", "Kirloskar Engine Controls", "Greaves Governor Systems",
        "Triveni Turbine Controls", "Thermax Turbine Equipment", "BHEL Turbine Controls",
        "Shakti Engine Governors", "Precision Turbine Works", "Apex Engine Controls",
        "Nucon Governor Systems", "Vidyut Turbine Controls", "Sanghvi Engine Equipment",
        "Meher Turbine Systems", "Deccan Engine Controls", "Elgi Governor Controls",
    ],
    "B": [
        "Bharat Forge Components", "Rane Auto Parts", "Sundram Fasteners Auto",
        "Motherson Auto Systems", "Bosch Auto Components", "Wabco Brake Systems",
        "Sona Steering Components", "Amtek Auto Parts", "Endurance Auto Systems",
        "Gabriel Suspension Parts", "Munjal Auto Components", "JBM Auto Systems",
        "Minda Auto Parts", "Lumax Auto Components", "Fiem Auto Systems",
        "Subros Auto Parts", "Jamna Auto Components", "Pricol Auto Systems",
    ],
    "C": [
        "Kirloskar Brothers Pumps", "KSB Pumps India", "Grundfos Fluid Systems",
        "Shakti Pumps Industrial", "Roto Pumps Machinery", "WPIL Fluid Machinery",
        "CRI Pumps Industrial", "Texmo Pumps Machinery", "Aqua Pump Industries",
        "Darling Pumps Machinery", "Kirloskar Ebara Pumps", "Mather Platt Fluid",
        "Flowserve Valve Systems", "Audco Valves India", "Forbes Marshall Valves",
        "Leader Valves Industrial", "IVC Valves Machinery", "Jash Fluid Systems",
    ],
}

_CLUSTER_CITIES = {
    "A": ["Pune", "Chennai", "Bengaluru", "Hyderabad", "Nashik", "Coimbatore"],
    "B": ["Pune", "Gurugram", "Chennai", "Aurangabad", "Manesar", "Pantnagar"],
    "C": ["Pune", "Coimbatore", "Ahmedabad", "Kolkata", "Ghaziabad", "Vadodara"],
}

# NAICS: A -> 333611 (turbine sets); B -> 336390 (motor-vehicle parts);
# C -> 333914 (pumps) for first 12, 332911 (valves) for last 6.
_CLUSTER_NAICS = {
    "A": ("333611", "Turbine and Turbine Generator Set Units Manufacturing"),
    "B": ("336390", "Other Motor Vehicle Parts Manufacturing"),
}
_C_PUMP = ("333914", "Measuring, Dispensing, and Other Pumping Equipment Manufacturing")
_C_VALVE = ("332911", "Industrial Valve Manufacturing")

_CLUSTER_HOOVERS = {
    "A": ("31111100", "Turbine & Power Transmission Equipment"),
    "B": ("33999900", "Motor Vehicle Parts Manufacturing"),
    "C": ("31122200", "Pumps, Valves & Fluid Machinery"),
}

_CLUSTER_ACTIVITY = {
    "A": ("manufactures precision engine governors and turbine control systems "
          "for power generation, marine and industrial OEMs"),
    "B": ("manufactures motor vehicle parts and auto components supplied to "
          "automotive OEMs and tier-1 assemblers"),
    "C": ("manufactures industrial pumps, valves and fluid handling machinery "
          "for process, utility and irrigation industries"),
}

_STATE_CODE = {"A": "MH", "B": "MH", "C": "TZ"}

# Sector trading-multiple bands (EV/EBITDA) used to synthesize REALISTIC market
# enterprise values for LISTED comparables. These reflect how each sub-sector
# actually trades on Indian exchanges: precision engineering / turbine controls
# and pumps command premium multiples; auto components trade lower.
#   Enterprise value of a listed peer = EBITDA x (a multiple drawn from its band),
#   and its market capitalisation = enterprise value - net debt.
# This is what makes the derived EV/EBITDA, EV/Revenue and EV/EBIT multiples true
# *trading* multiples rather than meaningless book ratios.
_CLUSTER_EV_EBITDA = {
    "A": (12.0, 18.0),   # engine / turbine controls, precision engineering
    "B": (8.0, 14.0),    # auto components / motor-vehicle parts
    "C": (14.0, 22.0),   # industrial pumps, valves & fluid machinery
}
_DEFAULT_EV_EBITDA = (9.0, 16.0)


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _market_valuation(fin, listed, band, rng):
    """
    Synthesize a REALISTIC market-based enterprise value for a LISTED company.

    The trading multiple is DRIVEN BY FUNDAMENTALS, as in real markets: higher EBITDA
    margin and higher revenue growth command a higher EV/EBITDA within the sector band.
    Without this, a company's multiple would be independent of its quality and any
    "positioning by margin" in the valuation engine would be pure luck. A residual noise
    term keeps it from being a perfect function (real multiples reflect factors we do not
    model), so calibration is good but never suspiciously exact.

        quality  = 0.65 * margin_rank + 0.35 * growth_rank            (0..1)
        fraction = clamp(quality + noise, 0, 1)                        (noise +/-0.12)
        EV/EBITDA = band_low + (band_high - band_low) * fraction
        EV        = EBITDA * EV/EBITDA
        mkt cap   = EV - net debt   (net debt = long-term debt - cash)

    Returns (market_ev_cr, market_cap_cr, ev_ebitda_multiple) or (None, None, None)
    for unlisted companies (no observable market value; excluded from trading multiples).
    """
    if not listed or (fin.get("ebitda") or 0) <= 0:
        return None, None, None
    lo, hi = band
    # normalize fundamentals within their generation ranges (see _make_financials_cr)
    m_rank = _clamp01((fin["margin"] - 0.12) / (0.19 - 0.12))
    g_rank = _clamp01((fin["growth"] - (-0.05)) / (0.28 - (-0.05)))
    quality = 0.65 * m_rank + 0.35 * g_rank
    noise = rng.uniform(-0.12, 0.12)
    fraction = _clamp01(quality + noise)
    mult = lo + (hi - lo) * fraction
    net_debt = fin["debt"] - fin["cash"]
    market_ev = fin["ebitda"] * mult
    market_cap = market_ev - net_debt
    return round(market_ev, 2), round(market_cap, 2), round(mult, 2)

# ---------------------------------------------------------------------------
# The 5 "wrong" entities (must be rejected by the economic filter)
# ---------------------------------------------------------------------------

_WRONG_ENTITIES = [
    {
        "name": "Metro Wholesale Distributors",
        "activity": ("wholesale distributor of industrial equipment sourced from "
                     "manufacturers and resold to retailers, dealers and contractors"),
        "major": ("F", "Wholesale Trade"),
        "naics": ("423830", "Industrial Machinery and Equipment Merchant Wholesalers"),
        "hoovers": ("42383000", "Industrial Equipment Wholesale"),
        "city": "Delhi", "state": "DL", "listed": False,
    },
    {
        "name": "Prime Industrial Traders",
        "activity": ("wholesale trader and distributor of pumps and valves sourced "
                     "from manufacturers, resold to industrial dealers"),
        "major": ("F", "Wholesale Trade"),
        "naics": ("423840", "Industrial Supplies Merchant Wholesalers"),
        "hoovers": ("42384000", "Industrial Supplies Wholesale"),
        "city": "Mumbai", "state": "MH", "listed": False,
    },
    {
        "name": "Sterling Steel Billets",
        "activity": ("manufacturer of steel billets and raw material supplied to "
                     "downstream forging, casting and rolling units"),
        "major": ("D", "Manufacturing"),
        "naics": ("331110", "Iron and Steel Mills and Ferroalloy Manufacturing"),
        "hoovers": ("33111000", "Iron & Steel Mills"),
        "city": "Raipur", "state": "CT", "listed": True,
    },
    {
        "name": "UrbanMart Retail Stores",
        "activity": ("operates retail stores selling home appliances and consumer "
                     "electronics direct to consumer"),
        "major": ("G", "Retail Trade"),
        "naics": ("443141", "Household Appliance Stores"),
        "hoovers": ("44314100", "Appliance Retail"),
        "city": "Bengaluru", "state": "KA", "listed": True,
    },
    {
        "name": "Insight Engineering Consulting",
        "activity": ("provides engineering consulting and design services to "
                     "industrial and infrastructure clients"),
        "major": ("I", "Services"),
        "naics": ("541330", "Engineering Services"),
        "hoovers": ("54133000", "Engineering Services"),
        "city": "Hyderabad", "state": "TG", "listed": False,
    },
]


# ---------------------------------------------------------------------------
# Universe generation (seeded -> deterministic)
# ---------------------------------------------------------------------------

def _cin(listed: bool, state: str, year: int, seq: int) -> str:
    """Build a realistic Indian CIN. First char L=listed, U=unlisted."""
    head = "L" if listed else "U"
    return f"{head}29100{state}{year}PLC{seq:06d}"


def _overview_thousand(fin_cr: dict) -> dict:
    """Convert a Crore financial dict into the D&B overview block (INR Thousand)."""
    def th(x):
        return None if x is None else round(x * 10000.0, 2)
    return {
        "salesRevenue": th(fin_cr["revenue"]),
        "ebitda": th(fin_cr["ebitda"]),
        "grossProfit": th(fin_cr["gross_profit"]),
        "operatingProfit": th(fin_cr["operating_profit"]),
        "profitAfterTax": th(fin_cr["pat"]),
        "netIncome": th(fin_cr["pat"]),
        "profitBeforeTaxes": th(fin_cr["pbt"]),
        "costOfSales": th(fin_cr["cost_of_sales"]),
        "cashAndLiquidAssets": th(fin_cr["cash"]),
        "longTermDebt": th(fin_cr["debt"]),
        "capitalEmployed": th(fin_cr["capital_employed"]),
        "netWorth": th(fin_cr["net_worth"]),
        "tangibleFixedAssets": th(fin_cr["tangible_fixed_assets"]),
        "depreciation": th(fin_cr["depreciation"]),
        "interestExpense": th(fin_cr["interest_expense"]),
        "totalAssets": th(fin_cr["total_assets"]),
        "accountsReceivable": th(fin_cr["accounts_receivable"]),
        "accountsPayable": th(fin_cr["accounts_payable"]),
        "inventory": th(fin_cr["inventory"]),
        "workingCapital": th(fin_cr["working_capital"]),
        "intangibleAssets": th(fin_cr["intangible_assets"]),
    }


def _make_financials_cr(rng: random.Random) -> dict:
    """Generate one realistic financial profile in INR Crore."""
    revenue = rng.uniform(150.0, 10000.0)
    margin = rng.uniform(0.12, 0.19)
    growth = rng.uniform(-0.05, 0.28)
    ebitda = revenue * margin
    depreciation = revenue * rng.uniform(0.03, 0.06)
    ebit = ebitda - depreciation
    interest = 0.0  # filled after debt known
    prior = revenue / (1.0 + growth)
    capital_employed = revenue * rng.uniform(0.80, 1.55)
    debt = revenue * rng.uniform(0.05, 0.40)
    cash = revenue * rng.uniform(0.03, 0.15)
    interest = debt * 0.09
    pbt = ebit - interest
    pat = pbt * 0.75
    fin = {
        "revenue": round(revenue, 2),
        "prior_revenue": round(prior, 2),
        "growth": round(growth, 4),
        "margin": round(margin, 4),
        "ebitda": round(ebitda, 2),
        "depreciation": round(depreciation, 2),
        "ebit": round(ebit, 2),
        "interest_expense": round(interest, 2),
        "pbt": round(pbt, 2),
        "pat": round(pat, 2),
        "gross_profit": round(revenue * 0.35, 2),
        "operating_profit": round(ebit, 2),
        "cost_of_sales": round(revenue * 0.65, 2),
        "cash": round(cash, 2),
        "debt": round(debt, 2),
        "capital_employed": round(capital_employed, 2),
        "net_worth": round(capital_employed * 0.60, 2),
        "tangible_fixed_assets": round(revenue * 0.50, 2),
        "total_assets": round(capital_employed * 1.30, 2),
        "accounts_receivable": round(revenue * 0.18, 2),
        "accounts_payable": round(revenue * 0.14, 2),
        "inventory": round(revenue * 0.16, 2),
        "working_capital": round(revenue * 0.20, 2),
        "intangible_assets": round(revenue * 0.04, 2),
    }
    return fin


def _build_universe(seed_offset: int = 0) -> dict:
    """Return {duns: record}. Deterministic via seeded RNG. `seed_offset` shifts
    every seed — used ONLY by validate.py's robustness sweep to prove results are
    not seed-luck. Default 0 keeps the canonical universe byte-identical."""
    universe = {}
    seq = 1

    for cluster in ("A", "B", "C"):
        rng = random.Random({"A": 101, "B": 202, "C": 303}[cluster] + seed_offset)
        names = _CLUSTER_NAMES[cluster]
        cities = _CLUSTER_CITIES[cluster]
        hoovers = _CLUSTER_HOOVERS[cluster]
        state = _STATE_CODE[cluster]
        for i, name in enumerate(names):
            if cluster == "C":
                if i < 12:
                    naics = _C_PUMP
                    activity = _CLUSTER_ACTIVITY["C"]
                else:
                    naics = _C_VALVE
                    activity = ("manufactures industrial valves, actuators and flow "
                                "control equipment for process and utility industries")
            else:
                naics = _CLUSTER_NAICS[cluster]
                activity = _CLUSTER_ACTIVITY[cluster]

            fin_cr = _make_financials_cr(rng)
            # ~60% listed so any peer set has a healthy number of trading comps.
            listed = rng.random() < 0.60
            year = rng.randint(1985, 2012)
            duns = f"IN{cluster}{seq:04d}0000"
            cin = _cin(listed, state, year, seq)
            market_ev_cr, market_cap_cr, ev_mult = _market_valuation(
                fin_cr, listed, _CLUSTER_EV_EBITDA[cluster], rng)

            universe[duns] = {
                "duns": duns,
                "name": name,
                "cluster": cluster,
                "cin": cin,
                "listed": listed,
                "naics": naics,
                "hoovers": hoovers,
                "major": ("D", "Manufacturing"),
                "activity": activity,
                "is_exporter": rng.random() < 0.55,
                "employees": rng.randint(200, 12000),
                "incorporated": f"{year}-04-01",
                "city": rng.choice(cities),
                "state": state,
                "value_chain": "finished_goods",
                "market_ev_cr": market_ev_cr,
                "market_cap_cr": market_cap_cr,
                "ev_ebitda_mult": ev_mult,
                "fin_cr": fin_cr,
                "principals": [f"Director {name.split()[0]} {n}" for n in ("A", "B", "C")],
            }
            seq += 1

    # ---- the 5 wrong entities -------------------------------------------
    rng = random.Random(909 + seed_offset)
    for w in _WRONG_ENTITIES:
        fin_cr = _make_financials_cr(rng)
        year = rng.randint(1990, 2015)
        duns = f"INW{seq:04d}0000"
        cin = _cin(w["listed"], w["state"], year, seq)
        market_ev_cr, market_cap_cr, ev_mult = _market_valuation(
            fin_cr, w["listed"], _DEFAULT_EV_EBITDA, rng)
        universe[duns] = {
            "duns": duns,
            "name": w["name"],
            "cluster": "WRONG",
            "cin": cin,
            "listed": w["listed"],
            "naics": w["naics"],
            "hoovers": w["hoovers"],
            "major": w["major"],
            "activity": w["activity"],
            "is_exporter": False,
            "employees": rng.randint(50, 3000),
            "incorporated": f"{year}-04-01",
            "city": w["city"],
            "state": w["state"],
            "value_chain": "raw_material" if "billets" in w["activity"] else "finished_goods",
            "market_ev_cr": market_ev_cr,
            "market_cap_cr": market_cap_cr,
            "ev_ebitda_mult": ev_mult,
            "fin_cr": fin_cr,
            "principals": [f"Director {w['name'].split()[0]} {n}" for n in ("A", "B")],
        }
        seq += 1

    return universe


# Build once at import — deterministic.
_UNIVERSE = _build_universe()


_SEED_OFFSET = 0


def rebuild_universe(seed_offset: int = 0) -> None:
    """VALIDATION-ONLY helper: swap the module-level universe for one built with
    shifted seeds. validate.py uses this to re-run the calibration backtest on
    freshly drawn universes (seed-robustness). Always restore with
    rebuild_universe(0) afterwards — offset 0 is the canonical universe.
    _SEED_OFFSET feeds MockDnBClient.cache_key so run.py's universe cache is
    invalidated whenever the universe actually changes."""
    global _UNIVERSE, _SEED_OFFSET
    _UNIVERSE = _build_universe(seed_offset)
    _SEED_OFFSET = seed_offset


# ---------------------------------------------------------------------------
# Response-envelope builders (REAL D&B schema)
# ---------------------------------------------------------------------------

def _reg_numbers(rec):
    return [{"registrationNumber": rec["cin"], "typeDescription": "CIN"}]


def _industry_codes(rec):
    naics_code, naics_desc = rec["naics"]
    hoo_code, hoo_desc = rec["hoovers"]
    major_code, major_desc = rec["major"]
    return [
        {
            "typeDescription": "North American Industry Classification System 2022",
            "code": naics_code,
            "description": naics_desc,
        },
        {
            "typeDescription": "D&B Hoovers Industry Classification",
            "code": hoo_code,
            "description": hoo_desc,
        },
        {
            "typeDescription": "D&B Standard Major Industry Code",
            "code": major_code,
            "description": major_desc,
        },
    ]


def _company_information_response(rec):
    return {
        "data": {
            "organization": {
                "duns": rec["duns"],
                "primaryName": rec["name"],
                "industryCodes": _industry_codes(rec),
                "activities": [{"description": rec["activity"]}],
                "isExporter": rec["is_exporter"],
                "numberOfEmployees": [{"value": rec["employees"]}],
                "incorporatedDate": rec["incorporated"],
                "primaryAddress": {
                    "addressLocality": {"name": rec["city"]},
                    "addressRegion": {"abbreviatedName": rec["state"]},
                    "addressCountry": {"isoAlpha2Code": "IN"},
                },
                "registrationNumbers": _reg_numbers(rec),
                # non-standard convenience flag; NOT read by core normalization
                "isListed": rec["listed"],
            }
        }
    }


def _company_financials_response(rec):
    fin = rec["fin_cr"]
    overview = _overview_thousand(fin)
    # otherFinancials: index [0] latest, [1] prior year, then declining history.
    latest = fin["revenue"] * 10000.0
    prior = fin["prior_revenue"] * 10000.0
    prior2 = prior / (1.0 + max(0.02, fin["growth"] * 0.8))
    prior3 = prior2 / (1.0 + max(0.02, fin["growth"] * 0.6))
    other = [
        {"financialStatementToDate": "2024-03-31", "salesRevenue": round(latest, 2)},
        {"financialStatementToDate": "2023-03-31", "salesRevenue": round(prior, 2)},
        {"financialStatementToDate": "2022-03-31", "salesRevenue": round(prior2, 2)},
        {"financialStatementToDate": "2021-03-31", "salesRevenue": round(prior3, 2)},
    ]
    # Market data — present ONLY for listed companies (observable market value).
    # In production this is the D&B Hoovers public-company market module (or a
    # market-data feed such as NSE/BSE); the schema/units mirror the financials.
    market_data = None
    if rec.get("market_ev_cr") is not None:
        market_data = {
            "units": "Thousand",
            "currency": "INR",
            "asOfDate": "2024-03-31",
            "isPubliclyTraded": True,
            "marketCapitalization": round(rec["market_cap_cr"] * 10000.0, 2),
            "enterpriseValue": round(rec["market_ev_cr"] * 10000.0, 2),
        }
    return {
        "data": {
            "organization": {
                "duns": rec["duns"],
                "latestFiscalFinancials": {
                    "units": "Thousand",
                    "currency": "INR",
                    "financialStatementToDate": "2024-03-31",
                    "overview": overview,
                },
                "otherFinancials": other,
                "marketData": market_data,
            }
        }
    }


def _company_management_response(rec):
    return {
        "data": {
            "organization": {
                "duns": rec["duns"],
                "currentPrincipals": [{"fullName": p} for p in rec["principals"]],
            }
        }
    }


def _company_search_response(name):
    q = (name or "").strip().lower()
    candidates = []
    for rec in _UNIVERSE.values():
        pn = rec["name"].lower()
        if not q:
            continue
        if q == pn:
            conf = 10
        elif q in pn:
            conf = 8
        elif any(tok in pn for tok in q.split()):
            conf = 6
        else:
            continue
        candidates.append((conf, rec))
    candidates.sort(key=lambda t: (-t[0], t[1]["name"]))
    match_candidates = []
    for conf, rec in candidates:
        match_candidates.append({
            "organization": {
                "duns": rec["duns"],
                "primaryName": rec["name"],
                "registrationNumbers": _reg_numbers(rec),
                "primaryAddress": {
                    "addressCountry": {"isoAlpha2Code": "IN"},
                },
            },
            "matchQualityInformation": {"confidenceCode": conf},
        })
    return {"data": {"matchCandidates": match_candidates}}


def _empty_envelope():
    return {"data": {"organization": {}}}


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class MockDnBClient:
    """
    Mock Dun & Bradstreet `dnbhoovers` client.

    Returns the exact real D&B response schema for the four datasources used by
    the pipeline. Swapping this for the live API is a one-method change.

    LIVE SWAP — replace the body of `request()` with:

        import httpx
        resp = httpx.post(
            f"{base}/request/dnbhoovers",
            headers={"Authorization": f"Bearer {token}"},
            json={"body": body, "datasource": datasource, "no_cache": False},
            timeout=30,
        )
        return resp.json()

    Nothing else in the codebase changes: response paths are identical.
    """

    DATA_SOURCE_LABEL = "Synthetic mock universe (59 companies, real D&B schema)"

    def __init__(self, audit=None):
        self._audit = audit

    def set_audit(self, audit):
        self._audit = audit

    @property
    def cache_key(self):
        return f"mock:59:seed{_SEED_OFFSET}"

    def _log(self, datasource, detail, data=None):
        if self._audit is None:
            return
        # Prefer the structured audit API when available; fall back to the
        # legacy {source, detail} shape so this client stays framework-free.
        if hasattr(self._audit, "log"):
            self._audit.log("dnb", "INFO", "DNB_" + datasource.upper(), detail, data)
        else:
            self._audit.append({"source": f"dnb:{datasource}", "detail": detail})

    def request(self, datasource: str, body: dict) -> dict:
        body = body or {}
        if datasource == "company_search":
            name = body.get("name", "")
            self._log(datasource, f"search name='{name}' country={body.get('countryISOAlpha2Code')}")
            return _company_search_response(name)

        if datasource in ("company_information", "company_financials", "company_management"):
            duns = body.get("duns")
            rec = _UNIVERSE.get(duns)
            self._log(datasource, f"duns={duns} found={rec is not None}")
            if rec is None:
                return _empty_envelope()
            if datasource == "company_information":
                return _company_information_response(rec)
            if datasource == "company_financials":
                return _company_financials_response(rec)
            return _company_management_response(rec)

        # unknown datasource -> empty envelope of the same shape
        self._log(datasource, "unknown datasource")
        return _empty_envelope()

    def universe_duns(self) -> list:
        """
        All DUNS known to the mock universe.

        Prototype helper only. In production this list is built OFFLINE by paging
        `company_search` across the target sector and caching the DUNS set.
        """
        return list(_UNIVERSE.keys())
