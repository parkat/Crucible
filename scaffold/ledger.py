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
import json, math, os, sys, time, uuid
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
        for fname in _NUMERIC_FIELDS:
            v = getattr(self, fname)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"field {fname!r} must be a number or null, "
                                 f"got {type(v).__name__} {v!r}")
            if not math.isfinite(v):
                raise ValueError(f"field {fname!r} must be finite, got {v!r}")


# Numeric fields feeding comparisons/objectives. A string or non-finite value here would
# TypeError every downstream front/stats/dashboard read (finding #7); since the ledger is
# append-only, one quoted number would brick reads for the rest of the campaign. Reject at
# write time instead.
_NUMERIC_FIELDS = (
    "epoch", "decode_tok_s", "decode_tok_s_var", "prefill_tok_s", "ttft_s", "quality",
    "perf_per_watt", "peak_rss_bytes", "roofline_ceiling_tok_s", "roofline_efficiency",
    "accept_rate", "kld_vs_fp16", "bpb", "math_pass", "code_pass", "agentic_score",
    "toolcall_pass", "ifeval_pass", "gsm8k_pass",
)


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
def _quality_coord(r: dict):
    """Return (source, value) with 'higher = better', or None. The source TAG matters:
    agentic_score (0..1), -bpb, and the legacy `quality` scalar (~1000 in old runs) are NOT
    on a common scale, so _dominates only ever compares two records that share a source
    (finding #8 — otherwise a legacy quality=1078 "dominates" an agentic=0.8 on the quality
    axis and silently evicts every agentic/bpb contender from the front)."""
    a = r.get("agentic_score")
    if a is not None:
        return ("agentic", a)            # already higher = better, 0..1
    b = r.get("bpb")
    if b is not None:
        return ("bpb", -b)               # lower bpb is better -> negate
    q = r.get("quality")
    if q is not None:
        return ("quality", q)            # single-model fallback only (NOT cross-model valid)
    return None

def _perf_coords(r: dict) -> tuple:
    return (r.get("decode_tok_s"), r.get("prefill_tok_s"))

def _num_ok(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

def _eligible(r: dict) -> bool:
    if r.get("status") in ("degenerate", "failed", "couldnt_load"):
        return False
    d, p = _perf_coords(r)
    q = _quality_coord(r)
    # v0.5 policy (operator 2026-07-10): rank the front ONLY on agentic_score. Legacy records whose
    # only quality signal is bpb or the pre-agentic `quality` scalar are DEMOTED to context —
    # recorded in the ledger, but not front-eligible (so they can't sit on the front un-comparable to
    # the agentic contenders). bpb/quality remain on the record for reference.
    # (A non-numeric/non-finite objective is also excluded, so one corrupt line can't TypeError the
    # whole front read — finding #7 read-side guard.)
    return (_num_ok(d) and _num_ok(p)
            and q is not None and q[0] == "agentic" and _num_ok(q[1]))

def _dominates(a: dict, b: dict) -> bool:
    """a dominates b: >= on every objective and > on at least one (all maximized). The
    quality axis is comparable only when both records share a quality source; across sources
    it is incomparable, so neither dominates and both stay on the front."""
    da, pa = _perf_coords(a); db, pb = _perf_coords(b)
    qa, qb = _quality_coord(a), _quality_coord(b)
    if qa is None or qb is None or qa[0] != qb[0]:
        return False
    oa = (da, pa, qa[1]); ob = (db, pb, qb[1])
    ge = all(x >= y for x, y in zip(oa, ob))
    gt = any(x > y for x, y in zip(oa, ob))
    return ge and gt

def _config_identity(r: dict) -> tuple:
    c = r.get("config") or {}
    return (c.get("model"), c.get("quant"))

def pareto_front(records: list[dict]) -> list[dict]:
    pts = [r for r in records if _eligible(r)]
    # SUPERSEDE (v0.5): for the same config identity (model, quant), keep only the most recent
    # measurement. A fresh re-eval retires an older/stale-hardware row of the same config, so a
    # phantom point (e.g. Qwen0.5B on dead GTX-1060 numbers) can't linger beside its honest re-bench.
    latest: dict[tuple, dict] = {}
    for r in pts:
        k = _config_identity(r)
        if k not in latest or (r.get("epoch") or 0) > (latest[k].get("epoch") or 0):
            latest[k] = r
    pts = list(latest.values())
    front = [r for r in pts if not any(_dominates(o, r) for o in pts if o is not r)]
    # de-dup configs that tie on all objectives (within a source), keep the most recent
    best: dict[tuple, dict] = {}
    for r in front:
        d, p = _perf_coords(r); q = _quality_coord(r)
        key = (round(d, 6), round(p, 6), q[0], round(q[1], 6))
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
        try:
            n = max(0, int(argv[3])) if len(argv) > 3 else 10   # clamp: n=0/neg no longer dumps the whole ledger (#58)
        except ValueError:
            n = 10
        for r in (recs[-n:] if n else []):
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
