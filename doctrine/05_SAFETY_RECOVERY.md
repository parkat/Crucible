# 05 — SAFETY & RECOVERY

Target boxes are disposable salvage; bricking, SIGILL, and hard hangs are *acceptable
outcomes*. The rules here exist so those outcomes never **lose the campaign** and so a
fast-but-wrong result never **wins**.

## ISA-compatibility guard (not AVX-paranoia)

The hardware scan (`scaffold/hardware_scan.sh`) records the target's exact ISA flags.
**Build for what the box actually has** — if it has AVX2, use AVX2; the goal is
correctness-for-this-target, not avoiding AVX on principle. Then, before *any* benchmark:

1. **`objdump -d` the binary** and grep for mnemonics the target's ISA cannot execute
   (e.g. VEX-encoded `vfmadd*`, `vmovaps`, `vpmaddwd` on a box without AVX). A stray
   dependency emitting instructions the build host supports but the target doesn't is
   the **number-one footgun** of this whole exercise — it runs fine on the build box and
   SIGILLs on the target.
2. **One-token smoke test on the target.** Generate a single token over SSH. If it
   SIGILLs or garbages, the build is invalid for this target → status `failed`, logged
   with the offending instruction if identifiable. No benchmark runs.

## Build hygiene

- **Never `-march=native` on the build host.** Build for the target's scanned arch
  (explicit `-m` flags, or cross-compile to its exact microarch).
- Cache build artifacts by **(source SHA + flags + target ISA)** so identical artifacts
  are never recompiled. Wasted compiles waste campaign clock.
- All builds and runs happen **on the disposable target**, never on the host.

## Correctness gating (the hard rule)

Speed counts **only after** equivalence passes. References:

- **Kernel / engine change** → numerical equivalence vs. **stock llama.cpp at the
  matched quant**: same fixed prompts, compare logits / token streams within tolerance
  (`scaffold/correctness.py`).
- **Quantization damage** → KLD vs. the model's **own fp16** logits (`02`).

A kernel that fails equivalence is **not allowed to benchmark**, no matter how fast it
looks. This is invariant-kernel (`00`); it is the thing that stops a garbage-fast kernel
from topping the front.

## Measurement hygiene

- Pin the performance governor; ensure the box is otherwise idle.
- Discard warmup; report **median + variance** over N runs on **fixed inputs**.
- Thermals are a stated non-concern on this fleet, but still sanity-check for mid-run
  throttling and discard a run whose tok/s drifts downward across its window — a
  throttled run silently corrupts the number.
- **Prefill and decode are measured separately** (`00` physics anchor).

## Target recovery (watchdog)

A bad kernel can hang the box. The campaign must survive it.

- Wrap every on-target run in a **timeout** and a **heartbeat**: if a run exceeds a
  generous wall-clock bound or the heartbeat stops, treat the target as wedged.
- **Recover:** attempt SSH reconnect and a clean kill of the run; if the box is hard-hung
  and a power control is available (IPMI / smart PDU — record it in `hardware.json` if
  so), power-cycle it; otherwise write a clear `RECOVERY_NEEDED` flag to `MEMORY.md`,
  checkpoint, and **continue the campaign on other branches** so a manual reboot loses
  nothing.
- Every wedge is a ledger record (`failed`, with the config that caused it) so the
  search learns to avoid that neighborhood.

## The independent verifier (defense-in-depth)

You run with sudo and can edit any file, so protection of the invariant kernel is
layered, and its teeth is `scaffold/verify.py`:

- It is **separate, blessed, and run outside your loop** — by the human or by cron.
- It independently re-runs the smoke test, the correctness check, and a quick tok/s
  measurement on the **current blessed config**, using its own simple instrumentation
  (it does **not** import yours).
- It writes to its own `verify_audit.jsonl` and **flags divergence** between what it
  measures and what the ledger claims, beyond tolerance.

If the ledger says 40 tok/s and the verifier measures 22, that divergence is the signal.
Write your instrumentation honestly; it is being checked by instrumentation you don't
control. Do not modify `verify.py` — that's an eval-kernel change, gated at every tier
(`04`).

## Credential hygiene

The setup wizard takes a plaintext SSH password (interactively) and grants passwordless
sudo on the target. For disposable lab hardware this is fine, with these rules:

- Only the secret-free `startup.template.md` is tracked; any filled-in local copy
  (`startup.md`/`startup.local.md`) and anything containing the password is **gitignored and
  stays local** (the repo-root `.gitignore` covers this). Prefer entering the password only
  when the wizard asks — never write it to a file.
- Use the password **once** — to install the SSH key and write the sudoers grant — then
  rely on the key. **Do not retain the plaintext** in `MEMORY.md`, the ledger, or any
  committed file.
- The sudoers grant lives in `/etc/sudoers.d/crucible` on the target; it dies with the
  box.
