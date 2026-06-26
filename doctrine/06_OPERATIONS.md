# 06 — OPERATIONS

The actual loop mechanics, the clock, and how a fresh session resumes a campaign it has
no memory of.

## Time discipline (you have no clock)

At campaign start, `campaign.json` is written with:

```json
{
  "nickname": "...",
  "autonomy_tier": "T4",
  "duration_label": "3day",
  "start_epoch": 1750000000,
  "deadline_epoch": 1750259200,     // null for open-ended
  "state": "running"
}
```

**Every iteration**, shell `date +%s` and compare to `deadline_epoch`. Never estimate
elapsed time from your sense of how long things took — that sense is unreliable and
resets on session death. The ledger timestamps every record, so a resumed session
derives true elapsed time from data.

Duration labels and their meaning:

- `1hr`, `6hr`, `1day`, `3day` → set `deadline_epoch = start_epoch + label`.
- `open` → `deadline_epoch = null`: **continuous improvement, no termination criteria.**
  Run until the human stops you.

## Wind-down protocol (set-length campaigns)

As `now` approaches `deadline_epoch` (e.g. within the last ~10%):

1. Stop launching expensive new branches (no new L2/L3 escalations, no new big-model
   downloads).
2. Finish in-flight measurements and let the cheap L1 sampler keep refining.
3. Write the **final report** to `reports/` (Pareto front, blessed configs, roofline
   findings, what was ruled out, open hypotheses for next time) and set
   `campaign.json.state = "completed"`.

Open-ended campaigns never wind down; they periodically checkpoint a rolling report.

## The iteration loop

```
loop:
  now = `date +%s`
  if deadline_epoch and now >= deadline_epoch: run wind-down; stop
  if deadline_epoch and now >= deadline_epoch - winddown_margin: enter wind-down mode

  # 1. DECIDE (evidence-driven; 03)
  consult ledger + live Pareto front + roofline class of recent results
  if front stalled K iters OR roofline says kernel-bound (and tier allows): escalate L2/L3
  else: draw next config from the NSGA-II/TPE sampler (L1)
  periodically: run a web-research phase; write findings to MEMORY.md

  # 2. BUILD (05)
  build for target's scanned ISA; cache by (SHA+flags+ISA)
  objdump-grep for un-runnable mnemonics  -> fail fast if found

  # 3. SMOKE + CORRECTNESS (05) — invariant kernel
  one-token smoke on target -> fail fast on SIGILL/garbage
  if engine/kernel changed: numerical equivalence vs reference -> must pass to proceed

  # 4. MEASURE (05) — invariant kernel
  governor pinned, box idle, warmup discarded
  prefill tok/s + TTFT  AND  decode tok/s (median + variance, separately)
  perf/watt + peak RSS recorded

  # 5. EVALUATE (02)
  Tier 0 degeneracy (+ KLD-vs-own-fp16 for config changes)
  Tier 1 BPB + auto-gradable math/code
  Tier 2 pairwise judge IFF this is a front contender
  synthesize quality coordinate (01)

  # 6. RECORD + LEARN
  append ledger record (status, all axes, roofline efficiency, git SHA, parent id)
  update Pareto archive + sampler surrogate
  promote per tier (04): auto -> blessed/ ; gated -> GATE_QUEUE.md
  rewrite MEMORY.md to current truth
  (T4) git commit if anything structural changed
```

The **parent id** on each record encodes the recursion DAG — it's what lets you diff
lineages and reconstruct *why* a config exists.

## Resume protocol (a fresh session, no memory)

On "resume this campaign":

1. Read all of `doctrine/` (re-load the constitution).
2. Read `boxes/<nickname>/MEMORY.md` (the brain transplant).
3. Tail `ledger.jsonl`; reconstruct the live Pareto front from it.
4. `date +%s`; compare to `deadline_epoch`; determine remaining time and whether to
   enter wind-down.
5. Check `git log` / working tree for an aborted self-modification; if the loop is
   broken, roll back to the last good commit (`04`).
6. Check `GATE_QUEUE.md` for items the human may have approved/rejected while you were
   gone.
7. Continue the loop.

**Never** reconstruct state from your sense of "what I was doing." Reconstruct from
disk. The session is ephemeral; the campaign lives on disk.

## The MEMORY.md contract

`MEMORY.md` must, at all times, let a memory-less successor continue. Keep current:

- **Phase / status:** what you're doing right now, current baseline engine + config.
- **Live Pareto front:** the non-dominated configs and their axis values (summary).
- **Tried & ruled out:** what didn't work and *why* (so it isn't re-tried).
- **Research findings:** accumulated web-research knowledge — the compounding asset of a
  long run. Cite sources.
- **Open hypotheses:** the queue of things to try next, roughly prioritized.
- **Deadline:** the label and `deadline_epoch`, restated for fast human reading.
- **Flags:** any `RECOVERY_NEEDED`, gated items awaiting the human, or anomalies.

Treat `MEMORY.md` as a lab notebook you are writing *to a future stranger who is you*.

## Logging discipline

Every experiment is a ledger record — including `failed`, `couldnt_load`, and
`degenerate`. Negative and null results are research output and they steer the search
away from dead neighborhoods. A campaign with no logged failures is a campaign that
isn't exploring.
