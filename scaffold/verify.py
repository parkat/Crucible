#!/usr/bin/env python3
"""
crucible verify.py — the INDEPENDENT VERIFIER (doctrine/05, defense-in-depth).

Run this OUTSIDE the agent's loop — by hand or from cron. It re-measures the current
blessed config from scratch, with its own simple instrumentation, and flags any
divergence from what the ledger claims. It exists because the agent runs with sudo and
can edit any file, so the invariant kernel is protected by an external check, not by
trust.

This script intentionally does NOT import the agent's ledger/roofline/measurement code
(it reproduces the tiny bits it needs) so that a compromised or self-modified harness
cannot also subvert the auditor. Do not let the agent modify this file — that is an
eval-kernel change, gated at every tier (doctrine/04).

Usage:
    python3 verify.py boxes/<nickname> \
        --ssh "ssh -i ~/.ssh/crucible_<nick> user@host" \
        [--tolerance 0.15]

What it does:
    1. Reads boxes/<nick>/blessed/blessed.json (the agent writes this on promotion).
    2. Over SSH: runs the blessed command's one-token smoke test (SIGILL/garbage check).
    3. Over SSH: measures decode tok/s independently (warmup discarded, median of runs).
    4. Compares measured tok/s to the ledger's claim for that config id.
    5. Appends a record to boxes/<nick>/verify_audit.jsonl and prints PASS/DIVERGENCE.
"""
from __future__ import annotations
import argparse, json, os, re, statistics, subprocess, sys, time

def sh(cmd: str, timeout: int = 600) -> tuple[int, str, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr

def load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try: out.append(json.loads(ln))
                except json.JSONDecodeError: pass
    return out

def ledger_claim(box: str, config_id: str) -> dict | None:
    recs = load_jsonl(os.path.join(box, "ledger.jsonl"))
    for r in reversed(recs):
        if r.get("id") == config_id:
            return r
    return None

def parse_tok_s(text: str) -> float | None:
    """
    Extract a decode tok/s number from engine stderr/stdout. llama.cpp-style emits e.g.
    'eval time = ... ( N tokens per second)'. Adjust the regexes if your engine differs;
    keeping this auditor engine-aware but simple is the point.
    """
    pats = [
        r"([0-9]+\.[0-9]+)\s*tokens? per second",
        r"eval time.*?([0-9]+\.[0-9]+)\s*t/?s",
        r"decode[:\s]+([0-9]+\.[0-9]+)\s*tok",
    ]
    cands = []
    for p in pats:
        cands += [float(m) for m in re.findall(p, text, re.IGNORECASE)]
    return max(cands) if cands else None

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("box", help="path to boxes/<nickname>")
    ap.add_argument("--ssh", required=True, help="full ssh prefix to the target")
    ap.add_argument("--tolerance", type=float, default=0.15,
                    help="allowed fractional shortfall of measured vs claimed tok/s")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--warmup", type=int, default=1)
    a = ap.parse_args()

    blessed_path = os.path.join(a.box, "blessed", "blessed.json")
    if not os.path.exists(blessed_path):
        print(f"[verify] no blessed config at {blessed_path}; nothing to verify.")
        return 0
    with open(blessed_path) as f:
        blessed = json.load(f)

    config_id   = blessed.get("config_id", "")
    smoke_cmd   = blessed["smoke_cmd"]     # generates 1 token on the target
    measure_cmd = blessed["measure_cmd"]   # prints engine timing to stdout/stderr
    claimed     = blessed.get("claimed_decode_tok_s")

    audit = {"epoch": time.time(), "config_id": config_id,
             "claimed_decode_tok_s": claimed}

    # 1. smoke test (correctness floor) ----------------------------------------
    rc, out, err = sh(f'{a.ssh} {json.dumps(smoke_cmd)}', timeout=300)
    blob = (out + err)
    bad = (rc != 0) or ("illegal instruction" in blob.lower()) or ("\ufffd" in out)
    audit["smoke_ok"] = not bad
    if bad:
        audit["verdict"] = "SMOKE_FAIL"
        _write_audit(a.box, audit)
        print(f"[verify] SMOKE_FAIL for {config_id}: rc={rc} (illegal-instruction or garbage). "
              f"The blessed config does not even produce a clean token on the target.")
        return 2

    # 2. independent tok/s measurement -----------------------------------------
    samples = []
    for i in range(a.warmup + a.runs):
        rc, out, err = sh(f'{a.ssh} {json.dumps(measure_cmd)}', timeout=1200)
        ts = parse_tok_s(out + err)
        if i >= a.warmup and ts is not None:
            samples.append(ts)
    if not samples:
        audit["verdict"] = "NO_MEASURE"
        _write_audit(a.box, audit)
        print("[verify] could not parse tok/s from engine output; adjust parse_tok_s regexes.")
        return 2
    measured = statistics.median(samples)
    audit["measured_decode_tok_s"] = measured
    audit["measured_samples"] = samples

    # 3. compare to ledger / blessed claim -------------------------------------
    claim = claimed
    lc = ledger_claim(a.box, config_id)
    if lc and lc.get("decode_tok_s"):
        claim = lc["decode_tok_s"]
    audit["ledger_claim"] = claim

    if claim:
        shortfall = (claim - measured) / claim
        audit["shortfall_frac"] = shortfall
        if shortfall > a.tolerance:
            audit["verdict"] = "DIVERGENCE"
            _write_audit(a.box, audit)
            print(f"[verify] ⚠ DIVERGENCE for {config_id}: ledger/blessed claims "
                  f"{claim:.1f} tok/s, independently measured {measured:.1f} "
                  f"({shortfall:.0%} short, tolerance {a.tolerance:.0%}). "
                  f"Investigate the agent's instrumentation.")
            return 1

    audit["verdict"] = "PASS"
    _write_audit(a.box, audit)
    print(f"[verify] ✓ PASS for {config_id}: claim={claim} measured={measured:.1f} tok/s, "
          f"smoke clean. Blessed config independently confirmed.")
    return 0

def _write_audit(box: str, audit: dict) -> None:
    with open(os.path.join(box, "verify_audit.jsonl"), "a") as f:
        f.write(json.dumps(audit, separators=(",", ":")) + "\n")

if __name__ == "__main__":
    raise SystemExit(main())
