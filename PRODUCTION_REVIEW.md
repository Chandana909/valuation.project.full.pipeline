# Production Readiness Review — MSME Comparable Valuation Engine v2.1.0

**Reviewer stance:** Senior Valuation Engineer / Financial Software Architect /
Big-4 Valuation Partner. This review assumes deployment inside a regulated
financial institution where every output may face auditors, bankers and clients.
It is deliberately adversarial. Findings use: *Issue · Why it matters ·
Consequence · Example · Severity · Recommendation · Expected improvement.*

**Verdict up front (no burying the lede):** the engine's *architecture,
auditability and honesty machinery are genuinely institution-grade* — better
than most first-year commercial tools. Its *absolute valuation accuracy is
NOT yet institution-grade*, for one dominant reason: **the data source contains
no market prices, no borrowings and no cash**, so levels rest on a static,
hand-set sector-anchor table and net debt is assumed zero. Until a market-price
feed and borrowings data are wired in, outputs are defensible as **screening /
indicative valuations with disclosed basis**, not as fairness-opinion-grade
numbers. Everything below details that boundary and how to cross it.

---

## Part 1 — Valuation Methodology Review

### 1.1 Book-derived dispersion under sector re-levelling
- **Issue:** When <3 listed comps exist (always, on this extract), multiples come
  from the book pool re-levelled so the *median* hits the sector anchor. The
  *dispersion and ranks* of the re-levelled distribution are still book-derived.
  Positioning the target at its margin percentile assumes book-multiple ranks ≈
  market-multiple ranks. That rank-preservation is plausible but **unproven**.
- **Why it matters:** the low/high band and the positioned multiple inherit
  book-world shape, not market-world shape.
- **Consequence:** bands may be too tight or too wide vs real trading dispersion;
  positioning may reward/punish the wrong companies within a sector.
- **Example:** two chemical companies with equal book CE but different debt
  structures have identical book multiples and different true EVs.
- **Severity: HIGH.**
- **Recommendation:** when a market feed lands, back-fit: compute rank
  correlation between book and market multiples per sector; where it is weak,
  switch that sector's dispersion to the observed market band around the anchor.
- **Expected improvement:** honest bands; positioning validated per sector.

### 1.2 Single-year financials, no normalization
- **Issue:** Valuation uses the latest 12-month year only (prior year only for
  growth). No 3-year averaging, no cycle normalization, no LTM stub logic.
- **Consequence:** cyclical peaks/troughs are capitalized as if permanent —
  a commodity processor at peak margin gets a peak multiple on peak EBITDA.
- **Example:** a sugar company in a price-spike year values ~2× its mid-cycle worth.
- **Severity: HIGH.**
- **Recommendation:** deterministic normalization: use median EBITDA margin of
  the last 3 years × latest revenue as the "maintainable EBITDA" driver when
  3 years exist and margin volatility > threshold; disclose which was used.
  (P&L history is already in the extract — only the ETL "keep 2 years" cap
  blocks this; raise to 4.)
- **Expected improvement:** removes the largest single source of over/under-
  valuation for cyclicals.

### 1.3 Quality positioning is margin-only
- **Issue:** the central multiple is picked purely by EBITDA-margin percentile.
  Growth, returns on capital, size and leverage do not move the position.
- **Why it matters:** markets pay for growth and ROCE at least as much as for
  margin. Professional practice positions on multiple factors (or regresses
  multiple on fundamentals across peers — still deterministic).
- **Severity: HIGH.**
- **Recommendation:** deterministic composite quality rank:
  `q = 0.5·pct(margin) + 0.3·pct(revenue growth) + 0.2·pct(ROCE≈EBIT/CE)`
  (all computable today; ROCE from existing fields). Keep the clamp and window.
- **Expected improvement:** the mock backtest machinery can verify directly
  whether composite positioning beats margin-only (extend `validate.py`).

### 1.4 Triangulation & the ≤2.5 band
- **Issue:** the three methods are reported side by side (good) but the
  acceptance band `max/min ≤ 2.5` is arbitrary, and EV/Revenue gets equal
  visual weight even for margin-outlier targets where it is known-biased.
- **Severity: MEDIUM.**
- **Recommendation:** de-emphasize EV/Revenue automatically (report as
  "context, not headline") when |target margin − peer median margin| > 10pts;
  keep all numbers visible.

### 1.5 DLOM and control premium interaction
- **Issue:** DLOM (30/25/20%) is applied to reach a minority, non-marketable
  equity value; the transactions view then applies a 20–30% control premium *to
  that post-DLOM value*. Control transactions are typically priced off
  *marketable minority* levels — premium-on-post-DLOM mixes two adjustments.
- **Consequence:** the indicative acquisition range is understated for private
  targets (DLOM partially reversed in a control sale).
- **Severity: MEDIUM** (the block is labelled indicative).
- **Recommendation:** compute the acquisition view from the pre-DLOM equity:
  `acq = equity_before_DLOM × (1 + premium)`, and say so in the caveat.
- **Expected improvement:** internally consistent levels-of-value chain
  (control ↔ marketable minority ↔ non-marketable minority) — a question every
  Big-4 reviewer will ask (see Part 9).

### 1.6 Confidence score is not a calibrated probability
- **Issue:** the 6-signal blend is sensible ordinal machinery, but "0.71" has
  no defined statistical meaning; weights are hand-set.
- **Severity: MEDIUM.**
- **Recommendation:** map confidence deciles to empirical absolute-error bands
  from the backtest ("HIGH historically ⇒ |err| ≤ ~10% in backtest") and print
  that mapping on the report. Deterministic, honest, auditable.

### 1.7 Anchors are static and hand-set (the dominant methodology issue)
- **Issue:** `core/calibration.py` values are typed-in Jan-2025 aggregates —
  not observed, not refreshed, India-wide per ~20 coarse sectors, with a crude
  size factor (×0.80/<₹100 Cr, ×0.90/<₹500 Cr).
- **Consequence:** levels drift with markets; niche subsectors mis-anchored;
  every absolute number inherits this table's error.
- **Example:** capital-goods re-rating 2023-24 moved sector EV/EBITDA several
  turns; a static 13× misses it.
- **Severity: CRITICAL for absolute values; disclosed, so HIGH for the product
  as shipped.**
- **Recommendation:** replace the hand table with a *derived* table: ingest a
  monthly listed-universe price file (ISIN join key already stored), compute
  sector median EV/EBITDA / EV/Sales deterministically, version and store each
  vintage in the DB with its own hash. The code path already exists — only the
  source of the table changes.
- **Expected improvement:** from "plausible band" to "market-dated levels" with
  a documented refresh cycle — the single biggest accuracy upgrade available.

---

## Part 2 — Comparable Company Selection (filter-by-filter)

| Filter | Useful? | Keep? | Verdict & change |
|---|---|---|---|
| Geography (IN) | Yes | Keep (hard) | Correct as universe scope. Consider state/region as a *soft* dimension later (labour/logistics cost differ), weight ≤0.05. |
| Operating model | Yes — highest-value knock-out | Keep (hard) | BUT it rides on keyword classification of sometimes-empty descriptions (see Part 3). Add a "classifier confidence < 0.6 ⇒ demote to soft penalty −0.15" escape to avoid wrongful hard rejections. |
| Value chain | Yes | Keep (hard) | Binary raw/finished is crude for intermediates; acceptable with the same low-confidence demotion. |
| Major industry | Partially | **Demote** | It is *derived from the same keyword group* as the industry dimension — it double-counts industry and can hard-reject a mislabeled valid peer. Make it a strong soft penalty (−0.2) instead; industry proximity already carries 0.40. |
| Industry similarity (0.40) | Yes | Keep, rebuild granularity | 137 CD_Industry categories are too coarse ("Engineering" is enormous). **The NIC code column is loaded and unused** — build the hierarchy on NIC: same 5-digit class = 1.0, same 3-digit group = 0.8, same 2-digit division = 0.5, else 0. |
| Revenue similarity (0.20) | Yes | Keep, fix decay | See Part 4 — current form is too forgiving at 10×+ gaps. |
| EBITDA-margin similarity (0.15) | Yes | Keep | Reasonable; keep linear-cliff but cite the 20pt rationale in docs. |
| Customer type (0.15) | Yes | Keep at 0.10 | Classification is noisy from text; over-weighted at 0.15 given its noise. |
| Export flag (0.10) | Weakly | Keep at 0.05, upgrade to ratio | The data supports **export intensity = fx_inflow / revenue** — replace the binary flag with `1 − |int_t − int_p|` banding. |

### Missing dimensions that SHOULD exist (all computable from data already loaded)
1. **Revenue growth proximity** — markets price growth; entirely absent. Weight 0.10.
2. **Capital intensity** — `net_block / revenue` (both loaded). Separates asset-heavy
   from asset-light "engineering". Weight 0.10.
3. **ROCE proximity** — `EBIT / (net worth or seg CE)`. Quality of the business
   model. Weight 0.05–0.10.
4. **Working-capital profile** — `inventory / revenue` (loaded). Distinguishes
   project/inventory-heavy models. Weight 0.05.
5. **Export intensity** (upgrade of #10 above).
6. **R&D intensity** — `R&D.xlsx` is *shipped but unconsumed*. IP-led vs commodity.
7. **Promoter/ownership profile** — `Shareholding.xlsx` unconsumed; free-float
   and promoter concentration affect marketability and governance risk.
8. **Product overlap** — `Product.xlsx` unconsumed; token-overlap (deterministic
   Jaccard on normalized product strings) is a real comparability signal.
9. From richer D&B when live: recurring vs project revenue, OEM vs own-brand,
   contract manufacturing, customer concentration — text-rule extractable.

Recommended re-weighting after additions (sums to 1.0):
industry 0.30 · scale 0.15 · margin 0.10 · growth 0.10 · capital intensity 0.10 ·
ROCE 0.075 · working capital 0.05 · customer 0.075 · export intensity 0.05 ·
product overlap 0.10 (when texts exist; else redistribute pro-rata, disclosed).

---

## Part 3 — Hard Filter Review

- **Can they reject valid peers? Yes — two ways.** (a) Empty/sparse business
  descriptions push classification onto the major letter; a distributor with a
  blank description in a "D" industry group is classed manufacturer → *admitted*
  wrongly, while a manufacturer whose text says only "trading and manufacturing
  of…" may trip distributor keywords → *rejected* wrongly. (b) Major-industry
  equality double-punishes any category mislabeled in CD_Industry.
  **Severity: HIGH.** Mitigation implemented in-principle by classifier
  confidence; make the demotion rule concrete (Part 2 table).
- **Can they admit poor peers? Yes.** Within "MACH", a shipyard and a fastener
  maker both pass every hard filter. Granularity (NIC hierarchy) is the fix, not
  more hard filters.
- **Should more hard filters exist?** Only one: **active/operating status**
  (dormant shells with tiny stale revenue pass today; the new ₹0.1 Cr floor and
  plausibility screen catch part of this, but a "latest year not older than N years"
  gate should exist — fiscal-year staleness is currently unchecked).
  **Severity: HIGH** for staleness. Add: exclude companies whose latest fiscal
  year is > 3 years older than the target's.
- **Should some be removed?** Major-industry as a *hard* gate — demote (above).

---

## Part 4 — Similarity Model (mathematics)

- **Compensatory linear sum:** a peer can score 0 on industry and still reach
  0.55 from scale+margin+customer+export — nonsense comparables can outrank
  imperfect same-industry peers in sparse sectors. **Fix (deterministic):**
  non-compensatory gate: final score = linear score × min(1, industry_raw + 0.5)
  — i.e., a zero-industry peer is halved, never a "strong match".
- **Scale decay too slow:** `1/(1+|Δln|)`: at 10× revenue gap raw = 0.30; at
  100× raw = 0.18. A ₹50 Cr target vs ₹5,000 Cr peer keeps 6 of 20 scale points.
  **Fix:** `exp(−|Δln rev| / 1.2)` → 10× gap ⇒ 0.15, 100× ⇒ 0.02. Calibrate τ
  against the mock backtest.
- **Margin formula:** acceptable; note it treats +5pt and −5pt gaps identically
  although downside margin gaps matter more for multiples — optional asymmetry
  `1 − 6·max(0, m_t−m_p) − 4·max(0, m_p−m_t)`.
- **Export binary → intensity ratio** (Part 2).
- **Normalization:** raw dimensions are already [0,1] — fine. Weights sum to 1 —
  fine. Rounding at 4dp — fine.
- **Suggested model (deterministic, complete):**
  `score = gate(industry) × Σ w_i · raw_i` with the Part-2 weight set, gate as
  above, exponential scale decay, NIC-hierarchy industry, intensity-based export,
  plus growth/capex/ROCE/WC dimensions. Every term closed-form and auditable.

---

## Part 5 — Peer Selection: Top-15 vs threshold

Fixed Top-15 is the wrong primitive and the engine already half-knows it (it
had to invent borderline down-weighting and effective-count widening to survive
Top-15's failure mode). **Recommendation — threshold-with-ladder:**
1. take all peers with score ≥ 0.60 (cap 20 by score);
2. if < 8, extend to score ≥ 0.50 flagged "extended set", widen band to ±30;
3. if still < 5 → status `insufficient_peers`, no headline, intake/manual review.
This makes peer count an *output* of comparability, not an input constant —
exactly what a reviewer expects ("why 15?" has no defensible answer; "score ≥
0.60, here is the score definition" does). Keep similarity weighting on top.
**Severity of current design: MEDIUM** (mitigations exist). **Expected
improvement:** honest peer counts in sparse sectors; no dilution in rich ones.

---

## Part 6 — Statistical Review

- **Tukey 1.5×IQR on n as low as 4:** quartiles on 4 points are noise; the fence
  either does nothing or deletes a real observation. **Fix:** require n ≥ 8 for
  trimming; below that, use min/max winsorization at the 10th/90th weighted
  percentile. Severity: MEDIUM.
- **Weighted percentile:** implementation (cumulative-midpoint interpolation) is
  correct and monotonic. Sound.
- **Effective peer count = Σw:** fine as a coverage notion, but the statistically
  standard measure is **Kish effective n = (Σw)²/Σw²**; Σw overstates
  effective information when weights are unequal. Swap; thresholds re-tune.
  Severity: LOW-MEDIUM.
- **Borderline weighting (taper to 0.15 floor):** sound and well-disclosed. The
  0.40/0.85 knots are arbitrary — document them as policy, or fit to backtest.
- **Scale adjustment (−7.5 %/decade, +5 %/decade):** direction is supported by
  size-premium literature; the magnitudes are uncited. Keep, but label the
  parameters "policy constants pending calibration" in the dictionary, and
  calibrate against the market feed when available. Severity: LOW (disclosed).
- **The elephant: validation runs on the synthetic mock only.** corr=0.49,
  MAE 8.3% are properties of a universe *constructed* to price quality. They
  prove the machinery, not real-world accuracy. No institutional team will
  accept synthetic-only validation. **Severity: CRITICAL** for accuracy claims.
  **Recommendation:** the moment prices land, run the identical backtest on the
  real listed subset (the code is source-agnostic already) and publish the real
  error distribution on every report.

---

## Part 7 — Financial Review

- **Net debt = 0 when unknown:** overstates equity of levered companies by the
  full debt amount; understates cash-rich ones. Warning-disclosed, but the
  *numbers* still carry the bias. **Severity: CRITICAL** for DB targets
  (intake targets can supply debt/cash — already implemented).
  **Recommendation:** (a) procure borrowings columns (they exist in Accord's
  full export); (b) until then, print equity as "EV-basis equity assuming zero
  net debt" in the headline label itself, not only in warnings; (c) for sectors
  with structurally high leverage (construction, power, telecom) force
  confidence ≤ MEDIUM. 
- **Capital employed proxy = net worth:** understates CE for levered companies →
  overstates book multiples pre-calibration; post-calibration the median error
  washes out but cross-sectional ranks retain the bias (links to 1.1).
- **Listed/unlisted from CIN prefix:** reasonable; note ~data-entry risk; ISIN
  presence is a corroborating signal — use both (listed = CIN 'L' OR ISIN
  non-null & exchange-format).
- **Separate valuation paths should exist:** YES —
  financials (banks/NBFC/insurance/AMC): P/B and P/E on net worth & PAT (both
  loaded) — EV methods are conceptually invalid; real estate/holding: NAV /
  sum-of-parts (needs data not in extract → honest `unsupported_industry`
  status); utilities: CE-based (RAB-like) methods are actually defensible here.
  Today FIN falls to un-anchored book basis — *silently weak*. Route it.
  **Severity: HIGH.**
- Minority interests, preference capital, contingent liabilities, leases: absent
  from the extract; list them in `source_caveats` explicitly (one line each) so
  reviewers see the full bridge-item inventory. Severity: LOW (disclosure).

---

## Part 8 — Industry Edge Cases (who fails and why)

| Industry | Failure mode today | Route |
|---|---|---|
| Banks / NBFC / Insurance / AMC | EV/EBITDA meaningless (interest is the business); FIN group unanchored → raw book basis | Hard-route to P/B–P/E path (Part 7); until built → status `unsupported_industry`, never a number |
| Software / SaaS | anchors for "IT" too coarse; no ARR/retention data; loss-makers fall to EV/Revenue with a sector-wide 2.8× that ignores growth dispersion | growth-adjusted positioning (1.3) helps; real fix needs richer descriptors |
| Healthcare (hospitals vs pharma) | one PHARMA group merges asset-heavy hospitals with IP-led pharma | NIC hierarchy split |
| Construction / EPC | lumpy POC revenue, high WIP, leverage unknown | staleness gate + forced-widen band + leverage caveat |
| Utilities / Power | PPA/regulated returns; leverage dominant and unknown | CE-methods OK, but net-debt gap is fatal for equity — label EV-only |
| Holding companies | consolidated vs standalone ambiguity; SOTP required | detect ("holding"/"investment" keywords) → `unsupported_industry` |
| Real estate | NAV world; book ≠ market land values | route out |
| Consumer brands | brand intangibles absent from book; even anchored multiples under-rank strong brands (margin positioning partially compensates) | product/brand text dimension |
| Mining / Oil & Gas | reserves drive value, not current EBITDA; cyclicality (1.2) | normalization + explicit caveat |
| Telecom | leverage + capex intensity; EV/EBITDA−capex is the real metric | capital-intensity dimension + net-debt fix |

The engine's honest-degradation machinery makes these *visible* failures, not
silent ones — but several should be *routed* failures (`unsupported_industry`)
rather than weak numbers. **Add that status.**

---

## Part 9 — Audit Review (what PwC/Deloitte/EY/KPMG will ask)

1. *"Source and vintage of the sector anchors? Who approves changes?"* — today:
   hardcoded, git history is the change log. **Demand:** parameter governance —
   anchors in a versioned data file, change requires a recorded rationale;
   embed `parameters_hash` in every result meta. (Not yet present — gap.)
2. *"Why 1.5×IQR, why 15 peers, why ±20pts, why 30/25/20 DLOM, why 20–30%
   premium?"* — the parameter dictionary answers *what*, partially *why*; the
   honest answer for several is "policy constant, pending calibration" — say
   exactly that in the dictionary (done for some; extend to all).
3. *"244 P&L rows failed reconciliation — were they excluded from the peer
   pool?"* — **No. They are flagged in lineage but still eligible as peers.**
   A reviewer will (rightly) object. **Fix:** exclude recon-failed company-years
   from the universe (they are 0.9% — cheap). **Severity: HIGH, easy fix.**
4. *"Your backtest is synthetic"* — concede; present it as machinery validation;
   commit to the real backtest upon feed integration (Part 6).
5. *"Levels of value chain?"* — fix 1.5 and the answer is clean.
6. *"Reproducibility?"* — result meta carries methodology version + timestamps;
   **add the git commit SHA and DB etl_timestamp + source hashes** to meta so a
   result is reproducible bit-for-bit. (DB hashes exist in DB, not in results.)
7. *"Show me one number's full derivation"* — the audit trail + lineage +
   parameter dictionary genuinely support this today. This is the system's
   strongest audit asset. Evidence: 55-record typed trail per run.

---

## Part 10 — Hallucination Review

Deterministic? **Yes** — verified: identical inputs produce identical outputs
(test_api determinism check); real path has no RNG (custom-target IDs use uuid,
cosmetic only); mock is seeded. No LLM anywhere in the calculation path; the
LangGraph intake nodes are pure validators — unanswered fields stay `None` and
trigger warnings rather than being filled.

Residual "fabrication-shaped" risks (values presented that were never observed):
1. **Sector anchors, size factors, DLOM bands, control premium, penalty rates** —
   policy constants, not observations. All disclosed; the exposure is a *reader
   skimming past disclosures*. Mitigation: the report now prints basis strings
   in the headline sections (keep them there).
2. **"NAICS 1520"-style labels** — pseudo-NAICS codes displayed with a real
   taxonomy's name can mislead a reader into thinking official NAICS was used.
   **Recommendation:** relabel as "industry code (internal)" in UI/report.
   Severity: MEDIUM (presentation honesty).
3. **Mock company names resemble real firms** (Woodward, Kirloskar…) — fine for
   dev; ensure the mock is never served in production (env guard: refuse
   DATA_SOURCE=mock when `ENV=production`). Severity: MEDIUM.
4. Intake figures are user-asserted, unverified — correctly lineage-flagged.

---

## Part 11 — Explainability Review

- *Can an auditor trace every number?* Largely **yes**: per-field lineage
  (file+row+fy), typed audit trail with the calibration factor, positioning
  percentile, adjustment amounts, per-peer component scores and weights, and a
  code-verified parameter dictionary. This is above industry norm.
- Weaknesses: (a) no `parameters_hash`/git SHA in result meta (Part 9.6);
  (b) intermediate rounding (4dp) is applied at several stages — document the
  rounding policy once in the dictionary; (c) the sector-anchor *derivation*
  is a citation, not data — becomes traceable only with the derived-anchor
  service (1.7); (d) confidence weights' rationale is one line — expand.

---

## Part 12 — Production Readiness (blockers, classified)

**CRITICAL**
1. No market-price feed → static anchors carry absolute levels (1.7).
2. Net debt unknown for all DB targets (7.1).
3. Accuracy validated only on synthetic data (6).
4. Financial-sector companies produce weak numbers instead of being routed
   (7/8) — reputational risk with any FIN query.

**HIGH**
5. Recon-failed rows eligible as peers (9.3) — one-line fix.
6. Fiscal-year staleness unchecked in peer pool (3).
7. NIC granularity unused → coarse industry matching (2).
8. Single-year drivers, no cyclical normalization (1.2).
9. No authentication/authorization/rate limiting on the API; CORS `*` default.
10. Results are not persisted (no immutable run ledger); intake sessions are
    in-memory and die on restart.
11. Single-process, lock-serialized valuations (~2s each) — fine for a desk,
    not for concurrent load; no horizontal-scale story (SQLite is fine
    read-only; the universe cache per process is the constraint).

**MEDIUM**
12. No CI (suites exist but run manually); no pinned dependency lockfile.
13. Repo runs from OneDrive-synced path in dev — file-lock flakiness risk.
14. Structured logging is plain text; no request IDs.
15. Pseudo-NAICS labelling (10.2); mock-in-prod guard (10.3).
16. DLOM/premium interaction (1.5).

**LOW**
17. CRLF/LF mixing warnings; Windows-only tested; ₹-encoding footguns handled.

---

## Part 13 — Missing Data (fields worth ingesting)

**Already IN the shipped files, unconsumed (use first — zero procurement):**
NIC economic-activity code (Basic Data — the industry-granularity fix) ·
R&D spend (R&D.xlsx) · promoter/institutional shareholding (Shareholding.xlsx) ·
product lines (Product.xlsx) · ISIN (market-feed join key).

**From the Accord source (procure columns):** borrowings, cash & equivalents,
current liabilities, market capitalization, trade payables detail, employee
count, incorporation year, auditor remarks.

**From D&B when live:** full business description + Hoovers description
(rule-extractable: recurring vs project, OEM vs brand, contract mfg) ·
corporate family tree / global ultimate (exclude subsidiaries whose economics
are parent-driven — currently a hidden peer-quality hole) · import/export % ·
employees · year started · legal form/ownership · PAYDEX & financial-stress
scores (credit dimension) · SIC/NAICS official codes · UBO · branch count.

---

## Part 14 — Accuracy Verdict

Can the current methodology consistently produce good *comparable companies*?
**Within data limits, yes** — the staged design (knock-outs → granular-enough
similarity → weighting → penalties) is the correct professional shape, and with
the NIC hierarchy + the added dimensions (Part 2) it reaches genuinely strong
peer selection on this dataset.

Can it consistently produce institution-grade *values*? **Not yet.** The
binding constraints are external data (prices, debt), not logic: levels ride on
a static anchor table, equity bridges assume zero net debt, and validation is
synthetic. As shipped it is an honest, auditable **indicative-valuation and
peer-intelligence system**; it becomes institution-grade valuation the week the
market feed + borrowings land, because every downstream mechanism (market pool,
cross-check, backtest, bridge) is already built and tested for that data.

---

## Part 15 — Final Architecture (revised, deterministic, D&B-grounded)

```
INGESTION PLANE (scheduled, versioned)
  Accord/D&B extracts ─┐
  market-price file ───┼─ ETL gates (header/recon/plausibility/staleness)
  transactions file ───┘        │  every snapshot: SHA-256 + vintage id
                                ▼
FEATURE STORE (SQLite→Postgres when concurrent)
  companies · financials(4yr) · market · deals · derived features
  (growth, ROCE, capital intensity, WC, export intensity, NIC hierarchy)
                                ▼
CLASSIFICATION SERVICE (deterministic rules, confidence-scored)
  operating model / value chain / customer / industry(NIC) / status
  low-confidence ⇒ soft-penalty mode, never silent hard rejection
                                ▼
PEER ENGINE
  hard gates: geography · self · staleness · operating-model(conf≥0.6)
  similarity: non-compensatory gated score, Part-2 weights
  selection: threshold ladder (≥0.60 → ≥0.50 flagged → insufficient_peers)
                                ▼
METHOD ROUTER (by industry class)
  industrial/services → EV comps (market pool primary; derived-anchor
     calibration fallback, monthly vintage)
  financials → P/B + P/E path      real-estate/holding → unsupported (honest)
                                ▼
VALUATION CORE (stdlib, unchanged philosophy)
  normalized drivers (3yr) · composite quality positioning · winsor/Tukey by n
  · scale penalty (calibrated) · net-debt bridge (real, or labelled EV-basis)
  · DLOM (pre-control chain fixed) · transactions view from observed deals
                                ▼
UNCERTAINTY & CONFIDENCE
  empirical error bands per confidence decile from CONTINUOUS real backtest
                                ▼
RUN LEDGER (immutable)
  result + audit trail + code SHA + parameters_hash + data vintage ids
                                ▼
DELIVERY
  authenticated API (keys, rate limits) · UI · reports · webhook exports
GOVERNANCE
  parameter file versioning + four-eyes change log · CI running both suites
```

Everything in this design is deterministic, explainable, auditable and
D&B-groundable; ~70% of it already exists in v2.1.0 — the deltas are the data
feeds, the method router, the NIC hierarchy, threshold selection, the run
ledger, and parameter governance.

---

## Addendum — the specific data-quality questions

**Zeros / negatives / garbage in raw columns — handling as implemented:**
header verification (mis-mapped columns abort) · P&L reconciliation flag per
row (99.1% pass; recommendation 9.3: exclude failures from the pool) ·
valuation-grade gate (revenue ≥ ₹0.1 Cr, EBITDA present, net worth > 0, 12-month
periods) · **plausibility screen** (|EBITDA| ≤ 1.5×revenue — a "170% margin" is
a data error; 287 rows removed) · negative EBITDA **kept deliberately**
(loss-making is real; EV/EBITDA skips with an audited reason, EV/Revenue
prices) · zeros in optional fields flow to `None` → disclosed-warning paths ·
Tukey/winsorization catches residual outlier multiples · nothing is ever
imputed or invented.

**Critical missing financials — handling as implemented:** the data-quality
gate grades A–D and blocks unvaluable targets (`insufficient_data`, no number);
missing debt/cash → net-debt-0 with `NET_DEBT_UNKNOWN` warning and DQ penalty;
missing depreciation → EV/EBIT skipped with audit code; **and the API/UI now
actively route the user to the guided intake agent** (`hint` field + UI CTA) so
a human can supply the missing figures — which then carry "user-provided"
lineage rather than silent assumptions.

*End of review — v2.1.0, 2026-07-17. 69/69 automated checks green at time of
writing; the findings above are the distance between "green checks" and
"institution-grade valuation", stated without cosmetics.*
