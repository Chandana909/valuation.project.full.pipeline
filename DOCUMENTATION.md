# MSME Comparable Valuation Engine — Complete Documentation (v2.1.0)

> **Purpose of this file.** Read this and you can run the working demo, use every
> API, understand every filter and formula, and extend the system — without any
> other context. If this document and the code ever disagree, the code and its
> two test suites (`run.py`, `test_api.py`) are the truth; fix this file.

---

## 1. What this is

A production-grade, **touchless**, **deterministic** comparable-company valuation
engine for Indian MSMEs. Given a company — either one of **13,906 valuation-grade
real companies** loaded from 9 Excel extracts, or a company **you describe through a
guided conversational intake** — it:

1. classifies the company's economic profile (rule-based, no LLM),
2. screens the whole universe through a **15-control filter chain**,
3. prices it off its peers with **sector-calibrated multiples**, quality positioning,
   an explicit scale-mismatch penalty, and a size-scaled DLOM,
4. returns a **range** (never a fake-precise point) with a confidence breakdown,
   data-quality grade, per-field source lineage, an indicative comparable-transactions
   view, and a complete typed audit trail,
5. serves everything through a REST API, a single-page UI, and print-to-PDF reports.

**Design contract (do not violate when extending):** the calculation core
(`core/`) is pure Python stdlib — no pandas/numpy/sklearn, no network, no LLM
calls; FastAPI exists only in the delivery layer (`api.py`); clients are injected;
degraded inputs produce honest degraded outputs, never fabricated numbers.

---

## 2. Quickstart — zero to working demo

Prerequisites: Python 3.11+, the 9 source `.xlsx` files in the repo root
(they ship with the repo).

```bash
pip install -r requirements.txt      # fastapi, uvicorn, openpyxl — nothing else
python etl.py                        # ~1-3 min: builds realdata.db from the Excels
python api.py                        # serves http://localhost:8733
```

Then open `http://localhost:8733`:

- **Search the database** — type "20 Microns", press Run valuation.
- **Describe your company** — click *Start guided intake*, answer ~12 questions.
- **Upload PDFs** — visible but disabled (extraction layer not wired yet).

Verify the installation (both must end PASS):

```bash
python run.py        # 39-check methodology acceptance suite (mock + real data)
python test_api.py   # 30-check live API integration suite (server must be running)
```

Other entry points:

```bash
python run.py "20 Microns Ltd." --data real     # CLI valuation -> output/result.json + dashboard.html
python validate.py                               # anti-overfitting backtest + 5-seed robustness sweep
python etl.py --report                           # print the stored ETL report of realdata.db
python make_parameter_dictionary.py              # regenerate output/parameter_dictionary.xlsx
```

Environment variables: `PORT` (default 8733) · `DATA_SOURCE` = `real`|`mock`
(default: auto — real when `realdata.db` exists) · `CORS_ORIGINS` (default `*`).

Docker: `docker build -t msme-valuation . && docker run -p 8733:8733 msme-valuation`
(build `realdata.db` first, or mount it).

**Windows note:** console output needs UTF-8 (₹ symbol); every entry point calls
`sys.stdout.reconfigure(encoding="utf-8")` already.

---

## 3. Repository layout

```
├── *.xlsx (9 files)              # raw Accord extracts — the data source
├── etl.py                        # Excel -> realdata.db (SQLite): header verify, P&L recon
│                                 #   gate, per-row provenance, source-file SHA-256, WAL
├── realdata/client.py            # RealDnBClient: realdata.db -> D&B-schema envelopes
├── mock_api/dnb_mock.py          # MockDnBClient: 59-company synthetic universe (has
│                                 #   synthetic market caps -> methodology backtests)
├── core/                         # PURE STDLIB calculation core (imports nothing above)
│   ├── pipeline.py               #   normalize, classify, validate, discover, value
│   ├── calibration.py            #   sector trading anchors (book -> market re-levelling)
│   └── audit.py                  #   typed, ordered audit trail
├── intake.py                     # conversational intake graph (12 questions) -> Company
├── run.py                        # orchestrator: run_pipeline / run_pipeline_custom,
│                                 #   confidence, CLI, 39-check acceptance suite
├── validate.py                   # anti-overfitting backtest + seed-robustness sweep
├── api.py                        # FastAPI delivery layer: 14 endpoints + UI + reports
├── dashboard/build_dashboard.py  # result dict -> self-contained report HTML
├── ui/index.html                 # single-file UI (search / chat intake / tabs / PDF)
├── test_api.py                   # 30-check live API integration suite
├── make_parameter_dictionary.py  # -> output/parameter_dictionary.xlsx (all parameters)
├── parameters_from_latest_report.xlsx   # the generated parameter dictionary (committed)
├── architecture.html             # all-in-one architecture + filter data-flow presentation
├── requirements.txt · Dockerfile
└── output/                       # CLI outputs: result.json, dashboard.html, samples
```

Import direction (enforced): `api.py → run.py → {core, clients, dashboard}`;
`core/*` imports **nothing** outside `core/`. The client is injected in `run.py`.

---

## 4. Data layer — the 9 Excel files

| File | Loaded columns | Provides |
|---|---|---|
| `Basic Data.xlsx` | accord, name, CD_Industry, NIC, description, CIN, ISIN | identity; industry (→ pseudo-NAICS + sector group + major letter); classifier text; listing status (CIN 'L') |
| `PL data.xlsx` | year, months, net sales, EBITDA (excl OI), other income, interest, PBDT, depreciation, PBT, PAT | the financial spine: 26,317 company-years |
| `BS data.xlsx` | plant & machinery, net block, inventories, total assets | balance-sheet detail |
| `Net worth.xlsx` | net worth | the book EV proxy (when no segment CE) |
| `Forex.xlsx` | FX inflow | exporter flag |
| `Segment.xlsx` | capital employed (summed per company-year) | preferred book EV proxy |
| `Product.xlsx`, `R&D.xlsx`, `Shareholding.xlsx` | — | stored, not consumed yet |

**ETL gates** (`etl.py`): header **verification** before any read (stale layout →
loud abort, never silent mis-mapping) · **P&L reconciliation** — EBITDA + Other
Income − Interest ≈ PBDT within max(2%, ₹0.5 Cr) — 99.1% of determinable rows pass ·
per-row **provenance** (source file + Excel row stored for every figure) ·
**SHA-256 fingerprint** of each source file stored in the DB (staleness detection) ·
WAL journal mode + ANALYZE.

**Valuation-grade universe:** revenue > 0 AND EBITDA present AND net worth > 0 AND
12-month period → **13,906 companies** (of 42,951 loaded). Latest + prior year kept.

**Honest gaps in the source (disclosed on every result):** no market prices, no
borrowings, no cash, no current liabilities. Consequences: multiples are
book-based **re-levelled by sector anchors** (§6); net debt assumed 0 with a
`NET_DEBT_UNKNOWN` warning.

---

## 5. The filter chain — all 15 controls

Peer discovery takes every valuation-grade company and passes it through four
stages. Example real run (20 Microns Ltd.): 13,905 candidates → 6,897 survive the
knock-outs → top 15 priced → effective (weighted) count ≈ 14.

**Stage A — Eligibility**
| # | Filter | Rule |
|---|---|---|
| 1 | Geography | India only (`countryISOAlpha2Code = "IN"`; the whole DB is Indian) |
| 2 | Self-exclusion | the target's own record is removed from its candidate pool |

**Stage B — Hard knock-outs** (rejected *before* scoring; every rejection logged
`PEER_REJECTED` with a reason)
| # | Filter | Rule |
|---|---|---|
| 3 | Operating model | must equal the target's (manufacturer / distributor / retailer / service). Classifier requires a manufacturing **verb** — the bare noun "manufacturers" in "sourced from manufacturers" does NOT make a distributor a manufacturer (collision rule) |
| 4 | Value chain | finished_goods vs raw_material must match |
| 5 | Major industry | derived major letter must match (D/F/G/I/N/T/C/U) |

**Stage C — Weighted similarity** (survivors scored 0–1; `score = Σ raw × weight`)
| # | Dimension | Weight | Raw rule |
|---|---|---|---|
| 6 | Industry proximity | 0.40 | same industry category (pseudo-NAICS) = 1.0 · same sector group = 0.6 · else 0 |
| 7 | Scale proximity | 0.20 | `1 / (1 + |log1p(rev_t) − log1p(rev_p)|)` |
| 8 | Margin proximity | 0.15 | `max(0, 1 − 5·|margin_t − margin_p|)` |
| 9 | Customer type | 0.15 | same B2B / B2C / mixed = 1.0 else 0 |
| 10 | Export profile | 0.10 | same exporter flag = 1.0 else 0.3 |

**Stage D — Post-scoring quality controls**
| # | Control | Rule |
|---|---|---|
| 11 | Top-N cut | top **15** by score enter the multiple math |
| 12 | Similarity weighting | weight = clamp((score − 0.40)/0.45, **0.15**, 1.0); borderline comps (< 0.85) taper; **effective peer count** = Σ weights drives range widening (±20pt → ±30pt when < 10) |
| 13 | Multiple eligibility | ≥ 3 **listed** market-EV comps per method, else book pool + **sector calibration** (§6); target driver must be > 0; ≥ 3 multiples after trimming |
| 14 | Outlier trim | Tukey fence 1.5×IQR per method (skipped when < 4 values) |
| 15 | Scale-mismatch penalty | target revenue outside [0.8 × smallest peer, 1.25 × largest peer] → all multiples adjusted: **−7.5%/log-decade below (cap 15%)**, **+5%/decade above (cap 10%)** — audited `COMPARABILITY_ADJUSTMENT` |

**Guarantees** (can never happen): a different-model/chain/industry peer reaching
the valuation; one outlier moving the answer; borderline comps dominating; a
size-mismatched peer set silently inflating a small company; a fabricated number
when nothing survives. **Cannot guarantee:** correctness of misdescribed source
data (it misclassifies *visibly* in the audit trail); product-level industry
overlap; market-exact levels without a price feed. Machine-readable version:
`GET /api/v1/filters`.

---

## 6. Valuation methodology

Per method (EV/EBITDA → EV/Revenue → EV/EBIT; first computable is the headline):

1. **Pool** — listed comps' market-EV multiples when ≥ 3 exist; else the book pool.
2. **Sector calibration** (`core/calibration.py`, book pool only) — book capital
   employed is *not* enterprise value (~1x book → 2–4x undervaluation). The peer
   distribution is re-levelled so its weighted median equals the **sector trading
   anchor** (20 sectors, India aggregates, Jan-2025 defaults; size factor ×0.80
   < ₹100 Cr, ×0.90 < ₹500 Cr). Shape/dispersion/weights preserved; factor clamped
   [0.25, 25]; audited `SECTOR_CALIBRATED`. *Replace this one table with a live
   market feed for exact levels — nothing else changes.*
3. **Trim** — Tukey 1.5×IQR on (multiple, weight) pairs.
4. **Position** — the central multiple is the weighted peer percentile at the
   target's **EBITDA-margin rank** (clamped P15–P85), NOT the flat median; band =
   ±20pt (±30 when effective peers < 10). Positioning never sees the target's own
   market value → the market cross-check stays independent.
5. **Adjust** — scale-mismatch penalty (filter 15) multiplies all three multiples.
6. **Bridge** — `EV = multiple × driver` → `− net debt` → `× (1 − DLOM)` = equity
   low/mid/high. **DLOM** (private targets only): 30% < ₹100 Cr · 25% < ₹500 Cr ·
   20% otherwise · 0% listed.
7. **Transactions view** — indicative acquisition range = headline equity ×
   (1 + 20/25/30% control premium); labelled derived (no deal DB in source).

**Confidence** = 0.20·profile + 0.20·effective-peer coverage + 0.10·EBITDA>0 +
0.10·methods + **0.25·triangulation agreement** + **0.15·comparable tightness**;
HIGH ≥ 0.75, MEDIUM ≥ 0.50, else LOW. It spans ~0.31–0.95 across the universe —
it discriminates. **Data quality** (before valuation): 8 weighted field checks →
grade A–D + a `valuable` gate; failing it → `insufficient_data`, no number.

**Anti-overfitting proof** (`validate.py`, runs on the mock which has synthetic
market caps): every listed company re-valued from its peers vs its own market cap —
positioning MAE **8.3%** beats the naive flat-median 10.5%, wins 24/32; corr(margin,
multiple) 0.49; robust on **5/5** freshly reseeded universes. Enforced by
acceptance check #12.

Every constant above lives in `parameters_from_latest_report.xlsx` (44 parameters,
code-verified complete; regenerate with `python make_parameter_dictionary.py`).

---

## 7. API reference (OpenAPI at `/docs`)

| Method + endpoint | Returns / notes |
|---|---|
| `GET /api/v1/health` | `{ok, ready}` — ready flips when the universe cache is warm |
| `GET /api/v1/status` | engine + data source + universe size + caveats |
| `GET /api/v1/companies/suggest?q=&limit=` | autocomplete over 13,906 names |
| `GET /api/v1/valuations?name=` | full result JSON — **404** no match · **422** insufficient data · 200 otherwise |
| `GET /api/v1/valuations/report?name=&print=1` | self-contained HTML report; `print=1` auto-opens the PDF dialog |
| `POST /api/v1/intake/start` | 201 → `{session_id, question{prompt, help, optional}, progress}` |
| `POST /api/v1/intake/{sid}/answer` `{value}` | validation error + same question, or next question, or `done: true` (send `"skip"` for optional) |
| `GET /api/v1/intake/{sid}` | session state |
| `POST /api/v1/intake/{sid}/value` | values the intake company vs database peers — **409** if incomplete |
| `GET /api/v1/intake/{sid}/report?print=1` | report for the intake valuation — **409** before /value |
| `GET /api/v1/filters` | all 15 controls + guarantees + limitations |
| `GET /api/v1/validation` | the backtest summary |
| `GET /api/v1/robustness` | 5-seed sweep (slow first call, then cached) |
| `GET /api/v1/database/status` | ETL report: counts, recon rate, source hashes, age |

Legacy routes consumed by the UI (kept 1:1): `/api/health,/api/status,
/api/companies,/api/suggest,/api/value,/api/robustness`. Result JSON top-level
keys: `meta, query, target, target_profile, target_lineage, source_caveats,
data_quality, peers, peers_ranked_count, rejected, rejected_total, valuation,
confidence, audit_trail` (+ `validation` when attached). `valuation` includes
`methods[], comparability_adjustment, transaction_analysis, market_cross_check,
effective_peer_count, ev_basis, discount, warnings, notes`.

**Intake question graph** (12 nodes, in order): name · description (≥15 chars —
feeds the classifier) · industry (matched to the DB's own category catalog) ·
listed y/n · revenue ₹Cr · prior revenue* · EBITDA (may be negative) ·
depreciation* · net worth · debt* · cash* · exporter y/n (* = skippable; skipped
debt/cash → net debt 0 with a disclosed warning). Deterministic state machine —
`IntakeSession.submit()` is the seam where an LLM front-end could translate free
text into answers.

---

## 8. UI guide (`/`)

Top bar: search box (server-side suggestions) · **Run valuation** · **Full
report** (opens the server-rendered report) · **Download PDF** (print dialog).
Three entry cards: database search · guided chat intake · PDF upload (disabled
placeholder). Result tabs: **Overview** (KPI tiles, football-field chart,
confidence bars, lineage) · **Filters** (all 15 controls, live funnel, rejected
sample, guarantees/limitations) · **Peer Analysis** (one stacked card per peer:
match %, 8 checks, 5 similarity bars, multiples, why selected) · **Valuation**
(triangulation table, equity bridge, comparability + transactions callouts) ·
**Validation** (backtest + on-demand robustness) · **Audit Trail** (every typed
record) · **API & Report** (this valuation's endpoints as clickable links + full
catalog).

---

## 9. Testing & verification

| Suite | Command | Coverage |
|---|---|---|
| Acceptance (39 checks) | `python run.py` | 3 mock targets × 11 methodology checks · anti-overfitting backtest gate · 5 real-data checks. Exit 0 iff all pass |
| API integration (30 checks) | `python test_api.py` (server running) | every endpoint · error mapping (404/409/422) · full intake conversation · determinism (identical re-run) · calibrated-multiple sanity band · report sections |

Run both after ANY change. The backtest gate is what prevents overfitting to a
single lucky example.

---

## 10. Extension guide

- **Live market prices (the highest-value upgrade):** feed `marketData`
  (market cap + EV) per listed company — extend `RealDnBClient._fin_env` to emit
  it (ISIN is already stored as the join key). Methods automatically switch from
  sector-calibrated book to true trading multiples; delete nothing.
- **PDF upload intake:** implement extraction → produce the same `answers` dict →
  `intake.build_company()`; the UI button and API pattern are already placed.
- **LLM classifier / LLM intake:** `build_profile(company)` and
  `IntakeSession.submit(value)` are the two marked seams; keep signatures.
- **Precedent transactions:** replace the derived control-premium block in
  `compute_valuation` with observed deal multiples; keep the output shape.
- **New valuation method:** add to `_METHOD_SPEC` in `core/pipeline.py`; if
  equity-based (P/E), branch the bridge so net debt isn't subtracted twice.
- **Tune anything:** every constant is in `parameters_from_latest_report.xlsx`
  with its code location.

## 11. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `realdata.db not found` | run `python etl.py` first |
| `ETL ABORT: … header … does not contain expected` | source Excel layout changed — update the column map in `etl.py` (this abort is the safety working) |
| API on mock data unexpectedly | `realdata.db` missing or `DATA_SOURCE=mock` set |
| First valuation slow (~4s) | one-time universe build; the server warms it in the background at startup |
| `UnicodeEncodeError ₹` in your own scripts | `sys.stdout.reconfigure(encoding="utf-8")` |
| Valuations look low for financial-sector companies | FIN group has no EV anchors by design (EV multiples are wrong for banks/NBFCs) — disclosed book basis until a P/B method is added |
