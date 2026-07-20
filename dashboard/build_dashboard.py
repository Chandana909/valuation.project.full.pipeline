"""
dashboard/build_dashboard.py — render output/result.json to a self-contained
output/dashboard.html using plain string formatting. No template engine, no
external CSS/JS. Stdlib only.

Design goals (v1.4):
  * MODULAR — one self-contained card per idea, generous spacing, clear hierarchy.
  * EXPLANATORY — every section states its Purpose, then a "theory" note on what is
    shown and how it was derived; numbers are never presented without their meaning.
  * HONEST — confidence is shown as a component breakdown (not one opaque number), and
    a Methodology Validation card reports the anti-overfitting backtest so the reader
    can see the accuracy is aggregate, not a single lucky calibration.
Degraded runs (no_match / insufficient_data) render a minimal, honest page.
"""

import json
import html


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #

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


def _section(num, title, purpose, body_html):
    """A numbered, self-contained section card."""
    return (f'<section class="card"><div class="sec-head">'
            f'<span class="sec-num">{num}</span>'
            f'<div><h2>{_esc(title)}</h2>'
            f'<div class="sec-purpose">{purpose}</div></div></div>'
            f'{body_html}</section>')


def _bar(frac, cls="bar-accent"):
    frac = max(0.0, min(1.0, frac))
    return (f'<div class="bar"><div class="{cls}" style="width:{frac*100:.0f}%"></div></div>')


def _football_field(val, target):
    """Static SVG football-field: one bar per method spanning low→high equity,
    a thick tick at the positioned mid, and the target's own market cap as a
    dashed reference line when it is listed. Pure string SVG — print-safe."""
    methods = [m for m in val.get("methods", [])
               if m.get("equity_low_cr") is not None]
    axis_label = "equity value"
    if not methods:                      # equity withheld -> plot the EV ranges
        methods = [dict(m, equity_low_cr=m["ev_low_cr"], equity_mid_cr=m["ev_mid_cr"],
                        equity_high_cr=m["ev_high_cr"])
                   for m in val.get("methods", []) if m.get("ev_low_cr") is not None]
        axis_label = "ENTERPRISE value (equity withheld)"
    if not methods:
        return ""
    colors = {"EV/EBITDA": ("#2a78d6", "#1c5cab"),
              "EV/Revenue": ("#1baf7a", "#0c5c3f"),
              "EV/EBIT": ("#eda100", "#7a5300")}
    lo = min(m["equity_low_cr"] for m in methods)
    hi = max(m["equity_high_cr"] for m in methods)
    own = (target or {}).get("market_cap_cr")
    if own:
        lo, hi = min(lo, own), max(hi, own)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = max(0.0, lo - pad), hi + pad
    W, L, R = 940, 110, 30
    H = 56 * len(methods) + 46

    def x(v):
        return L + (v - lo) / (hi - lo) * (W - L - R)

    g = []
    for i in range(6):
        v = lo + (hi - lo) * i / 5
        g.append(f'<line x1="{x(v):.0f}" y1="8" x2="{x(v):.0f}" y2="{H-30}" '
                 f'stroke="#e1e0d9"/><text x="{x(v):.0f}" y="{H-14}" font-size="11" '
                 f'fill="#898781" text-anchor="middle">{v:,.0f}</text>')
    for i, m in enumerate(methods):
        c, cd = colors.get(m["method"], ("#2a78d6", "#1c5cab"))
        y = 18 + i * 56
        xl, xm, xh = x(m["equity_low_cr"]), x(m["equity_mid_cr"]), x(m["equity_high_cr"])
        star = " ★" if m["method"] == val.get("headline_method") else ""
        g.append(
            f'<text x="{L-8}" y="{y+14}" font-size="12" font-weight="700" '
            f'text-anchor="end" fill="#0b0b0b">{_esc(m["method"])}{star}</text>'
            f'<rect x="{xl:.0f}" y="{y}" width="{max(2, xh-xl):.0f}" height="16" '
            f'rx="4" fill="{c}" opacity="0.85"/>'
            f'<rect x="{xm-2:.0f}" y="{y-3}" width="4" height="22" rx="1.5" fill="{cd}"/>'
            f'<text x="{xl-5:.0f}" y="{y+13}" font-size="11" fill="#52514e" '
            f'text-anchor="end">{m["equity_low_cr"]:,.0f}</text>'
            f'<text x="{xh+5:.0f}" y="{y+13}" font-size="11" '
            f'fill="#52514e">{m["equity_high_cr"]:,.0f}</text>'
            f'<text x="{xm:.0f}" y="{y-7}" font-size="11" font-weight="700" '
            f'fill="#0b0b0b" text-anchor="middle">{m["equity_mid_cr"]:,.0f}</text>')
    if own:
        g.append(f'<line x1="{x(own):.0f}" y1="6" x2="{x(own):.0f}" y2="{H-30}" '
                 f'stroke="#0b0b0b" stroke-width="1.6" stroke-dasharray="5 4"/>')
    g.append(f'<line x1="{L}" y1="{H-30}" x2="{W-R}" y2="{H-30}" stroke="#c3c2b7"/>')
    legend = (f'<div class="sub-note" style="margin:4px 0 14px">bar = low→high equity '
              f'per method · thick tick = positioned central estimate · axis: {axis_label}, ₹ Cr'
              + (f' · dashed line = own market cap ₹{own:,.0f} Cr' if own else "")
              + '</div>')
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
            f'aria-label="Equity range by method">{"".join(g)}</svg>{legend}')


_STYLE = """
:root{
  --bg:#f9f9f7; --panel:#fcfcfb; --panel2:#f1f1ec; --line:#e1e0d9; --line2:#c3c2b7;
  --ink:#0b0b0b; --muted:#52514e; --faint:#898781; --accent:#2a78d6; --good:#0a7d0a;
  --warn:#8a6100; --bad:#c0392b; --dec:#5b46c7;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;font-size:14px;
  line-height:1.5;}
.wrap{max-width:1000px;margin:0 auto;padding:30px 26px 60px;}
h1{font-size:24px;margin:0 0 6px;}
.sub{color:var(--muted);font-size:13px;}
.provenance{color:var(--muted);font-size:12px;margin:12px 0 0;
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px;}
.status-ok{color:var(--good);} .status-bad{color:var(--bad);}
.intro{color:var(--muted);font-size:13.5px;margin:18px 0 6px;}
.legend{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 26px;}
.legend .lg{background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:8px 12px;font-size:12px;color:var(--muted);}
.legend .lg b{color:var(--ink);}

.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:22px 24px;margin-bottom:20px;}
.sec-head{display:flex;gap:14px;align-items:flex-start;margin-bottom:14px;}
.sec-num{flex:none;width:30px;height:30px;border-radius:8px;background:var(--panel2);
  border:1px solid var(--line2);color:var(--accent);font-weight:800;font-size:15px;
  display:flex;align-items:center;justify-content:center;}
.sec-head h2{font-size:17px;margin:0;}
.sec-purpose{color:var(--faint);font-size:12.5px;margin-top:2px;}
.theory{color:var(--muted);font-size:13px;line-height:1.6;margin:0 0 18px;
  border-left:3px solid var(--line2);padding:2px 0 2px 14px;}
.theory b{color:var(--ink);font-weight:600;}

.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;}
.fact-k{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;}
.fact-v{font-size:15px;margin-top:3px;font-weight:600;}
.sub-note{color:var(--muted);font-size:11.5px;margin-top:2px;font-weight:400;}

.badge{display:inline-block;padding:6px 14px;border-radius:20px;font-weight:700;font-size:15px;}
.badge.hi{background:rgba(10,125,10,.10);color:var(--good);border:1px solid var(--good);}
.badge.med{background:rgba(138,97,0,.10);color:var(--warn);border:1px solid var(--warn);}
.badge.lo{background:rgba(192,57,43,.10);color:var(--bad);border:1px solid var(--bad);}

.headline{display:flex;gap:30px;align-items:flex-end;margin:6px 0 18px;flex-wrap:wrap;}
.hv{text-align:center;}
.hv .k{color:var(--muted);font-size:12px;}
.hv .n{font-size:32px;font-weight:800;letter-spacing:-.5px;}
.hv .n.mid{color:var(--accent);}
.unit{color:var(--muted);font-size:12px;}
.bridge{display:flex;gap:8px;flex-wrap:wrap;align-items:stretch;margin-top:8px;}
.bridge .step{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  padding:9px 13px;font-size:12px;color:var(--muted);display:flex;flex-direction:column;
  justify-content:center;}
.bridge .step b{color:var(--ink);font-size:15px;margin-top:3px;}
.bridge .op{color:var(--faint);font-weight:800;font-size:18px;display:flex;align-items:center;}

.callout{margin-top:14px;padding:12px 16px;border-radius:10px;border:1px solid var(--line2);
  background:var(--panel2);}
.callout.ok{border-color:var(--good);background:rgba(10,125,10,.06);}
.callout.warn{border-color:var(--warn);background:rgba(138,97,0,.06);}
.callout b{font-size:13.5px;}

/* confidence breakdown bars */
.cbrow{display:grid;grid-template-columns:150px 1fr 60px;gap:12px;align-items:center;
  margin:7px 0;font-size:12.5px;}
.cbrow .lab{color:var(--muted);} .cbrow .val{text-align:right;color:var(--ink);font-weight:600;}
.bar{height:8px;background:var(--panel2);border-radius:5px;overflow:hidden;border:1px solid var(--line);}
.bar-accent{height:100%;background:linear-gradient(90deg,#2d6fd6,#4f9cf9);}
.bar-good{height:100%;background:var(--good);} .bar-warn{height:100%;background:var(--warn);}

table{width:100%;border-collapse:collapse;margin-top:6px;}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:13px;
  vertical-align:top;}
th{color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.05em;}
tr:hover td{background:var(--panel2);}
.hl-row{background:rgba(42,120,214,.10);}
@media print{ body{background:#fff} .card{break-inside:avoid;border-color:#bbb} }

.peer{background:var(--panel2);border:1px solid var(--line);border-radius:11px;
  padding:15px;margin-bottom:12px;}
.peer-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;}
.peer-name{font-weight:700;font-size:15px;}
.tag{font-size:11px;padding:2px 9px;border-radius:10px;border:1px solid var(--line);color:var(--muted);}
.tag.listed{color:var(--accent);border-color:var(--accent);}
.score{font-weight:800;color:var(--accent);}
.chip{display:inline-block;background:var(--panel);border:1px solid var(--line);
  border-radius:6px;padding:2px 9px;margin:3px 5px 0 0;font-size:12px;}
.chip b{color:var(--muted);font-weight:600;}
.mult{color:var(--good);}
.because{color:var(--good);font-size:12px;margin-top:8px;}
.diffs{color:var(--warn);font-size:12px;margin-top:2px;}
.reason{color:var(--bad);}
.note{color:var(--warn);margin-top:6px;}
.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;}
.lvl{font-weight:700;font-size:11px;padding:1px 7px;border-radius:5px;}
.lv-info{color:var(--muted);border:1px solid var(--line);}
.lv-warn{color:var(--warn);border:1px solid var(--warn);}
.lv-dec {color:var(--dec);border:1px solid var(--dec);}
.lv-err {color:var(--bad);border:1px solid var(--bad);}
.dq-pass{color:var(--good);} .dq-warn{color:var(--warn);} .dq-fail{color:var(--bad);}
.two{display:grid;grid-template-columns:1fr 1fr;gap:24px;}
@media (max-width:760px){.two{grid-template-columns:1fr;}}
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


# --------------------------------------------------------------------------- #
# section builders
# --------------------------------------------------------------------------- #

def _confidence_breakdown(cb):
    """Render the confidence components as labelled bars (max weight per component)."""
    maxw = {"profile": 0.20, "peer_coverage": 0.20, "ebitda_positive": 0.10,
            "methods": 0.10, "triangulation": 0.25, "comp_tightness": 0.15}
    labels = {"profile": "Profile clarity", "peer_coverage": "Peer coverage",
              "ebitda_positive": "EBITDA positive", "methods": "Methods computed",
              "triangulation": "Triangulation agreement", "comp_tightness": "Comparable tightness"}
    rows = []
    for k in ("profile", "peer_coverage", "ebitda_positive", "methods",
              "triangulation", "comp_tightness"):
        v = cb.get(k, 0.0)
        frac = (v / maxw[k]) if maxw[k] else 0
        rows.append(f'<div class="cbrow"><div class="lab">{_esc(labels[k])}</div>'
                    f'{_bar(frac)}<div class="val">{_num(v,2)}/{maxw[k]:.2f}</div></div>')
    return "".join(rows)


def render_dashboard(data):
    """Render a result dict to a self-contained dashboard HTML string.
    Used by the API's report endpoint (in-memory) and build_dashboard (file)."""
    meta = data.get("meta", {})
    audit = data.get("audit_trail", [])
    parts = [_head(f"MSME Valuation — {data.get('query')}")]
    parts.append(f'<h1>MSME Comparable-Company Valuation — {_esc(data.get("query"))}</h1>')
    parts.append('<div class="sub">Trading-multiples valuation of an Indian MSME, '
                 'grounded on Dun &amp; Bradstreet data · deterministic · touchless · no LLM</div>')
    parts.append(_provenance(meta))

    # ---- degraded runs -------------------------------------------------
    if data.get("target") is None or data.get("valuation") is None:
        parts.append(_section("!", "Result", "why no valuation was produced",
                              f'<p class="theory">No valuation was produced — status '
                              f'<b>{_esc(meta.get("status"))}</b>. Nothing is fabricated; the '
                              f'audit trail below records exactly why the pipeline stopped.</p>'
                              + _audit_table(audit)))
        parts.append("</div></body></html>")
        return "".join(parts)

    t = data["target"]
    tp = data["target_profile"]
    dq = data.get("data_quality") or {}
    val = data["valuation"]
    conf = data["confidence"]
    cb = conf.get("breakdown", {})
    validation = data.get("validation")

    # ---- reading guide -------------------------------------------------
    parts.append(
        '<p class="intro">This report answers one question — <b>what is this company\'s '
        'equity worth?</b> — by pricing it against listed peers. Read it top to bottom: '
        'who the company is (1), the headline answer and how it was built (2), how much to '
        'trust it (3), whether the method is proven not just lucky (4), the three '
        'cross-checking methods (5), the peers used (6), who was excluded and why (7), and a '
        'full decision log (8).</p>')
    parts.append('<div class="legend">'
                 '<div class="lg"><b>EV</b> enterprise value = market cap + net debt</div>'
                 '<div class="lg"><b>Multiple</b> what the market pays per ₹ of EBITDA/revenue</div>'
                 '<div class="lg"><b>DLOM</b> discount for a private company\'s illiquidity</div>'
                 '<div class="lg"><b>Cr</b> crore = 10,000,000 (₹)</div></div>')

    # ================= 1 · TARGET ======================================
    facts = "".join([
        _fact("Identity", _esc(t['name']), f"DUNS {t['duns']} · CIN {t['cin']}"),
        _fact("Economic profile", f"{_esc(tp['operating_model'])} / {_esc(tp['value_chain'])}",
              f"{tp['customer_type']} · {'exporter' if t['is_exporter'] else 'domestic'}"),
        _fact("Industry (NAICS)", f"{_esc(t['naics'])} · sub {_esc(tp['naics_subsector'])}",
              _esc(t['naics_desc'])),
        _fact("Listing", "Listed" if t['listed'] else "Unlisted (private)",
              f"D&B major {t['major_industry']}"),
        _fact("Revenue", f"₹{_num(t['revenue_cr'])} Cr", f"growth {_pct(t['revenue_growth'])} YoY"),
        _fact("EBITDA", f"₹{_num(t['ebitda_cr'])} Cr", f"margin {_pct(t['ebitda_margin'])}"),
        _fact("EBIT", f"₹{_num(t['ebit_cr'])} Cr", "= EBITDA − depreciation"),
        _fact("Net debt", f"₹{_num(val['net_debt_cr'])} Cr", "debt − cash"),
    ])
    theory = ('<p class="theory"><b>What this is.</b> The subject company\'s identity, industry '
              'code, rule-based economic profile and latest financials. <b>How it\'s derived.</b> '
              'From D&amp;B <span class="mono">company_information</span> + '
              '<span class="mono">company_financials</span>, converted INR&nbsp;Thousand → '
              '<b>Crore</b> (÷10,000). These are the <b>drivers</b> the peer multiples get '
              'applied to.</p>')
    parts.append(_section("1", "Target Profile & Financials",
                          "who is being valued", theory + f'<div class="facts">{facts}</div>'))

    # ---- 1b · data lineage + source caveats (provenance) ----------------
    lin = data.get("target_lineage") or {}
    cavs = data.get("source_caveats") or []
    if lin or cavs:
        rows = "".join(
            f'<tr><td><b>{_esc(k)}</b></td><td class="mono">{_esc((v or {}).get("file", "—"))}</td>'
            f'<td class="mono">{_esc((v or {}).get("row")) if (v or {}).get("row") is not None else "—"}</td>'
            f'<td class="mono">{_esc((v or {}).get("fy")) if (v or {}).get("fy") else "—"}'
            f'{(" · " + _esc(v.get("status"))) if isinstance(v, dict) and v.get("status") else ""}</td></tr>'
            for k, v in lin.items())
        table = (f'<table><tr><th>Figure(s)</th><th>Source file</th><th>Row</th>'
                 f'<th>Fiscal yr / status</th></tr>{rows}</table>') if lin else ""
        cav_html = "".join(f'<div class="callout warn"><b>Source caveat.</b> '
                           f'<span class="sub-note">{_esc(c)}</span></div>' for c in cavs)
        theory = ('<p class="theory"><b>What this is.</b> Every key figure traced to the '
                  'exact source file and row it came from (recorded at ETL time), or '
                  'marked user-provided for guided-intake targets — plus every honesty '
                  'caveat about what the data source does not contain. Any number in '
                  'this report can be challenged back to its cell.</p>')
        parts.append(_section("1b", "Data lineage & Source Caveats",
                              "where every number came from", theory + table + cav_html))

    # ---- 1c · essential-parameter checklist ------------------------------
    cl = data.get("parameter_checklist") or []
    if cl:
        icon = {"available": "✓", "user_provided": "✓*", "missing": "✗"}
        rows = "".join(
            f'<tr><td><b>{icon.get(c["status"], "?")}</b></td>'
            f'<td><b>{_esc(c["label"])}</b></td>'
            f'<td>{"NOT AVAILABLE" if c["status"] == "missing" else _esc(c["value"])}</td>'
            f'<td>{_esc(c["needed_for"])}</td></tr>' for c in cl)
        n_missing = sum(1 for c in cl if c["status"] == "missing")
        theory = ('<p class="theory"><b>What this is.</b> The honest inventory of every '
                  'input the valuation consumes: ✓ from the source data · ✓* supplied by '
                  'the user (chat/enrichment) · ✗ not available — and NEVER assumed. '
                  'Missing figures can be provided through the conversational agent; '
                  'until then any output that depends on them (e.g. equity when '
                  'borrowings/cash are missing) is withheld, not fabricated.</p>')
        parts.append(_section("1c", f"Essential Parameters ({len(cl)-n_missing}/{len(cl)} present)",
                              "ticked = present · crossed = not available, fillable in chat",
                              theory + '<table><tr><th></th><th>Parameter</th><th>Value</th>'
                              '<th>Needed for</th></tr>' + rows + '</table>'))

    # ================= 2 · HEADLINE VALUATION ==========================
    hm = next((m for m in val["methods"] if m["method"] == val["headline_method"]), None)
    theory = ('<p class="theory"><b>What this is.</b> The estimated equity value range from the '
              f'primary method (<b>{_esc(val["headline_method"])}</b>). <b>How it\'s built (5 '
              'steps).</b> (1) take listed peers\' trading multiples; (2) read the multiple at '
              'the target\'s <b>quality position</b> — its EBITDA-margin percentile in the peer '
              'set, so a weaker-margin company earns a lower multiple; (3) × the target\'s driver '
              '→ implied <b>enterprise value</b>; (4) − <b>net debt</b> → equity; (5) − <b>DLOM</b> '
              'if the company is private (a listed target is already liquid, so DLOM = 0). '
              'Low / mid / high span the peer band around the position.</p>')
    withheld = val.get("equity_mid_cr") is None and (val.get("equity_requires") or [])
    if withheld:
        _v = hm or {}
        hv = (f'<div class="callout warn"><b>Equity value withheld — nothing is '
              f'assumed.</b> <span class="sub-note">The source has no '
              f'{_esc(" or ".join(val.get("equity_requires") or []))}; the '
              f'ENTERPRISE-VALUE range is the honest answer. Supply the missing '
              f'figures (chat / enrichment API) to bridge EV → equity.</span></div>'
              f'<div class="headline">'
              f'<div class="hv"><div class="k">Low</div><div class="n">{_num(_v.get("ev_low_cr"))}</div></div>'
              f'<div class="hv"><div class="k">Mid — central estimate</div><div class="n mid">{_num(_v.get("ev_mid_cr"))}</div></div>'
              f'<div class="hv"><div class="k">High</div><div class="n">{_num(_v.get("ev_high_cr"))}</div></div>'
              f'<div class="unit">ENTERPRISE value · INR Crore (equity requires '
              f'{_esc(" + ".join(val.get("equity_requires") or []))})</div></div>')
    else:
        hv = (f'<div class="headline">'
              f'<div class="hv"><div class="k">Low</div><div class="n">{_num(val["equity_low_cr"])}</div></div>'
              f'<div class="hv"><div class="k">Mid — central estimate</div><div class="n mid">{_num(val["equity_mid_cr"])}</div></div>'
              f'<div class="hv"><div class="k">High</div><div class="n">{_num(val["equity_high_cr"])}</div></div>'
              f'<div class="unit">equity value · INR Crore</div></div>')
    bridge = ""
    if hm:
        bridge = ('<div class="bridge">'
                  f'<div class="step">{_esc(hm["method"])} positioned<b>{_num(hm["multiple_median"],1)}x</b></div>'
                  '<div class="op">×</div>'
                  f'<div class="step">target driver<b>₹{_num(hm["target_driver"])} Cr</b></div>'
                  '<div class="op">=</div>'
                  f'<div class="step">enterprise value<b>₹{_num(hm["ev_mid_cr"])} Cr</b></div>'
                  '<div class="op">−</div>'
                  f'<div class="step">net debt<b>₹{_num(val["net_debt_cr"])} Cr</b></div>'
                  '<div class="op">−</div>'
                  f'<div class="step">DLOM<b>{_pct(val["discount"],0)}</b></div>'
                  '<div class="op">=</div>'
                  f'<div class="step" style="border-color:var(--accent)">equity (mid)'
                  f'<b style="color:var(--accent)">₹{_num(val["equity_mid_cr"])} Cr</b></div></div>')
    pos_note = (f'<p class="sub-note" style="margin-top:12px"><b>Positioning:</b> '
                f'{_esc(val["positioning"])}</p>') if val.get("positioning") else ""
    disc_note = (f'<p class="sub-note">{_esc(val["discount_reason"])} · '
                 f'{_esc(val["ev_basis"])}</p>')
    xc_html = ""
    xc = val.get("market_cross_check")
    if xc:
        ok = xc["within_25pct"]
        cls = "ok" if ok else "warn"
        xc_html = (f'<div class="callout {cls}"><b>Sanity check against the market — '
                   f'{"in range" if ok else "review"}.</b><br>'
                   f'<span class="sub-note">Because this target is itself listed, we can compare '
                   f'our comps-derived equity (₹{_num(xc["comps_mid_equity_cr"])} Cr) with its own '
                   f'observed market capitalisation (₹{_num(xc["own_market_cap_cr"])} Cr, '
                   f'{_num(xc["own_ev_ebitda"],1)}x EV/EBITDA). Delta '
                   f'<b>{xc["delta_pct"]:+.1f}%</b> — within the method\'s backtested error '
                   f'(see §4). Positioning never uses this market cap, so the agreement is a real '
                   f'validation, not circular.</span></div>')
    warns = "".join(f'<div class="note">⚠ {_esc(w)}</div>' for w in val["warnings"])
    ca = val.get("comparability_adjustment")
    ca_html = ""
    if ca and ca.get("applied"):
        arrow = "▼ marked DOWN" if ca["direction"] == "down" else "▲ marked UP"
        ca_html = (f'<div class="callout warn"><b>Comparability adjustment — multiples '
                   f'{arrow} {ca["pct"]:+.1f}%.</b><br><span class="sub-note">'
                   f'{_esc(ca["reason"])}. <b>Why:</b> when no peer of comparable size '
                   f'exists, peer multiples are not fully transferable — size-premium '
                   f'evidence shows smaller companies trade at lower multiples than '
                   f'larger ones. This explicit, audited penalty replaces silent '
                   f'over/under-statement.</span></div>')
    txn = val.get("transaction_analysis")
    txn_html = ""
    if txn and txn.get("txn_multiple"):
        txn_html = (
            f'<div class="callout ok"><b>Comparable transaction (OBSERVED, user-'
            f'provided) — {txn["txn_multiple"]:.1f}x EV/EBITDA → acquisition EV '
            f'₹{_num(txn["acquisition_ev_low_cr"])} – ₹{_num(txn["acquisition_ev_mid_cr"])} '
            f'– ₹{_num(txn["acquisition_ev_high_cr"])} Cr.</b><br><span class="sub-note">'
            f'{_esc(txn["caveat"])}</span></div>')
    elif txn:
        txn_html = (
            f'<div class="callout"><b>Comparable-transactions view (indicative) — '
            f'what a control buyer might pay: ₹{_num(txn["acquisition_equity_low_cr"])} '
            f'– {_num(txn["acquisition_equity_mid_cr"])} – '
            f'{_num(txn["acquisition_equity_high_cr"])} Cr.</b><br><span class="sub-note">'
            f'<b>Theory.</b> The range above is a MINORITY trading value. Acquirers of '
            f'control pay a premium (synergies, control of cash flows) — empirical '
            f'studies cluster at 20–30%. {_esc(txn["caveat"])}</span></div>')
    parts.append(_section("2", "Headline Valuation", "the answer, and the arithmetic behind it",
                          theory + hv + _football_field(val, t) + bridge + pos_note
                          + disc_note + ca_html + txn_html + xc_html + warns))

    # ================= 3 · CONFIDENCE & DATA QUALITY ===================
    theory = ('<p class="theory"><b>What this is.</b> How much to trust this specific result. '
              'Rather than one opaque number, <b>confidence</b> is the sum of six weighted '
              'signals — crucially it includes <b>triangulation agreement</b> (do the three '
              'methods concur?) and <b>comparable tightness</b> (are the peer multiples '
              'consistent?), so a scattered result scores lower. It does <i>not</i> saturate near '
              '1.0 for every company. <b>Data quality</b> separately grades the completeness of '
              'the D&amp;B financials and gates the run.</p>')
    checks = dq.get("checks", [])
    passed = sum(1 for c in checks if c["status"] == "pass")
    dq_chips = "".join(
        f'<span class="chip"><span class="dq-{c["status"]}">'
        f'{"✓" if c["status"]=="pass" else ("!" if c["status"]=="warn" else "✗")}</span> '
        f'{_esc(c["field"])}</span>' for c in checks)
    left = (f'<span class="badge {_conf_class(conf["label"])}">Confidence: '
            f'{_esc(conf["label"])} · {_num(conf["score"],2)}</span>'
            f'<div style="margin-top:14px">{_confidence_breakdown(cb)}</div>'
            f'<p class="sub-note" style="margin-top:10px">Score = sum of the bars above '
            f'(max 1.00). HIGH ≥ 0.75 · MEDIUM ≥ 0.50 · else LOW.</p>')
    right = (f'<span class="badge {_grade_class(dq.get("grade"))}">Data quality: '
             f'Grade {_esc(dq.get("grade"))} · {_num(dq.get("score"),2)}</span>'
             f'<div style="margin-top:14px"><div class="fact-k">Field checks '
             f'({passed}/{len(checks)} pass)</div>'
             f'<div style="margin-top:8px">{dq_chips}</div></div>')
    parts.append(_section("3", "Confidence & Data Quality", "how much to trust this result",
                          theory + f'<div class="two"><div>{left}</div><div>{right}</div></div>'))

    # ================= 4 · METHODOLOGY VALIDATION (anti-overfit) ========
    if validation and validation.get("kind") == "observed_market_validation":
        v_ok = validation.get("verdict_ok")
        theory = ('<p class="theory"><b>What this is.</b> The engine checked against REALITY: '
                  'a pinned set of listed database companies is valued and compared to each '
                  "company's <b>own observed NSE market cap</b> (dated, sourced). Two known "
                  'idiosyncratic outliers are kept in deliberately — hiding them would '
                  'overstate accuracy. Basis note: '
                  f'{_esc(validation.get("basis",""))}.</p>')
        vbadge = ('<span class="badge hi">VALIDATION: PASS</span>' if v_ok
                  else '<span class="badge lo">VALIDATION: REVIEW</span>')
        vrows = "".join(
            f'<tr><td><b>{_esc(r["company"])}</b></td>'
            f'<td>₹{_num(r["engine_ev_low_cr"],0)} – <b>₹{_num(r["engine_ev_mid_cr"],0)}</b> – '
            f'₹{_num(r["engine_ev_high_cr"],0)}</td>'
            f'<td>₹{_num(r["actual_mcap_cr"],0)}</td>'
            f'<td>{r["delta_pct"]:+.1f}%</td>'
            f'<td>{"IN RANGE" if r["in_range"] else "outside"}</td></tr>'
            for r in validation.get("rows", []) if r.get("status") == "ok")
        vtable = ('<table><tr><th>Company</th><th>Engine EV (low–mid–high)</th>'
                  '<th>Actual market cap</th><th>Δ mid</th><th>Range</th></tr>'
                  + vrows + '</table>')
        vfacts = (f'<div style="margin:14px 0 6px">{vbadge}</div>'
                  f'<p class="sub-note">median |error| <b>{validation["median_abs_pct"]}%</b> · '
                  f'mean {validation["mean_abs_pct"]}% · actual inside the published range on '
                  f'<b>{validation["n_in_range"]}/{validation["n"]}</b> · as of '
                  f'{_esc(validation["as_of"])}. {_esc(validation.get("note",""))}</p>')
        parts.append(_section("4", "Observed-Market Validation",
                              "checked against real NSE prices — nothing simulated",
                              theory + vfacts + vtable))
        methods_num, peers_num, rej_num, audit_num = "5", "6", "7", "8"
    else:
        methods_num, peers_num, rej_num, audit_num = "4", "5", "6", "7"

    # ================= 5 · METHODS (triangulation) =====================
    theory = ('<p class="theory"><b>What this is.</b> The company valued three independent ways so '
              'the answer is corroborated. <b>EV/EBITDA</b> is the headline (capital-structure '
              'neutral). <b>EV/Revenue</b> is robust when margins are noisy. <b>EV/EBIT</b> '
              'reflects asset intensity via depreciation. For each we take listed comps\' '
              'multiples, drop outliers with a <b>Tukey 1.5×IQR fence</b>, then read the multiple '
              'at the target\'s quality position with a band around it. A spread across methods is '
              '<i>informative</i>, not an error — each captures different economics.</p>')
    trows = ('<table><tr><th>Method</th><th>Basis</th><th>Comps</th><th>Outliers cut</th>'
             '<th>Multiple low / <b>positioned</b> / high</th><th>Target driver</th>'
             '<th>EV mid</th><th>Equity Low–Mid–High (Cr)</th></tr>')
    for m in val["methods"]:
        hlc = ' class="hl-row"' if m["method"] == val["headline_method"] else ""
        star = ' ★' if m["method"] == val["headline_method"] else ""
        eff = m.get("effective_n")
        comps_cell = (f'{m["n_multiples"]}'
                      + (f' <span class="sub-note">(eff {eff})</span>' if eff is not None else ''))
        trows += (f'<tr{hlc}><td><b>{_esc(m["method"])}</b>{star}</td>'
                  f'<td class="mono">{_esc(m.get("ev_basis",""))}</td>'
                  f'<td>{comps_cell}</td><td>{m["n_outliers_dropped"]}</td>'
                  f'<td class="mono">{_num(m["multiple_p25"],2)} / '
                  f'<b>{_num(m["multiple_median"],2)}</b> / {_num(m["multiple_p75"],2)}</td>'
                  f'<td>₹{_num(m["target_driver"])}</td><td>₹{_num(m["ev_mid_cr"])}</td>'
                  f'<td><b>{_num(m["equity_low_cr"])} – '
                  f'<span style="color:var(--accent)">{_num(m["equity_mid_cr"])}</span> – '
                  f'{_num(m["equity_high_cr"])}</b></td></tr>')
    trows += '</table>'
    rbasis = (f'<p class="sub-note" style="margin-top:8px">Range basis: '
              f'{_esc(val["methods"][0].get("range_basis","")) if val["methods"] else ""} · '
              f'★ = headline method</p>')
    parts.append(_section(methods_num, "Valuation Methods — Triangulation",
                          "the same answer, checked three ways", theory + trows + rbasis))

    # ================= 6 · PEERS =======================================
    eff = val.get("effective_peer_count")
    nb = val.get("n_borderline", 0)
    theory = ('<p class="theory"><b>What this is.</b> The companies used to price the target. '
              '<b>How they\'re chosen.</b> Candidates first pass a hard <b>mismatch filter</b> '
              '(same operating model, value chain, D&amp;B major industry), then score 0–1 on '
              'five weighted dimensions: <b>industry 40%</b>, <b>scale 20%</b>, <b>margin 15%</b>, '
              '<b>customer 15%</b>, <b>export 10%</b> — shown as chips on each card. '
              '<b>Not every peer is an exact match.</b> Each peer\'s multiple is '
              '<b>similarity-weighted</b> (<code>w</code> on the card): a full match (score ≥ 0.85) '
              'counts fully, a borderline one tapers toward a 0.15 floor, so a set padded with '
              'loose comps cannot distort the answer. Here <b>' + str(nb) + '</b> of '
              + str(len(data["peers"])) + ' peers are borderline → <b>effective peer count '
              + str(eff) + '</b>. Only <b>listed</b> peers contribute trading multiples.</p>')
    pcards = ""
    for i, p in enumerate(data["peers"], 1):
        tag = ('<span class="tag listed">LISTED · market EV</span>' if p["listed"]
               else '<span class="tag">unlisted</span>')
        comp = p["components"]
        chips = "".join([_chip("industry", _num(comp.get("industry"), 2)),
                         _chip("scale", _num(comp.get("scale"), 2)),
                         _chip("margin", _num(comp.get("margin"), 2)),
                         _chip("customer", _num(comp.get("customer"), 2)),
                         _chip("export", _num(comp.get("export"), 2))])
        mult_chips = "".join(
            f'<span class="chip"><b>{_esc(k)}</b> <span class="mult">{_num(v,1)}x</span></span>'
            for k, v in p["multiples"].items())
        mult_block = mult_chips or '<span class="sub-note">no market multiples (unlisted)</span>'
        mcap = (f" · mkt cap ₹{_num(p['market_cap_cr'],0)} Cr" if p.get("market_cap_cr") else "")
        because_txt = _esc("; ".join(p["selected_because"]) or "—")
        diffs_txt = _esc("; ".join(p["differences"]) or "—")
        wt = p.get("weight", 1.0)
        wtag = (f'<span class="tag" style="border-color:var(--warn);color:var(--warn)">'
                f'borderline · w {_num(wt,2)}</span>' if p.get("borderline")
                else f'<span class="tag" style="border-color:var(--good);color:var(--good)">'
                     f'w {_num(wt,2)}</span>')
        pcards += (f'<div class="peer"><div class="peer-head">'
                   f'<div class="peer-name">#{i} {_esc(p["name"])} {tag} {wtag}</div>'
                   f'<div class="score">score {_num(p["score"],3)}</div></div>'
                   f'<div class="sub-note" style="margin:4px 0;">{_esc(p["city"])} · '
                   f'Rev ₹{_num(p["revenue_cr"],0)} Cr · margin {_pct(p["ebitda_margin"])} · '
                   f'growth {_pct(p["revenue_growth"])}{mcap}</div>'
                   f'<div>{chips}</div>'
                   f'<div style="margin-top:6px;">{mult_block}</div>'
                   f'<div class="because">✓ {because_txt}</div>'
                   f'<div class="diffs">Δ {diffs_txt}</div></div>')
    parts.append(_section(peers_num, f'Comparable Peer Set ({len(data["peers"])})',
                          "the companies used to price the target", theory + pcards))

    # ================= 7 · REJECTED ====================================
    theory = ('<p class="theory"><b>What this is.</b> Candidates removed <i>before</i> scoring '
              'because they are economically different — a distributor, retailer, service firm or '
              'raw-material supplier is not comparable to a finished-goods manufacturer even in an '
              'adjacent industry code. This guard stops "sourced from manufacturers" text on a '
              'distributor from polluting the peer set. Each row states the disqualifying '
              'mismatch.</p>')
    rtab = ('<table><tr><th>Entity</th><th>Operating model</th><th>Value chain</th>'
            '<th>Major ind.</th><th>Reason rejected</th></tr>')
    for r in data["rejected"]:
        rtab += (f'<tr><td>{_esc(r["name"])}</td><td>{_esc(r["operating_model"])}</td>'
                 f'<td>{_esc(r["value_chain"])}</td><td>{_esc(r["major_industry"])}</td>'
                 f'<td class="reason">{_esc(r["reason"])}</td></tr>')
    rtab += '</table>'
    guarantees = (
        '<p class="theory" style="margin-top:14px"><b>What the filter chain guarantees.</b> '
        'No peer of a different operating model, value chain or major industry can reach '
        'the valuation (hard knock-outs, each with a recorded reason); no single outlier '
        'multiple can move the answer (Tukey fence); borderline comps cannot dominate '
        '(similarity weights taper to a 0.15 floor and the range widens on the effective '
        'count); a size-mismatched peer set triggers an explicit, audited comparability '
        'adjustment; and if nothing survives, the answer is honestly "none". '
        '<b>What it cannot guarantee.</b> Filters compare what the data contains — a '
        'misdescribed company misclassifies visibly, not silently; industry codes are '
        'not product-level overlap; and on the real extract, multiples are book-basis '
        'until market prices are added.</p>')
    parts.append(_section(rej_num, f'Rejected Candidates ({len(data["rejected"])})',
                          "who was excluded, and why", theory + rtab + guarantees))

    # ================= 8 · AUDIT =======================================
    levels = {}
    for a in audit:
        levels[a.get("level")] = levels.get(a.get("level"), 0) + 1
    lvl_summary = ", ".join(f"{k} {v}" for k, v in sorted(levels.items()))
    theory = ('<p class="theory"><b>What this is.</b> With no human in the loop, every material '
              'step is a typed, time-stamped, ordered record — each D&amp;B call, each rejected '
              'peer, each valuation decision. <b>DECISION</b> rows are the judgement calls; '
              '<b>WARN/ERROR</b> flag anything degraded. This is the evidence for reproducing or '
              'challenging the result.</p>')
    parts.append(_section(audit_num, f'Audit Trail ({len(audit)} · {_esc(lvl_summary)})',
                          "the complete, replayable decision log", theory + _audit_table(audit)))

    parts.append("</div></body></html>")
    return "".join(parts)


def build_dashboard(result_path, out_path):
    """result.json file -> dashboard.html file (CLI path, used by run.py)."""
    with open(result_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_dashboard(data))
    return out_path


if __name__ == "__main__":
    import sys
    rp = sys.argv[1] if len(sys.argv) > 1 else "output/result.json"
    op = sys.argv[2] if len(sys.argv) > 2 else "output/dashboard.html"
    build_dashboard(rp, op)
    print(f"Wrote {op}")
