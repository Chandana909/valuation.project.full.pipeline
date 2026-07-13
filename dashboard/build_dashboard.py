"""
dashboard/build_dashboard.py — render output/result.json to a self-contained
output/dashboard.html using plain string formatting. No template engine, no
external CSS/JS. Stdlib only.

Design goals (v1.2):
  * The three headline sections (Target, Valuation, Confidence & Data Quality) are
    STACKED VERTICALLY (full width), not crammed side-by-side, for readability.
  * Every section opens with a short "theory" paragraph explaining WHAT is shown
    and HOW it was derived, so a non-specialist can follow the methodology.
  * The valuation section shows the full equity BRIDGE
    (multiple -> enterprise value -> less net debt -> less DLOM -> equity).
Degraded runs (no_match / insufficient_data) render a minimal, honest page.
"""

import json
import html


def _esc(x):
    return html.escape("" if x is None else str(x))


def _num(x, nd=1):
    if x is None:
        return "n/a"
    return f"{x:,.{nd}f}"


def _pct(x, nd=1):
    if x is None:
        return "n/a"
    return f"{x * 100:,.{nd}f}%"


def _chip(label, value):
    return f'<span class="chip"><b>{_esc(label)}</b> {_esc(value)}</span>'


def _conf_class(label):
    return {"HIGH": "hi", "MEDIUM": "med", "LOW": "lo"}.get(label, "med")


def _grade_class(grade):
    return {"A": "hi", "B": "hi", "C": "med", "D": "lo"}.get(grade, "med")


def _level_class(level):
    return {"INFO": "lv-info", "WARN": "lv-warn",
            "DECISION": "lv-dec", "ERROR": "lv-err"}.get(level, "lv-info")


def _fact(label, value, sub=None):
    s = f'<div class="sub-note">{_esc(sub)}</div>' if sub else ""
    return (f'<div class="fact"><div class="fact-k">{_esc(label)}</div>'
            f'<div class="fact-v">{value}</div>{s}</div>')


_STYLE = """
:root {
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2b3444;
  --ink:#e6edf3; --muted:#9aa7b4; --accent:#4f9cf9; --good:#3fb950;
  --warn:#d29922; --bad:#f85149; --dec:#a371f7;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; font-size:14px;
  line-height:1.45; }
.wrap { max-width:1040px; margin:0 auto; padding:26px; }
h1 { font-size:23px; margin:0 0 6px; }
.sub { color:var(--muted); margin-bottom:6px; font-size:13px; }
.provenance { color:var(--muted); font-size:12px; margin:10px 0 4px;
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px; }
.status-ok { color:var(--good); } .status-bad { color:var(--bad); }
.intro { color:var(--muted); font-size:13.5px; margin:14px 0 20px; }

/* stacked full-width sections */
.section { background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:20px 22px; margin-bottom:18px; }
.section-num { color:var(--accent); font-weight:700; margin-right:8px; }
.section h2 { font-size:16px; margin:0 0 4px; border:0; padding:0; text-transform:none;
  letter-spacing:0; color:var(--ink); }
.theory { color:var(--muted); font-size:13px; line-height:1.55; margin:6px 0 16px;
  border-left:2px solid var(--line); padding-left:12px; }
.theory b { color:var(--ink); font-weight:600; }

.facts { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px; }
.fact-k { color:var(--muted); font-size:11.5px; text-transform:uppercase; letter-spacing:.04em; }
.fact-v { font-size:15px; margin-top:3px; }
.sub-note { color:var(--muted); font-size:11.5px; margin-top:2px; }

.badge { display:inline-block; padding:6px 14px; border-radius:20px; font-weight:700; font-size:15px; }
.badge.hi { background:rgba(63,185,80,.15); color:var(--good); border:1px solid var(--good); }
.badge.med { background:rgba(210,153,34,.15); color:var(--warn); border:1px solid var(--warn); }
.badge.lo { background:rgba(248,81,73,.15); color:var(--bad); border:1px solid var(--bad); }

/* valuation headline */
.headline { display:flex; gap:26px; align-items:flex-end; margin:6px 0 16px; flex-wrap:wrap; }
.hv { text-align:center; }
.hv .k { color:var(--muted); font-size:12px; }
.hv .n { font-size:30px; font-weight:800; }
.hv .n.mid { color:var(--accent); }
.unit { color:var(--muted); font-size:12px; }
.bridge { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
.bridge .step { background:var(--panel2); border:1px solid var(--line); border-radius:8px;
  padding:8px 12px; font-size:12.5px; }
.bridge .step b { display:block; font-size:15px; margin-top:2px; }
.bridge .op { color:var(--muted); font-weight:700; font-size:16px; }

table { width:100%; border-collapse:collapse; margin-top:6px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px;
  vertical-align:top; }
th { color:var(--muted); text-transform:uppercase; font-size:11px; letter-spacing:.05em; }
tr:hover td { background:var(--panel2); }

.peer { background:var(--panel2); border:1px solid var(--line); border-radius:10px;
  padding:14px; margin-bottom:12px; }
.peer-head { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
.peer-name { font-weight:700; font-size:15px; }
.tag { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--line); color:var(--muted); }
.tag.listed { color:var(--accent); border-color:var(--accent); }
.score { font-weight:700; color:var(--accent); }
.chip { display:inline-block; background:var(--panel); border:1px solid var(--line);
  border-radius:6px; padding:2px 8px; margin:3px 4px 0 0; font-size:12px; }
.chip b { color:var(--muted); font-weight:600; }
.mult { color:var(--good); }
.because { color:var(--good); font-size:12px; margin-top:6px; }
.diffs { color:var(--warn); font-size:12px; margin-top:2px; }
.reason { color:var(--bad); }
.note { color:var(--warn); }
.mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; }
.hl-row { background:rgba(79,156,249,.10); }
.lvl { font-weight:700; font-size:11px; padding:1px 7px; border-radius:5px; }
.lv-info { color:var(--muted); border:1px solid var(--line); }
.lv-warn { color:var(--warn); border:1px solid var(--warn); }
.lv-dec  { color:var(--dec); border:1px solid var(--dec); }
.lv-err  { color:var(--bad); border:1px solid var(--bad); }
.dq-pass { color:var(--good); } .dq-warn { color:var(--warn); } .dq-fail { color:var(--bad); }
.two { display:grid; grid-template-columns:1fr 1fr; gap:22px; }
@media (max-width:720px){ .two { grid-template-columns:1fr; } }
"""


def _head(title):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_esc(title)}</title><style>{_STYLE}</style></head>'
            f'<body><div class="wrap">')


def _provenance(meta):
    status = meta.get("status", "?")
    scls = "status-ok" if status == "ok" else "status-bad"
    return (f'<div class="provenance">'
            f'{_esc(meta.get("engine"))} v{_esc(meta.get("methodology_version"))} · '
            f'status <span class="{scls}">{_esc(status)}</span> · '
            f'run {_esc(meta.get("run_timestamp"))} · '
            f'source {_esc(meta.get("data_source"))} ({_esc(meta.get("dnb_schema_version"))}) · '
            f'units {_esc(meta.get("source_units"))}→{_esc(meta.get("reporting_units"))} '
            f'{_esc(meta.get("currency"))} · '
            f'touchless={_esc(not meta.get("human_in_the_loop"))}</div>')


def _audit_table(audit):
    rows = ['<table><tr><th>#</th><th>Time</th><th>Stage</th><th>Level</th>'
            '<th>Code</th><th>Detail</th></tr>']
    for a in audit:
        ts = (a.get("ts") or "")[11:23]
        rows.append(
            f'<tr><td>{a.get("seq")}</td>'
            f'<td class="mono">{_esc(ts)}</td>'
            f'<td class="mono">{_esc(a.get("stage"))}</td>'
            f'<td><span class="lvl {_level_class(a.get("level"))}">{_esc(a.get("level"))}</span></td>'
            f'<td class="mono">{_esc(a.get("code"))}</td>'
            f'<td>{_esc(a.get("detail"))}</td></tr>')
    rows.append("</table>")
    return "".join(rows)


def build_dashboard(result_path, out_path):
    with open(result_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    meta = data.get("meta", {})
    audit = data.get("audit_trail", [])
    parts = [_head(f"MSME Valuation — {data.get('query')}")]
    parts.append(f'<h1>MSME Comparable-Company Valuation — {_esc(data.get("query"))}</h1>')
    parts.append(_provenance(meta))

    # ---- degraded runs -------------------------------------------------
    if data.get("target") is None or data.get("valuation") is None:
        parts.append(f'<div class="section"><h2>Result</h2>'
                     f'<p class="theory">No valuation was produced — status '
                     f'<b>{_esc(meta.get("status"))}</b>. Nothing is fabricated; the '
                     f'audit trail below records exactly why the pipeline stopped.</p></div>')
        parts.append(f'<div class="section"><h2>Audit Trail ({len(audit)})</h2>')
        parts.append(_audit_table(audit))
        parts.append("</div></div></body></html>")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("".join(parts))
        return out_path

    t = data["target"]
    tp = data["target_profile"]
    dq = data.get("data_quality") or {}
    val = data["valuation"]
    conf = data["confidence"]

    # ---- report intro --------------------------------------------------
    parts.append(
        '<p class="intro">This report estimates the <b>equity value</b> of the subject '
        'company using <b>comparable-company (trading multiples) analysis</b>. The engine '
        'is grounded on a single data source (Dun &amp; Bradstreet), is fully '
        '<b>deterministic</b> (no LLM, no randomness), and is <b>touchless</b> — trust rests '
        'on the confidence score, the data-quality grade, and the complete audit trail. '
        'Every figure below is a <b>range</b>, never a false-precision point estimate.</p>')

    # ===================================================================
    # SECTION 1 — TARGET PROFILE  (stacked, full width)
    # ===================================================================
    parts.append('<div class="section">')
    parts.append('<h2><span class="section-num">1</span>Target Profile &amp; Financials</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> The subject company\'s identity, its '
        'industry classification, its rule-based <b>economic profile</b> (operating model, '
        'value chain, customer type), and its latest financials. '
        '<b>How it\'s derived.</b> Pulled from D&amp;B <span class="mono">company_information</span> '
        'and <span class="mono">company_financials</span>; every monetary field is converted '
        'from INR&nbsp;Thousand to <b>INR&nbsp;Crore</b> (÷10,000). EBIT is computed as '
        'EBITDA − depreciation; revenue growth from the prior fiscal year. These figures are '
        'the <b>drivers</b> the peer multiples are later applied to.</p>')
    parts.append('<div class="facts">')
    parts.append(_fact("Identity", f"{_esc(t['name'])}",
                       f"DUNS {t['duns']} · CIN {t['cin']}"))
    parts.append(_fact("Economic profile",
                       f"{_esc(tp['operating_model'])} / {_esc(tp['value_chain'])}",
                       f"{tp['customer_type']} · {'exporter' if t['is_exporter'] else 'domestic'}"))
    parts.append(_fact("Industry (NAICS)", f"{_esc(t['naics'])} · sub {_esc(tp['naics_subsector'])}",
                       _esc(t['naics_desc'])))
    parts.append(_fact("Listing", "Listed" if t['listed'] else "Unlisted",
                       f"D&B major {t['major_industry']}"))
    parts.append(_fact("Revenue", f"₹{_num(t['revenue_cr'])} Cr",
                       f"growth {_pct(t['revenue_growth'])} YoY"))
    parts.append(_fact("EBITDA", f"₹{_num(t['ebitda_cr'])} Cr",
                       f"margin {_pct(t['ebitda_margin'])}"))
    parts.append(_fact("EBIT", f"₹{_num(t['ebit_cr'])} Cr", "= EBITDA − depreciation"))
    parts.append(_fact("Net worth", f"₹{_num(t['net_worth_cr'])} Cr",
                       f"net debt ₹{_num(val['net_debt_cr'])} Cr"))
    parts.append('</div></div>')

    # ===================================================================
    # SECTION 2 — HEADLINE VALUATION  (stacked, full width, with bridge)
    # ===================================================================
    hm = next((m for m in val["methods"] if m["method"] == val["headline_method"]), None)
    parts.append('<div class="section">')
    parts.append('<h2><span class="section-num">2</span>Headline Valuation</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> The estimated equity value range from the '
        f'primary method (<b>{_esc(val["headline_method"])}</b>). '
        '<b>How it\'s built.</b> We take the <b>median trading multiple</b> of the listed '
        'comparable companies, multiply it by the target\'s corresponding driver to get an '
        'implied <b>enterprise value (EV)</b>, subtract <b>net debt</b> (debt − cash) to bridge '
        'from EV to equity, then apply a <b>DLOM</b> (discount for lack of marketability) because '
        'a private MSME is less liquid than the listed peers whose multiples we used. '
        'Low / mid / high correspond to the peers\' '
        f'<b>{_esc(hm["range_basis"]) if hm else "P25/median/P75"}</b> multiples.</p>')
    parts.append('<div class="headline">')
    parts.append(f'<div class="hv"><div class="k">Low</div><div class="n">{_num(val["equity_low_cr"])}</div></div>')
    parts.append(f'<div class="hv"><div class="k">Mid (central estimate)</div><div class="n mid">{_num(val["equity_mid_cr"])}</div></div>')
    parts.append(f'<div class="hv"><div class="k">High</div><div class="n">{_num(val["equity_high_cr"])}</div></div>')
    parts.append('<div class="unit">equity value · INR Crore</div>')
    parts.append('</div>')
    # equity bridge (mid)
    if hm:
        parts.append('<div class="bridge">')
        parts.append(f'<div class="step">{_esc(hm["method"])} median<b>{_num(hm["multiple_median"],1)}x</b></div>')
        parts.append('<div class="op">×</div>')
        parts.append(f'<div class="step">target driver<b>₹{_num(hm["target_driver"])} Cr</b></div>')
        parts.append('<div class="op">=</div>')
        parts.append(f'<div class="step">enterprise value<b>₹{_num(hm["ev_mid_cr"])} Cr</b></div>')
        parts.append('<div class="op">−</div>')
        parts.append(f'<div class="step">net debt<b>₹{_num(val["net_debt_cr"])} Cr</b></div>')
        parts.append('<div class="op">−</div>')
        parts.append(f'<div class="step">DLOM<b>{_pct(val["discount"],0)}</b></div>')
        parts.append('<div class="op">=</div>')
        parts.append(f'<div class="step" style="border-color:var(--accent)">equity (mid)<b style="color:var(--accent)">₹{_num(val["equity_mid_cr"])} Cr</b></div>')
        parts.append('</div>')
    parts.append(f'<p class="sub-note" style="margin-top:12px">{_esc(val["discount_reason"])} · '
                 f'{_esc(val["ev_basis"])}</p>')
    for w in val["warnings"]:
        parts.append(f'<div class="note">⚠ {_esc(w)}</div>')
    parts.append('</div>')

    # ===================================================================
    # SECTION 3 — CONFIDENCE & DATA QUALITY  (stacked, full width)
    # ===================================================================
    parts.append('<div class="section">')
    parts.append('<h2><span class="section-num">3</span>Confidence &amp; Data Quality</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> Two independent trust signals for a touchless '
        'system. <b>Confidence</b> blends how cleanly the target was classified (35%), how well '
        'the peer set is populated (35%), whether EBITDA is positive (15%), and whether ≥2 '
        'methods triangulate (15%); HIGH ≥ 0.75, MEDIUM ≥ 0.50, else LOW. '
        '<b>Data quality</b> grades the completeness of the D&amp;B financials on a weighted '
        'per-field checklist (revenue is critical; the EV proxy and EBITDA are high-weight) and '
        'gates the run — if the target lacks revenue and any EV proxy it is marked '
        '<i>not valuable</i> and no number is produced.</p>')
    checks = dq.get("checks", [])
    passed = sum(1 for c in checks if c["status"] == "pass")
    dq_rows = "".join(
        f'<span class="chip"><span class="dq-{c["status"]}">'
        f'{"✓" if c["status"]=="pass" else ("!" if c["status"]=="warn" else "✗")}</span> '
        f'{_esc(c["field"])}</span>'
        for c in checks)
    n_peers_used = len(data["peers"])
    n_ranked = data["peers_ranked_count"]
    n_rejected = len(data["rejected"])
    n_methods = len(val["methods"])
    peers_fact = _fact("Peers used", f"{n_peers_used} of {n_ranked} ranked",
                       f"{n_rejected} rejected")
    methods_fact = _fact("Methods computed", f"{n_methods} of 3", "triangulated")
    conf_cls = _conf_class(conf["label"])
    dq_cls = _grade_class(dq.get("grade"))
    n_checks = len(checks)
    parts.append('<div class="two">')
    parts.append(
        f'<div><span class="badge {conf_cls}">Confidence: '
        f'{_esc(conf["label"])} · {_num(conf["score"],2)}</span>'
        f'<div class="facts" style="margin-top:14px">{peers_fact}{methods_fact}</div></div>')
    parts.append(
        f'<div><span class="badge {dq_cls}">Data quality: '
        f'Grade {_esc(dq.get("grade"))} · {_num(dq.get("score"),2)}</span>'
        f'<div style="margin-top:12px"><div class="fact-k">Checks ({passed}/{n_checks} pass)</div>'
        f'<div style="margin-top:6px">{dq_rows}</div></div></div>')
    parts.append('</div></div>')

    # ===================================================================
    # SECTION 4 — VALUATION METHODS (triangulation)
    # ===================================================================
    parts.append('<div class="section">')
    parts.append('<h2><span class="section-num">4</span>Valuation Methods — Triangulation</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> The same company valued three independent ways '
        'so the answer is corroborated, not taken on faith. <b>EV/EBITDA</b> is the headline '
        '(capital-structure-neutral, the market standard); <b>EV/Revenue</b> is robust when '
        'margins are noisy; <b>EV/EBIT</b> accounts for asset intensity via depreciation. For '
        'each method we take every listed comp\'s multiple, remove statistical outliers with a '
        '<b>Tukey 1.5×IQR fence</b>, then read the P25 / median / P75 to form the low / mid / '
        'high. A method needs ≥3 comps and a positive target driver, else it is skipped. '
        'Methods differ because each captures a different slice of economics — a spread is '
        'informative, not an error.</p>')
    parts.append('<table><tr>'
                 '<th>Method</th><th>Basis</th><th>Comps</th><th>Outliers cut</th>'
                 '<th>Multiple P25 / Med / P75</th><th>Target driver</th>'
                 '<th>EV mid</th><th>Equity Low–Mid–High (Cr)</th></tr>')
    for m in val["methods"]:
        hl = ' class="hl-row"' if m["method"] == val["headline_method"] else ""
        star = ' ★' if m["method"] == val["headline_method"] else ""
        parts.append(
            f'<tr{hl}><td><b>{_esc(m["method"])}</b>{star}</td>'
            f'<td class="mono">{_esc(m.get("ev_basis",""))}</td>'
            f'<td>{m["n_multiples"]}</td>'
            f'<td>{m["n_outliers_dropped"]}</td>'
            f'<td class="mono">{_num(m["multiple_p25"],2)} / <b>{_num(m["multiple_median"],2)}</b> / {_num(m["multiple_p75"],2)}</td>'
            f'<td>₹{_num(m["target_driver"])}</td>'
            f'<td>₹{_num(m["ev_mid_cr"])}</td>'
            f'<td><b>{_num(m["equity_low_cr"])} – <span style="color:var(--accent)">{_num(m["equity_mid_cr"])}</span> – {_num(m["equity_high_cr"])}</b></td></tr>')
    parts.append('</table>')
    parts.append(f'<p class="sub-note" style="margin-top:8px">Range basis: '
                 f'{_esc(val["methods"][0].get("range_basis","")) if val["methods"] else ""} · '
                 f'★ = headline method</p>')
    parts.append('</div>')

    # ===================================================================
    # SECTION 5 — COMPARABLE PEER SET
    # ===================================================================
    parts.append('<div class="section">')
    parts.append(f'<h2><span class="section-num">5</span>Comparable Peer Set ({len(data["peers"])})</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> The companies judged similar enough to price '
        'the target. <b>How they\'re chosen.</b> Candidates first pass a hard <b>mismatch '
        'filter</b> (same operating model, value chain and D&amp;B major industry), then are '
        'scored 0–1 on five weighted dimensions: <b>industry 40%</b> (NAICS 3-digit subsector), '
        '<b>scale 20%</b> (revenue proximity), <b>margin 15%</b>, <b>customer type 15%</b>, '
        '<b>export profile 10%</b>. The chips on each card show each dimension\'s contribution. '
        'Only <b>listed</b> peers contribute market trading multiples; unlisted peers inform '
        'comparability but have no observable market value.</p>')
    for i, p in enumerate(data["peers"], 1):
        tag = '<span class="tag listed">LISTED · market EV</span>' if p["listed"] else '<span class="tag">unlisted</span>'
        comp = p["components"]
        chips = "".join([
            _chip("industry", _num(comp.get("industry"), 2)),
            _chip("scale", _num(comp.get("scale"), 2)),
            _chip("margin", _num(comp.get("margin"), 2)),
            _chip("customer", _num(comp.get("customer"), 2)),
            _chip("export", _num(comp.get("export"), 2)),
        ])
        mults = p["multiples"]
        mult_chips = "".join(
            f'<span class="chip"><b>{_esc(k)}</b> <span class="mult">{_num(v,1)}x</span></span>'
            for k, v in mults.items())
        because = "; ".join(p["selected_because"]) or "—"
        diffs = "; ".join(p["differences"]) or "—"
        mcap = (f" · mkt cap ₹{_num(p['market_cap_cr'],0)} Cr"
                if p.get("market_cap_cr") else "")
        parts.append(f'''<div class="peer">
  <div class="peer-head">
    <div class="peer-name">#{i} {_esc(p['name'])} {tag}</div>
    <div class="score">score {_num(p['score'],3)}</div>
  </div>
  <div class="sub-note" style="margin:4px 0;">
     {_esc(p['city'])} · Rev ₹{_num(p['revenue_cr'],0)} Cr · margin {_pct(p['ebitda_margin'])}
     · growth {_pct(p['revenue_growth'])}{mcap}</div>
  <div>{chips}</div>
  <div style="margin-top:6px;">{mult_chips or '<span class="sub-note">no market multiples (unlisted)</span>'}</div>
  <div class="because">✓ {_esc(because)}</div>
  <div class="diffs">Δ {_esc(diffs)}</div>
</div>''')
    parts.append('</div>')

    # ===================================================================
    # SECTION 6 — REJECTED CANDIDATES
    # ===================================================================
    parts.append('<div class="section">')
    parts.append(f'<h2><span class="section-num">6</span>Rejected Candidates ({len(data["rejected"])})</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> Candidates removed <i>before</i> scoring because '
        'they are economically different — a distributor, retailer, service firm or raw-material '
        'supplier is not comparable to a finished-goods manufacturer even if it sits in an '
        'adjacent industry code. This guard is what stops "sourced from manufacturers" text on a '
        'distributor from polluting the peer set. Each row states the exact disqualifying '
        'mismatch.</p>')
    parts.append('<table><tr><th>Entity</th><th>Operating model</th><th>Value chain</th>'
                 '<th>Major ind.</th><th>Reason rejected</th></tr>')
    for r in data["rejected"]:
        parts.append(f'<tr><td>{_esc(r["name"])}</td>'
                     f'<td>{_esc(r["operating_model"])}</td><td>{_esc(r["value_chain"])}</td>'
                     f'<td>{_esc(r["major_industry"])}</td>'
                     f'<td class="reason">{_esc(r["reason"])}</td></tr>')
    parts.append('</table></div>')

    # ===================================================================
    # SECTION 7 — AUDIT TRAIL
    # ===================================================================
    levels = {}
    for a in audit:
        levels[a.get("level")] = levels.get(a.get("level"), 0) + 1
    lvl_summary = ", ".join(f"{k} {v}" for k, v in sorted(levels.items()))
    parts.append('<div class="section">')
    parts.append(f'<h2><span class="section-num">7</span>Audit Trail ({len(audit)} · {_esc(lvl_summary)})</h2>')
    parts.append(
        '<p class="theory"><b>What this is.</b> Because there is no human in the loop, every '
        'material step is recorded as a typed, time-stamped, ordered record — each D&amp;B call, '
        'each rejected peer, each valuation decision (discount applied, method chosen, fallback '
        'taken). <b>DECISION</b> rows are the judgement calls; <b>WARN/ERROR</b> rows flag '
        'anything degraded. This is the primary evidence for reproducing or challenging the '
        'result.</p>')
    parts.append(_audit_table(audit))
    parts.append('</div>')

    parts.append("</div></body></html>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return out_path


if __name__ == "__main__":
    import sys
    rp = sys.argv[1] if len(sys.argv) > 1 else "output/result.json"
    op = sys.argv[2] if len(sys.argv) > 2 else "output/dashboard.html"
    build_dashboard(rp, op)
    print(f"Wrote {op}")
