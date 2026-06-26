# crucible

A recursive, self-improving research harness that drives a single throwaway target
box over SSH to discover **maximally fast and intelligent batch-1 LLM inference** on
**AVX-less, memory-bandwidth-limited salvage hardware**.

One Claude Code session (Opus 4.8) on a host machine is the orchestrator, the
proposer, and the judge. It SSHes into one target box, scans it, and then runs an
open-ended search over engines, models, quantizations, kernels, compile flags, and
algorithms — extending its own search code as it goes — to push a Pareto frontier of
speed vs. quality. The mission is to advance local AI for people whose only hardware
is salvage.

## The one idea that organizes everything

**Open hypothesis space, protected evaluation.**

- *Everything worth trying is discovered, never hardcoded.* Which engine, which fork,
  which model, which architecture (dense / MoE / SSM / whatever the agent finds),
  which kernel, which quant, which compile flags, the search algorithm itself, and —
  at the top autonomy tier — the orchestration code. The action space is genuinely
  open; the agent populates it by web research, testing, and random hypotheses.
- *A tiny kernel of method is protected from the search.* Five invariants
  (correctness gating, measurement hygiene, time discipline, resumability, target
  recovery) and the frozen evaluation assets. These are **not optimization avenues**.
  They exist because any optimizer that can rewrite both its engine and its own
  evaluation will, with certainty, find ways to make the number go up that aren't
  real progress. The kernel protects the thing you actually want — real progress —
  from being Goodharted by the search.

Freedom in *what to try*; discipline in *how it's judged*.

## The physics anchor

Batch-1 **decode** is memory-bandwidth-bound: `tok/s ≈ effective_BW / active_bytes_per_token`.
**Prefill** is compute/SIMD-bound. AVX-lessness mostly costs prefill and the
dequant+dot-product hot path; it barely moves the decode ceiling because that ceiling
is the DRAM bus. The two phases are optimized separately, always. This single fact
dictates what is even worth searching — see `doctrine/03_PROPOSER_PLAYBOOK.md`.

## How to start

Open `startup.md`, fill in the SSH details and pick an autonomy tier + campaign
length, then tell the session:

> follow startup.md

The session establishes SSH, installs a key, grants itself sudo on the target, scans
the hardware, scaffolds a per-box folder, and begins the campaign.

To resume a campaign days later, point a fresh session at the box folder and say:

> resume this campaign

It reconstructs its entire state from disk (`MEMORY.md` + `ledger.jsonl` + the clock).
**The session is ephemeral; the campaign lives on disk.**

## Layout

```
crucible/
  README.md              <- you are here
  startup.md             <- entry point: fill-ins + pickers + boot procedure
  doctrine/              <- the agent's constitution (read in full before acting)
    00_PRIME_DIRECTIVE.md    mission, physics, the invariant kernel
    01_RUBRIC.md             the Pareto axes + how quality is scored
    02_EVAL_FUNNEL.md        how "any model" is evaluated cheaply and un-gameably
    03_PROPOSER_PLAYBOOK.md  roofline router, action space, recursion levels
    04_AUTONOMY_TIERS.md     T1-T4 and what each unlocks
    05_SAFETY_RECOVERY.md    ISA guard, watchdog, the independent verifier
    06_OPERATIONS.md         the iteration loop, time discipline, resume protocol
  templates/             <- instantiated once per box
  scaffold/              <- runnable spine (ledger, scan, roofline, verifier, eval, dashboard)
  boxes/<nickname>/      <- created per target box; the live campaign state
```

## Safety posture

Target boxes are disposable salvage. Bricking, SIGILL, and hard hangs are acceptable
outcomes — but a wedged box must auto-recover or checkpoint so the campaign survives
(`doctrine/05_SAFETY_RECOVERY.md`). The host is never the target. Credentials stay
local and gitignored.
