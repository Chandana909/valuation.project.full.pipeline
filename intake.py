"""
intake.py — conversational intake agent on a REAL LangGraph StateGraph.

The conversation is a compiled `langgraph.graph.StateGraph`: one node per
question (12 nodes), a conditional entry point that routes each turn to the
node for the current position, and edges that either advance the state
(validated answer) or hold it in place with a typed error (bad answer).
`IntakeSession.submit()` invokes the compiled graph exactly once per user turn.

Deterministic by construction — the graph runtime is LangGraph, but every node
is a pure validator function: NO LLM, no network, no sampling, nothing
fabricated. If a figure was not provided, it stays None and every downstream
warning fires (net-debt-unknown, method-skipped, …) — the agent never fills
gaps with guesses. An LLM layer could later sit in FRONT of `submit()`
(free text → answer string) without changing the graph or anything downstream.

Answers build the same `Company` dataclass the rest of the engine consumes, so
a company that is NOT in the database is valued against database peers with
zero changes to the core. Industry free-text is matched against the live data
source's own catalog (137 CD_Industry categories), falling back to the keyword
sector-group rules — the intake company lands in the same classification space
as its peers.
"""

import uuid
from math import isfinite
from typing import Optional, TypedDict

from langgraph.graph import StateGraph, START, END

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
    {"id": "txn_ev_cr",
     "prompt": "Optional — do you know a recent comparable ACQUISITION in this "
               "sector? Enterprise value paid, in ₹ Crore (type 'skip' if none)",
     "help": "An OBSERVED deal beats any derived premium: if you know a real "
             "transaction, its EV/EBITDA becomes the comparable-transactions "
             "method instead of the derived 20-30% control-premium view.",
     "validate": _v_number(lo=0.01, hi=5_000_000), "optional": True},
    {"id": "txn_ebitda_cr",
     "prompt": "That acquired company's EBITDA, in ₹ Crore? (type 'skip' if unknown)",
     "help": "EV ÷ EBITDA gives the observed transaction multiple, applied to "
             "your company's EBITDA. Both figures are needed; either skipped "
             "falls back to the derived view.",
     "validate": _v_number(lo=0.01, hi=1_000_000), "optional": True},
]


def txn_multiple_from(answers):
    """Observed transaction multiple from the two optional intake answers, or
    None — nothing is derived or assumed here."""
    ev, eb = answers.get("txn_ev_cr"), answers.get("txn_ebitda_cr")
    if ev and eb and eb > 0:
        return round(ev / eb, 4)
    return None
_BY_ID = {n["id"]: n for n in GRAPH}


# ---------------------------------------------------------------------------
# The LangGraph StateGraph — one node per question, conditional entry routing
# ---------------------------------------------------------------------------

class IntakeState(TypedDict):
    answers: dict
    idx: int
    pending: Optional[str]      # the user's raw answer for this turn
    error: Optional[str]        # validation error (state held in place)
    industry_choices: Optional[list]   # live catalog for the industry node


def _match_choice(raw, choices):
    """Deterministic selection matching: exact (case-insensitive) wins, else a
    UNIQUE substring match; ambiguous or unknown input is rejected with the
    matching candidates listed — the user selects, never free-types a sector."""
    t = raw.strip().lower()
    if not t:
        return None, "please pick an industry from the list"
    exact = [c for c in choices if c.lower() == t]
    if exact:
        return exact[0], None
    subs = [c for c in choices if t in c.lower()]
    if len(subs) == 1:
        return subs[0], None
    if 1 < len(subs) <= 8:
        return None, "ambiguous — did you mean: " + " · ".join(subs)
    if len(subs) > 8:
        return None, f"'{raw}' matches {len(subs)} sectors — keep typing to narrow it"
    return None, f"'{raw}' is not in the industry list — type part of a sector name"


def _make_node(node_def):
    """A pure validator node: consume `pending`, either advance (answer
    recorded) or hold with a typed error. No side effects, no fabrication."""
    def node(state: IntakeState) -> IntakeState:
        raw = (state.get("pending") or "").strip()
        answers = dict(state["answers"])
        if node_def.get("optional") and raw.lower() in ("skip", "na", "n/a", "-", ""):
            answers[node_def["id"]] = None
            return {"answers": answers, "idx": state["idx"] + 1,
                    "pending": None, "error": None}
        # the industry node becomes a SELECTION when a live catalog is present:
        # the answer must resolve to exactly one catalog category, so the
        # sub-sector classification downstream is exact, never approximate
        if node_def["id"] == "industry" and state.get("industry_choices"):
            val, err = _match_choice(raw, state["industry_choices"])
        else:
            val, err = node_def["validate"](raw)
        if err:
            return {"answers": answers, "idx": state["idx"],
                    "pending": None, "error": err}
        answers[node_def["id"]] = val
        return {"answers": answers, "idx": state["idx"] + 1,
                "pending": None, "error": None}
    node.__name__ = f"q_{node_def['id']}"
    return node


def _route(state: IntakeState) -> str:
    """Conditional entry: send this turn to the node of the current question."""
    if state["idx"] >= len(GRAPH):
        return END
    return GRAPH[state["idx"]]["id"]


def _build_graph():
    g = StateGraph(IntakeState)
    for node_def in GRAPH:
        g.add_node(node_def["id"], _make_node(node_def))
        g.add_edge(node_def["id"], END)
    g.add_conditional_edges(START, _route,
                            {**{n["id"]: n["id"] for n in GRAPH}, END: END})
    return g.compile()


_COMPILED = _build_graph()          # compiled once at import; stateless runtime


# ---------------------------------------------------------------------------
# Session — one conversation driving the compiled graph
# ---------------------------------------------------------------------------

class IntakeSession:
    """One guided conversation. JSON-serializable state; deterministic.
    Each submit() = one invocation of the compiled LangGraph StateGraph."""

    def __init__(self, industry_choices=None):
        self.session_id = uuid.uuid4().hex[:12]
        self.answers = {}
        self.idx = 0
        self.industry_choices = sorted(industry_choices) if industry_choices else None

    # -- state ---------------------------------------------------------------
    @property
    def done(self):
        return self.idx >= len(GRAPH)

    def current(self):
        if self.done:
            return None
        n = GRAPH[self.idx]
        q = {"id": n["id"], "prompt": n["prompt"], "help": n["help"],
             "optional": bool(n.get("optional"))}
        if n["id"] == "industry" and self.industry_choices:
            q["choices"] = self.industry_choices
            q["prompt"] = ("Which industry / sector is it in? Pick from the list "
                           "(type to search — the answer must match a category).")
        return q

    def progress(self):
        return {"answered": self.idx, "total": len(GRAPH),
                "pct": round(self.idx / len(GRAPH) * 100)}

    # -- the one transition --------------------------------------------------
    def submit(self, raw):
        """Run one turn through the compiled graph. Returns {ok, error?,
        question?, done, progress}. This is the LLM seam: a language layer
        would translate free text into these submits."""
        if self.done:
            return {"ok": False, "error": "intake already complete",
                    "done": True, "progress": self.progress()}
        out = _COMPILED.invoke({"answers": self.answers, "idx": self.idx,
                                "pending": str(raw if raw is not None else ""),
                                "error": None,
                                "industry_choices": self.industry_choices})
        self.answers = out["answers"]
        self.idx = out["idx"]
        if out.get("error"):
            return {"ok": False, "error": out["error"], "question": self.current(),
                    "done": False, "progress": self.progress()}
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
