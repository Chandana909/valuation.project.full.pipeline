"""
etl.py — one-time ETL: the 9 uploaded Accord-style Excel extracts -> realdata.db (SQLite).

Architecture note. The engine's core never reads Excel or SQL: it consumes D&B-schema
envelopes from an injected client. This ETL is the OFFLINE data-preparation step
(exactly the role §12 of PROJECT_CONTEXT reserves for "build the universe offline"):
it flattens the workbooks into a queryable SQLite database with PER-ROW PROVENANCE
(source file + row number for every figure) so any number in a valuation can be traced
back to the exact cell range it came from. `realdata/client.py` then serves this DB
through the same request() interface as the mock.

openpyxl is used HERE ONLY (build tool, not the runtime core). All money stays in the
source unit (INR Crore); the client converts to the D&B Thousand convention on emit.

Robustness:
  * header VERIFICATION — every expected column name is checked before reading; a
    mismatch aborts loudly rather than silently mis-mapping figures.
  * P&L reconciliation — EBITDA(excl OI) + Other Income − Interest must equal PBDT
    within max(2%, ₹0.5 Cr); pass/fail stored per company-year and surfaced to the
    data-quality gate.
  * an etl_report is stored in the DB (row counts, join coverage, recon rate).

Run:  python etl.py            (writes realdata.db; ~1-3 min)
      python etl.py --report   (print the stored report of an existing DB)
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "realdata.db")

# ---- source files + the exact columns we read (0-based index, expected header) ----
PL_FILE = "PL data.xlsx"
PL_COLS = {  # index: (expected header contains, field)
    0: ("Accord Code", "accord"),
    2: ("PL_Year End", "year_end"),
    5: ("PL_No of Months", "months"),
    67: ("PL_Net Sales", "revenue"),
    247: ("PL_Operating Profit (Excl OI)", "ebitda"),
    248: ("PL_Other Income", "other_income"),
    281: ("PL_Interest", "interest"),
    292: ("PL_PBDT", "pbdt"),
    293: ("PL_Depreciation", "depreciation"),
    304: ("PL_Profit Before Tax", "pbt"),
    316: ("PL_Profit After Tax", "pat"),
}
BS_FILE = "BS data.xlsx"
BS_COLS = {1: ("Accord Code", "accord"), 3: ("BS_Year End", "year_end"),
           4: ("Plant & Machinery", "plant_mach"), 5: ("BS_Net Block", "net_block"),
           6: ("BS_Inventories", "inventories"), 7: ("BS_Total Assets", "total_assets")}
NW_FILE = "Net worth.xlsx"
NW_COLS = {1: ("Accord Code", "accord"), 3: ("FH_Year End", "year_end"),
           4: ("FH_Net Worth", "net_worth")}
FX_FILE = "Forex.xlsx"
FX_COLS = {1: ("Accord Code", "accord"), 3: ("FX_Year_End", "year_end"),
           4: ("FX_Total Inflow In Foreign Currency", "fx_inflow")}
BASIC_FILE = "Basic Data.xlsx"
BASIC_COLS = {1: ("Accord Code", "accord"), 2: ("Company Name", "name"),
              3: ("CD_Industry", "industry"), 4: ("CD_Economic Activity(NIC)", "nic"),
              5: ("CD_Business Description", "description"),
              6: ("CD_CIN Number", "cin"), 7: ("CD_ISIN No", "isin")}
SEG_FILE = "Segment.xlsx"
SEG_COLS = {1: ("Accord Code", "accord"), 3: ("FSG_Year End", "year_end"),
            14: ("FSG_Capital employed", "seg_ce")}


def _open_rows(path, cols):
    """Yield dicts of the mapped columns after VERIFYING the header. Row numbers are
    the real 1-based Excel rows (header = 1), stored as provenance."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = next(it)
    for idx, (expect, field) in cols.items():
        got = str(header[idx] or "")
        # headers contain non-breaking spaces / padding — compare on squeezed text
        if expect.replace(" ", "").lower() not in got.replace("\xa0", "").replace(" ", "").lower():
            wb.close()
            raise SystemExit(
                f"ETL ABORT: {os.path.basename(path)} col {idx} header {got!r} "
                f"does not contain expected {expect!r} — column map is stale, refusing "
                f"to mis-map financial data.")
    rownum = 1
    for row in it:
        rownum += 1
        if not row or row[max(cols)] is None and row[0] is None:
            continue
        rec = {"src_row": rownum}
        for idx, (_e, field) in cols.items():
            rec[field] = row[idx] if idx < len(row) else None
        yield rec
    wb.close()


def _num(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build(db_path=DB_PATH):
    for f in (PL_FILE, BS_FILE, NW_FILE, FX_FILE, BASIC_FILE, SEG_FILE):
        if not os.path.exists(os.path.join(BASE, f)):
            raise SystemExit(f"ETL ABORT: missing source file {f}")

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE companies(
      accord INTEGER PRIMARY KEY, name TEXT NOT NULL, industry TEXT, nic TEXT,
      description TEXT, cin TEXT, isin TEXT, src_row INTEGER);
    CREATE TABLE fin(
      accord INTEGER, year_end INTEGER, months REAL,
      revenue REAL, ebitda REAL, other_income REAL, interest REAL, pbdt REAL,
      depreciation REAL, pbt REAL, pat REAL, src_row_pl INTEGER,
      net_worth REAL, src_row_nw INTEGER,
      plant_mach REAL, net_block REAL, inventories REAL, total_assets REAL,
      src_row_bs INTEGER,
      fx_inflow REAL, src_row_fx INTEGER,
      seg_ce REAL, src_row_seg INTEGER,
      recon_ok INTEGER, recon_gap REAL,
      PRIMARY KEY(accord, year_end));
    CREATE INDEX idx_companies_name ON companies(name);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    """)

    report = {"started": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # ---- companies -------------------------------------------------------
    rows = []
    for r in _open_rows(os.path.join(BASE, BASIC_FILE), BASIC_COLS):
        if r["accord"] is None or not r["name"]:
            continue
        rows.append((int(r["accord"]), str(r["name"]).strip(), r["industry"], r["nic"],
                     r["description"], r["cin"], r["isin"], r["src_row"]))
    cur.executemany("INSERT OR REPLACE INTO companies VALUES(?,?,?,?,?,?,?,?)", rows)
    report["companies"] = len(rows)
    print(f"companies: {len(rows)}")

    # ---- P&L (the fin-table spine) ---------------------------------------
    rows = []
    for r in _open_rows(os.path.join(BASE, PL_FILE), PL_COLS):
        if r["accord"] is None or r["year_end"] is None:
            continue
        rows.append((int(r["accord"]), int(r["year_end"]), _num(r["months"]),
                     _num(r["revenue"]), _num(r["ebitda"]), _num(r["other_income"]),
                     _num(r["interest"]), _num(r["pbdt"]), _num(r["depreciation"]),
                     _num(r["pbt"]), _num(r["pat"]), r["src_row"]))
    cur.executemany("""INSERT OR REPLACE INTO fin
        (accord,year_end,months,revenue,ebitda,other_income,interest,pbdt,
         depreciation,pbt,pat,src_row_pl)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    report["pl_rows"] = len(rows)
    print(f"P&L rows: {len(rows)}")

    # ---- attach the satellite files to fin rows --------------------------
    def attach(path, cols, sets, src_field):
        n = 0
        for r in _open_rows(os.path.join(BASE, path), cols):
            if r["accord"] is None or r["year_end"] is None:
                continue
            vals = [_num(r[f]) for f in sets] + [r["src_row"],
                                                 int(r["accord"]), int(r["year_end"])]
            setsql = ",".join(f"{f}=?" for f in sets) + f",{src_field}=?"
            cur.execute(f"UPDATE fin SET {setsql} WHERE accord=? AND year_end=?", vals)
            if cur.rowcount:
                n += 1
            else:
                # financial fact without a P&L spine row — keep it (INSERT), so
                # nothing silently disappears
                cur.execute(
                    f"INSERT OR IGNORE INTO fin(accord,year_end,{','.join(sets)},{src_field}) "
                    f"VALUES(?,?,{','.join('?'*len(sets))},?)",
                    [int(r["accord"]), int(r["year_end"])] + [_num(r[f]) for f in sets]
                    + [r["src_row"]])
        return n

    report["bs_joined"] = attach(BS_FILE, BS_COLS,
                                 ["plant_mach", "net_block", "inventories", "total_assets"],
                                 "src_row_bs")
    print(f"balance-sheet rows joined: {report['bs_joined']}")
    report["nw_joined"] = attach(NW_FILE, NW_COLS, ["net_worth"], "src_row_nw")
    print(f"net-worth rows joined: {report['nw_joined']}")
    report["fx_joined"] = attach(FX_FILE, FX_COLS, ["fx_inflow"], "src_row_fx")
    print(f"forex rows joined: {report['fx_joined']}")

    # segment capital employed: SUM the segments per company-year
    seg = {}
    seg_src = {}
    for r in _open_rows(os.path.join(BASE, SEG_FILE), SEG_COLS):
        ce = _num(r["seg_ce"])
        if r["accord"] is None or r["year_end"] is None or ce is None:
            continue
        k = (int(r["accord"]), int(r["year_end"]))
        seg[k] = seg.get(k, 0.0) + ce
        seg_src[k] = r["src_row"]
    for (a, y), ce in seg.items():
        cur.execute("UPDATE fin SET seg_ce=?, src_row_seg=? WHERE accord=? AND year_end=?",
                    (ce, seg_src[(a, y)], a, y))
    report["seg_company_years"] = len(seg)
    print(f"segment capital-employed company-years: {len(seg)}")

    # ---- P&L reconciliation gate -----------------------------------------
    # identity: EBITDA(excl OI) + Other Income − Interest ≈ PBDT
    cur.execute("SELECT accord,year_end,ebitda,other_income,interest,pbdt FROM fin")
    ok = bad = unk = 0
    for a, y, e, oi, i, pbdt in cur.fetchall():
        if None in (e, pbdt):
            cur.execute("UPDATE fin SET recon_ok=NULL WHERE accord=? AND year_end=?", (a, y))
            unk += 1
            continue
        calc = e + (oi or 0.0) - (i or 0.0)
        gap = abs(calc - pbdt)
        tol = max(0.02 * abs(pbdt), 0.5)
        good = 1 if gap <= tol else 0
        ok += good
        bad += (1 - good)
        cur.execute("UPDATE fin SET recon_ok=?, recon_gap=? WHERE accord=? AND year_end=?",
                    (good, round(gap, 4), a, y))
    report["recon"] = {"ok": ok, "fail": bad, "unknown": unk,
                       "rate_pct": round(ok / max(1, ok + bad) * 100, 1)}
    print(f"P&L reconciliation: {ok} ok / {bad} fail / {unk} n.a. "
          f"({report['recon']['rate_pct']}% of determinable rows reconcile)")

    # ---- valuation-grade universe count ------------------------------------
    cur.execute("""SELECT COUNT(DISTINCT f.accord) FROM fin f
                   JOIN companies c ON c.accord=f.accord
                   WHERE f.revenue>0 AND f.ebitda IS NOT NULL
                     AND f.net_worth IS NOT NULL AND f.net_worth>0
                     AND (f.months IS NULL OR f.months=12)""")
    report["valuation_grade_companies"] = cur.fetchone()[0]
    print(f"valuation-grade companies (rev>0, EBITDA, net worth, 12m): "
          f"{report['valuation_grade_companies']}")

    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur.execute("INSERT OR REPLACE INTO meta VALUES('etl_report',?)", (json.dumps(report),))
    cur.execute("INSERT OR REPLACE INTO meta VALUES('etl_timestamp',?)", (report["finished"],))
    con.commit()
    con.close()
    print(f"\nwrote {db_path}")
    return report


def show_report(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT value FROM meta WHERE key='etl_report'").fetchone()
    con.close()
    print(json.dumps(json.loads(row[0]), indent=2) if row else "no report stored")


if __name__ == "__main__":
    if "--report" in sys.argv:
        show_report()
    else:
        build()
