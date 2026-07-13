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
import argparse, json, os, re, shlex, statistics, subprocess, time

def sh(ssh_prefix: str, remote_cmd: str, timeout: int = 600) -> tuple[int, str, str]:
    # argv form, NOT shell=True: the LOCAL shell never parses remote_cmd, which closes
    # the RCE where a malicious blessed.json (written by the sudo agent) smuggles $(...)
    # or backticks onto the more-trusted auditor host. ssh still hands remote_cmd to the
    # REMOTE shell as one argument, which is the intended remote execution.
    # errors="replace" turns genuine non-UTF8 engine garbage into U+FFFD (which the smoke
    # check scans for) instead of raising a UnicodeDecodeError that crashes the auditor.
    argv = shlex.split(ssh_prefix) + [remote_cmd]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or ""), f"TIMEOUT after {timeout}s"
    except OSError as e:
        return 125, "", f"SPAWN_ERROR: {e}"

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
    # llama.cpp prints TWO 'tokens per second' lines: 'prompt eval time' (prefill, high)
    # and 'eval time' (decode, low). The old max() grabbed prefill as the "decode" number,
    # so the shortfall went negative and the DIVERGENCE check could essentially never
    # fire. Anchor on the decode line, exclude the prompt-eval line, and return None
    # (-> NO_MEASURE, a loud non-PASS) rather than falling back to the larger value.
    num = r"([0-9]+\.[0-9]+)"
    decode: list[float] = []
    for ln in text.splitlines():
        low = ln.lower()
        if "prompt eval" in low:
            continue                       # prefill — never usable as decode
        if "eval time" in low or "decode" in low:
            m = (re.search(num + r"\s*tokens? per second", ln, re.IGNORECASE)
                 or re.search(num + r"\s*t/?s", ln, re.IGNORECASE)
                 or re.search(r"decode[:\s]+" + num, ln, re.IGNORECASE))
            if m:
                decode.append(float(m.group(1)))
    return decode[-1] if decode else None

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
    rc, out, err = sh(a.ssh, smoke_cmd, timeout=300)
    blob = (out + err)
    # empty output must NOT pass: an engine that exits 0 with no token is a silent
    # smoke failure. Require positive evidence of a decoded token.
    bad = (rc != 0) or ("illegal instruction" in blob.lower()) \
          or ("\ufffd" in out) or (not out.strip())
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
        rc, out, err = sh(a.ssh, measure_cmd, timeout=1200)
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

    # A missing/zero claim is UNVERIFIABLE, not a pass. The old `if claim:` fell straight
    # through to PASS when neither blessed.json nor the ledger supplied a truthy tok/s —
    # a zero-effort bypass, since the sudo agent writes both files. Fail loud instead.
    if not claim:
        audit["verdict"] = "NO_CLAIM"
        _write_audit(a.box, audit)
        print(f"[verify] ⚠ NO_CLAIM for {config_id}: no truthy decode tok/s in blessed.json "
              f"or ledger to compare against. Unverifiable — not a pass.")
        return 2

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
    # Honesty flag (finding #16): this auditor re-checks smoke + tok/s only. The doctrine
    # /05 correctness (KLD) re-check is NOT yet reproduced here — do not read PASS as a
    # numerical-equivalence re-verification. Full re-check is a tracked 0.5 follow-up.
    audit["correctness_rechecked"] = False
    _write_audit(a.box, audit)
    print(f"[verify] ✓ PASS for {config_id}: claim={claim} measured={measured:.1f} tok/s, "
          f"smoke clean (tok/s + smoke only; correctness re-check not yet implemented).")
    return 0

def _write_audit(box: str, audit: dict) -> None:
    with open(os.path.join(box, "verify_audit.jsonl"), "a") as f:
        f.write(json.dumps(audit, separators=(",", ":")) + "\n")

if __name__ == "__main__":
    raise SystemExit(main())
