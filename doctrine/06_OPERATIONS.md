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
  "deadline_epoch": 1750259200,        // null for open-ended
  "winddown_margin_frac": 0.10,        // winddown_epoch = deadline - (deadline-start)*frac
  "n_measure_runs": 5,                 // bench repeats for median+variance
  "front_stall_K": 8,                  // research-trigger: # measured records with no front gain
  "roofline_efficiency_threshold": 0.60,
  "quality_floor_frac_of_fp16": 0.70,
  "state": "running"                   // -> "completed" after the relauncher consolidates
}
```

The threshold fields are agent-revisable priors; record any revision in `MEMORY.md`.

**Every iteration**, shell `date +%s` and compare to `deadline_epoch`. Never estimate
elapsed time from your sense of how long things took — that sense is unreliable and
resets on session death. The ledger timestamps every record, so a resumed session
derives true elapsed time from data.

Duration labels and their meaning:

- `1hr`, `6hr`, `1day`, `3day` → set `deadline_epoch = start_epoch + label`.
- `open` → `deadline_epoch = null`: **continuous improvement, no termination criteria.**
  Run until the human stops you.

## Wind-down protocol (set-length campaigns)

The **external relauncher** (`scaffold/run_window.sh`) owns the clock. It computes
`winddown_epoch = deadline_epoch − (deadline_epoch − start_epoch) × winddown_margin_frac`
(the last ~10% by default) and:

1. **Until `winddown_epoch`:** keep launching bounded units normally. **Wind-down ≠ stop** —
   units run full-tilt right up to the wind-down edge.
2. **At `winddown_epoch`:** stop launching research units and run the **consolidate** pass once
   (`scaffold/prompts/consolidate.md`): reconcile `MEMORY.md` against the ledger, leave the queue
   clean + tagged + takeable-top, drain the `STEERING.md` inbox, and write the **final report** to
   `reports/FINAL_<date>_window.md` (Pareto front, blessed configs, roofline findings, what was
   ruled out and where each negative localized the bottleneck, open hypotheses for next window).
3. The relauncher then sets `campaign.json.state = "completed"` and exits.

A `work/QUEUE_EMPTY` sentinel (written by a unit that genuinely exhausts the queue) ends the
window early the same way — straight to consolidate. Open-ended campaigns (`deadline_epoch =
null`) never auto-wind-down; they run until the queue empties or the human stops them.

## The iteration loop (owned by the external relauncher)

The loop lives **outside** any session, in `scaffold/run_window.sh`. It launches one bounded
`claude -p` **unit** at a time; each unit does exactly the steps below for **ONE** queue item and
then **STOPs**. The relauncher — not the unit — owns the clock and decides whether to launch the
next unit, switch to research, or wind down (so a dead or derailed session can't run the campaign
off the rails). A unit never loops or starts a second item.

```
# the relauncher, each turn:
now = `date +%s`
if winddown_epoch reached (or work/QUEUE_EMPTY present): run `consolidate` once -> state=completed -> exit
if the Pareto front has not gained ground in front_stall_K *measured* records: next unit is a
    web-research unit (refresh landscape, push fresh takeable hypotheses) — never two in a row
else: launch a normal unit

# one unit does, then STOPs:
  # 0. STEER (operator inbox) — read boxes/<nick>/STEERING.md; an unprocessed note PREEMPTS the
  #    queue (fold it in as the takeable top), then move it to Picked up / Dropped (consume-once)

  # 1. DECIDE (evidence-driven; 03)
  consult ledger + live Pareto front + roofline class of recent results
  if front stalled OR roofline says kernel-bound (and tier allows): escalate L2/L3
  else: draw next config from the NSGA-II/TPE sampler (L1)

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
   gone, and `STEERING.md` for any operator notes still pending.
7. Hand the loop back to the relauncher: `./scaffold/run_window.sh boxes/<nickname>` (add an
   hours argument to re-arm a fresh window). The session does **not** run the loop itself.

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
