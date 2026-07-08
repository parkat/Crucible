# GATE_PROPOSALS — eval-kernel changes (doctrine/04)

> ✅ **HUMAN GATE PASSED — 2026-07-02, operator parker@pureturbos.com.** Proposals A and C are
> **APPROVED** and shipped in **v0.3**. The operator accepted the residual caveat that C
> (eval engine-compat) was **not exercised on a real engine** in the audit checkout — it is to
> be confirmed on the next stock/ik engine run; back it out if that run shows a regression.

Two fixes touch the eval/measurement kernel that doctrine/04 gates even at T4. Originally staged
on branch `fix/v0.2-audit-and-token-opt`; **now merged to master (v0.3) with human-gate approval
recorded above.** Everything else on the branch is ungated and was applied directly (see
"Ungated fixes" at the bottom). Source: optiplex5050 v0.2 deployment handoff, 2026-07-02.

---

## Proposal A — Pareto front must rank on a cross-model-valid quality axis
**Files:** `scaffold/ledger.py` (`_quality_coord` / `_objs` / `pareto_front`, front print),
`scaffold/dashboard/server.py` (`_models_table` quality labelling).

**Problem (confirmed in code):** `quality` was a raw front objective. When populated with `100/ppl`
(as in the optiplex5050 run — ledger `e232e626` `quality=13.2689 = 100/7.5364`), raw perplexity is
**not comparable across tokenizers**, so `ledger.py front` and the dashboard printed a wrong front
on any multi-model campaign. `bpb` — the doctrine 01/02 cross-tokenizer ruler — was recorded in the
schema but was **not** a front objective. The dashboard additionally hard-labelled any populated
`quality` as `quality_kind:"elo"` even though no Tier-2 Elo ran (see Proposal C).

**Change staged:** the quality objective is now `-bpb` (lower bpb = better) whenever `bpb` is
present, falling back to the raw `quality` scalar only for single-model runs with no bpb. The
dashboard labels the number by its true source: `bpb` (ruler), else `objective` (math/code), else
`quality` (single-model scalar) — **never** blanket `elo`.

**Verify before merge:** re-run `ledger.py front` against the optiplex5050 ledger and confirm the
front matches the report's hand-stamped "trust the bpb-front". Confirm no single-model campaign
(bpb absent) regresses. Decide whether to additionally *require* a real Elo before a row is
front-eligible on quality (audit option i) — not staged; current change is option (ii).

## Proposal C — eval harness must detect the engine's real binary/flags
**File:** `scaffold/eval/eval_config.py`.

**Problem (confirmed in code):** hardcodes a `llama-completion` binary and the flags
`--no-display-prompt`, `-cnv -st --jinja`, `--no-cnv`. On the optiplex5050 engines (stock
llama.cpp `4fc4ec5`, ik `068b173`) that binary/those flags don't exist → `unknown argument: -st`,
`--no-conversation is not supported by llama-cli`, `rc=124` interactive-mode hangs. The Tier-1
objective grader never ran, so quality silently fell back to `100/ppl` — **this is the root cause
of Proposal A** (the ruler was never exercised).

**Change staged:** `_resolve_bin()` falls back to a sibling `llama-cli`/`llama-completion` when the
given binary is absent; `_help_text()`+`_flag_ok()` gate every optional flag on what `<bin> --help`
advertises, so a run degrades (with a warning) instead of dying on an unknown argument.

**Verify before merge (REQUIRES a real engine — could NOT be exercised in this checkout):** run
`eval_config.py` against both a stock `llama-cli` build and an `ik_llama.cpp` build; confirm a
template-bearing GGUF still routes through the chat template (or warns clearly if the build can't),
and that a true base model still runs raw. This patch is defensive but unverified on-engine.

---

## Ungated fixes on this branch (already applied, no gate needed)
- **B** `ledger.py record` subcommand + `unit.md` doc — verified (round-trip + bad-status reject).
- **D** `run_window.sh --dry-run` no longer mutates `campaign.json` — verified (fixture, unchanged).
- **F** `run_window.sh` re-arm now writes `duration_label` from `HOURS`.
- **G** "Known-good flags per engine" block added to `unit.md` + `MEMORY.template.md`.
- **H** `scaffold/remote_run.sh` detached-run+poll helper (atomic DONE sentinel; always resolves
  the lock via `boxpaths.py --lock-path`, killing the `"$LOCK"`-unset class). Needs on-box test.
- **Token-opt** per-unit model routing in `run_window.sh` (`pick_model`) + MEMORY head/archive
  split conventions in `unit.md` / `consolidate.md` / `MEMORY.template.md`.

## Not code changes (host/provisioning — documented, not patched)
- **E** `boxpaths.py --wake` already degrades cleanly and already reads `wol_helper` from the
  contract; the gap is host-side (`apt install wakeonlan` or ship a `.ps1`). No master change.
- **§2 stray files** (`"$LOCK"`, `work_dashboard.log`) were optiplex5050-checkout-only — not
  present in this tree; nothing to delete here. (H removes the cause of the `"$LOCK"` one.)

---

## v0.4 (branch `release/0.4`) — Quality axis becomes the AGENTIC composite  (2026-07-07)

**Eval-kernel change, HUMAN-DIRECTED by the operator** (parker@pureturbos.com) in a dev session.
Doctrine/04 gates eval/measurement-kernel changes even at T4; this one is authored by the human
(not silently self-applied by the loop), developed on the `release/0.4` branch, and recorded here
per the gate. **Finalize at the 0.4 merge.**

**Rationale:** Crucible feeds an agent cluster (project Cynosure), so configs must be ranked by
AGENTIC usefulness, not generic-text quality. Operator's two explicit choices (2026-07-07): center
scoring on agentic performance + industry-standard benchmarks; make the ranked quality coordinate an
agentic composite (bpb/Elo demoted to recorded context).

**Files:**
- `scaffold/eval/runner.py` — graders `grade_ifeval`, `grade_toolcall` (+`_extract_toolcall`,
  `_first_json`) and the `agentic_score` composite (weights tool 0.40 / ifeval 0.25 / gsm8k 0.20 /
  code 0.15 — agent-revisable). All auto-graded; objective Tier-0/1 unchanged.
- `scaffold/eval/eval_config.py` — runs the batteries on the target, emits `agentic_score` +
  `toolcall_pass`/`ifeval_pass`/`gsm8k_pass` (assets optional so older staged dirs still run).
- `scaffold/ledger.py` — Record gains `agentic_score`/`toolcall_pass`/`ifeval_pass`/`gsm8k_pass`;
  `_quality_coord` returns `agentic_score` when present (front stays 3-D). bpb/Elo = fallback ranker
  for legacy records + recorded context.
- `scaffold/eval/assets/{toolcall,ifeval,gsm8k}.jsonl` — new FROZEN seed sets (10 / 12 / 12 items).
- `doctrine/01_RUBRIC.md`, `doctrine/02_EVAL_FUNNEL.md`, `eval/assets/README.md`, `prompts/unit.md`,
  `dashboard/server.py` — amended to describe/surface the agentic quality axis.

**Verify before merge:** graders pass `runner.py`'s self-test (done). On-box: run `eval_config.py`
against a real staged GGUF and confirm sane sub-scores + composite; confirm `ledger.py front` ranks
on `agentic_score`. Heavier multi-step agentic harnesses (tau-bench/SWE-bench) are deferred as
future plugins on the same registry.

---

## Proposal E (v0.4) — agentic graders are format-fragile + token-truncated; harden before the composite is trusted for ranking  (2026-07-08)

**Status: PROPOSED — awaiting human gate.** Surfaced by the operator on the 2026-07-07 18hr live
window (the first on-box exercise of the agentic kernel from Proposal-v0.4 above). The plumbing works;
the **numbers do not yet measure capability**, so the composite must NOT re-rank the blessed front
until this lands. `bpb`/objective stay the trustworthy rulers in the meantime.

**Live evidence (blessed front, this window).** composite = tool·0.40 + ifeval·0.25 + gsm8k·0.20 + code·0.15:

| config | toolcall | ifeval | **gsm8k** | code | composite |
|---|---|---|---|---|---|
| Qwen2.5-**0.5B** Q4_K_M | 0.96 | 0.58 | **0.83** | 0.81 | **0.818** |
| Llama-3.2-**3B** Q4_K_M | 0.95 | 0.75 | **0.17** | 0.94 | 0.742 |
| Qwen2.5-**3B** Q4_K_M | 1.00 | 0.75 | **0.08** | 0.94 | 0.745 |
| DeepSeek-R1-7B IQ2_M | 0.20 | 0.31 | 0.50 | 0.00 | 0.256 |

Two smoking guns:
1. **GSM8K is inverted** — a 0.5B scores 0.83 while two 3B models score 0.08–0.17 (real GSM8K is the
   opposite: 3B ≈ 79%, 0.5B ≈ 35%). Classic **token-budget truncation**: bigger/reasoning models run
   out of budget mid-derivation and never emit the final answer, so the grader scores 0.
2. **Huge variance at n=10–16** — the *same* DeepSeek IQ2_M config scored **0.256 and 0.542** on two
   runs this window (toolcall alone 0.20→0.725). Smoke-test sample sizes, not stable evals.

**Problem (confirmed in code):**
- `runner.py grade_math` (used for gsm8k) does "exact numeric match on **the LAST number emitted**" —
  fragile to truncation (last number is an intermediate step) and to any trailing non-answer number.
- `runner.py _extract_code` / `_extract_toolcall` scan for the first ```` ```python ````/JSON block —
  a `<think>` preamble (R1-Distill) or unfenced output yields no match → 0 (DeepSeek code 0.00, tool 0.20).
- `eval_config.py` already has a "truncated-mid-think → one-shot larger budget" retry (see its budget
  comments), but the live data shows it is **insufficient** — the 3B gsm8k collapse and the DeepSeek
  run-to-run swing both persist through it.
- Assets are frozen SEED sets: `toolcall` 10 / `ifeval` 12 / `gsm8k` 12 / `code` 16 items.

**Change proposed (all in the gated eval kernel — `runner.py`, `eval_config.py`, `eval/assets/*`):**
1. **Answer extraction, not last-number.** `grade_math`: strip `<think>…</think>` first; prefer an
   explicit answer marker (`#### N`, `\boxed{N}`, "the answer is N") and only fall back to last-number
   when no marker is present. Same `<think>`-strip for `_extract_code` (+ an unfenced `def …` fallback)
   and `_extract_toolcall` (scan for JSON after the reasoning).
2. **Real token budget + robust escalation.** Raise the per-item budget (esp. gsm8k/code) and make the
   truncation-retry fire on *"no answer extracted"* (not just a heuristic), escalating budget until an
   answer appears or a hard cap. Record whether a retry/truncation occurred per item, so a low score
   from truncation is distinguishable from a low score from being wrong.
3. **Grow the assets** 10–16 → ≥ 50 items/battery to bring the per-item quantum (currently 1/12 ≈ 0.083)
   and the variance down to usable levels. Keep them FROZEN once expanded.

**Verify before merge:** re-run the four blessed configs on-box after the change; the 3B gsm8k scores
should jump (confirming truncation was the cause) and the DeepSeek run-to-run swing should shrink.
Confirm `runner.py` self-test still passes and the composite weights are unchanged (only the graders +
budget + asset size change). Until then, treat `agentic_score` as **plumbing-verified but not
capability-valid**; do not front-rank on it.
