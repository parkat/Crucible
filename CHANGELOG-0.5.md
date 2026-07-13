# Crucible v0.5 (2026-07-13)

**Hardening + honest-front release.** A multi-agent audit of the whole backend surfaced 61 verified
bugs/bottlenecks/inefficiencies; **all 19 HIGH-severity findings + every pre-release loop-reliability
blocker are fixed, each tested against its own exploit.** The Pareto front was then rebuilt honestly
via an on-box re-eval on consistent hardware + the synced grader. **Cut 2026-07-13** — `release/0.5`
merged to `main`, tagged `v0.5`.

## How it started
A `Workflow`-orchestrated **backend audit** (`backend-audit`, 136 agents): per-file + cross-cutting
analyzers found candidate issues, an adversarial verifier tried to *refute* each (killed 36 false
positives), and a synthesis pass ranked the survivors — 61 findings, ~42 fixed here.

## On `release/0.5`

### Batch 1 — eval/verify funnel — `1c9f4ef`
The cluster where a numerically-wrong-but-fast config could be blessed onto the front.
- `correctness.py`: NaN logits (#11), truncated candidates (#12), and empty rows (#53) now **fail the
  KLD gate closed** instead of passing.
- `verify.py`: reads *decode* not prefill (#17); a missing claim is `NO_CLAIM` not a silent PASS (#15);
  **RCE closed** (#18 — no more `shell=True` on `json.dumps`); empty smoke output fails (#22);
  timeout-safe (#52).
- `eval/runner.py` grader: the `exit 0` cheat is blocked via a per-item random success **sentinel**
  (#10); bounded stdout (#20); orphan reaping via `killpg` (#21); `stdin=DEVNULL` (#61).

### Batch 2 — ledger / Pareto front — `c751535`
- `#7` one hand-typed quoted number no longer bricks every front read (write- **and** read-side).
- `#8` the quality axis is source-tagged; incomparable scales (agentic vs bpb vs legacy `quality`) can
  no longer cross-evict.

### Batch 3 — loop-driver — `618d4bf`
run_window.sh + window.py "window silently wastes itself" bugs: backoff + hard failure cap vs infinite
spin (#1); a transient refill crash no longer reads as "campaign exhausted" (#2); stale `STOP` can't
ambush a fresh window (#3); `add-hours` on an open-ended window no longer inverts winddown into the
past (#4); interruptible session-limit sleep (#24); two-driver guard (#25); torn-config hard-fail (#27).

### Batch 4 — eval-gate ordering — `0616eb7`
Tier-0 degeneracy now **short-circuits** the expensive battery instead of scoring it after the fact
(#5); the timeout/retry amplification that turned a hang into ~4×900s holding the box lock is gone
(#6, #44); the escalation length-fallback no longer overrides the answer probe (#43).

### Batch 5 — concurrency / locks — `124d1c0`
Atomic **locked** writes for `MEMORY.md` / `STEERING.md` (flock + tmp+os.replace) so the queue and
inbox can't be torn or silently clobbered (#9, #28, #30); note sanitization (#29); `--delete 0` fix
(#49); and the remote jobspec is **base64-encoded** so any quoting survives `flock -c` (#13).

### Batch 6 — parsing / config — `60a2ed7`
Preflight **blocks** on missing eval seeds instead of "Safe to proceed" then crashing (#14); the hw
probe emits **valid JSON** (#19); `pick_model` parses the real queue header — **`[BOX]` benches now
actually route to Opus** instead of silently running on Sonnet (#34); base-10 hours (#48); roofline
bandwidth guards (#37); ledger/queue CLI foot-guns (#57, #58); agentic assets tracked (#50).

### Honest front — agentic-only ranking + supersede — `5e858b3`, `460a496`
- The front ranks **only on `agentic_score`**; legacy bpb/`quality`-only records are demoted to context.
- The most-recent measurement per `(model, quant)` **supersedes** older/stale-hardware rows.
- **On-box re-eval** (750 Ti + the synced Proposal-E grader) rebuilt the front. The old on-box grader
  had deflated the 3B scores ~0.15, collapsing the front to a single stale point. Corrected:
  - **Qwen2.5-3B-Instruct Q4_K_M** — dec 13.0 / pre 179 / **agentic 0.928** (clean quality leader)
  - **Llama-3.2-3B-Instruct Q4_K_M** — dec 13.5 / pre 174 / agentic 0.898 *(tier0_degenerate flag)*
  - **Qwen2.5-0.5B-Instruct Q4_K_M** — dec 66.0 / pre 1199 / agentic 0.834 *(speed corner)*

### Pre-release hardening — `93c2a0f`
"Can it run unattended for days without wedging?" — `boxpaths` rejects shell-fragile `~`/`$` remote
paths (#40) and its SSH prefix fails fast on a down box (`BatchMode`/`ConnectTimeout`, #32); a **per-model
eval wall-clock cap** `CRUCIBLE_EVAL_MAX_S` (a reasoning model held the box 2.5h+); transcript pruning
(#47); driver signal trap so death doesn't orphan the unit + its box-lock (#46); atomic `campaign.json`
(#45); and `remote_run` poll now has a liveness check + 6h ceiling (#31).

## Deferred (post-0.5 polish — none block the loop)
hardware_scan.sh cosmetics (#35/#36/#59/#60), roofline refinements (#54/#55), SSH ControlMaster (#51),
INJECT-id (#56, needs a coordinated dashboard change), marker-integrity (#39), mid-file ledger
corruption (#42), blessed-promotion path (#41, a feature), crashed-unit idempotency (#26), PID-reuse
(#23, partially guarded).
