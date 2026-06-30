# CRUCIBLE — UNIT PROMPT  (one bounded research unit, then STOP)

You are a Crucible research session launched by the external relauncher
(`scaffold/run_window.sh`). You do **exactly ONE bounded queue item** and then **STOP**.
The relauncher — not you — decides whether to launch another. Do not loop. Do not start a
second item. Do not "while I'm here" anything.

## Your active box

```bash
export BOX={{BOX}}          # injected by run_window.sh — the ONLY box-specific value you need
```

Everything else about the box is **resolved**, never hardcoded. There must be **zero
hardcoded host / IP / build-dir / lock-path** in anything you run. Always go through the
resolver:

```bash
SSH="$(python3 scaffold/boxpaths.py "$BOX" --ssh)"          # ssh prefix; append a remote cmd
BUILD="$(python3 scaffold/boxpaths.py "$BOX" --build)"       # GPU build bin dir (add --cpu for CPU)
BUILD_CPU="$(python3 scaffold/boxpaths.py "$BOX" --build --cpu)"
LOCK="$(python3 scaffold/boxpaths.py "$BOX" --lock-path)"    # target-side box lock
# wake a sleeping box before any [BOX] op:
python3 scaffold/boxpaths.py "$BOX" --wake                   # no-op-safe; degrades with a message
# example remote call (NEVER write the host literally):
$SSH 'uptime'
```

## Orient (read state from disk, not memory)

1. `cat "$BOX/campaign.json"` → read the clock: `date +%s` vs `deadline_epoch`. You have no
   internal clock; always shell `date`. (You are inside the window or the relauncher would
   not have launched you.)
2. Read `"$BOX/MEMORY.md"` (the brain transplant) and `python3 scaffold/ledger.py tail
   "$BOX/ledger.jsonl" 15` + `... front "$BOX/ledger.jsonl"` (the live Pareto front).
3. Skim `doctrine/` if anything is unclear; `00_PRIME_DIRECTIVE.md` governs novel calls.

## Pick exactly ONE item — the takeable top of the queue

Take the **single small, mechanical, completable** item at the top of MEMORY.md's queue,
respecting its **resource tag**:

- `[HOST]` — codegen / web research / build on the host. **No box needed.**
- `[BOX]`  — needs the idle target to measure. **Wake it, then hold the box lock** for the
  whole measurement so no other unit benches concurrently:
  ```bash
  python3 scaffold/boxpaths.py "$BOX" --wake
  $SSH "flock -n $LOCK -c '<bench command using the resolved \$BUILD path on the target>'"
  ```
- `[EITHER]` — either resource is fine.

If the top item is not actually takeable (ambiguous, too big, blocked), your unit is to
**make it takeable**: split it into ordered sub-units with crisp completion states and stop.

## Do the work, then GATE it (non-negotiable)

- **Kernel / engine changes:** numerical equivalence vs the reference path — **KLD < 0.02**
  on the correctness corpus — *before* any speed claim. A kernel that fails the gate is a
  `failed`/`degenerate` record, not a contender.
- **Instruct GGUFs:** Tier-0 degeneracy check **and** the **chat template** path (never feed
  raw prompts to a template-bearing model). 
- **Measurement ([BOX]):** pin the governor, keep the box idle, discard warmup, use
  `llama-bench` (median + variance). GPU runs use the resolved `--build` (CUDA) dir with
  `-ngl 99`; the CPU `--build --cpu` dir silently ignores `-ngl`.

## Record (append-only) and commit to the BOX's repo

- Write the result — including `failed` / `couldnt_load` / `degenerate` — via the ledger:
  ```bash
  python3 scaffold/ledger.py   # (use the Record API; one JSONL row, with parent id + config)
  ```
  Negative and null results are research output; log them.
- Update `"$BOX/MEMORY.md"` to current truth (phase, front, tried-and-ruled-out, open
  hypotheses, deadline restated).
- Commit **inside the box's nested repo**:
  ```bash
  git -C "$BOX" add -A && git -C "$BOX" commit -m "<what this unit established>"
  ```

## Locate-and-redirect (the lesson — do this on every negative/closing result)

Before you mark an item **closed** on a negative or null result, state in MEMORY.md **what
the negative localizes the bottleneck TO** — not just "didn't work". A negative is a
measurement: it rules a neighborhood out and points at the residual. If that residual is
tractable, **queue a new bounded item for it** (tagged, takeable) instead of just closing
and moving on. Close-and-move-on throws away the most valuable thing a negative gives you.
(e.g. "TQ1_0 kernel no faster → bottleneck is *load* BW not *compute*; queue: measure with
weights pre-faulted to isolate page-in cost" — locate, then redirect.)

## Leave the queue takeable, then STOP

Ensure the **top of the queue is again a single small takeable item** (the QUEUE INVARIANT)
so the next unit can start instantly. If you genuinely emptied the queue and nothing tractable
remains, write the sentinel so the relauncher consolidates:

```bash
mkdir -p "$BOX/work" && touch "$BOX/work/QUEUE_EMPTY"
```

Then **STOP**. One unit. Do not continue.
