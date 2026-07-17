"""
intake.py — conversational intake agent: a deterministic question-graph that
collects a target company's profile turn by turn, then builds the same `Company`
object the rest of the engine consumes — so a company that is NOT in the
database can be valued against database peers with zero changes to the core.

Design (LangGraph-style, no LLM):
  * The conversation is a STATE GRAPH: each node is a question with a typed
    validator and a `help` explainer (the theory shown to the user); edges are
    the `skip_if` conditions that route past questions that no longer apply.
  * Deterministic and touchless, consistent with the project contract — the
    graph IS the agent. An LLM layer can later sit in front (free-text →
    answers) without changing anything downstream: the seam is `submit()`.
  * Sessions are plain dicts (JSON-serializable) so any delivery layer — the
    bundled API, a CLI, a chatbot — can drive the same graph.

Industry resolution: free-text sector answers are matched against the live data
source's own industry catalog (RealDnBClient exposes the 137 CD_Industry
categories), falling back to the keyword sector-group rules — so the intake
company lands in the same classification space as its database peers.
"""

import uuid
from math import isfinite

from core import Company
from realdata.client import _GROUP_RULES, _GROUP_MAJOR, _group_of

# ---------------------------------------------------------------------------
# The question graph
# ---------------------------------------------------------------------------


def _v_text(minlen):
    def check(s):
        s = (s or "").strip()
        if len(s) < minlen:
            return None, f"needs at least {minlen} characters"
        return s, None
    return check


def _v_number(lo=None, hi=None, allow_negative=False):
    def check(s):
        s = str(s).replace(",", "").replace("₹", "").strip()
        try:
            x = float(s)
        except ValueError:
            return None, "please enter a number (in ₹ Crore, e.g. 42.5)"
        if not isfinite(x):
            return None, "please enter a finite number"
        if not allow_negative and x < 0:
            return None, "cannot be negative"
        if lo is not None and x < lo:
            return None, f"must be at least {lo}"
        if hi is not None and x > hi:
            return None, f"{x:,.0f} looks too large — figures are in ₹ CRORE"
        return x, None
    return check


def _v_yesno(s):
    t = str(s).strip().lower()
    if t in ("y", "yes", "true", "1", "listed", "exporter"):
        return True, None
    if t in ("n", "no", "false", "0", "unlisted", "domestic"):
        return False, None
    return None, "please answer yes or no"


# Each node: id · prompt · help (theory for the user) · validator · optional
# flag (skippable with "skip") · skip_if (edge condition on collected answers).
GRAPH = [
    {"id": "name", "prompt": "What is the company's name?",
     "help": "Used only for labelling the report — the valuation itself is "
             "driven by the figures you provide next.",
     "validate": _v_text(2)},
    {"id": "description",
     "prompt": "Describe what the company does — what it makes or sells, and who buys it.",
     "help": "This drives the ECONOMIC CLASSIFIER. Words like 'manufactures', "
             "'distributor of', 'retail stores', 'consulting services' decide the "
             "operating model, which is a hard peer filter — a manufacturer is never "
             "compared to a trader. Mention customers (OEMs? consumers?) too.",
     "validate": _v_text(15)},
    {"id": "industry",
     "prompt": "Which industry / sector is it in? (e.g. auto components, pharmaceuticals, textiles, engineering)",
     "help": "Matched against the database's own industry catalog so your company "
             "lands in the same classification space as its candidate peers. Industry "
             "proximity is the single biggest similarity weight (40%).",
     "validate": _v_text(3)},
    {"id": "listed", "prompt": "Is the company listed on a stock exchange? (yes/no)",
     "help": "A private company gets a DLOM — a discount for lack of marketability — "
             "because it is priced off listed peers whose shares are liquid. A listed "
             "company gets no DLOM.",
     "validate": _v_yesno},
    {"id": "revenue_cr", "prompt": "Latest annual revenue, in ₹ Crore?",
     "help": "The primary scale driver. It anchors the EV/Revenue method, the size "
             "band for the DLOM, and the scale-proximity peer filter.",
     "validate": _v_number(lo=0.01, hi=2_000_000)},
    {"id": "revenue_prior_cr",
     "prompt": "Revenue the year before, in ₹ Crore? (type 'skip' if unknown)",
     "help": "Gives revenue growth, which sharpens peer comparison. Skippable.",
     "validate": _v_number(lo=0.0, hi=2_000_000), "optional": True},
    {"id": "ebitda_cr", "prompt": "EBITDA (operating profit before depreciation), in ₹ Crore?",
     "help": "The headline driver: EV/EBITDA is the primary valuation method, and "
             "the EBITDA MARGIN positions your company inside the peer multiple "
             "range — a stronger-margin company earns a higher multiple. May be "
             "negative; the engine then falls back to EV/Revenue honestly.",
     "validate": _v_number(allow_negative=True, hi=1_000_000)},
    {"id": "depreciation_cr",
     "prompt": "Annual depreciation, in ₹ Crore? (type 'skip' if unknown)",
     "help": "Lets the engine compute EBIT = EBITDA − depreciation, enabling the "
             "third triangulation method (EV/EBIT). Skippable.",
     "validate": _v_number(lo=0.0, hi=1_000_000), "optional": True},
    {"id": "net_worth_cr", "prompt": "Net worth (shareholders' funds), in ₹ Crore?",
     "help": "The book-value floor. The database peers are priced on book capital "
             "employed (the extract has no market prices), so your company needs the "
             "same anchor for like-for-like multiples.",
     "validate": _v_number(lo=0.01, hi=2_000_000)},
    {"id": "debt_cr", "prompt": "Total borrowings/debt, in ₹ Crore? (type 'skip' if unknown)",
     "help": "Debt − cash = net debt, the bridge from enterprise value to equity "
             "value. If skipped, net debt is assumed 0 WITH a disclosed warning.",
     "validate": _v_number(lo=0.0, hi=2_000_000), "optional": True},
    {"id": "cash_cr", "prompt": "Cash and equivalents, in ₹ Crore? (type 'skip' if unknown)",
     "help": "The other half of the net-debt bridge. Skippable, same disclosure.",
     "validate": _v_number(lo=0.0, hi=2_000_000), "optional": True},
    {"id": "is_exporter", "prompt": "Does the company export? (yes/no)",
     "help": "Exporters and domestic-only companies face different demand and "
             "currency dynamics; the export flag is a 10% similarity dimension.",
     "validate": _v_yesno},
]
_BY_ID = {n["id"]: n for n in GRAPH}


# ---------------------------------------------------------------------------
# Session — one conversation walking the graph
# ---------------------------------------------------------------------------

class IntakeSession:
    """One guided conversation. JSON-serializable state; deterministic."""

    def __init__(self):
        self.session_id = uuid.uuid4().hex[:12]
        self.answers = {}
        self.idx = 0

    # -- state ---------------------------------------------------------------
    @property
    def done(self):
        return self.idx >= len(GRAPH)

    def current(self):
        if self.done:
            return None
        n = GRAPH[self.idx]
        return {"id": n["id"], "prompt": n["prompt"], "help": n["help"],
                "optional": bool(n.get("optional"))}

    def progress(self):
        return {"answered": self.idx, "total": len(GRAPH),
                "pct": round(self.idx / len(GRAPH) * 100)}

    # -- the one transition --------------------------------------------------
    def submit(self, raw):
        """Validate the answer for the current node and advance. Returns
        {ok, error?, question?, done, progress}. This is the LLM seam: a
        language layer would translate free text into these submits."""
        if self.done:
            return {"ok": False, "error": "intake already complete",
                    "done": True, "progress": self.progress()}
        node = GRAPH[self.idx]
        raw_s = str(raw if raw is not None else "").strip()
        if node.get("optional") and raw_s.lower() in ("skip", "na", "n/a", "-", ""):
            self.answers[node["id"]] = None
            self.idx += 1
        else:
            val, err = node["validate"](raw_s)
            if err:
                return {"ok": False, "error": err, "question": self.current(),
                        "done": False, "progress": self.progress()}
            self.answers[node["id"]] = val
            self.idx += 1
        return {"ok": True, "question": self.current(), "done": self.done,
                "progress": self.progress(),
                "summary": self.summary() if self.done else None}

    def summary(self):
        a = self.answers
        return {k: a.get(k) for k in _BY_ID}

    def to_dict(self):
        return {"session_id": self.session_id, "answers": self.summary(),
                "done": self.done, "progress": self.progress(),
                "question": self.current()}


# ---------------------------------------------------------------------------
# answers -> Company (the same dataclass the whole engine consumes)
# ---------------------------------------------------------------------------

def resolve_industry(text, client=None):
    """Map free-text sector to (naics, hoovers, major, description) in the SAME
    classification space as the database universe. Prefers an exact category
    from the live client's catalog; falls back to keyword sector groups."""
    t = (text or "").strip().lower()
    # 1) exact/substring match against the client's own category catalog
    catalog = getattr(client, "industry_catalog", None)
    if callable(catalog):
        best = None
        for cat, naics in catalog().items():
            cl = cat.lower()
            if t and (t in cl or cl in t):
                if best is None or len(cat) < len(best[0]):   # tightest match
                    best = (cat, naics)
        if best:
            grp = _group_of(best[0])
            return (best[1], f"GRP_{grp}", _GROUP_MAJOR.get(grp, "I"), best[0])
    # 2) keyword sector-group fallback (same rules the real client uses)
    grp = _group_of(t)
    return ("9990", f"GRP_{grp}", _GROUP_MAJOR.get(grp, "I"), text)


def build_company(answers, client=None):
    """Intake answers -> Company. Missing optional figures stay None/flagged
    (debt_known/cash_known) so every downstream warning fires honestly."""
    a = answers
    naics, hoovers, major, ind_desc = resolve_industry(a.get("industry"), client)
    rev, prior = a.get("revenue_cr"), a.get("revenue_prior_cr")
    ebitda, dep = a.get("ebitda_cr"), a.get("depreciation_cr")
    debt, cash = a.get("debt_cr"), a.get("cash_cr")
    growth = ((rev - prior) / prior) if (rev and prior) else None
    margin = (ebitda / rev) if (rev and ebitda is not None) else None
    ebit = (ebitda - dep) if (ebitda is not None and dep is not None) else None
    nw = a.get("net_worth_cr")
    return Company(
        duns="CUSTOM-" + uuid.uuid4().hex[:8],
        name=a.get("name") or "Unnamed company", cin=None,
        naics=naics, naics_desc=ind_desc, hoovers=hoovers,
        major_industry=major, major_industry_desc=None,
        activities=a.get("description") or "",
        is_exporter=bool(a.get("is_exporter")),
        employees=None, incorporated=None, city=None,
        listed=bool(a.get("listed")),
        revenue_cr=rev, revenue_prior_cr=prior, revenue_growth=growth,
        ebitda_cr=ebitda, ebitda_margin=margin,
        depreciation_cr=dep, ebit_cr=ebit,
        gross_profit_cr=None, operating_profit_cr=None,
        pat_cr=None, net_income_cr=None,
        cash_cr=(cash or 0.0), debt_cr=(debt or 0.0),
        capital_employed_cr=nw,      # book anchor, same approximation as the DB
        net_worth_cr=nw, total_assets_cr=None, working_capital_cr=None,
        listing_status="listed" if a.get("listed") else "unlisted",
        debt_known=debt is not None, cash_known=cash is not None,
    )


def intake_lineage(answers):
    """Per-field provenance for a user-provided target (mirrors the DB lineage
    shape so the report's lineage card renders identically)."""
    lin = {}
    for k, v in answers.items():
        if v is None:
            continue
        lin[k] = {"file": "guided intake (user-provided)", "row": None, "fy": None}
    return lin
