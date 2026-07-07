# 01 — RUBRIC

How "fast and intelligent" is measured. **No scalar collapse.** Maintain a Pareto
archive and have the optimizer chase *hypervolume improvement*, not a weighted sum.
Weighted sums bake in a tradeoff you don't yet know; the front keeps every
non-dominated config so the human can pick the operating point later.

## The four recorded axes

1. **Decode throughput** — batch-1 steady-state tok/s. Warmup discarded. Report
   **median over N runs and the variance.** This is the bandwidth-bound axis.
2. **Prefill throughput + TTFT** — prompt tok/s and time-to-first-token, as a
   **separate** axis. ISA/kernel/compile changes move this without touching decode;
   keeping it separate is the whole point of the physics anchor.
3. **Quality** — a single coordinate synthesized from objective and subjective signals
   (below, and `02_EVAL_FUNNEL.md`). Valid across *different* models, not just across
   configs of one model.
4. **Perf/watt** — tok/s per watt. **Recorded, never gating.** (Matters for off-grid
   deployment, but must never prune an avenue.) Measure via RAPL/IPMI if available,
   else estimate and mark the record `power_source: "estimated"`.

Also recorded alongside, **not** as objectives:

- **Peak RSS** — so you can reason about the fit-in-RAM vs. page-from-disk tradeoff
  without that tradeoff being decided in advance.
- **Roofline efficiency** — the meta-signal below.

## Roofline efficiency — the signal that makes this *research*, not a sweep

For every (model, quant, config) on a calibrated target:

```
ceiling_tok_s = effective_BW_bytes_per_s / active_bytes_per_token
efficiency    = achieved_decode_tok_s / ceiling_tok_s
```

`effective_BW` is the **measured** STREAM-triad bandwidth from the hardware scan, never
the DIMM spec sheet. `active_bytes_per_token` is the bytes actually streamed per token
— for MoE that is *active* params (routed experts + always-on), not total params —
times bytes/weight from the quant, plus per-token KV-cache traffic. See
`scaffold/roofline.py` for the estimator and its documented assumptions.

Efficiency is **hardware-normalized**, so it transports across heterogeneous salvage
boxes in a way raw tok/s never will. It routes the search:

- **efficiency < ~0.6** → you're leaving performance on the floor; kernel, threading,
  and compile work will pay. Route to the **kernel-bound** action space (`03`).
- **efficiency ≥ ~0.6** → you're near the bus ceiling; no kernel cleverness helps. The
  only moves are **reducing bytes streamed**: lower-bit quant, MoE with fewer active
  params, speculative decoding (amortize the stream over K tokens). Route to the
  **memory-bound** action space (`03`).

The 0.6 threshold is an **agent-revisable prior**, not a law. If measurement shows the
knee elsewhere, move it and log why.

## Quality floor — relative to each model's own ceiling

There is **no absolute quality floor** (it would be model-dependent and would prune
avenues — a model that's mediocre at coding but fast and coherent must still compete).
Instead, floor each model against *itself*:

1. At iteration 0 for a newly adopted model, calibrate its **fp16 functional pass-rate**
   and its baseline behavior (the eval funnel's objective tiers).
2. Any config that drops below **~70% of that model's own fp16 pass-rate**, or exceeds
   a **per-model-calibrated KLD threshold**, is tagged **`degenerate`**: logged, but
   excluded from the Pareto front.
3. The 70% figure and the KLD threshold are **agent-revisable priors.** Revise on
   evidence; record the revision.

This is model-agnostic and honors the rule that speciality must not gate model choice.

## The single quality coordinate (cross-model, cheap)

The problem: KLD-vs-own-fp16 measures *damage within one model* but is blind *across*
models (different vocab, different reference; a flawless reproduction of a dumb model
has KLD≈0 and is still dumb). Resolve it like this:

- Each model `M` gets a **base quality** `Q_base(M)` = its Elo (Bradley-Terry) from
  **pairwise** judging at its best (fp16) config, anchored against the current front
  members (`02`).
- A lossy config `c` of the *same* model inherits
  `Q(M,c) = Q_base(M) − penalty(KLD(M,c))`,
  where `penalty` is monotonic increasing with `penalty(0)=0`. KLD precisely measures
  how far the quant/kernel moved the distribution, so it interpolates quality cheaply
  *within* a model without re-judging every config.
- Configs that look like **genuine front contenders** are promoted to their own
  **direct** pairwise judging, replacing the estimate with a measured Elo — which also
  supplies the data to recalibrate `penalty`'s shape (default prior: linear in KLD,
  slope fit from the directly-judged configs).

So: KLD does cheap within-model interpolation; pairwise Elo does expensive cross-model
anchoring; together every config gets one quality coordinate without judging the
thousands. Full mechanics in `02_EVAL_FUNNEL.md`.

## v0.4 — Quality is the AGENTIC composite (supersedes Elo/BPB for RANKING)

Crucible feeds an agent cluster (project Cynosure), so a config's worth is its **agentic
usefulness**, not generic-text quality. From v0.4 the ranked **quality axis is an agentic
composite** (`scaffold/eval/runner.py:agentic_score`) over runnable, industry-standard benchmarks:

- **tool / function calling** (BFCL-style single-turn call emission) — weight **0.40**, the center
- **instruction following** (IFEval-style programmatic constraint checks) — **0.25**
- **reasoning** (GSM8K exact-match) — **0.20**
- **code** (unit-tested, the existing `code.jsonl`) — **0.15**

`agentic_score` (0..1) becomes `ledger._quality_coord`; the Pareto front stays **3-D**
{decode, prefill, agentic-quality}. Each sub-benchmark is also recorded (`toolcall_pass`,
`ifeval_pass`, `gsm8k_pass`, `code_pass`) so a config is comparable to published leaderboard
numbers. **Elo/BPB are retained as recorded context** and remain the fallback ranker only when no
agentic score is present (legacy records). The weights are an **agent-revisable prior** — revise
on evidence and record why. The frozen benchmark item sets are invariant-kernel (gated like every
eval asset, doctrine/04). Heavier multi-step agentic harnesses (tau-bench / SWE-bench) are future
plugins on the same registry, not part of this seed.

## Status semantics (written to every ledger record)

- **`degenerate`** — broke a within-model floor; logged, off-front.
- **`failed`** — didn't build/run (SIGILL, crash). Logged with the failure reason.
- **`couldnt_load`** — no available engine could load this model/format. Logged; a
  loader backport is now a candidate research task, not a dead end.
- **`contender`** — non-dominated on the current front (or close enough to warrant
  direct judging).
- **`blessed`** — promoted (see `04` for what auto-promotes vs. what's gated). Blessed
  configs live in `boxes/<nickname>/blessed/` and are what the independent verifier
  re-checks.

## What you optimize

Hypervolume of the Pareto archive over axes {decode tok/s, prefill tok/s + TTFT,
quality}, with perf/watt and peak RSS carried as context. A proposal is worth pursuing
if it plausibly expands that volume — pushes a frontier outward — not merely if it
improves one number while regressing another already on the front.
