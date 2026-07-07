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
    python3 ledger.py record boxes/<nick>/ledger.jsonl --json '{...}'  # append one row (or stdin)
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

    # agentic funnel (v0.4, doctrine/01+02): the quality axis is now an agentic composite.
    agentic_score: Optional[float] = None   # 0..1 weighted composite -> the RANKED quality coordinate
    toolcall_pass: Optional[float] = None   # function/tool-call emission accuracy (BFCL-style, single-turn)
    ifeval_pass: Optional[float] = None     # instruction-following (programmatic constraint checks)
    gsm8k_pass: Optional[float] = None      # grade-school math reasoning (exact-match)

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
# Objectives (doctrine/01): maximize decode_tok_s, maximize prefill_tok_s, and maximize a
# CROSS-MODEL-VALID quality coordinate. (TTFT is carried with prefill; perf/watt and RSS are
# context, not axes.) Degenerate/failed/couldnt_load are excluded from the front.
#
# GATED (doctrine/04 eval-kernel change — see GATE_PROPOSALS.md bug A): the quality axis MUST be
# comparable across tokenizers. Doctrine 01/02 mandate BPB for exactly this reason, so we rank on
# -bpb (lower bpb = better) whenever bpb is present. The raw `quality` scalar (in the optiplex5050
# run it was 100/ppl) is NOT cross-model-comparable and is used only as a single-model fallback
# when bpb is absent.
_PERF = ("decode_tok_s", "prefill_tok_s")

def _quality_coord(r: dict) -> Optional[float]:
    # v0.4: the ranked quality coordinate is the AGENTIC composite (doctrine/01+02). It centers
    # the front on agentically-useful configs (Cynosure targets an agent cluster). bpb/Elo remain
    # RECORDED context and are the fallback ranker only when no agentic score is present (e.g. a
    # legacy record or a config evaluated before the agentic tier ran).
    agentic = r.get("agentic_score")
    if agentic is not None:
        return agentic                   # already "higher = better", 0..1
    bpb = r.get("bpb")
    if bpb is not None:
        return -bpb                      # lower bpb is better -> negate so "higher = better"
    return r.get("quality")              # single-model fallback only (NOT cross-model valid)

def _objs(r: dict) -> tuple:
    return (r.get("decode_tok_s"), r.get("prefill_tok_s"), _quality_coord(r))

def _eligible(r: dict) -> bool:
    if r.get("status") in ("degenerate", "failed", "couldnt_load"):
        return False
    return all(v is not None for v in _objs(r))

def _dominates(a: dict, b: dict) -> bool:
    """a dominates b: >= on every objective and > on at least one (all maximized)."""
    oa, ob = _objs(a), _objs(b)
    ge = all(x >= y for x, y in zip(oa, ob))
    gt = any(x > y for x, y in zip(oa, ob))
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
        key = tuple(round(v, 6) for v in _objs(r))
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
            ruler = f"bpb={r['bpb']}" if r.get("bpb") is not None else f"Q={r.get('quality')}"
            print(f"{r['id']}  dec={r['decode_tok_s']:.1f}  pre={r.get('prefill_tok_s')}  "
                  f"{ruler}  eff={r.get('roofline_efficiency')}  {r['config'].get('model','?')}")
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
    elif cmd == "record":
        # Append ONE row from a JSON object (--json '<obj>' or stdin). Unknown keys are ignored
        # with a stderr warning (so a mis-spelled field is flagged, not silently dropped). Prints
        # the new record id. (bug B: replaces the documented-but-nonexistent "Record API" CLI.)
        raw = ""
        if len(argv) > 3 and argv[3] == "--json":
            raw = argv[4] if len(argv) > 4 else ""
        elif len(argv) > 3 and argv[3].startswith("--json="):
            raw = argv[3][len("--json="):]
        else:
            raw = sys.stdin.read()
        try:
            obj = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            print(f"ledger record: invalid JSON ({e})", file=sys.stderr); return 2
        if not isinstance(obj, dict):
            print("ledger record: expected a JSON object", file=sys.stderr); return 2
        known = set(Record.__dataclass_fields__)
        unknown = sorted(set(obj) - known)
        if unknown:
            print(f"ledger record: ignoring unknown field(s): {', '.join(unknown)}", file=sys.stderr)
        try:
            rec = append(path, Record(**{k: v for k, v in obj.items() if k in known}))
        except (ValueError, TypeError) as e:
            print(f"ledger record: {e}", file=sys.stderr); return 2
        print(rec.id)
    else:
        print(__doc__); return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
