# CRUCIBLE — CONSOLIDATE PROMPT (wind-down: tidy, seal, STOP)

Launched by `scaffold/run_window.sh` at wind-down or on an empty queue. **No new work** — no
hypotheses, kernels, downloads, or benches. Leave the campaign clean for the next window, then **STOP**.

`export BOX={{BOX}}` (host-side tidy; resolve any box detail via `scaffold/boxpaths.py`).

## Orient
`python3 scaffold/ledger.py front "$BOX/ledger.jsonl"` + `... stats "$BOX/ledger.jsonl"`;
`cat "$BOX/campaign.json"`; read `"$BOX/MEMORY.md"` and `"$BOX/GATE_QUEUE.md"`.

## Consolidate (make what exists current + consistent — nothing new)
1. **Reconcile MEMORY.md with the ledger**: fix stale numbers in the headline / Pareto front /
   tried-and-ruled-out / queue; restate the deadline; remove contradictions.
   **Keep the head bounded** (units read it every unit — this is the single biggest per-unit token
   cost). **Budget: ≤ ~12k tokens.** Keep only {current phase, queue + takeable top, live Pareto
   front, last ~3 findings, latest landscape snapshot, Known-good flags per engine}; **each
   tried/findings entry ≤ 2 lines** (the full detail lives in the ledger + archive — cite the ledger
   id, don't restate it). Move everything else into `"$BOX/MEMORY_ARCHIVE.md"` (append; prepend a
   one-line index of what moved). Nothing is lost — units grep the archive on demand. If the head is
   over budget, compaction is the priority of this pass. (The Known-good-flags block + landscape
   snapshot dominate the head; if it must shrink below ~9k, move those to a grep-on-demand reference
   file the head points to — the doctrine/archive pattern.)
2. **Queue clean**: every item resource-tagged `[BOX]`/`[HOST]`/`[EITHER]`, top = one small takeable
   item, open hypotheses carried with their locate-and-redirect residuals.
   **Drain `STEERING.md`** — Inbox empty: viable leftover notes → MEMORY queue + move to `## Picked up`
   (`→ carried to queue`); dead → `## Dropped` with a reason. Keep the Picked up / Dropped history.
3. **Final report** `"$BOX/reports/FINAL_$(date +%F)_window.md"`: Pareto front + blessed configs,
   roofline findings, what was ruled out **and what each negative localized the bottleneck to**, open
   hypotheses for next window. Cite ledger ids.
4. **GATE_QUEUE.md**: anything awaiting the human is clearly stated.

## Seal + STOP
```bash
git -C "$BOX" add -A && git -C "$BOX" commit -m "consolidate <date>: MEMORY current, queue takeable, FINAL report"
```
The relauncher sets `campaign.json.state="completed"` after you exit (the dashboard then renders the
sealed MEMORY.md as the closing document). Do not launch another unit. **STOP.**
