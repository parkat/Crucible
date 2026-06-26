# 02 — EVAL FUNNEL

How to evaluate **any model the agent can find**, cheaply enough to run in a loop and
hard enough that a self-modifying T4 agent can't game it. The organizing idea: **the
expensive, gameable judge only runs on candidates that already passed cheap *objective*
filters and are fast enough to matter.** Objective tiers gate entry to the subjective
one, so the judge can never single-handedly elevate something the cheap signals say is
garbage.

`scaffold/eval/runner.py` implements this. The frozen assets it reads
(`scaffold/eval/assets/`) are part of the **invariant kernel** — you may *propose*
changes to them, you may not silently edit them (`00`, `04`).

## Tier 0 — degeneracy filter (≈free, every config)

Run the model on a small fixed prompt set and confirm the output isn't broken:

- no repetition loops (n-gram cycle detection),
- no NaN / invalid-token / control-character garbage,
- valid UTF-8,
- sane length distribution (not instant-EOS, not runaway).

A large fraction of bad quants and wrong kernels die here — a kernel returning subtly
wrong numbers collapses the model into repetition, which is a *free correctness signal*
as much as a quality one. For a **config change on a known model**, also compute
**KLD-vs-own-fp16** here (the within-model damage signal from `01`). Failing Tier 0 →
status `degenerate` (or `failed` if it's a crash), and you stop; no point spending more.

## Tier 1 — cheap cross-model proxy (cheap, per model)

Two **objective, un-gameable** numbers — graded by execution and arithmetic, not opinion:

- **Bits-per-byte (BPB) on a frozen held-out generic-text corpus.** BPB normalizes by
  *raw bytes*, not tokens, so it is **tokenizer-independent and genuinely cross-model
  comparable** — unlike raw perplexity, which is not. This is your cheap "is this model
  even in the right ballpark" signal.
- **A small fixed auto-gradable set:** math problems with numeric answers (exact-match)
  and code problems with unit tests (execution-checked in a sandbox). The judge cannot
  Goodhart these because a unit test either passes or it doesn't.

Tier 1 produces the objective floor and the data for the per-model quality-floor
calibration (`01`).

## Tier 2 — pairwise judge (expensive, Pareto-front contenders only)

When a (model, config) is fast enough to seriously threaten the front, the
orchestrating session judges it — but **pairwise, never absolute**:

> Here are outputs A and B to the same frozen prompt. Which is better, and why?

Pairwise comparison is dramatically more reliable than "score this 1–10": it sidesteps
scale drift and the length/verbosity/position biases that wreck absolute LLM-judging.
(This is the Chatbot-Arena insight.) New entrants are compared against current front
members, updating a **Bradley-Terry / Elo** score — O(n) comparisons per new entrant,
not O(n²), and it fires **only on contenders**, so cost stays bounded across a 3-day or
open-ended run.

**The judge is the orchestrating Opus 4.8 session itself.** The runner prepares the
frozen prompt, collects outputs A and B, and records the session's verdict plus the
Elo update. Judging instructions and rubric live in the frozen assets.

## Synthesis → the single quality coordinate

Combine per `01`:

```
Q_base(M)  = Elo(M @ best/fp16 config)          # cross-model anchor, from Tier 2
Q(M, c)    = Q_base(M) - penalty(KLD(M, c))      # within-model interpolation, from Tier 0
```

Promote front contenders to **direct** Tier-2 judging to replace the interpolated
estimate with a measured Elo and to recalibrate `penalty`.

## Why this survives a T4 self-modifying agent

- Everything that **is the ruler** — the frozen corpus, the auto-gradable item sets,
  the pairwise prompts, the judge rubric — is invariant-kernel and cannot be silently
  re-cut.
- **Objective anchors (Tier 1) gate the subjective judge (Tier 2).** Any contradiction
  — the judge loves a model that fails every unit test — is logged with the **objective
  signal winning**.
- **Judge-drift control:** periodically re-judge a *frozen reference pair* with a known
  expected verdict. If the session starts disagreeing with itself across resumes, that
  drift is visible in the audit trail.
- The **independent verifier** (`05`) re-checks blessed configs outside your loop.

## Honest limitations (so they're not surprises)

- **Contamination.** Public benchmark items leak into training sets. BPB-on-generic-
  text and non-standard pairwise prompts are more contamination-resistant, and are
  preferred for that reason. But note: for the *actual* goal — *which salvage-deployable
  model is best* — some contamination is tolerable, because a model that memorized an
  answer still produces a good output in deployment. Contamination wrecks *science
  claims*, not *"which model should I run."* Don't over-index on it.
- **Loadability gates "any model."** An exotic model you find won't be evaluable until
  some engine can load it. "Couldn't load with available engines" is a logged outcome
  (`couldnt_load`), not a crash — and backporting a loader is exactly the research you
  want, so this generates good work rather than blocking it.
- **Judge bias persists** even pairwise. The objective anchors exist precisely so the
  subjective axis can never run away unchecked. Trust Tier 1 over Tier 2 on conflict.

## Frozen asset manifest (build once, then freeze)

In `scaffold/eval/assets/` — see `assets/README.md` for schemas and seed examples:

- `corpus.txt` — held-out generic text for BPB. Must be text the candidate models are
  unlikely to have memorized verbatim; rotate only with human approval.
- `math.jsonl` — `{id, prompt, answer}` exact-match items.
- `code.jsonl` — `{id, prompt, tests}` unit-test items (tests run sandboxed).
- `pairwise_prompts.jsonl` — `{id, prompt}` open-ended prompts for Tier-2 judging.
- `judge_rubric.md` — the instructions the session follows when judging A vs B.
- `reference_pair.json` — a frozen A/B with a known expected verdict, for drift checks.

These start small. Growing them is a legitimate proposal (surface the diff); shrinking
or weakening them to make a number move is exactly what the gate prevents.
