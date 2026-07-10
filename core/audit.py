"""
core/audit.py — structured, tamper-evident-style audit trail.

Every material step (a D&B call, a normalization decision, a rejected peer, a
valuation fallback) is recorded as an ordered, typed AuditRecord. This is the
single trust mechanism in a touchless pipeline, so it is deliberately explicit:

  * seq    — monotonic sequence number (ordering is guaranteed)
  * ts     — ISO-8601 UTC timestamp
  * stage  — coarse pipeline stage (resolve | fetch | normalize | profile |
             validate | discover | value | confidence | dnb)
  * level  — INFO | WARN | DECISION | ERROR
  * code   — machine-readable code (e.g. TARGET_RESOLVED, PEER_REJECTED,
             FALLBACK_HEADLINE, DATA_QUALITY_WARN) — stable for downstream tooling
  * detail — human-readable message
  * data   — optional structured payload

Stdlib only. No dependency on mock_api or any framework. Supports the legacy
`.append({"source":..., "detail":...})` shape so existing callers keep working.
"""

from datetime import datetime, timezone

INFO = "INFO"
WARN = "WARN"
DECISION = "DECISION"
ERROR = "ERROR"

_VALID_LEVELS = {INFO, WARN, DECISION, ERROR}


class AuditTrail:
    """Ordered collection of structured audit records."""

    def __init__(self):
        self._entries = []
        self._seq = 0

    # -- primary structured API ------------------------------------------
    def log(self, stage, level, code, detail, data=None):
        if level not in _VALID_LEVELS:
            level = INFO
        self._seq += 1
        rec = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "stage": stage,
            "level": level,
            "code": code,
            "detail": detail,
            "data": data,
        }
        self._entries.append(rec)
        return rec

    def info(self, stage, code, detail, data=None):
        return self.log(stage, INFO, code, detail, data)

    def warn(self, stage, code, detail, data=None):
        return self.log(stage, WARN, code, detail, data)

    def decision(self, stage, code, detail, data=None):
        return self.log(stage, DECISION, code, detail, data)

    def error(self, stage, code, detail, data=None):
        return self.log(stage, ERROR, code, detail, data)

    # -- legacy shim: callers doing audit.append({"source","detail"}) -----
    def append(self, item):
        if isinstance(item, dict) and "source" in item:
            src = item.get("source", "")
            stage = src.split(":", 1)[0] if ":" in src else src
            code = src.split(":", 1)[1].upper() if ":" in src else "EVENT"
            return self.log(stage or "event", INFO, code, item.get("detail", ""))
        return self.log("event", INFO, "EVENT", str(item))

    # -- access ----------------------------------------------------------
    def to_list(self):
        return list(self._entries)

    def counts_by_level(self):
        out = {INFO: 0, WARN: 0, DECISION: 0, ERROR: 0}
        for e in self._entries:
            out[e["level"]] = out.get(e["level"], 0) + 1
        return out

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)
