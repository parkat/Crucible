# GATE_PROPOSALS — eval-kernel changes awaiting maintainer approval (doctrine/04)

Two fixes on branch `fix/v0.2-audit-and-token-opt` touch the eval/measurement kernel that
doctrine/04 gates even at T4. They are **staged, not shipped** — review and approve (or amend)
before merging to master. Everything else on the branch is ungated and applied directly (see
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
