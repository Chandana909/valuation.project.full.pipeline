# PROJECT CONTEXT — D&B-Grounded MSME Comparable Valuation Engine

> **Purpose of this document.** This is the complete, self-contained technical
> handoff for the `dnb_valuation` project. Read this and you will know *exactly*
> what the system does, what data flows through it, every parameter and formula,
> how peers are selected, how the valuation is computed, how errors and fallbacks
> are handled, how the audit trail works, and what is deliberately deferred to the
> live-API phase. It is written so a fresh engineer or agent can extend, debug, or
> productionize the system without reading tribal knowledge anywhere else.

---

## 0. One-paragraph summary

Given an Indian MSME company **name**, the system resolves it to a Dun & Bradstreet
**DUNS**, pulls its identity/industry/financials from the D&B `dnbhoovers` API,
classifies its **economic profile** (rule-based, no LLM), discovers **15–20
comparable companies** from a large candidate universe using a weighted similarity
model, and produces a **multi-method comparable-company valuation** (EV/EBITDA,
EV/Revenue, EV/EBIT) as a **range** (low / mid / high) with an illiquidity discount,
a **confidence** label, a **data-quality** grade, and a **complete structured audit
trail**. It is **touchless** (no human in the loop), **deterministic** (pure Python,
seeded), and runs today on a `MockDnBClient` that returns the *exact real D&B
response schema*, so going live is a one-method swap.

---

## 1. Non-negotiable constraints (the design contract)

These are hard rules the code obeys. Do not violate them when extending.

1. **Single data source: D&B.** All company data (identity, industry, financials,
   directors) comes from the D&B `dnbhoovers` API. No second data provider.
2. **Mock now, live last.** `MockDnBClient` returns the *real* D&B response schema.
   The live swap is a single method body (see §12).
3. **Touchless.** No human review. The **confidence score + audit trail** are the
   only trust mechanisms.
4. **Deterministic core.** Discovery and valuation are pure Python. **No LLM call
   anywhere.** The economic classifier is rule-based; the seam where an LLM could
   later drop in is marked in code but never called.
5. **Dependency budget.** Core = Python stdlib + `dataclasses` only. `httpx` may
   appear **only** inside the (inactive-until-live) real client. No pandas, numpy,
   sklearn, fastapi, flask, jinja, or any framework. The dashboard is plain HTML
   built with string formatting.
6. **Money units.** D&B returns INR in **Thousand**. Convert to **INR Crore**
   (÷10,000) at the normalization boundary; all downstream math is in Crore.
7. **Peer count.** Return **15–20 peers** (aim 15, min 10) for a well-populated
   sector. The mock universe is large enough to make this real, never padded.
8. **Never fake precision.** Every valuation is a **RANGE** with a confidence label
   and a disclosed EV basis. If no method is computable, return `"none"` with a
   warning — never a fabricated number.
9. **Decoupling.** `core/` MUST NOT import from `mock_api/`. The client is injected
   in `run.py`.

---

## 2. Repository layout

```
dnb_valuation/
├── mock_api/
│   ├── __init__.py            # exports MockDnBClient
│   └── dnb_mock.py            # MockDnBClient + 59-company universe (REAL D&B schema)
├── core/
│   ├── __init__.py            # exports pipeline + AuditTrail symbols
│   ├── audit.py               # structured, typed audit trail (AuditTrail)
│   └── pipeline.py            # normalize, profile, validate, discover, value (STDLIB ONLY)
├── dashboard/
│   ├── __init__.py            # exports build_dashboard
│   └── build_dashboard.py     # result.json -> dashboard.html (plain HTML strings)
├── run.py                     # orchestrator + confidence + acceptance suite
├── output/
│   ├── result.json            # machine-readable full result (written by run.py)
│   └── dashboard.html         # human dashboard (written by run.py)
├── README.md                  # quickstart + method summary
└── PROJECT_CONTEXT.md         # THIS FILE
```

**Import direction (enforced):** `run.py → {mock_api, core, dashboard}`;
`core/__init__.py → core/audit.py, core/pipeline.py`; `core/*` imports **nothing**
from `mock_api`. The D&B client is created in `run.py` and injected.

---

## 3. How to run

```bash
cd dnb_valuation
python run.py "Woodward"                      # default target if arg omitted
python run.py "Kirloskar Brothers Pumps"
python run.py "Bharat Forge Components"
```

**What a run does, in order:**
1. Resolves the name → DUNS via `company_search`.
2. Fetches + normalizes the target; classifies its profile; runs the data-quality gate.
3. Loads the whole candidate universe from D&B (fetch + normalize + profile each).
4. Discovers peers (mismatch filter → similarity scoring).
5. Computes the multi-method valuation on the top 15 peers.
6. Scores confidence.
7. Writes `output/result.json`, prints a readable console summary, builds
   `output/dashboard.html`.
8. Runs the **acceptance suite** (`§11`) for two targets and prints a PASS summary;
   process exit code is `0` iff all checks pass.

**Windows note:** `run.py` calls `sys.stdout.reconfigure(encoding="utf-8")` because
the default console codec (cp1252) cannot encode `₹`/`–`.

---

## 4. Data model — where every number comes from

### 4.1 D&B `dnbhoovers` API contract (real)

```
POST {corpintel}/request/dnbhoovers
Headers: Authorization: Bearer {corpintel-api-token}
         Content-Type: application/json
Body:    { "body": { ... }, "datasource": "<operation>", "no_cache": false }
```

Four datasources are used. Responses nest under `data.organization...` (or
`data.matchCandidates[]...` for search).

| datasource | input | what we read |
|---|---|---|
| `company_search` | `{name, countryISOAlpha2Code:"IN"}` | `data.matchCandidates[].organization.{duns, primaryName, registrationNumbers[]}`, `matchQualityInformation.confidenceCode` |
| `company_information` | `{duns}` | `data.organization.{industryCodes[], activities[], isExporter, numberOfEmployees[], incorporatedDate, primaryAddress, registrationNumbers[]}` |
| `company_financials` | `{duns}` | `data.organization.latestFiscalFinancials.{units, currency, overview}` and `otherFinancials[]` |
| `company_management` | `{duns}` | `data.organization.currentPrincipals[].fullName` (directors; optional) |

**industryCodes `typeDescription` values we split on:**
- `"North American Industry Classification System 2022"` → **NAICS** (primary = first)
- `"D&B Hoovers Industry Classification"` → **Hoovers**
- `"D&B Standard Major Industry Code"` → **major** (`D`=Manufacturing, `F`=Wholesale
  Trade, `G`=Retail Trade, `I`=Services)

**registrationNumbers:** the entry with `typeDescription == "CIN"` gives the Indian
Corporate Identity Number. **CIN first character encodes listing status**: `L` =
listed/public, `U` = unlisted. We derive `listed` from this.

### 4.2 `company_financials.overview` — exact fields (INR **Thousand**)

All may be `null` for smaller companies; every consumer is null-safe.

```
salesRevenue  ebitda  grossProfit  operatingProfit  profitAfterTax  netIncome
profitBeforeTaxes  costOfSales  cashAndLiquidAssets  longTermDebt  capitalEmployed
netWorth  tangibleFixedAssets  depreciation  interestExpense  totalAssets
accountsReceivable  accountsPayable  inventory  workingCapital  intangibleAssets
```

- **`ebit` is NOT a field.** We compute `ebit = ebitda − depreciation` **after**
  the ÷10,000 conversion.
- **Prior-year revenue** comes from `otherFinancials[1].salesRevenue` (index `[0]` is
  the latest period, `[1]` is the prior year).

---

## 5. The mock universe (`mock_api/dnb_mock.py`)

The mock is a faithful stand-in: `MockDnBClient.request(datasource, body)` returns
the exact nested D&B envelope. It exists so the whole pipeline is runnable and
testable offline. **Nothing about the pipeline knows it's talking to a mock.**

### 5.1 Universe composition — 59 entities

Built once at import by `_build_universe()` using **seeded** `random.Random`
(per-cluster seeds `A=101, B=202, C=303`, wrong-entities seed `909`) → fully
deterministic values.

| group | count | NAICS | major | purpose |
|---|---|---|---|---|
| **Cluster A** — engine/turbine controls & precision engine equipment | 18 | `333611` | `D` | main manufacturing cluster |
| **Cluster B** — auto components / motor-vehicle parts | 18 | `336390` | `D` | main manufacturing cluster |
| **Cluster C** — pumps (first 12) `333914` + valves (last 6) `332911` | 18 | `333914`/`332911` | `D` | main manufacturing cluster |
| **Wrong entities** | 5 | various | `F/G/I/D` | MUST be rejected by the economic filter |

**Hoovers codes** are distinct per cluster (`A=31111100`, `B=33999900`,
`C=31122200`) so cross-cluster industry similarity is 0 unless the NAICS 3-digit
subsector matches.

**The 5 "wrong" entities** (and why each is rejected — see §8.1):
1. `Metro Wholesale Distributors` — *"wholesale distributor … sourced from
   manufacturers and resold to …"* → **distributor** (major `F`).
2. `Prime Industrial Traders` — *"wholesale trader and distributor … sourced from
   manufacturers, resold to …"* → **distributor** (major `F`).
3. `Sterling Steel Billets` — *"manufacturer of steel billets and raw material
   supplied to downstream …"* → **manufacturer but value_chain = raw_material**
   (major `D`).
4. `UrbanMart Retail Stores` — *"operates retail stores selling … direct to
   consumer"* → **retailer** (major `G`).
5. `Insight Engineering Consulting` — *"provides engineering consulting and design
   services …"* → **service** (major `I`).

The distributor activity text deliberately contains the word **"manufacturers"** to
exercise the collision rule (§7.1): the bare noun must NOT trigger the manufacturer
classification.

### 5.2 How each company's financials are generated (`_make_financials_cr`)

Values are generated **in Crore** (realistic ranges), then converted to Thousand for
the API envelope (×10,000). Ranges:

| driver | range / rule |
|---|---|
| `revenue` | uniform ₹150–10,000 Cr |
| `margin` (EBITDA) | uniform 12%–19% |
| `growth` | uniform −5%…+28% |
| `ebitda` | `revenue × margin` |
| `depreciation` | `revenue × [3%,6%]` |
| `ebit` | `ebitda − depreciation` |
| `capital_employed` | `revenue × [0.80,1.55]` (this is the **book EV proxy**) |
| `debt` (long-term) | `revenue × [5%,40%]` |
| `cash` | `revenue × [3%,15%]` |
| `interest` | `debt × 9%` |
| `pbt` | `ebit − interest`; `pat = pbt × 0.75` |
| `net_worth` | `capital_employed × 0.60` |
| others | grossProfit=rev×35%, costOfSales=rev×65%, receivables=18%, payables=14%, inventory=16%, working_capital=20%, intangibles=4%, totalAssets=capital_employed×1.30 |

`otherFinancials` is a 4-period declining sales history; `[0]` latest, `[1]` prior.

**Listing & market EV.** ~60% of companies are listed (`rng.random()<0.60`),
reflected in a CIN starting with `L`. For listed companies the mock synthesizes a
**realistic market enterprise value** via `_market_valuation`: it draws a
sector-realistic EV/EBITDA multiple from `_CLUSTER_EV_EBITDA`
(`A=12–18x`, `B=8–14x`, `C=14–22x`), then `market_ev = EBITDA × multiple` and
`market_cap = market_ev − net_debt`. Both are emitted in the `company_financials`
response under a `marketData` block (`marketCapitalization`, `enterpriseValue`, in
Thousand) **only for listed companies**. This is what makes the derived multiples
*true trading multiples*. Unlisted companies have no `marketData` (no observable
market value). In production this market block comes from the D&B Hoovers
public-company market module or a market feed (NSE/BSE); the schema/units mirror the
financials. See §9.1 and §12.

### 5.3 `MockDnBClient` API

- `request(datasource, body) -> dict` — returns the real-schema envelope for the four
  datasources; any unknown datasource returns an empty envelope of the same shape.
- `universe_duns() -> list[str]` — all DUNS the mock knows. **Prototype helper only**;
  in production this list is built offline by paging `company_search` across the
  sector and caching the DUNS set.
- `_log(datasource, detail, data=None)` — writes a `DNB_*` audit record if an
  `AuditTrail` was injected (prefers the structured `.log`; falls back to legacy
  `.append`).

---

## 6. Normalization (`core/pipeline.py :: normalize_company`)

`normalize_company(info_resp, fin_resp, mgmt_resp=None) -> Company`

Flattens `company_information` + `company_financials` (+ optional
`company_management`) into a single `Company` dataclass. **Every monetary field is
divided by 10,000 to Crore** via `_to_cr` (null-safe).

Derived fields:
- `ebit_cr = ebitda_cr − depreciation_cr` (null-safe; `None` if either missing).
- `revenue_prior_cr` = `_to_cr(otherFinancials[1].salesRevenue)`.
- `revenue_growth = (revenue − prior)/prior` (null-safe; `None` if prior missing/0).
- `ebitda_margin = ebitda/revenue` (null-safe).
- `cin` from `registrationNumbers` (type `CIN`); `listed = cin[0]=='L'`.
- `naics/naics_desc/hoovers/major_industry/major_industry_desc` split from
  `industryCodes[]` (primary NAICS = first NAICS entry).
- **Defaults:** `cash_cr` and `debt_cr` default to `0.0` if null.
- `market_ev_cr` = **always `None`** at normalization (the listed-market hook; D&B
  doesn't supply it).
- `directors` from `currentPrincipals[].fullName` if `mgmt_resp` provided.

### 6.1 The `Company` dataclass (full field list)

Identity: `duns, name, cin`. Industry: `naics, naics_desc, hoovers, major_industry,
major_industry_desc, activities`. Firmographics: `is_exporter, employees,
incorporated, city, listed`. Financials (Crore): `revenue_cr, revenue_prior_cr,
revenue_growth, ebitda_cr, ebitda_margin, depreciation_cr, ebit_cr, gross_profit_cr,
operating_profit_cr, pat_cr, net_income_cr, cash_cr, debt_cr, capital_employed_cr,
net_worth_cr, total_assets_cr, working_capital_cr`. Hooks: `market_ev_cr=None,
directors=[]`.

---

## 7. Economic-profile classifier (`build_profile`) — RULE-BASED, LLM-swap seam

`build_profile(company) -> EconomicProfile`. Pure function. **This is the marked seam
where a future LLM classifier could drop in** (must remain `company -> profile`).
Until then it is deliberately rule-based; **no LLM is called**.

Inputs used: `company.activities` (lowercased) and `company.major_industry`.

Keyword sets:
```
_MFG_VERBS      = manufactures | manufacturer of | produces | producing | manufacturing of
_DISTRIBUTOR_KW = wholesale distributor | distributor of | sourced from manufacturers
                  | resold to | wholesale trader | wholesale of
_RETAIL_KW      = retail store | stores selling | direct to consumer | retail
_SERVICE_KW     = consulting | design services | provides engineering consulting
_RAW_KW         = raw material | billets | supplied to downstream
```

### 7.1 THE COLLISION RULE (critical — commented in code)

A manufacturing **VERB** (`_MFG_VERBS`), *not* the bare noun `"manufacturers"`,
triggers the manufacturer classification. This is what stops distributor text like
*"sourced from manufacturers"* from being misclassified as a manufacturer. Implemented
as: `has_mfg_verb = any(v in text for v in _MFG_VERBS)`.

### 7.2 Operating model (priority order)

1. **distributor** — any `_DISTRIBUTOR_KW` **AND** `not has_mfg_verb`.
2. **retailer** — any `_RETAIL_KW` **AND** `not has_mfg_verb`.
3. **service** — any `_SERVICE_KW` **AND** `not has_mfg_verb`.
4. **manufacturer** — `has_mfg_verb` **OR** `major_industry == "D"`.
5. else **unknown**.

### 7.3 Value chain (independent axis)

`raw_material` if any `_RAW_KW` present, else `finished_goods`. Note a company can be
`manufacturer` **and** `raw_material` simultaneously (e.g. steel billets) — that's how
`Sterling Steel Billets` is a manufacturer yet rejected on value-chain mismatch.

### 7.4 Customer type

`B2C` if activities mention consumer/retail signals; `B2B` if they mention
oem/industrial/process/utility/assemblers/downstream/infrastructure/marine/irrigation;
else `mixed`.

### 7.5 Other outputs & confidence

- `naics_subsector` = first **3 digits** of the primary NAICS.
- `confidence ∈ [0,1]`: base `0.5`; `0.75` if a definite operating model; `0.90` when
  a manufacturing verb confirms `manufacturer` (or distributor keywords confirm
  `distributor`); `+0.05` if a NAICS subsector exists (capped at 1.0).

`EconomicProfile` fields: `operating_model, value_chain, customer_type,
naics_subsector, major_industry, confidence`.

---

## 8. Peer discovery (`discover_peers`)

`discover_peers(target, tprofile, universe, audit) -> (ranked, rejected)` where
`universe` is a list of `(Company, EconomicProfile)` tuples (target excluded upstream
in `run.py`). Returns `ranked` (sorted **descending** by score) and `rejected` (list
of `{duns, name, reason, operating_model, value_chain, major_industry}`).

### 8.1 Mismatch filter (reject BEFORE scoring)

A candidate is rejected — with a recorded reason string and a `PEER_REJECTED`
DECISION audit record — if **any** of these differ from the target:
1. `operating_model` (profile) — e.g. distributor/retailer/service vs manufacturer.
2. `value_chain` (profile) — e.g. raw_material vs finished_goods.
3. `major_industry` (company) — e.g. `F`/`G`/`I` vs `D`.

This is exactly why the 5 wrong entities are rejected (2 distributor + 1 retailer +
1 service on operating_model; 1 raw-material on value_chain), while all 54
manufacturers survive to scoring.

### 8.2 Similarity score (0–1), weighted sum of 5 dimensions

Implemented in `_similarity`. Each dimension contributes `raw × weight`:

| dimension | weight | rule (raw ∈ [0,1]) |
|---|---|---|
| **industry** | 0.40 | `1.0` if same NAICS 3-digit subsector; else `0.6` if same Hoovers code; else `0.0` |
| **scale** | 0.20 | `1 / (1 + |log1p(rev_t) − log1p(rev_p)|)` |
| **margin** | 0.15 | `max(0, 1 − 5·|margin_t − margin_p|)` |
| **customer** | 0.15 | `1.0` if same `customer_type` else `0.0` |
| **export** | 0.10 | `1.0` if same `is_exporter` flag else `0.3` |

`score = Σ(raw × weight)`, rounded to 4 dp. Each peer also carries:
- `selected_because[]` — human phrases for the dimensions it scored well on
  (same subsector, comparable scale, similar margin, same customer type, same export).
- `differences[]` — human phrases for weak dimensions (NAICS mismatch, revenue gap,
  customer-type gap).

`run.py` passes the full ranked list to valuation, which uses the **top 15**
(`top_n=15`). Discovery returns *all* survivors so the count is honest and auditable.

**Why cluster A target still gets ≥15 peers:** all 54 manufacturers pass the mismatch
filter; the top 15 by score are dominated by same-subsector companies (cluster A plus
cluster-C pumps that share NAICS subsector `333`), each scoring ~0.86–0.98.

---

## 9. Valuation engine (`compute_valuation`)

`compute_valuation(target, peers, top_n=15, audit=None) -> Valuation`. Uses
`used = peers[:top_n]`. All math in Crore.

### 9.1 Enterprise Value basis — market-primary, book-fallback

Peer multiples are collected into **two pools**:
- **`market_multiples`** — from **listed** comps priced off observed
  `market_ev_cr` (= market cap + net debt). These are genuine **trading multiples**
  and are the **primary** basis.
- **`book_multiples`** — from any comp with `capital_employed_cr`, priced off book
  capital employed. This is a documented **last-resort fallback**.

**Per method**, if there are **≥ 3 listed comps** the market pool is used
(`method_basis = "market"`); otherwise it falls back to the book pool
(`method_basis = "book"`, logged as a `FALLBACK_BOOK_EV` DECISION). Each peer records
which basis applied (`"market"` / `"book"` / `"none"`), and each computed method
records its `ev_basis`. The result's `ev_basis` string discloses the split, e.g.
*"Primary basis: market enterprise value … of 11 LISTED comparables. Book
capital-employed proxy (4 unlisted comps) is used only as a per-method fallback when
fewer than 3 listed comps exist for a metric."* A peer with neither market EV nor
capital employed is skipped for multiples. This is the **no-fake-precision** rule made
visible. **Why it matters:** book capital employed is *not* enterprise value —
pricing off it produces meaningless ~7x book ratios; pricing off market EV recovers
the real sector trading band (e.g. cluster A ≈ 12–18x EV/EBITDA).

### 9.2 Per-peer multiples (up to 3)

For each of the top-`top_n` peers, compute `EV / driver` for each method **only when
the denominator > 0**:

| method | driver |
|---|---|
| EV/EBITDA | `ebitda_cr` |
| EV/Revenue | `revenue_cr` |
| EV/EBIT | `ebit_cr` |

### 9.3 Outlier trimming — Tukey IQR fence (`_tukey_trim`)

For each method's list of peer multiples: if **fewer than 4** values, skip trimming
(report 0 dropped). Otherwise compute `Q1 = P25`, `Q3 = P75`, `IQR = Q3 − Q1`, and
drop values outside `[Q1 − 1.5·IQR, Q3 + 1.5·IQR]`. `n_outliers_dropped` is reported
per method.

### 9.4 Range basis (peer-count-dependent widening)

- `n_used ≥ 10` → range quantiles = **P25 / P75** (`range_basis = "P25/P75"`).
- `n_used < 10` → range quantiles **widen to P10 / P90**
  (`range_basis = "P10/P90 (widened: fewer than 10 peers)"`), and a `RANGE_WIDENED`
  DECISION is logged. This is a real behavioral fallback, not just a warning.

The **median** is always P50.

### 9.5 Method computability gate + fallback chain

A method is **computed** only if: (a) the target driver is **> 0**, and (b) there are
**≥ 3** peer multiples after trimming. Otherwise it is skipped with a
`METHOD_SKIPPED_DRIVER` or `METHOD_SKIPPED_THIN` WARN and a note.

**Headline method** = first computable of the ordered chain
**EV/EBITDA → EV/Revenue → EV/EBIT**:
- If EV/EBITDA is the headline → `HEADLINE_SELECTED` DECISION.
- If it fell through to a later method → `FALLBACK_HEADLINE` DECISION.
- If none computable → headline `"none"`, `NO_METHOD` ERROR, equity all `None`.

**All computable methods are always reported** (for triangulation), regardless of
which is the headline.

### 9.6 Per-method math

For the trimmed, sorted multiples of a method with quantiles `(low_q, median, high_q)`:
```
net_debt = target.debt_cr − target.cash_cr
discount = 0.30 if rev < 100
           0.25 if rev < 500
           0.20 otherwise                (rev = target.revenue_cr; DISCOUNT_APPLIED logged)

EV_x     = multiple_x × target_driver
equity_x = (EV_x − net_debt) × (1 − discount)
range: low = equity(low_q), mid = equity(median), high = equity(high_q)
```

The discount is an **illiquidity/marketability discount** scaled by size (smaller =
larger discount). `_percentile` uses linear interpolation (stdlib-only, no numpy).

### 9.7 `Valuation` output object

`headline_method`; `methods[]` (each: `method, target_driver, n_peers, n_multiples,
n_outliers_dropped, range_basis, multiple_p25/median/p75, ev_low/mid/high_cr,
equity_low/mid/high_cr`); `net_debt_cr`; `discount` + `discount_reason`; headline
`equity_low/mid/high_cr`; `peers_used[]` (each peer with its multiples, EV basis,
component scores, selected_because, differences); `ev_basis` string; `notes[]`;
`warnings[]`.

> **Field-name note:** `multiple_p25`/`multiple_p75` hold the *actual* low/high
> quantile used — P25/P75 normally, or P10/P90 when widened. `range_basis` tells you
> which. The dashboard column header reads "Multiple Low / Median / High".

### 9.8 Warnings appended (non-crashing)

- target EBITDA ≤ 0 → note headline fell back past EV/EBITDA.
- `< 10` peers used → widen/caution.
- no method computable → `"none"`.

---

## 10. Confidence, data quality, provenance, error handling

### 10.1 Confidence (`run.py :: compute_confidence`)

```
score = 0.35 · profile_confidence
      + 0.35 · min(peers, 15) / 15
      + 0.15 · (1 if target_ebitda > 0 else 0)
      + 0.15 · (1 if methods_computed ≥ 2 else 0)
label = HIGH if score ≥ 0.75 ; MEDIUM if ≥ 0.50 ; else LOW
```
`peers` = `min(len(ranked), top_n)`. A well-populated manufacturing target with a
clean profile, EBITDA>0, and 3 methods lands ~0.98 → HIGH.

### 10.2 Data-quality gate (`validate_company`) — runs BEFORE valuation

Produces a `DataQuality{score, grade, checks[], missing_fields[], valuable}`. Weighted
per-field checks (penalty subtracted from 1.0):

| field | level | penalty if failing | pass condition |
|---|---|---|---|
| `revenue` | CRITICAL | 0.50 | revenue_cr > 0 |
| `ebitda` | HIGH | 0.15 | ebitda_cr > 0 |
| `capital_employed` | HIGH | 0.15 | capital_employed_cr > 0 (book EV proxy) |
| `ebitda_margin` | MEDIUM | 0.07 | margin is None or 0 < margin < 0.60 |
| `ebit` | MEDIUM | 0.05 | ebit_cr not None |
| `naics` | MEDIUM | 0.05 | naics present |
| `prior_revenue` | LOW | 0.03 | prior-year revenue present (growth computable) |
| `cin` | LOW | 0.01 | cin present |

`grade`: A ≥ 0.85, B ≥ 0.70, C ≥ 0.50, else D. `valuable = revenue_ok AND
(capital_employed_ok OR ebitda_ok)` — i.e. is there enough to attempt any valuation.
Each failing check logs a `DATA_QUALITY_WARN/FAIL`; a `DATA_QUALITY_GRADE` INFO
summarizes. If `not valuable`, the run degrades to `insufficient_data` (§10.4).

### 10.3 Structured audit trail (`core/audit.py :: AuditTrail`)

The single trust mechanism in a touchless system. Every material step is an **ordered,
typed record**:
```
{ seq, ts (ISO-8601 UTC, ms), stage, level, code, detail, data }
```
- **levels:** `INFO | WARN | DECISION | ERROR`.
- **stages:** `run, resolve, dnb, fetch, normalize, profile, validate, universe,
  discover, value, confidence`.
- **codes (stable, machine-readable):** `START, DNB_COMPANY_SEARCH,
  DNB_COMPANY_INFORMATION, DNB_COMPANY_FINANCIALS, DNB_COMPANY_MANAGEMENT,
  MATCH_CANDIDATES, TARGET_RESOLVED, NO_MATCH, TARGET_PROFILED, DATA_QUALITY_GRADE,
  DATA_QUALITY_WARN, DATA_QUALITY_FAIL, INSUFFICIENT_DATA, UNIVERSE_LOADED,
  PEER_REJECTED, PEERS_RANKED, DISCOUNT_APPLIED, RANGE_WIDENED, METHOD_COMPUTED,
  METHOD_SKIPPED_DRIVER, METHOD_SKIPPED_THIN, HEADLINE_SELECTED, FALLBACK_HEADLINE,
  NO_METHOD, VALUATION_DONE, CONFIDENCE_SCORED, COMPLETE`.

API: `.log(stage, level, code, detail, data=None)`, plus `.info/.warn/.decision/
.error` shortcuts; `.append(dict)` **legacy shim** (converts `{source, detail}` to a
structured record so the mock stays framework-free); `.to_list()`,
`.counts_by_level()`, `__len__`, `__iter__`. A typical successful run emits ~139
records (≈9 DECISION + rest INFO).

### 10.4 Error handling & fallback ladder (every fallback is audited)

`run_pipeline` **never raises on bad input**; it always returns `(result, ctx)` with a
`meta.status` and a complete audit trail.

| trigger | behavior | `meta.status` | audit code |
|---|---|---|---|
| `company_search` returns no candidates | return degraded result, no target | `no_match` | `NO_MATCH` (ERROR) |
| target `not valuable` (data-quality) | return degraded result, no valuation | `insufficient_data` | `INSUFFICIENT_DATA` (ERROR) |
| peer lacks market EV | use book capital-employed proxy | (n/a) | disclosed in `ev_basis` |
| preferred method uncomputable | fall to EV/Revenue then EV/EBIT | `ok` | `FALLBACK_HEADLINE` (DECISION) |
| target driver ≤ 0 or < 3 multiples | skip that method, keep others | `ok` | `METHOD_SKIPPED_*` (WARN) |
| `< 10` peers | widen range P25/P75 → **P10/P90** | `ok` | `RANGE_WIDENED` (DECISION) |
| no method computable at all | headline `"none"`, equity None | `no_valuation` | `NO_METHOD` (ERROR) |

Both degraded paths are tested end-to-end (no-match query; a synthetic no-financials
target grading `D`, `valuable=False`). Degraded runs still write `result.json` and an
honest **minimal** dashboard whose only body is the audit trail.

### 10.5 Provenance metadata (`result.meta`)

Every result carries: `engine, methodology_version (1.1.0), dnb_schema_version
(dnbhoovers-2024), run_timestamp (UTC), data_source, currency (INR), source_units
(Thousand) → reporting_units (Crore), human_in_the_loop (false), query, status`.
Purpose: every archived valuation is reproducible and traceable to a methodology
version and a moment in time.

---

## 11. `run.py` orchestration & the acceptance suite

### 11.1 `run_pipeline(name, client=None, top_n=15) -> (result, ctx)`

Steps: create `AuditTrail` → create/inject `MockDnBClient(audit)` → `START` →
resolve (`_best_match`) → [no_match guard] → fetch+normalize target (with mgmt) →
`build_profile` → `TARGET_PROFILED` → `validate_company` → [insufficient_data guard]
→ loop `universe_duns()` fetching+normalizing+profiling each candidate (excluding the
target DUNS) → `UNIVERSE_LOADED` → `discover_peers` → `compute_valuation(top_n)` →
`VALUATION_DONE` → `compute_confidence` → assemble `result` (with `meta`,
`data_quality`, `audit_trail`) → `COMPLETE`. `ctx` carries live objects
(`target, tprofile, ranked, rejected, valuation, data_quality`) for callers/tests.

`result` top-level keys: `meta, query, target, target_profile, data_quality, peers,
peers_ranked_count, rejected, valuation, confidence, audit_trail`.

### 11.2 Console summary (`print_summary`)

Prints: provenance banner; target identity/profile/financials + data-quality grade;
top-15 peer lines (score, EV/EBITDA, revenue, city, listing); rejected list;
per-method triangulation lines; EV basis; net debt + discount; headline range;
warnings; confidence; and an audit level-count summary. Tolerates degraded runs
(prints a "NO VALUATION — status …" block).

### 11.3 Acceptance suite (`acceptance_tests`) — runs 2 targets, 10 checks each = 20

Targets: `Woodward` (cluster A) and `Kirloskar Brothers Pumps` (cluster C).

1. Target resolves to a DUNS.
2. Financials normalize (revenue & EBITDA in Crore, non-zero).
3. **≥ 15 peers** used.
4. **All 5 wrong entities rejected**, each with a reason (and exactly 5 rejected).
5. **All three methods compute** and their mid-equity values **triangulate**
   (`max/min ≤ 2.5`).
6. IQR trimming ran (each method reports `n_outliers_dropped`; may be 0).
7. Headline equity is a range `low < mid < high` with a disclosed discount ∈ (0,1).
8. `result` has all top-level sections and a non-empty audit trail.
9. Audit trail is **structured** (records carry `seq/ts/stage/level/code`) and
   contains at least one `DECISION`.
10. Provenance metadata present (`status=ok`, methodology_version, currency INR,
    reporting_units Crore) **and** data-quality grade ∈ {A,B,C,D} with `valuable=True`.

Process exits `0` iff all 20 pass. **Current status: 20/20 PASS.**

### 11.4 `main()`

Reads `argv[1]` (default `"Woodward"`), runs the pipeline, writes `output/result.json`
(pretty JSON, UTF-8), prints the summary, builds `output/dashboard.html`, then runs
the acceptance suite and exits with its status.

---

## 12. Going LIVE — the one-method swap

The mock and the live client are interface-identical. To go live:

1. In `mock_api/dnb_mock.py`, replace the body of `MockDnBClient.request` with (this
   is already written in the class docstring, inactive):
   ```python
   import httpx
   resp = httpx.post(
       f"{base}/request/dnbhoovers",
       headers={"Authorization": f"Bearer {token}"},
       json={"body": body, "datasource": datasource, "no_cache": False},
       timeout=30,
   )
   return resp.json()
   ```
2. In `run.py`, inject the live client at the `client = client or MockDnBClient(...)`
   seam.
3. Replace `universe_duns()` with an **offline-built** cached DUNS set (page
   `company_search` across the target sector; store the DUNS list). This is the only
   place the prototype shortcut lives.

**Nothing else changes** — response paths (`data.organization...`) are identical, so
normalization, profiling, discovery, and valuation are untouched. `httpx` is the only
new dependency and it appears **only** inside the live client.

---

## 13. Parameters — implemented vs. deferred (explicit)

**Implemented & active:**
- All 4 D&B datasources against the real schema (mocked).
- Thousand→Crore normalization; `ebit = ebitda − depreciation`; prior-year growth.
- Rule-based economic classifier with the collision rule.
- Mismatch filter (operating_model / value_chain / major_industry).
- 5-dimension weighted similarity (industry .40 / scale .20 / margin .15 /
  customer .15 / export .10).
- 3 valuation methods; Tukey IQR trimming; headline fallback chain; size-based
  illiquidity discount; peer-count range widening.
- Confidence, data-quality gate, structured audit, provenance metadata, graceful
  degradation.

**Implemented in 1.2.0 (was previously deferred):**
- **Market enterprise value for listed comps.** `marketData` (market cap + EV) is now
  emitted for listed companies and read in `normalize_company` → `market_cap_cr`,
  `market_ev_cr`. Valuation prices multiples off market EV (primary) with a book
  fallback. In production, point `marketData` at a real market feed — no code change
  needed downstream.

**Deferred / hooked but intentionally inactive:**
- **LLM economic classifier.** `build_profile` is the marked swap seam. Keep it a pure
  `Company -> EconomicProfile` function so an LLM variant is a black-box replacement.
- **Directors / governance flag.** `company_management` is fetched for the target and
  `directors` is populated, but no governance signal is computed yet.
- **Live universe paging.** `universe_duns()` is a prototype in-memory list; production
  builds it offline (see §12.3).

**Not present by design (dependency budget):** no pandas/numpy/sklearn/fastapi/flask/
jinja; no second data provider; no network in the core path.

---

## 14. Extension guide (how to change things safely)

- **Add a valuation method** (e.g. EV/EBIT already exists; to add P/E): add a
  `(name, driver)` to `_METHOD_SPEC`, ensure the driver is a positive `Company`
  field, and — if it's equity-based rather than EV-based — branch the equity math
  (don't subtract net debt twice). Update the headline `order` list if it should be
  in the fallback chain.
- **Tune similarity weights:** edit the weights in `_similarity` (they should sum to
  1.0 for interpretability). The dashboard chips read these automatically.
- **Change discount bands:** edit the `rev < 100 / < 500` ladder in
  `compute_valuation`; the `DISCOUNT_APPLIED` audit record carries the chosen value.
- **Add a data-quality check:** add an `add(field, ok, level, detail, weight)` call in
  `validate_company`; keep total possible penalty sane so grades stay meaningful.
- **Add an audit code:** just call `audit.decision/warn/info/error(stage, code,
  detail, data)`. Keep codes stable and greppable for downstream tooling.
- **Never** import `mock_api` from `core`; never call an LLM or network from `core`;
  never emit a single "exact" valuation without a range + confidence + disclosed EV
  basis.

---

## 15. Known properties, edge cases, and honest caveats

- **Deterministic:** same input → same output (seeded universe, no randomness in the
  core). Timestamps in `meta.run_timestamp` and audit `ts` are the only wall-clock
  values.
- **Multiples are market-based trading multiples** from listed comps (market cap + net
  debt), recovering realistic sector bands (e.g. cluster A ≈ 12–18x EV/EBITDA). The
  book capital-employed proxy is a per-method fallback only, used when <3 listed comps
  exist for a metric and always disclosed. Earlier versions (≤1.1.0) priced off book
  capital employed everywhere, which understated value — fixed in 1.2.0.
- **DLOM, not a market discount:** the size-scaled discount is applied because the
  *target* is a private MSME being priced off *listed* peers' liquid multiples. For a
  genuinely listed target you would waive it.
- **Triangulation band:** EV/EBITDA, EV/Revenue, EV/EBIT mids will differ; the
  acceptance suite bounds them to `max/min ≤ 2.5`. Large divergence in real data is a
  legitimate signal (margin/asset-intensity differences), surfaced rather than hidden.
- **Small universe would break peer counts** — the mock is intentionally 59 entities
  so 15–20 peers are real; in production the offline DUNS set must be similarly rich.
- **`multiple_p25`/`multiple_p75` may hold P10/P90** when the peer set is thin — always
  read `range_basis` alongside them.
- **Console encoding:** requires UTF-8 stdout (handled on Windows via `reconfigure`).

---

*End of PROJECT_CONTEXT.md — methodology version 1.2.0.*
