# 03 — PROPOSER PLAYBOOK

How to actually search. The roofline (`01`) is the brain: it classifies each result as
**memory-bound** or **kernel-bound** and routes the next proposal. The recursion is
what makes this a research loop rather than a parameter sweep — the search *substrate*
gets rewritten and the inner loop re-runs on top of it.

## The recursion levels (map to the four-tier intensity idea)

- **L0 — repeated measurement.** Re-run one config N times for median + variance. The
  cheapest level; it exists because surplus hardware is noisy.
- **L1 — config-space optimization.** A multi-objective sampler over the categorical /
  continuous knobs (quant type, KV bits, thread count, batch internals, NUMA binding,
  draft model, speculation depth, compile flags). Automatic and cheap — use Optuna
  NSGA-II or TPE; **zero orchestrator-reasoning tokens** spent here. Grind the easy
  axes.
- **L2 — kernel / engine modification.** When L1's front **stalls for K iterations**
  *or* the **roofline says kernel-bound**, escalate to writing actual engine code: a
  rewritten integer-SIMD GEMM, a prefetch on the weight stream, a different quant block
  layout, a fork swap. **A winning patch becomes the new baseline engine, and L1
  restarts on top of it.** This is where "heavily modified inference engines" lives.
- **L3 — family switch (meta).** When even kernel work saturates the current
  model/quant/algorithm *family*, switch families: dense→MoE, adopt speculative
  decoding, swap the quant scheme, change the engine fork, try an SSM architecture.
  Re-baseline everything beneath.

Escalation is triggered by **evidence** (front stall, roofline class), not by whim or
boredom. Log the trigger when you escalate.

## The tiered proposer

- **Config-space (automatic, cheap):** the NSGA-II / TPE sampler. Reserve no
  intelligence for this; let it run.
- **Code-space (orchestrator-driven, expensive):** when escalation fires, *you* reason
  about and write the engine modification. This is where orchestrator tokens belong.

## The roofline router (the brain)

Given the calibrated target (`hardware.json`, measured BW) and the ledger, classify the
latest result and route:

```
if efficiency < 0.6:   route -> KERNEL-BOUND action space   (better code/threads/compile)
else:                  route -> MEMORY-BOUND action space   (reduce bytes streamed)
```

(0.6 is an agent-revisable prior — `01`.) Routing is not a straitjacket: if research
surfaces a cross-cutting idea, pursue it and log the rationale.

## Action space — MEMORY-BOUND (the common case for decode)

Every move reduces **bytes streamed per token**:

- **MoE-first model selection.** Active params are the whole game. A 30B-A3B streams
  ~3B, not 30B. Bias the family search hard toward small-active MoE.
- **Speculative decoding.** A tiny draft (~0.5B) proposes K tokens; the target verifies
  them in one forward pass, so you stream the big weights once per *K* tokens. On
  bandwidth-bound CPU this is frequently the single biggest multiplier. **Log
  acceptance rate** — `acceptance × K` is the real speedup, not K. Sweep draft-model
  pairings and depth; EAGLE-style self-speculation is in scope.
- **Lower-bit quant sweep**, with KLD as the guardrail: Q4_0 (simple, SSE-friendly dot
  product) vs Q4_K_M vs IQ4_XS vs IQ3 vs Q8_0 — plot KLD-vs-tok/s per target.
- **KV-cache quant** (Q8 / Q4 KV), validated for quality at your *real* context lengths.

## Action space — KERNEL-BOUND (prefill, or low efficiency)

- **ik_llama.cpp as the baseline fork to beat.** Ikawrakow's CPU kernels and IQK quants
  are specifically strong on this hardware class. Beat *that* before writing your own.
- **Integer-SIMD kernel work — the key insight: you don't need AVX *float*, you need
  good integer SIMD.** SSE already has `_mm_maddubs_epi16` and `_mm_madd_epi16`, which
  *are* the int8-quant dot product. Confirm the `vec_dot` hot path uses them on the
  target's scanned ISA, then **software-pipeline and prefetch the weight stream.** This
  is the prime orchestrator-modification target on AVX-less boxes.
- **Thread-count knee.** The optimum is usually *below* physical core count once the bus
  saturates; extra threads then only add contention. Finding the knee is a near-free
  win and a great L1 sanity check.
- **Compile:** explicit ISA for the scanned target (e.g. `-msse4.2 -mno-avx -mno-avx2`,
  or cross-compile to the box's exact arch) + `-mtune` for the microarch, then PGO on
  the hot kernels. **Never `-march=native` on the build host.**
- **NUMA:** on multi-socket boxes bind weights socket-local; bandwidth is per-socket and
  crossing QPI/UPI murders the roofline.

## The menus are priors, not fences

Both lists are **starting points**. The mission (`00`) requires you to *extend* them.
On a fresh box, and whenever a front stalls, run a **web-research phase**:

- search current results on AVX-less / bandwidth-limited CPU inference, backporting
  techniques, alternative architectures (SSM/RWKV/Mamba and successors), relevant forks,
  and recent papers (use the actual current year in queries);
- treat the mid-2026 landscape as live and changing — what's SOTA shifts fast;
- **write findings into `MEMORY.md`** so a 3-day or open-ended run *compounds knowledge*
  across sessions instead of re-discovering the same things.

Research is a first-class phase, logged like any experiment — not a one-off at the start.

## Mid-2026 landscape snapshot (researched 2026-06-25 — REFRESH THIS)

These are *current priors as of the date above*, seeded from a research pass. They are
not fences and they go stale fast — re-run the research phase and update this section
(it is doctrine, so surface the diff per `04`). Sources are named so you can re-verify;
do not trust a number here without re-measuring it on the actual target.

**Ternary / sub-2-bit via table lookup — the highest-leverage new memory-bound move on
AVX-less hardware.** Microsoft's `bitnet.cpp` (MIT, built on llama.cpp + the T-MAC
lookup-table method; Jan-2026 optimization pass added tiling + parallel kernels) runs
1-bit/1.58-bit ternary {−1,0,+1} weights with reported x86 CPU speedups of ~2.4–6.2×
and large energy cuts. The mechanism is the point for *this* fleet: weights pack ~4
ternary values per int8 and the hot path is **table lookup, not wide-SIMD multiply**, so
it sidesteps exactly the missing AVX float units — and at ~0.2 bytes/weight it slashes
`active_bytes_per_token`, lifting the decode ceiling directly. Two flavors: natively-
trained ternary models (e.g. BitNet b1.58 2B), and **post-training-quantized** third-
party models to ternary (Falcon3, Llama-3-8B variants) — PTQ-to-ternary is lossy, so
the per-model KLD floor (`01`) matters more here than anywhere. Related research to mine
if this pays: **T-MAC** (EuroSys 2025, the lookup-table foundation) and **T-SAR** (2026,
CPU-only ternary via in-place SIMD ALU reorganization). The "100B on a laptop" headline
is aspirational/PoC; treat the *kernels* as real and the *model quality* as something you
must verify. **Add ternary to the quant sweep and to `roofline.py`'s byte table.**

**ik_llama.cpp is richer than the base menu implies.** Beyond IQK quants: **R4 row-
interleaved quant packing** (e.g. `IQ4_XS_R4`; engaged via `-rtr` repack or pre-repacked
weights) is the main CPU throughput trick — but its biggest wins assume `HAVE_FANCY_SIMD`
(AVX2/Zen4); **on a truly AVX-less box, check the build log to see which kernels actually
engage** and don't assume the headline numbers transfer. Also: **SER (Smart Expert
Reduction, `-ser`)** runs fewer experts than the model default for a speed/quality trade
(a memory-bound lever — fewer active bytes); **`--k-cache-hadamard`** recovers quality
under heavy KV quant (below Q6); `-ot` tensor overrides and `-fa` CPU FlashAttention.
Use the built-in `llama-sweep-bench` for repeatable per-flag measurement.

**Speculative decoding has moved on from plain draft models.** **EAGLE-3** (NeurIPS
2025) is now the production standard in the GPU serving stacks, with reported acceptance
~0.80–0.88 on Llama/Qwen (vs ~0.72–0.78 for EAGLE-2) and the largest gains on **coding**;
**EAGLE-3.1** (vLLM, May 2026) fixes "attention drift" at deeper draft depth. **SLEM**
(ICML 2025) and universal-drafting break the **shared-vocabulary requirement** — any
draft can now accelerate any target using text as the bridge, which widens the draft/
target pairing search. **CPU caveat:** these numbers come from GPU stacks where parallel
verification compute is ~free; on an AVX-less CPU the K-token verify step is SIMD-bound
(the thing you're short on), so the net win is real but smaller than GPU-reported — **you
must measure acceptance×K on the actual box**, never assume it. Confirm what your engine
(llama.cpp `--draft`/`-md`, ik_llama, or an EAGLE-capable fork) actually supports.

**Current small-active open MoE candidates (the bandwidth-friendly sweet spot).** As of
the date above, the 3B-active class is populated: **Qwen3-30B-A3B** (the existing
baseline), **Poolside Laguna XS.2** (33B total / 3B active, 256 experts, Apache 2.0,
strong agentic-coding), plus **Qwen3.6**, **GLM-4.x-flash**, **Gemma 4 E-variants**
("effective"-param edge MoE, Apr 2026), and dense **Devstral-Small 24B** as a coding-
specialist reference. Bias the family search toward open, small-active MoE; verify
loadability per engine (`couldnt_load` is a logged outcome, not a dead end). Huge MoEs
the user tracks (e.g. GLM-5.2-class 744B, Ling/Ring trillion-param) are out of scope for
salvage RAM but worth a loadability note.

**Orchestration aside (not an inference technique).** Sakana **Fugu** (TRINITY +
Conductor, ICLR 2026) frames recursive self-orchestration — a model reading its own
output and deciding whether to re-plan — as a tunable test-time compute axis with no
retraining. That's conceptual validation of *this harness's* own recursion structure
(`L0–L3`), not a kernel/quant move; note it, don't chase it here.

## Where to start a fresh campaign

Open on **ik_llama.cpp + a small-active MoE + speculative decoding**, because those
three are where the bandwidth wall actually moves. Let kernel work be an L2 escalation
once the roofline says you've earned it. Then let research and the front decide
everything after.

## Each iteration updates the search state

After every experiment: write the ledger record, update the Pareto archive, update the
sampler's surrogate, and decide continue / escalate / converge. The decision is
evidence-driven (front movement, roofline class, clock) — see `06_OPERATIONS.md` for
the exact loop.
