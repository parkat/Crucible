#!/usr/bin/env python3
"""
crucible roofline — the router brain (doctrine/01, 03).

Batch-1 decode is bandwidth-bound:  ceiling_tok_s = effective_BW / active_bytes_per_token
This module estimates active_bytes_per_token from model metadata, computes the ceiling
from the MEASURED bandwidth in hardware.json, derives efficiency = achieved/ceiling, and
routes the next proposal to the memory-bound or kernel-bound action space.

All assumptions are explicit and the thresholds are agent-revisable priors.

Stdlib only.

CLI:
    python3 roofline.py classify <hardware.json> <achieved_decode_tok_s> \
        --total-params 30e9 --active-params 3e9 --quant q4 --kv-bytes-per-token 0
"""
from __future__ import annotations
import argparse, json, math

# Bytes per stored weight by quant family (includes typical block/scale overhead).
# These are PRIORS — refine from measured file sizes / params when you have them.
BYTES_PER_WEIGHT = {
    "fp16": 2.0, "bf16": 2.0, "fp32": 4.0,
    "q8": 1.06, "q8_0": 1.06,
    "q6": 0.82, "q6_k": 0.82,
    "q5": 0.69, "q5_k_m": 0.69,
    "q4": 0.56, "q4_0": 0.56, "q4_k_m": 0.58, "iq4_xs": 0.53, "iq4_nl": 0.53,
    "q3": 0.43, "iq3": 0.43, "q3_k_m": 0.45,
    "q2": 0.33, "iq2": 0.33,
    # ternary / sub-2-bit (bitnet.cpp / T-MAC): ~1.58 bits packed ~4 weights per int8,
    # plus scale overhead. This is the regime that most lifts the decode ceiling on
    # bandwidth-bound, AVX-less hardware. See doctrine/03 mid-2026 landscape.
    "ternary": 0.22, "bitnet": 0.22, "b1.58": 0.22, "i2_s": 0.25, "tl1": 0.22, "tl2": 0.22,
    "1bit": 0.16, "i1": 0.16,
}

def bytes_per_weight(quant: str) -> float:
    q = quant.lower().strip()
    if q in BYTES_PER_WEIGHT:
        return BYTES_PER_WEIGHT[q]
    # fall back to nearest family by leading token
    for k in BYTES_PER_WEIGHT:
        if q.startswith(k):
            return BYTES_PER_WEIGHT[k]
    raise ValueError(f"unknown quant {quant!r}; add it to BYTES_PER_WEIGHT")


def active_bytes_per_token(active_params: float, quant: str,
                           kv_bytes_per_token: float = 0.0) -> float:
    """
    Bytes streamed per decoded token. For MoE pass ACTIVE params (routed experts +
    always-on), NOT total — that distinction is the whole reason MoE wins on bandwidth.
    kv_bytes_per_token accounts for reading the KV cache each step (grows with context;
    pass an estimate at your real context length, or 0 to ignore as a first cut).
    """
    return active_params * bytes_per_weight(quant) + kv_bytes_per_token


def ceiling_tok_s(hardware: dict, active_bytes: float) -> float | None:
    bw = hardware.get("bandwidth_gbps")
    # needs-probe unless bandwidth is a real POSITIVE finite number AND we stream >0 bytes. This
    # guards bw==0 (ZeroDivisionError in classify's eff), bw<0 (negative eff -> misclassified
    # kernel_bound), and a non-numeric sentinel like 'unknown'/'TBD' (ValueError) — all -> None (#37).
    try:
        bwf = float(bw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(bwf) or bwf <= 0 or active_bytes <= 0:
        return None
    return (bwf * 1e9) / active_bytes


def classify(hardware: dict, achieved_decode_tok_s: float,
             active_params: float, quant: str,
             kv_bytes_per_token: float = 0.0,
             efficiency_threshold: float = 0.60) -> dict:
    """Return ceiling, efficiency, the bound class, and the routing decision."""
    ab = active_bytes_per_token(active_params, quant, kv_bytes_per_token)
    ceil = ceiling_tok_s(hardware, ab)
    if ceil is None:
        return {
            "active_bytes_per_token": ab,
            "ceiling_tok_s": None,
            "efficiency": None,
            "bound": "unknown",
            "route": "measure_bandwidth_first",
            "rationale": "no measured bandwidth in hardware.json (needs-probe); "
                         "cannot compute roofline until STREAM runs on target",
        }
    eff = achieved_decode_tok_s / ceil
    if eff < efficiency_threshold:
        route, bound = "kernel_bound", "kernel_bound"
        rationale = (f"efficiency {eff:.2f} < {efficiency_threshold}: leaving perf on the "
                     f"floor -> kernel/threading/compile work pays. See doctrine/03 "
                     f"KERNEL-BOUND action space (int-SIMD vec_dot, thread knee, PGO, NUMA).")
    else:
        route, bound = "memory_bound", "memory_bound"
        rationale = (f"efficiency {eff:.2f} >= {efficiency_threshold}: near the bus ceiling -> "
                     f"only reducing bytes streamed helps. See doctrine/03 MEMORY-BOUND "
                     f"action space (lower-bit quant, smaller-active MoE, speculative decoding).")
    return {
        "active_bytes_per_token": ab,
        "ceiling_tok_s": ceil,
        "efficiency": eff,
        "bound": bound,
        "route": route,
        "rationale": rationale,
    }


def headroom_summary(hardware: dict, achieved: float, active_params: float,
                     quant: str, **kw) -> str:
    c = classify(hardware, achieved, active_params, quant, **kw)
    if c["ceiling_tok_s"] is None:
        return f"[roofline] {c['route']}: {c['rationale']}"
    return (f"[roofline] achieved {achieved:.1f} tok/s of ceiling {c['ceiling_tok_s']:.1f} "
            f"(eff {c['efficiency']:.0%}, {active_params/1e9:.1f}B active @ {quant}) "
            f"-> {c['bound']}\n           {c['rationale']}")


def _main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["classify"])
    p.add_argument("hardware_json")
    p.add_argument("achieved_decode_tok_s", type=float)
    p.add_argument("--total-params", type=float, default=None, help="informational")
    p.add_argument("--active-params", type=float, required=True,
                   help="ACTIVE params streamed/token (==total for dense)")
    p.add_argument("--quant", required=True)
    p.add_argument("--kv-bytes-per-token", type=float, default=0.0)
    p.add_argument("--eff-threshold", type=float, default=0.60)
    a = p.parse_args()
    with open(a.hardware_json) as f:
        hw = json.load(f)
    res = classify(hw, a.achieved_decode_tok_s, a.active_params, a.quant,
                   a.kv_bytes_per_token, a.eff_threshold)
    print(json.dumps(res, indent=2))
    print("\n" + headroom_summary(hw, a.achieved_decode_tok_s, a.active_params, a.quant,
                                   kv_bytes_per_token=a.kv_bytes_per_token,
                                   efficiency_threshold=a.eff_threshold))
    return 0

if __name__ == "__main__":
    raise SystemExit(_main())
