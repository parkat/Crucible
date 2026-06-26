#!/usr/bin/env python3
"""
crucible correctness — the equivalence gate (doctrine/05, invariant kernel).

A kernel/engine change may NOT benchmark until it passes numerical equivalence vs a
reference. References (doctrine/05):
  - kernel/engine change  -> stock llama.cpp at the matched quant
  - quantization damage    -> the model's OWN fp16 logits (KLD)

This module is the metric layer. It takes already-collected outputs (token streams
and/or logit rows on fixed prompts) and decides pass/fail against tolerances. It does
NOT run the engines itself — the orchestrator collects outputs over SSH and passes them
here, so this stays simple, dependency-light, and hard to accidentally couple to the
thing it's checking.

Stdlib + (optional) math only.
"""
from __future__ import annotations
import json, math, sys
from typing import Sequence

# ---- token-stream equivalence (greedy/deterministic decoding) ----------------
def token_match_rate(candidate: Sequence[int], reference: Sequence[int]) -> float:
    """Fraction of positions where candidate token == reference token (greedy decode)."""
    if not reference:
        return 0.0
    n = min(len(candidate), len(reference))
    if n == 0:
        return 0.0
    same = sum(1 for i in range(n) if candidate[i] == reference[i])
    # penalize length mismatch by scoring over the longer length
    return same / max(len(candidate), len(reference))


# ---- logit-level equivalence -------------------------------------------------
def max_abs_logit_diff(cand_rows: Sequence[Sequence[float]],
                       ref_rows: Sequence[Sequence[float]]) -> float:
    """Worst-case absolute logit difference across all compared positions/vocab."""
    worst = 0.0
    for cr, rr in zip(cand_rows, ref_rows):
        for x, y in zip(cr, rr):
            d = abs(x - y)
            if d > worst:
                worst = d
    return worst


def _softmax(row: Sequence[float]) -> list[float]:
    m = max(row)
    ex = [math.exp(x - m) for x in row]
    s = sum(ex)
    return [e / s for e in ex]


def kld_rows(p_rows: Sequence[Sequence[float]],
             q_rows: Sequence[Sequence[float]]) -> float:
    """
    Mean KL(P||Q) over rows, where P is the reference (e.g. fp16) and Q the candidate.
    Inputs are LOGITS; softmaxed here. This is the within-model damage signal (doctrine
    /02) — lower is closer to the reference distribution.
    """
    tot, n = 0.0, 0
    for pr, qr in zip(p_rows, q_rows):
        p = _softmax(pr)
        q = _softmax(qr)
        s = 0.0
        for pi, qi in zip(p, q):
            if pi > 0.0:
                s += pi * math.log(pi / max(qi, 1e-12))
        tot += s
        n += 1
    return tot / n if n else float("inf")


# ---- the gate ----------------------------------------------------------------
def equivalence_gate(candidate_tokens: Sequence[int] | None = None,
                     reference_tokens: Sequence[int] | None = None,
                     candidate_logits: Sequence[Sequence[float]] | None = None,
                     reference_logits: Sequence[Sequence[float]] | None = None,
                     *,
                     min_token_match: float = 0.98,
                     max_logit_diff: float = 0.5,
                     max_kld: float = 0.02) -> dict:
    """
    Decide pass/fail for a kernel/engine change. Pass requires (whichever signals are
    provided) to be within tolerance. Tolerances are PRIORS — tighten for kernels that
    claim bit-exactness, loosen only with logged justification.

    Returns {pass, signals:{...}, reason}.
    """
    signals: dict[str, float] = {}
    ok = True
    reasons = []

    if candidate_tokens is not None and reference_tokens is not None:
        tm = token_match_rate(candidate_tokens, reference_tokens)
        signals["token_match"] = tm
        if tm < min_token_match:
            ok = False; reasons.append(f"token_match {tm:.3f} < {min_token_match}")

    if candidate_logits is not None and reference_logits is not None:
        mld = max_abs_logit_diff(candidate_logits, reference_logits)
        kl = kld_rows(reference_logits, candidate_logits)
        signals["max_logit_diff"] = mld
        signals["kld"] = kl
        if mld > max_logit_diff:
            ok = False; reasons.append(f"max_logit_diff {mld:.3f} > {max_logit_diff}")
        if kl > max_kld:
            ok = False; reasons.append(f"kld {kl:.4f} > {max_kld}")

    if not signals:
        return {"pass": False, "signals": {}, "reason": "no comparison data provided"}
    return {"pass": ok, "signals": signals,
            "reason": "within tolerance" if ok else "; ".join(reasons)}


# ---- CLI: feed it a JSON blob of collected outputs ---------------------------
def _main() -> int:
    """
    Usage: python3 correctness.py <outputs.json>
    where outputs.json = {
      "candidate_tokens": [...], "reference_tokens": [...],         # optional
      "candidate_logits": [[...],...], "reference_logits": [[...],...] # optional
      "min_token_match": 0.98, "max_logit_diff": 0.5, "max_kld": 0.02 # optional overrides
    }
    Exit code 0 = pass, 1 = fail (so it can gate a shell pipeline).
    """
    if len(sys.argv) < 2:
        print(_main.__doc__); return 2
    with open(sys.argv[1]) as f:
        d = json.load(f)
    res = equivalence_gate(
        d.get("candidate_tokens"), d.get("reference_tokens"),
        d.get("candidate_logits"), d.get("reference_logits"),
        min_token_match=d.get("min_token_match", 0.98),
        max_logit_diff=d.get("max_logit_diff", 0.5),
        max_kld=d.get("max_kld", 0.02),
    )
    print(json.dumps(res, indent=2))
    return 0 if res["pass"] else 1

if __name__ == "__main__":
    raise SystemExit(_main())
