#!/usr/bin/env python3
"""
crucible ledger — the append-only source of truth for a campaign.

One JSONL record per experiment. The session writes here every iteration (including
failures); a resumed session reconstructs the live Pareto front from here. The
`parent` pointer encodes the recursion DAG.

This module is deliberately dependency-free (stdlib only) so it runs on a minimal host.

CLI:
    python3 ledger.py front  boxes/<nick>/ledger.jsonl     # print non-dominated set
    python3 ledger.py tail   boxes/<nick>/ledger.jsonl 10  # last N records
    python3 ledger.py stats  boxes/<nick>/ledger.jsonl     # counts by status
"""
from __future__ import annotations
import json, os, sys, time, uuid
try:
    import fcntl  # Unix (the target + the independent verifier run here, lock unchanged)
except ImportError:  # Windows orchestrator host: no fcntl. Single-writer atomic append.
    fcntl = None     # PORTABILITY SHIM — see boxes/<nick>/GATE_QUEUE.md. Semantics preserved:
                     # append-only + flush + fsync intact; only the advisory lock is skipped.
from dataclasses import dataclass, asdict, field
from typing import Optional, Any

# ---- status vocabulary (doctrine/01) -----------------------------------------
STATUS = {"degenerate", "failed", "couldnt_load", "contender", "blessed"}

# ---- record schema -----------------------------------------------------------
@dataclass
class Record:
    # identity / lineage
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    parent: Optional[str] = None            # id of the config this descends from (DAG)
    epoch: float = field(default_factory=time.time)
    status: str = "contender"               # one of STATUS

    # what was run
    config: dict[str, Any] = field(default_factory=dict)  # engine, model, quant, threads, kv, spec...
    config_hash: str = ""                   # stable hash of `config`
    engine_sha: Optional[str] = None        # git SHA of the engine/kernel patch under test
    hardware_ref: str = ""                  # path or hash of hardware.json this ran against

    # the rubric axes (doctrine/01) — None when not measured (e.g. failed before measure)
    decode_tok_s: Optional[float] = None
    decode_tok_s_var: Optional[float] = None
    prefill_tok_s: Optional[float] = None
    ttft_s: Optional[float] = None
    quality: Optional[float] = None         # synthesized single coordinate (Elo-anchored)
    perf_per_watt: Optional[float] = None
    power_source: Optional[str] = None      # "measured" | "estimated"

    # context, not objectives
    peak_rss_bytes: Optional[int] = None
    roofline_ceiling_tok_s: Optional[float] = None
    roofline_efficiency: Optional[float] = None  # achieved/ceiling
    accept_rate: Optional[float] = None     # speculative decoding acceptance (if used)

    # eval detail (doctrine/02)
    kld_vs_fp16: Optional[float] = None
    bpb: Optional[float] = None
    math_pass: Optional[float] = None       # fraction
    code_pass: Optional[float] = None       # fraction

    # bookkeeping
    notes: str = ""

    def validate(self) -> None:
        if self.status not in STATUS:
            raise ValueError(f"bad status {self.status!r}; must be one of {sorted(STATUS)}")


# ---- atomic append -----------------------------------------------------------
def append(ledger_path: str, rec: Record) -> Record:
    """Append one record with an exclusive lock + fsync. Returns the record."""
    rec.validate()
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    line = json.dumps(asdict(rec), separators=(",", ":"), sort_keys=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return rec


def load(ledger_path: str) -> list[dict]:
    if not os.path.exists(ledger_path):
        return []
    out = []
    with open(ledger_path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass  # tolerate a torn final line; never crash a resume on it
    return out


# ---- Pareto front ------------------------------------------------------------
# Objectives (doctrine/01): maximize decode_tok_s, maximize prefill_tok_s, maximize
# quality. (TTFT is carried with prefill; perf/watt and RSS are context, not axes.)
# Degenerate/failed/couldnt_load are excluded from the front.
_OBJ = ("decode_tok_s", "prefill_tok_s", "quality")

def _eligible(r: dict) -> bool:
    if r.get("status") in ("degenerate", "failed", "couldnt_load"):
        return False
    return all(r.get(k) is not None for k in _OBJ)

def _dominates(a: dict, b: dict) -> bool:
    """a dominates b: >= on every objective and > on at least one (all maximized)."""
    ge = all(a[k] >= b[k] for k in _OBJ)
    gt = any(a[k] > b[k] for k in _OBJ)
    return ge and gt

def pareto_front(records: list[dict]) -> list[dict]:
    pts = [r for r in records if _eligible(r)]
    front = []
    for r in pts:
        if not any(_dominates(o, r) for o in pts if o is not r):
            front.append(r)
    # de-dup configs that tie on all objectives, keep the most recent
    best: dict[tuple, dict] = {}
    for r in front:
        key = tuple(round(r[k], 6) for k in _OBJ)
        if key not in best or r["epoch"] > best[key]["epoch"]:
            best[key] = r
    return sorted(best.values(), key=lambda r: -r["decode_tok_s"])


# ---- CLI ---------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__); return 1
    cmd, path = argv[1], argv[2]
    recs = load(path)
    if cmd == "front":
        for r in pareto_front(recs):
            print(f"{r['id']}  dec={r['decode_tok_s']:.1f}  pre={r.get('prefill_tok_s')}  "
                  f"Q={r.get('quality')}  eff={r.get('roofline_efficiency')}  {r['config'].get('model','?')}")
    elif cmd == "tail":
        n = int(argv[3]) if len(argv) > 3 else 10
        for r in recs[-n:]:
            print(f"{r['id']}  [{r['status']}]  {r['config'].get('model','?')}  "
                  f"dec={r.get('decode_tok_s')}  {r.get('notes','')[:60]}")
    elif cmd == "stats":
        from collections import Counter
        c = Counter(r["status"] for r in recs)
        print(f"total={len(recs)}  " + "  ".join(f"{k}={v}" for k, v in c.items()))
        print(f"front size={len(pareto_front(recs))}")
    else:
        print(__doc__); return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
