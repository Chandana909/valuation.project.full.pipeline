"""
realdata/client.py — RealDnBClient: serves realdata.db (the ETL'd Accord extract of
42,951 real Indian companies) through the SAME request()/universe_duns() interface as
MockDnBClient, emitting the same D&B-schema envelopes.

Because the interface and envelope shape are identical, the entire core pipeline
(normalize -> profile -> discover -> value -> confidence -> audit) runs UNCHANGED on
real data. Stdlib only (sqlite3).

Honesty layer (critical):
  * The extract has NO market prices, NO borrowings, NO cash, NO current liabilities.
    -> marketData is never emitted (no fake trading multiples);
    -> longTermDebt / cashAndLiquidAssets are omitted (normalize marks them unknown);
    -> capitalEmployed = segment capital employed when reported, else NET WORTH as a
       disclosed approximation (book-equity floor of capital employed).
    These caveats are exposed via `source_caveats` and per-field `lineage()` so every
    number can be traced to its exact source file + row.

Industry mapping (the extract has no NAICS):
  * pseudo-NAICS: a stable 3-digit code per CD_Industry category (same category ->
    industry dimension 1.0);
  * Hoovers-level: a keyword-derived SECTOR GROUP (same group -> 0.6);
  * D&B major letter: derived from the group (D manufacturing, F trading, G retail,
    I services, U utilities, C construction, T transport, N finance) — the stage-B
    mismatch filter compares equality, so any consistent scheme works.
"""

import os
import sqlite3

DB_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "realdata.db")

# ---------------------------------------------------------------------------
# CD_Industry -> sector group (keyword rules, first match wins) and group -> major
# ---------------------------------------------------------------------------

_GROUP_RULES = [
    ("TRADE", ["dealers & distributors", "trading"]),
    ("RETAIL", ["retail"]),
    ("FIN", ["bank", "finance", "insurance", "broking", "investment", "nbfc",
             "financial", "asset management", "ratings"]),
    ("IT", ["it -", "software", "bpo", "ites", "e-commerce", "internet"]),
    ("AUTO", ["auto", "automobile", "tyres"]),
    ("PHARMA", ["pharma", "healthcare", "hospital", "diagnostics", "biotech"]),
    ("CHEM", ["chemical", "petrochem", "paints", "fertilizer", "pesticide",
              "agrochemical", "dyes", "gases"]),
    ("METAL", ["steel", "metal", "aluminium", "copper", "zinc", "iron", "castings",
               "forgings", "wire", "pipes", "tubes"]),
    ("TEXTILE", ["textile", "apparel", "garment", "yarn", "denim", "leather"]),
    ("BUILDMAT", ["cement", "ceramic", "glass", "granite", "marble", "refractor",
                  "tiles", "wood", "laminates"]),
    ("MACH", ["engineering", "machinery", "pumps", "compressor", "bearing",
              "abrasive", "tools", "electrode", "boiler", "fastener", "capital goods",
              "defence"]),
    ("ELEC", ["electronic", "electric", "cable", "transformer", "telecom equipment",
              "batteries", "air condition", "consumer durables", "appliances"]),
    ("FOOD", ["food", "fmcg", "sugar", "tea", "coffee", "dairy", "edible",
              "breweries", "beverage", "poultry", "marine", "agriculture", "aqua",
              "solvent", "tobacco", "cigarettes", "animal feed"]),
    ("POLYPAPER", ["paper", "plastic", "packaging", "rubber"]),
    ("MINING", ["mining", "mineral", "coal"]),
    ("ENERGY", ["power", "energy", "oil", "gas", "refiner", "solar", "petroleum",
                "lubricant"]),
    ("CONSTR", ["construction", "infrastructure", "realty", "real estate", "housing",
                "roads"]),
    ("LOGIST", ["logistics", "shipping", "transport", "courier", "port", "airline",
                "travel", "aviation"]),
    ("MEDIA", ["media", "entertainment", "advertis", "animation", "printing",
               "publishing", "film", "broadcast"]),
    ("HOTEL", ["hotel", "restaurant", "amusement", "recreation", "club", "gaming"]),
    ("SERVICES", ["business support", "consultancy", "education", "training",
                  "telecom", "services", "miscellaneous", "diversified"]),
]
_GROUP_MAJOR = {
    "TRADE": "F", "RETAIL": "G",
    "FIN": "N", "IT": "I", "MEDIA": "I", "HOTEL": "I", "SERVICES": "I",
    "LOGIST": "T", "CONSTR": "C", "ENERGY": "U",
    # manufacturing / industrial:
    "AUTO": "D", "PHARMA": "D", "CHEM": "D", "METAL": "D", "TEXTILE": "D",
    "BUILDMAT": "D", "MACH": "D", "ELEC": "D", "FOOD": "D", "POLYPAPER": "D",
    "MINING": "D",
}
_MAJOR_DESC = {"D": "Manufacturing", "F": "Wholesale Trade", "G": "Retail Trade",
               "I": "Services", "N": "Finance", "T": "Transport", "C": "Construction",
               "U": "Utilities/Energy"}


def _group_of(industry):
    low = (industry or "").lower()
    for grp, kws in _GROUP_RULES:
        if any(k in low for k in kws):
            return grp
    return "OTHER"


def _th(x):
    """Crore -> D&B Thousand convention (or None)."""
    return None if x is None else round(float(x) * 10000.0, 2)


class RealDnBClient:
    """D&B-schema adapter over realdata.db. Drop-in replacement for MockDnBClient."""

    DATA_SOURCE_LABEL = "Accord real extract (via realdata.db)"

    source_caveats = [
        "Borrowings, cash and current liabilities are not in the source extract: "
        "net debt is treated as 0 with a warning, and capital employed is "
        "approximated by segment capital-employed where reported, else Net Worth.",
        "Market prices are not in the source extract: no trading multiples — the "
        "valuation runs on the disclosed book basis until a market-price feed is added.",
    ]

    def __init__(self, db_path=DB_DEFAULT, audit=None):
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"{db_path} not found — run `python etl.py` first to build it "
                f"from the 9 Excel extracts.")
        self._audit = audit
        self._db_path = db_path
        con = sqlite3.connect(db_path)
        self._etl_ts = (con.execute(
            "SELECT value FROM meta WHERE key='etl_timestamp'").fetchone() or ["?"])[0]

        # ---- load companies ------------------------------------------------
        self._co = {}
        for a, name, ind, nic, desc, cin, isin, srow in con.execute(
                "SELECT accord,name,industry,nic,description,cin,isin,src_row "
                "FROM companies"):
            self._co[a] = {"accord": a, "name": name, "industry": ind or "",
                           "nic": nic or "", "desc": desc or "", "cin": cin,
                           "isin": isin, "src_row": srow}

        # ---- load financials: keep the two most recent 12-month years ------
        self._fin = {}
        rows = con.execute(
            "SELECT accord,year_end,months,revenue,ebitda,other_income,interest,"
            "pbdt,depreciation,pbt,pat,src_row_pl,net_worth,src_row_nw,plant_mach,"
            "net_block,inventories,total_assets,src_row_bs,fx_inflow,src_row_fx,"
            "seg_ce,src_row_seg,recon_ok FROM fin "
            "WHERE months IS NULL OR months=12 ORDER BY accord, year_end DESC")
        cols = ["accord", "year_end", "months", "revenue", "ebitda", "other_income",
                "interest", "pbdt", "depreciation", "pbt", "pat", "src_row_pl",
                "net_worth", "src_row_nw", "plant_mach", "net_block", "inventories",
                "total_assets", "src_row_bs", "fx_inflow", "src_row_fx", "seg_ce",
                "src_row_seg", "recon_ok"]
        for r in rows:
            rec = dict(zip(cols, r))
            self._fin.setdefault(rec["accord"], [])
            if len(self._fin[rec["accord"]]) < 2:          # latest + prior only
                self._fin[rec["accord"]].append(rec)
        con.close()

        # ---- stable pseudo-NAICS per industry category ----------------------
        cats = sorted({c["industry"] for c in self._co.values() if c["industry"]})
        self._naics = {cat: f"{100 + i}0" for i, cat in enumerate(cats)}

        # ---- valuation-grade universe ---------------------------------------
        # Beyond presence checks, a PLAUSIBILITY screen keeps garbage source
        # rows out of the peer pool: |EBITDA| must not exceed 1.5x revenue
        # (a "170% margin" is a data error, not a business), and revenue must
        # be a real operating scale (>= ₹0.1 Cr). Negative EBITDA is KEPT —
        # loss-making is a legitimate state the engine handles honestly
        # (EV/EBITDA skips, EV/Revenue prices). Zeros in optional columns stay
        # None/0 and flow into the disclosed-warning paths, never invented.
        self._universe = []
        for a, years in self._fin.items():
            y = years[0]
            if (a in self._co and (y["revenue"] or 0) >= 0.1
                    and y["ebitda"] is not None
                    and abs(y["ebitda"]) <= 1.5 * y["revenue"]
                    and y["net_worth"] is not None and y["net_worth"] > 0):
                self._universe.append(a)
        self._universe.sort()

        # search index
        self._names = sorted((c["name"], a) for a, c in self._co.items())

    # ------------------------------------------------------------------ util
    @property
    def cache_key(self):
        return f"real:{self._db_path}:{self._etl_ts}"

    def set_audit(self, audit):
        self._audit = audit

    def _log(self, datasource, detail, data=None):
        if self._audit is None:
            return
        if hasattr(self._audit, "log"):
            self._audit.log("dnb", "INFO", "DNB_" + datasource.upper(), detail, data)
        else:
            self._audit.append({"source": f"dnb:{datasource}", "detail": detail})

    def universe_duns(self):
        """Valuation-grade companies (revenue>0, EBITDA & net worth present, 12m)."""
        return [str(a) for a in self._universe]

    def industry_catalog(self):
        """{CD_Industry category -> pseudo-NAICS} — lets the intake agent map a
        free-text sector onto the SAME classification space as the universe."""
        return dict(self._naics)

    def search_names(self, q, limit=20):
        """Prefix/substring suggestions for the UI autocomplete."""
        ql = (q or "").strip().lower()
        if not ql:
            return []
        pref = [n for n, _a in self._names if n.lower().startswith(ql)]
        sub = [n for n, _a in self._names if ql in n.lower() and not n.lower().startswith(ql)]
        return (pref + sub)[:limit]

    def lineage(self, duns):
        """field -> {file, row, fiscal_year} provenance for the target's key figures."""
        a = int(duns)
        co = self._co.get(a)
        yrs = self._fin.get(a) or []
        if not co or not yrs:
            return {}
        y = yrs[0]
        fy = y["year_end"]
        lin = {
            "identity/industry/description": {"file": "Basic Data.xlsx",
                                              "row": co["src_row"], "fy": None},
            "revenue/EBITDA/interest/depreciation/PBT/PAT": {
                "file": "PL data.xlsx", "row": y["src_row_pl"], "fy": fy},
        }
        if y["src_row_nw"]:
            lin["net worth"] = {"file": "Net worth.xlsx", "row": y["src_row_nw"], "fy": fy}
        if y["src_row_bs"]:
            lin["total assets/net block/inventories"] = {
                "file": "BS data.xlsx", "row": y["src_row_bs"], "fy": fy}
        if y["src_row_fx"]:
            lin["forex inflow (export flag)"] = {
                "file": "Forex.xlsx", "row": y["src_row_fx"], "fy": fy}
        if y["src_row_seg"]:
            lin["capital employed (segment)"] = {
                "file": "Segment.xlsx", "row": y["src_row_seg"], "fy": fy}
        if len(yrs) > 1:
            lin["prior-year revenue"] = {"file": "PL data.xlsx",
                                         "row": yrs[1]["src_row_pl"], "fy": yrs[1]["year_end"]}
        lin["P&L reconciliation"] = {"file": "(computed in ETL)",
                                     "row": None,
                                     "fy": fy,
                                     "status": "ok" if y["recon_ok"] else
                                     ("FAIL" if y["recon_ok"] == 0 else "n.a.")}
        return lin

    # ------------------------------------------------------------- envelopes
    def _industry_codes(self, co):
        cat = co["industry"] or "Unclassified"
        grp = _group_of(cat)
        major = _GROUP_MAJOR.get(grp, "I")
        return [
            {"typeDescription": "North American Industry Classification System 2022",
             "code": self._naics.get(cat, "9990"), "description": cat},
            {"typeDescription": "D&B Hoovers Industry Classification",
             "code": f"GRP_{grp}", "description": grp},
            {"typeDescription": "D&B Standard Major Industry Code",
             "code": major, "description": _MAJOR_DESC.get(major, major)},
        ]

    def _info_env(self, a):
        co = self._co[a]
        yrs = self._fin.get(a) or [{}]
        y = yrs[0]
        state = (co["cin"] or "")[8:10] or None
        return {"data": {"organization": {
            "duns": str(a),
            "primaryName": co["name"],
            "industryCodes": self._industry_codes(co),
            "activities": [{"description": (co["desc"] or co["nic"] or co["industry"])}],
            "isExporter": bool((y.get("fx_inflow") or 0) > 0),
            "numberOfEmployees": [],
            "incorporatedDate": None,
            "primaryAddress": {"addressLocality": {"name": state and f"IN-{state}"},
                               "addressRegion": {"abbreviatedName": state},
                               "addressCountry": {"isoAlpha2Code": "IN"}},
            "registrationNumbers": ([{"registrationNumber": co["cin"],
                                      "typeDescription": "CIN"}] if co["cin"] else []),
        }}}

    def _fin_env(self, a):
        yrs = self._fin.get(a)
        if not yrs:
            return {"data": {"organization": {"duns": str(a)}}}
        y = yrs[0]
        ce = y["seg_ce"] if (y["seg_ce"] or 0) > 0 else y["net_worth"]
        ebit = None
        overview = {
            "salesRevenue": _th(y["revenue"]),
            "ebitda": _th(y["ebitda"]),
            "grossProfit": None,
            "operatingProfit": _th(ebit),
            "profitAfterTax": _th(y["pat"]),
            "netIncome": _th(y["pat"]),
            "profitBeforeTaxes": _th(y["pbt"]),
            "costOfSales": None,
            # cash & debt are genuinely absent from the extract: OMIT (None) so the
            # engine flags them unknown rather than silently zero-filling upstream.
            "cashAndLiquidAssets": None,
            "longTermDebt": None,
            "capitalEmployed": _th(ce),
            "netWorth": _th(y["net_worth"]),
            "tangibleFixedAssets": _th(y["net_block"]),
            "depreciation": _th(y["depreciation"]),
            "interestExpense": _th(y["interest"]),
            "totalAssets": _th(y["total_assets"]),
            "accountsReceivable": None, "accountsPayable": None,
            "inventory": _th(y["inventories"]),
            "workingCapital": None, "intangibleAssets": None,
        }
        other = [{"financialStatementToDate": str(y["year_end"]),
                  "salesRevenue": _th(y["revenue"])}]
        if len(yrs) > 1:
            other.append({"financialStatementToDate": str(yrs[1]["year_end"]),
                          "salesRevenue": _th(yrs[1]["revenue"])})
        return {"data": {"organization": {
            "duns": str(a),
            "latestFiscalFinancials": {"units": "Thousand", "currency": "INR",
                                       "financialStatementToDate": str(y["year_end"]),
                                       "overview": overview},
            "otherFinancials": other,
            # NO marketData: the extract has no market prices — nothing is invented.
        }}}

    # --------------------------------------------------------------- request
    def request(self, datasource, body):
        body = body or {}
        if datasource == "company_search":
            name = (body.get("name") or "").strip().lower()
            self._log(datasource, f"search name='{body.get('name')}'")
            cands = []
            for nm, a in self._names:
                pl = nm.lower()
                if not name:
                    continue
                if name == pl:
                    conf = 10
                elif pl.startswith(name):
                    conf = 9
                elif name in pl:
                    conf = 8
                elif all(t in pl for t in name.split()):
                    conf = 6
                else:
                    continue
                cands.append((conf, nm, a))
            cands.sort(key=lambda t: (-t[0], t[1]))
            return {"data": {"matchCandidates": [
                {"organization": {
                    "duns": str(a), "primaryName": nm,
                    "registrationNumbers": ([{"registrationNumber": self._co[a]["cin"],
                                              "typeDescription": "CIN"}]
                                            if self._co[a]["cin"] else []),
                    "primaryAddress": {"addressCountry": {"isoAlpha2Code": "IN"}}},
                 "matchQualityInformation": {"confidenceCode": conf}}
                for conf, nm, a in cands[:25]]}}

        if datasource in ("company_information", "company_financials",
                          "company_management"):
            try:
                a = int(body.get("duns"))
            except (TypeError, ValueError):
                a = None
            found = a in self._co if a is not None else False
            self._log(datasource, f"duns={body.get('duns')} found={found}")
            if not found:
                return {"data": {"organization": {}}}
            if datasource == "company_information":
                return self._info_env(a)
            if datasource == "company_financials":
                return self._fin_env(a)
            return {"data": {"organization": {"duns": str(a), "currentPrincipals": []}}}

        self._log(datasource, "unknown datasource")
        return {"data": {"organization": {}}}
