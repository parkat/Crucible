# 04 — AUTONOMY TIERS

The pre-session picker sets the **ceiling** of what you may do unattended. The default
is **T4** (target boxes are throwaway salvage). Each tier *includes* everything below
it.

## T1 — Conservative

Config-space search only, over an **existing** engine (the NSGA-II / TPE sampler, L0–L1
in `03`). **No code edits of any kind.** Any escalation to compile-tuning or code
requires the human. Use when you want to watch the loop behave before trusting it.

## T2 — Standard

Adds **compile / flag tuning** and **engine-fork swaps** (e.g. dropping in
ik_llama.cpp). Config-space wins **auto-promote** to `blessed/`. Anything that writes
*new low-level code* is gated (queued for human approval, see below).

## T3 — Aggressive

Adds the orchestrator **writing and modifying kernels** (intrinsics / asm), speculative
decoding setups, and architecture swaps (L2–L3). Kernel changes are
**correctness-gated**: they auto-pass and auto-promote *iff* they pass numerical
equivalence vs. the reference (`05`); they are always logged with the diff for review.
A kernel that fails equivalence cannot benchmark, period.

## T4 — Unleashed (default)

Adds **harness self-modification** (the proposer, the search algorithm, the
orchestration code) and **green-field engine attempts**. Only the invariant kernel
holds. Self-modification is made safe by git, not by restriction (below).

## What "gated" means operationally

- **Auto-promote:** the win is written to `blessed/`, recorded in the ledger as
  `blessed`, and the independent verifier will re-check it. No human needed.
- **Gated:** the proposal is written to `boxes/<nickname>/GATE_QUEUE.md` with the diff,
  the evidence, and the rationale, and surfaced to the human. The campaign **continues
  on other branches** while gated items wait — a gate never stalls the whole loop.

Mapping:

| Action                                   | T1   | T2    | T3    | T4    |
|------------------------------------------|------|-------|-------|-------|
| Config-space search                      | auto | auto  | auto  | auto  |
| Compile / flag tuning                    | gate | auto  | auto  | auto  |
| Engine-fork swap                         | gate | auto  | auto  | auto  |
| Write/modify kernels (correctness-gated) | gate | gate  | auto* | auto* |
| Speculative decoding / arch swap         | gate | gate  | auto  | auto  |
| Harness / proposer self-modification     | gate | gate  | gate  | auto† |
| **Eval / correctness / measurement kernel change** | **gate** | **gate** | **gate** | **gate** |

\* auto **only if** numerical equivalence passes (`05`); else it cannot run.
† auto, but **git-committed before and after** so it can be rolled back on resume.

## The one gate that survives T4

Changes to the **evaluation / correctness / measurement / resumability / recovery
kernel** and the **frozen eval assets** are gated at **every** tier, including T4. This
is the single human checkpoint that survives the deep end. Rationale in `00`: you do not
let the thing being measured silently re-cut its own ruler. You *may* propose such
changes (a better corpus, an added objective item set, a sharper rubric, a smarter
watchdog) — write them to `GATE_QUEUE.md` and wait.

This is **not** a contradiction of "nothing hardcoded": improving the eval is a real
research avenue, so it's allowed — it just requires a human to see the diff first.

## git is the undo for meta-recursion

Under T4 the box folder is a git repo. Before and after **any** self-modification of the
harness/proposer/orchestration code, commit (clear message: what changed and why). If a
self-edit breaks the loop, a resumed session detects the breakage and **rolls back to
the last good commit** rather than bricking the campaign. Treat `git` as the safety net
that makes self-modification survivable; never self-modify without a clean commit on
both sides.
