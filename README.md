# D&B-Grounded MSME Comparable Valuation

Production-grade, **touchless**, **deterministic** comparable-company discovery +
multi-method valuation for Indian MSMEs, grounded entirely on the **Dun & Bradstreet
`dnbhoovers`** API. Runs today on a mock D&B layer that returns the **exact real D&B
response schema**; going live is a one-method swap.

## Two data sources — real and mock

| | source | companies | valuation basis |
|---|---|---|---|
| **real** | 9 uploaded Excel extracts → `python etl.py` → `realdata.db` (SQLite) | **13,619** valuation-grade, plausibility-screened (of 42,951) | book basis (extract has no market prices / borrowings / cash — caveats disclosed on every result) |
| **mock** | synthetic 59-company universe generated in code | 59 | market trading multiples (used for methodology validation/backtests) |

The core calculation is identical for both: `RealDnBClient` and `MockDnBClient` emit the
same D&B-schema envelopes, so the engine never knows which database it's on. The ETL
stores **per-row provenance** (source file + Excel row for every figure) and runs a
**P&L reconciliation gate** (EBITDA + other income − interest ≈ PBDT; 99.1% reconcile);
every valuation carries a per-field **lineage table** tracing each number to its cell.

```bash
pip install -r requirements.txt      # fastapi + uvicorn (API layer) + openpyxl (ETL)
python etl.py                        # one-time: build realdata.db from the 9 Excel files
python run.py "20 Microns Ltd." --data real
python api.py                        # API + UI; auto-uses realdata.db when present
```

## Run — API + live UI (recommended)

```bash
python api.py               # → http://localhost:8733  (real data if realdata.db exists)
```

- **UI** at `/` — type a company name (autocomplete over the universe), press **Run
  valuation**, and the full industry-style report renders in-page — tabbed
  (Overview | Filters | Peer Analysis | Valuation | Validation | Audit Trail), with a
  football-field chart, the complete filter-chain documentation with live counts, and a
  **Download PDF** button.
- **REST API** (OpenAPI docs at `/docs`):

| endpoint | returns |
|---|---|
| `GET /api/v1/health` | liveness + readiness (universe cache warm?) |
| `GET /api/v1/status` | data source, universe size, versions, caveats |
| `GET /api/v1/companies/suggest?q=` | autocomplete suggestions |
| `GET /api/v1/valuations?name=` | full valuation JSON (404 no match, 422 insufficient data) |
| `GET /api/v1/valuations/report?name=&print=1` | self-contained HTML report (`print=1` opens the PDF dialog) |
| `POST /api/v1/intake/start` | open a guided-intake conversation (question + theory) |
| `POST /api/v1/intake/{sid}/answer` | answer / skip; returns next question or done |
| `GET /api/v1/intake/{sid}` | session state |
| `POST /api/v1/intake/{sid}/value` | value the intake company against database peers |
| `GET /api/v1/intake/{sid}/report?print=1` | HTML/PDF report for the intake valuation |
| `GET /api/v1/filters` | complete filter chain + guarantees + limitations |
| `GET /api/v1/validation` | anti-overfitting backtest |
| `GET /api/v1/robustness` | 5-seed robustness sweep |
| `GET /api/v1/database/status` | ETL provenance: counts, recon rate, source-file hashes, age |

**Three ways in** (UI at `/`): search the database · **describe your company** (a
deterministic conversational agent walks a question graph, then values the custom
company against database peers — with an explicit, audited **scale-mismatch penalty**
when no exact-size peer exists) · upload PDFs (placeholder — extraction layer not
wired yet). Every valuation also carries an indicative **comparable-transactions
view** (control premium over the minority trading value, disclosed as derived).

Deploy with `docker build -t msme-valuation . && docker run -p 8733:8733 msme-valuation`
(build `realdata.db` first). Config via env: `PORT`, `DATA_SOURCE=real|mock`,
`CORS_ORIGINS`. The **calculation core stays pure stdlib**; FastAPI exists only in
this delivery layer.

## Run — CLI

```bash
python run.py "Woodward"                 # or any company name
python run.py "Kirloskar Brothers Pumps"
python validate.py                       # calibration backtest + 5-seed robustness sweep
```

Outputs:
- `output/result.json` — target, peers, rejected, valuation (all methods), confidence, full audit trail
- `output/dashboard.html` — self-contained dashboard (open in a browser)

`run.py` also runs the acceptance suite (3 targets + anti-overfitting gate) and prints a
PASS summary.

## Design

- **Single source of truth: D&B.** Identity, industry, financials, directors all come
  from four `dnbhoovers` datasources (`company_search`, `company_information`,
  `company_financials`, `company_management`).
- **No LLM. No network. No frameworks.** The core is pure Python stdlib +
  `dataclasses`. `httpx` appears only inside the (inactive) live-swap note.
- **Money units.** D&B returns INR **Thousand**; normalization converts to INR
  **Crore** (÷10,000). All downstream math is in Crore.
- **Never fake precision.** Every valuation is a **range** (P25 / median / P75) with a
  confidence label and a disclosed EV basis. If no method is computable → `"none"`.

## Pipeline

```
company_search → best DUNS → company_information + company_financials
  → normalize_company (→ Crore)
  → build_profile      (rule-based economic classifier; LLM-swap seam)
  → discover_peers     (mismatch filter, then weighted similarity)
  → compute_valuation  (EV/EBITDA, EV/Revenue, EV/EBIT; Tukey IQR trim; range)
  → confidence + audit trail → result.json + dashboard.html
```

### Economic classifier (collision rule)
A manufacturing **verb** (`manufactures`, `manufacturer of`, `produces`, `producing`),
not the bare noun `manufacturers`, triggers the *manufacturer* model — so distributor
text like *"sourced from manufacturers"* is correctly classified as a **distributor**.

### The filter chain (complete — 14 controls in 4 stages)

| # | stage | filter | rule / parameter |
|---|---|---|---|
| 1 | A eligibility | Geography | `countryISOAlpha2Code = "IN"` at search |
| 2 | A eligibility | Self-exclusion | target's own DUNS removed from the pool |
| 3 | B knock-out | Operating model | must equal target's (manufacturer/distributor/retailer/service) |
| 4 | B knock-out | Value chain | finished_goods vs raw_material must match |
| 5 | B knock-out | Major industry | D&B major code must match (D/F/G/I) |
| 6 | C similarity | Industry proximity (0.40) | 1.0 same NAICS-3 subsector; 0.6 same Hoovers; else 0 |
| 7 | C similarity | Scale proximity (0.20) | `1/(1+abs(log1p(rev_t)-log1p(rev_p)))` |
| 8 | C similarity | Margin proximity (0.15) | `max(0, 1-5·abs(margin_t-margin_p))` |
| 9 | C similarity | Customer type (0.15) | 1.0 if same B2B/B2C/mixed else 0 |
| 10 | C similarity | Export profile (0.10) | 1.0 if same exporter flag else 0.3 |
| 11 | D quality | Top-N cut | top 15 by score enter valuation |
| 12 | D quality | Similarity weighting | score ≥ 0.85 → weight 1.0, tapering to 0.15 floor |
| 13 | D quality | Multiple eligibility | listed/market-EV comps primary (book fallback if <3); driver > 0 |
| 14 | D quality | Outlier trim | Tukey 1.5×IQR fence on each method's multiples |

Stage-B rejections happen **before** scoring, each with a recorded reason
(`PEER_REJECTED`). **Not used anywhere:** import data and per-capita metrics — they are
not in the D&B financial payload; the only trade-related signal is the export flag (#10).

### Valuation (trading comps)
- Peer multiples use **market enterprise value** of *listed* comps:
  `EV = market cap + net debt` (from the D&B market-data block). These are true
  trading multiples, not book ratios. Unlisted peers have no observable market value
  and are excluded from multiples (they still inform comparability). If a method has
  fewer than 3 listed comps, it falls back to the **book capital-employed proxy** for
  that method only — logged as `FALLBACK_BOOK_EV`. The `ev_basis` string discloses the
  split.
- **Similarity weighting (handles inexact peers):** each peer's multiple is weighted by
  its match quality via a **weighted percentile** — a full match (score ≥ 0.85) counts
  fully, a borderline comp tapers linearly to a 0.15 floor. So when there aren't enough
  *exact* peers, loose comps can't distort the headline. The **effective (weighted) peer
  count** drives both the range-widening trigger and the confidence peer-coverage term.
- **Quality positioning:** the central multiple is the peer multiple at the target's
  **EBITDA-margin percentile** within the peer set (clamped to P15–P85), *not* the flat
  median — a below-median-margin company earns a below-median multiple, and vice-versa.
  Low/high are a ±20pt (±30 if the effective peer count < 10) band around that position.
- Per method: Tukey 1.5×IQR outlier trim (skipped if <4 values), positioned multiple ×
  target driver → implied EV → `equity = (EV − net_debt) × (1 − DLOM)`.
- `net_debt = debt − cash`; **DLOM** (discount for lack of marketability) = 0.30 / 0.25
  / 0.20 by revenue band, applied **only to private (unlisted) targets** — a listed
  target's equity is already liquid, so DLOM = 0.
- **Accuracy cross-check:** when the target is itself listed, the comps-derived equity
  is compared to the target's **own market capitalisation**. On the sample universe this
  calibrates to within ~2% (Woodward +0.7%, Kirloskar +1.5%) — an independent validation,
  since positioning never sees the market cap.
- Headline = first computable of EV/EBITDA → EV/Revenue → EV/EBIT; all methods reported
  for triangulation. The dashboard shows the full equity **bridge**
  (positioned multiple → EV → less net debt → less DLOM → equity).

### Confidence (discriminating — not a flat 0.98)
Sum of six weighted signals, including **output coherence**:
`0.20·profile + 0.20·peer_coverage + 0.10·(EBITDA>0) + 0.10·(methods/3) +
0.25·triangulation_agreement + 0.15·comparable_tightness`
→ HIGH ≥ 0.75, MEDIUM ≥ 0.50, else LOW. Triangulation agreement falls as the three
methods diverge; comparable tightness falls as the peer multiples scatter. Across the
universe this spans **0.31 – 0.95** (the 5 wrong entities score LOW), so the number
actually means something.

### Validation (anti-overfitting) — `python validate.py`
A single valuation matching its market cap proves nothing. `validate.py` treats **every
listed company as a target**, values it from its peers, and compares to its **own market
cap**. Quality-positioning must beat the naive flat-median baseline across the board:

```
corr(EBITDA margin, EV/EBITDA) = 0.49   (market prices quality; ~0 would make positioning luck)
                     mean|Δ|  median|Δ|  max|Δ|   ≤15%
POSITIONED (used)      8.1%      7.0%     26%    28/32
FLAT MEDIAN (naive)   10.5%      8.3%     37%    23/32
positioning wins on 24/32 targets → VERDICT: PASS (generalizes, not overfit)
```
The backtest is embedded in `result.json` (`validation`), shown on the dashboard, and
enforced as an acceptance check.

**Seed-robustness (1.6.0).** The same backtest is re-run on **5 freshly drawn
universes** (`validate.py :: seed_robustness` — every RNG seed shifted, so different
financials, listings and market noise). Positioning beat the naive median on **5/5**
(mean MAE 8.5% vs 10.7%), including a draw where corr collapsed to 0.04 and positioning
still did no harm (6.9% vs 7.5%). The canonical result is not seed-luck. Also runnable
live from the UI's Validation tab.

## Production-grade controls

**Structured audit trail** ([core/audit.py](core/audit.py)). Every material step is a
typed `AuditRecord`: `seq · ts (ISO-8601 UTC) · stage · level · code · detail · data`.
Levels are `INFO / WARN / DECISION / ERROR`; codes are stable and machine-readable
(`TARGET_RESOLVED`, `PEER_REJECTED`, `DISCOUNT_APPLIED`, `FALLBACK_HEADLINE`,
`DATA_QUALITY_GRADE`, `RANGE_WIDENED`, `NO_METHOD`, …). The trail is the single trust
mechanism in a touchless system, so it is exhaustive and ordered.

**Data-quality gate** (`validate_company`). Runs *before* valuation. Grades the target
A–D on a weighted per-field check list (revenue is critical; EV proxy & EBITDA high;
identity fields informational) and sets a `valuable` flag. Every failing check is
logged. If not `valuable`, the run degrades cleanly instead of fabricating a number.

**Fallback ladder** — every fallback is an audited `DECISION`/`WARN`:
| trigger | fallback | audit code |
|---|---|---|
| no D&B match | structured `status: no_match`, no crash | `NO_MATCH` |
| target not valuable | `status: insufficient_data`, no valuation | `INSUFFICIENT_DATA` |
| peer has no market EV | book capital-employed proxy | (disclosed in `ev_basis`) |
| preferred method uncomputable | EV/EBITDA → EV/Revenue → EV/EBIT | `FALLBACK_HEADLINE` |
| target driver ≤ 0 / <3 multiples | method skipped, reported | `METHOD_SKIPPED_*` |
| fewer than 10 peers | range widened P25/P75 → **P10/P90** | `RANGE_WIDENED` |
| no method computable | headline `"none"` + warning | `NO_METHOD` |

**Provenance metadata** (`result.meta`). Every result carries `methodology_version`,
`dnb_schema_version`, `run_timestamp`, `currency`, `source_units`→`reporting_units`,
`human_in_the_loop: false`, and a `status` (`ok / no_match / insufficient_data /
no_valuation`) so every archived valuation is reproducible and traceable.

**Never crashes on bad input.** `run_pipeline` always returns a structured result +
full audit trail; degraded runs render an honest minimal dashboard.

## Going live (one change)
In `mock_api/dnb_mock.py`, replace the body of `MockDnBClient.request` with the
documented `httpx.post` call (see its docstring) and inject the live client in
`run.py` at the `client = ...` seam. **Nothing else changes** — response paths are
identical. In production the universe DUNS list is built offline by paging
`company_search` across the target sector.

## Layout
```
dnb_valuation/
├── *.xlsx (9 files)           # raw Accord extracts (data layer input)
├── etl.py                     # Excel -> realdata.db (SQLite, provenance, recon gate)
├── realdata/client.py         # RealDnBClient: realdata.db -> D&B envelopes
├── mock_api/dnb_mock.py       # MockDnBClient + 59-company universe (real schema)
├── core/pipeline.py           # normalize, profile, validate, discover, value (stdlib)
├── core/audit.py              # structured, typed audit trail
├── run.py                     # orchestrator + CLI + acceptance suite
├── validate.py                # anti-overfitting backtest + seed-robustness sweep
├── api.py                     # FastAPI service: versioned REST + UI + HTML reports
├── dashboard/build_dashboard.py  # result dict -> self-contained report HTML
├── ui/index.html              # single-file report app (tabs, charts, print-to-PDF)
├── requirements.txt · Dockerfile
└── output/                    # result.json + dashboard.html (CLI outputs)
```
`core/` never imports `mock_api/`, `realdata/`, or `api.py`; the client is injected
in `run.py`. `api.py` is a pure delivery layer over the unchanged core.
