# Crucible v0.4 — in progress (branch `release/0.4`)

Efficiency-focused release: agent-driven window control, token/model telemetry, and agentic-centered
scoring. **Not yet cut** — merge `release/0.4` → `main` + tag `v0.4` when ready.

## Base (already on `main`)
- **Anti-rabbit-hole novelty pivot** (`run_window.sh`) — after >100 K-items on one avenue with no new
  blessed config, abandon it, GC the dead experiments, and research a novel avenue. `c45ddba`.

## On `release/0.4`

### Agent window control — `d1ec221`
- New `scaffold/window.py` (companion to `steer.py`):
  - `stop` — **quick-stop**: drop `work/STOP` + kill the in-flight unit; the loop still consolidates and
    writes the FINAL report before exiting (`state → stopped`).
  - `stop --graceful` — let the current unit finish first.
  - `add-hours N` — extend the window (negative removes time); refreshes `duration_label`; guardrails
    (new deadline must be > now and > start).
  - `status` — window clock + state.
- `run_window.sh` — check the STOP flag at the top of every iteration; **re-read `deadline_epoch` each
  iteration** so time changes take effect live; capture the unit PID (`work/current_unit.pid`) via a
  `run_unit()` launcher so `window.py` can kill it; flip `state → stopped` on an operator stop.

### Agentic scoring — quality axis becomes an agentic composite — `4954380`
- Ranked quality coordinate is now **`agentic_score`** = tool-calling **0.40** / IFEval **0.25** /
  GSM8K **0.20** / code **0.15** (agent-revisable). Front stays 3-D {decode, prefill, agentic-quality}.
- `runner.py` — `grade_ifeval`, `grade_toolcall` (+`_extract_toolcall`, `_first_json`), `agentic_score`.
- `eval_config.py` — runs the batteries on the target, emits `agentic_score` + sub-scores.
- `ledger.py` — `_quality_coord` ranks on `agentic_score`; bpb/Elo = recorded context + legacy fallback.
- New FROZEN assets: `toolcall.jsonl` (10), `ifeval.jsonl` (12), `gsm8k.jsonl` (12).
- Doctrine 01/02, `assets/README.md`, `unit.md`, `GATE_PROPOSALS.md` amended (human-authored
  eval-kernel change per doctrine/04).

### Dashboard telemetry — `e801cc4` (server) + `e92ddac` (client)
- `server.py` — per-unit + per-window token telemetry from the stream-json `usage`/`modelUsage`:
  input/output/cache-read/cache-creation tokens, tok/s, approx peak context fill vs the model context
  window, and a per-model cost/token split (main vs sub-agent). New `_ingest_costs()` — approximate
  (chars/4) token cost to ingest key context files → `/data.ingest`. Agentic quality label.
- `index.html` — **Spend & tokens** panel (cost chart + token/context/per-model breakdown) and a new
  **Context ingest cost** panel. Quality meter relabeled elo → agentic. Theme-aware.

### Token efficiency — how agents read the scaffolding — `5f5232e`, `b42f47d`, `8aec98f` (+ box-repo compaction)
Input tokens are paid on every unit; this pass cuts the per-unit read ~half.
- **Compressed prompts** — `unit.md` 2,172→1,332 tok (−39%, −840 **every unit**), `consolidate.md` 886→533,
  hybrid (YAML/tables for structured rules + tight prose for rationale). Comprehension A/B verified (a
  fresh agent reproduced all 11 rules from the compressed `unit.md` alone — no regression).
- **Doctrine grep-on-demand** — new `doctrine/INDEX.md` (rule→file map); prompts now grep the one rule
  they need instead of preloading the ~12k-tok corpus. Cuts the boot read + prevents full-file reads.
- **MEMORY head budget + one-time compaction** — the head is read in full every unit and had ballooned
  to ~28k. Set a concrete budget (≤~12k; the "Known-good flags" block ~5.9k is the floor) and compacted
  Precision390's live head **27,989→13,411 tok (−52%, ~14.5k saved/unit)**, relocating superseded
  LFM/bitnet sagas to `MEMORY_ARCHIVE.md` verbatim (0 deletions — nothing lost).
- **Net per-unit read (unit.md + MEMORY head): ~30.2k → ~14.7k (≈−51%).**
- Deferred: doctrine prose compression (low leverage post-grep-on-demand); moving the flags block +
  snapshot to a grep-on-demand reference file (would take the head to ~6–7k).

## Verification done
- `window.py` — functional test (status / add-hours ± / stop / --graceful / guardrails / open-ended).
- `run_window.sh` — `bash -n` + dry-run intact.
- `runner.py` — self-test (objective + agentic graders). `ledger` — ranking test (agentic drives front,
  dominated config drops off, bpb fallback works).
- `server.py` — `/data` smoke test against real unit files (tokens, tok/s, per-model, ctx, ingest).
- `index.html` — JS brace/paren balance identical to the working baseline (no imbalance introduced);
  dashboard serves `/` (200) and `/data` on the new code.

## Pending / deferred
- **On-box**: run `eval_config.py`'s agentic battery against a real staged GGUF on the target (needs the
  box awake) to confirm live sub-scores + composite.
- **Live visual eyeball** of the new dashboard panels (Chrome extension was not connected this session) —
  open http://localhost:8787/.
- **Future plugins** on the same benchmark registry: heavier multi-step agentic harnesses
  (tau-bench / SWE-bench). Further dashboard visual work is the announced "next dashboard step".
