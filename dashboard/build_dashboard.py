"""
dashboard/build_dashboard.py — render output/result.json to a self-contained
output/dashboard.html using plain string formatting. No template engine, no
external CSS/JS. Stdlib only.

Renders: provenance banner, target profile, headline valuation, confidence,
data-quality gate, triangulation table, peer cards, rejected table, and the
FULL structured audit trail (seq / time / stage / level / code / detail).
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


_STYLE = """
:root {
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2b3444;
  --ink:#e6edf3; --muted:#9aa7b4; --accent:#4f9cf9; --good:#3fb950;
  --warn:#d29922; --bad:#f85149; --dec:#a371f7;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; font-size:14px; }
.wrap { max-width:1200px; margin:0 auto; padding:24px; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:15px; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:28px 0 12px; border-bottom:1px solid var(--line);
  padding-bottom:6px; }
.sub { color:var(--muted); margin-bottom:6px; }
.provenance { color:var(--muted); font-size:12px; margin-bottom:18px;
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace;
  background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 14px; }
.status-ok { color:var(--good); } .status-bad { color:var(--bad); }
.panels { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; }
.k { color:var(--muted); font-size:12px; }
.v { font-size:15px; margin:2px 0 10px; }
.big { font-size:24px; font-weight:700; }
.badge { display:inline-block; padding:6px 14px; border-radius:20px; font-weight:700; font-size:15px; }
.badge.hi { background:rgba(63,185,80,.15); color:var(--good); border:1px solid var(--good); }
.badge.med { background:rgba(210,153,34,.15); color:var(--warn); border:1px solid var(--warn); }
.badge.lo { background:rgba(248,81,73,.15); color:var(--bad); border:1px solid var(--bad); }
table { width:100%; border-collapse:collapse; margin-top:6px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); font-size:13px;
  vertical-align:top; }
th { color:var(--muted); text-transform:uppercase; font-size:11px; letter-spacing:.05em; }
tr:hover td { background:var(--panel2); }
.peer { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; margin-bottom:12px; }
.peer-head { display:flex; justify-content:space-between; align-items:baseline; gap:10px; }
.peer-name { font-weight:700; font-size:15px; }
.tag { font-size:11px; padding:2px 8px; border-radius:10px; border:1px solid var(--line); color:var(--muted); }
.tag.listed { color:var(--accent); border-color:var(--accent); }
.score { font-weight:700; color:var(--accent); }
.chip { display:inline-block; background:var(--panel2); border:1px solid var(--line);
  border-radius:6px; padding:2px 8px; margin:3px 4px 0 0; font-size:12px; }
.chip b { color:var(--muted); font-weight:600; }
.mult { color:var(--good); }
.because { color:var(--good); font-size:12px; margin-top:6px; }
.diffs { color:var(--warn); font-size:12px; margin-top:2px; }
.reason { color:var(--bad); }
.note { color:var(--warn); }
.mono { font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:12px; }
.headline-range { font-size:18px; font-weight:700; }
.lo-mid-hi span { margin-right:14px; }
.lvl { font-weight:700; font-size:11px; padding:1px 7px; border-radius:5px; }
.lv-info { color:var(--muted); border:1px solid var(--line); }
.lv-warn { color:var(--warn); border:1px solid var(--warn); }
.lv-dec  { color:var(--dec); border:1px solid var(--dec); }
.lv-err  { color:var(--bad); border:1px solid var(--bad); }
.dq-pass { color:var(--good); } .dq-warn { color:var(--warn); } .dq-fail { color:var(--bad); }
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
        ts = (a.get("ts") or "")[11:23]  # HH:MM:SS.mmm
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
    parts.append(f'<h1>MSME Comparable Valuation — {_esc(data.get("query"))}</h1>')
    parts.append(_provenance(meta))

    # ---- degraded runs: honest minimal page ----------------------------
    if data.get("target") is None or data.get("valuation") is None:
        parts.append(f'<div class="card"><div class="k">RESULT</div>'
                     f'<div class="v">No valuation produced — status '
                     f'<b>{_esc(meta.get("status"))}</b>. '
                     f'The audit trail below records exactly why.</div></div>')
        parts.append(f"<h2>Audit Trail ({len(audit)})</h2>")
        parts.append(_audit_table(audit))
        parts.append("</div></body></html>")
        html_str = "".join(parts)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_str)
        return out_path

    t = data["target"]
    tp = data["target_profile"]
    dq = data.get("data_quality") or {}
    val = data["valuation"]
    conf = data["confidence"]

    parts.append(f'<div class="sub mono">DUNS {_esc(t["duns"])} · CIN {_esc(t["cin"])} '
                 f'· Deterministic core, no LLM</div>')

    # ---- four top panels ----------------------------------------------
    parts.append('<div class="panels">')
    # Target profile
    parts.append(f"""<div class="card">
  <div class="k">TARGET PROFILE</div>
  <div class="v"><b>{_esc(tp['operating_model'])}</b> / {_esc(tp['value_chain'])}</div>
  <div class="k">Industry</div>
  <div class="v">NAICS {_esc(t['naics'])} · sub {_esc(tp['naics_subsector'])}<br>
     <span class="mono">{_esc(t['naics_desc'])}</span></div>
  <div class="k">Customer / Export / Listing</div>
  <div class="v">{_esc(tp['customer_type'])} ·
     {'Exporter' if t['is_exporter'] else 'Domestic'} ·
     {'Listed' if t['listed'] else 'Unlisted'}</div>
  <div class="k">Financials (INR Cr)</div>
  <div class="v">Rev {_num(t['revenue_cr'])} · EBITDA {_num(t['ebitda_cr'])}
     ({_pct(t['ebitda_margin'])}) · EBIT {_num(t['ebit_cr'])}<br>
     growth {_pct(t['revenue_growth'])}</div>
</div>""")
    # Headline valuation
    parts.append(f"""<div class="card">
  <div class="k">HEADLINE — {_esc(val['headline_method'])}</div>
  <div class="lo-mid-hi" style="margin:10px 0;">
    <div><span class="k">Low</span> <span class="big">{_num(val['equity_low_cr'])}</span></div>
    <div><span class="k">Mid</span> <span class="big" style="color:var(--accent)">{_num(val['equity_mid_cr'])}</span></div>
    <div><span class="k">High</span> <span class="big">{_num(val['equity_high_cr'])}</span></div>
  </div>
  <div class="k">Equity value, INR Crore</div>
  <div class="v" style="margin-top:8px;">Net debt {_num(val['net_debt_cr'])} Cr ·
     discount {_pct(val['discount'],0)}</div>
  <div class="k mono">{_esc(val['discount_reason'])}</div>
</div>""")
    # Confidence
    warn_html = "".join(f'<div class="note">! {_esc(w)}</div>' for w in val["warnings"])
    parts.append(f"""<div class="card">
  <div class="k">CONFIDENCE</div>
  <div style="margin:14px 0;"><span class="badge {_conf_class(conf['label'])}">
     {_esc(conf['label'])} · {_num(conf['score'],2)}</span></div>
  <div class="k">Peers used</div>
  <div class="v">{len(data['peers'])} of {data['peers_ranked_count']} ranked ·
     {len(data['rejected'])} rejected</div>
  <div class="k">Methods computed</div>
  <div class="v">{len(val['methods'])} of 3</div>
  {warn_html}
</div>""")
    # Data quality
    checks = dq.get("checks", [])
    passed = sum(1 for c in checks if c["status"] == "pass")
    dq_rows = "".join(
        f'<div class="k"><span class="dq-{c["status"]}">'
        f'{"✓" if c["status"]=="pass" else ("!" if c["status"]=="warn" else "✗")}</span> '
        f'{_esc(c["field"])}</div>'
        for c in checks)
    parts.append(f"""<div class="card">
  <div class="k">DATA QUALITY</div>
  <div style="margin:14px 0;"><span class="badge {_grade_class(dq.get('grade'))}">
     Grade {_esc(dq.get('grade'))} · {_num(dq.get('score'),2)}</span></div>
  <div class="k">Checks passed</div>
  <div class="v">{passed} of {len(checks)}</div>
  <div style="max-height:120px;overflow:auto;">{dq_rows}</div>
</div></div>""")

    # ---- valuation methods table --------------------------------------
    parts.append("<h2>Valuation Methods — Triangulation</h2>")
    parts.append("""<table><tr>
      <th>Method</th><th>Target driver (Cr)</th><th>Peers</th><th>Multiples</th>
      <th>Dropped</th><th>Range basis</th><th>Multiple Low / Median / High</th>
      <th>EV mid (Cr)</th><th>Equity Low–Mid–High (Cr)</th></tr>""")
    for m in val["methods"]:
        hl = ' style="background:rgba(79,156,249,.10)"' if m["method"] == val["headline_method"] else ""
        parts.append(f"""<tr{hl}>
      <td><b>{_esc(m['method'])}</b>{' ★' if m['method']==val['headline_method'] else ''}</td>
      <td>{_num(m['target_driver'])}</td>
      <td>{m['n_peers']}</td>
      <td>{m['n_multiples']}</td>
      <td>{m['n_outliers_dropped']}</td>
      <td class="mono">{_esc(m.get('range_basis',''))}</td>
      <td class="mono">{_num(m['multiple_p25'],2)} / <b>{_num(m['multiple_median'],2)}</b> / {_num(m['multiple_p75'],2)}</td>
      <td>{_num(m['ev_mid_cr'])}</td>
      <td class="headline-range">{_num(m['equity_low_cr'])} – <span style="color:var(--accent)">{_num(m['equity_mid_cr'])}</span> – {_num(m['equity_high_cr'])}</td>
    </tr>""")
    parts.append("</table>")
    parts.append(f'<div class="k mono" style="margin-top:8px;">EV basis: {_esc(val["ev_basis"])}</div>')

    # ---- peer cards ----------------------------------------------------
    parts.append(f"<h2>Peer Set ({len(data['peers'])})</h2>")
    for i, p in enumerate(data["peers"], 1):
        tag = '<span class="tag listed">LISTED</span>' if p["listed"] else '<span class="tag">unlisted</span>'
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
        parts.append(f"""<div class="peer">
  <div class="peer-head">
    <div class="peer-name">#{i} {_esc(p['name'])} {tag}</div>
    <div class="score">score {_num(p['score'],3)}</div>
  </div>
  <div style="color:var(--muted);font-size:12px;margin:4px 0;">
     {_esc(p['city'])} · Rev ₹{_num(p['revenue_cr'],0)} Cr · margin {_pct(p['ebitda_margin'])}
     · growth {_pct(p['revenue_growth'])} · EV basis {_esc(p['ev_basis'])}</div>
  <div>{chips}</div>
  <div style="margin-top:6px;">{mult_chips}</div>
  <div class="because">✓ {_esc(because)}</div>
  <div class="diffs">Δ {_esc(diffs)}</div>
</div>""")

    # ---- rejected table -----------------------------------------------
    parts.append(f"<h2>Rejected ({len(data['rejected'])})</h2>")
    parts.append("<table><tr><th>Entity</th><th>Operating model</th><th>Value chain</th>"
                 "<th>Major ind.</th><th>Reason</th></tr>")
    for r in data["rejected"]:
        parts.append(f"""<tr><td>{_esc(r['name'])}</td>
      <td>{_esc(r['operating_model'])}</td><td>{_esc(r['value_chain'])}</td>
      <td>{_esc(r['major_industry'])}</td>
      <td class="reason">{_esc(r['reason'])}</td></tr>""")
    parts.append("</table>")

    # ---- audit trail ---------------------------------------------------
    levels = {}
    for a in audit:
        levels[a.get("level")] = levels.get(a.get("level"), 0) + 1
    lvl_summary = ", ".join(f"{k} {v}" for k, v in sorted(levels.items()))
    parts.append(f"<h2>Audit Trail ({len(audit)} · {_esc(lvl_summary)})</h2>")
    parts.append(_audit_table(audit))

    parts.append("</div></body></html>")

    html_str = "".join(parts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_str)
    return out_path


if __name__ == "__main__":
    import sys
    rp = sys.argv[1] if len(sys.argv) > 1 else "output/result.json"
    op = sys.argv[2] if len(sys.argv) > 2 else "output/dashboard.html"
    build_dashboard(rp, op)
    print(f"Wrote {op}")
