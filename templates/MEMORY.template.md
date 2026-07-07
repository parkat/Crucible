# MEMORY — <NICKNAME>

> Lab notebook + brain transplant. A memory-less future session continues the campaign
> from THIS FILE alone (plus the ledger and the clock). Keep every section current.
> See doctrine/06 for the contract.
>
> **This file is the working HEAD** — keep it small; units read it in full every unit. A bounded
> head is the single biggest per-unit token saving. **Budget: ≤ ~6k tokens / ~150 lines; each
> tried/findings entry ≤ 2 lines** (cite the ledger id — the full detail lives in the ledger +
> archive, don't restate it). At consolidate, roll findings/snapshots older than the ~3 most recent
> into `MEMORY_ARCHIVE.md` (units `grep` it on demand).

## Deadline
- Label: <1hr|6hr|1day|3day|open>
- deadline_epoch: <int or null>   (null = open-ended, continuous improvement)
- Restated for humans: <e.g. "ends 2026-06-28 14:00 local" or "open-ended">

## Current phase
- Status right now: <what you are doing this moment>
- Baseline engine: <e.g. ik_llama.cpp @ <commit>>
- Baseline config: <model / quant / threads / kv / spec-decode>
- Autonomy tier: <T1..T4>

## Live Pareto front (summary)
<!-- non-dominated configs and their axis values; the dashboard renders the full set -->
| config id | decode tok/s | prefill tok/s | TTFT | quality (bpb↓ or Elo) | perf/W | roofline eff |
|-----------|--------------|---------------|------|---------------|--------|--------------|
|           |              |               |      |               |        |              |

## Tried & ruled out (with WHY)
<!-- so it is never re-tried; cite the ledger record id -->
-

## Research findings (accumulating — the compounding asset)
<!-- web-research knowledge: techniques, forks, papers, architectures. Cite sources. -->
-

## Known-good flags per engine
<!-- bug G: record the working non-interactive invocation per engine so units stop re-learning it.
     e.g. "stock llama.cpp @<sha>: llama-cli -p ... --no-display-prompt  (-st / --no-cnv REJECTED)". -->
-

## Open hypotheses (prioritized queue)
<!-- the next things to try; pop from here, push discoveries -->
1.

## Flags / anomalies
<!-- RECOVERY_NEEDED, gated items awaiting human (see GATE_QUEUE.md), measurement oddities -->
-
