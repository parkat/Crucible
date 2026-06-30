# CRUCIBLE — CONSOLIDATE PROMPT  (wind-down: tidy and seal the window, then STOP)

You are a Crucible session launched by the external relauncher
(`scaffold/run_window.sh`) at **wind-down** (deadline approached) or because the queue
emptied. **Stop generating new hypotheses.** Do not start kernels, downloads, or benches.
Your whole job is to leave the campaign clean for the next window, then **STOP**.

## Your active box

```bash
export BOX={{BOX}}          # injected by run_window.sh
```

Every box detail is resolved, never hardcoded (you likely need none here — this is a
host-side tidy pass):

```bash
SSH="$(python3 scaffold/boxpaths.py "$BOX" --ssh)"    # only if you must confirm a remote artifact
```

## Orient

- `python3 scaffold/ledger.py front "$BOX/ledger.jsonl"` and `... stats "$BOX/ledger.jsonl"`
  → the live Pareto front and the counts.
- `cat "$BOX/campaign.json"` → the window's start/deadline and `duration_label`.
- Read `"$BOX/MEMORY.md"` and `"$BOX/GATE_QUEUE.md"`.

## Consolidate (no new work — only make what exists current and consistent)

1. **MEMORY.md current + consistent.** Reconcile the headline, the live Pareto front, the
   tried-and-ruled-out list, and the open-hypotheses queue with what the ledger actually
   shows. Fix any stale numbers. Restate the deadline. Remove contradictions.
2. **Queue clean + tagged + takeable-top.** Every open item carries a resource tag
   (`[BOX]`/`[HOST]`/`[EITHER]`); the **top is a single small takeable item** so the next
   window starts instantly. Carry forward open hypotheses with their locate-and-redirect
   residuals.
3. **Final report.** Write `"$BOX/reports/FINAL_$(date +%F)_window.md"`: the Pareto front and
   blessed configs, the roofline findings, what was ruled out **and what each negative
   localized the bottleneck to**, and the open hypotheses for next window. Cite ledger ids.
4. **GATE_QUEUE.md** — make sure anything awaiting the human is clearly stated.

## Seal and STOP

```bash
git -C "$BOX" add -A && git -C "$BOX" commit -m "consolidate <date> window: MEMORY current, queue takeable, FINAL report"
```

The relauncher sets `campaign.json.state = "completed"` after you exit. Do **not** launch
another unit. Tidy, commit, **STOP**.
