# Frozen eval assets

These files are **the ruler** (doctrine/00, 02). They are invariant-kernel: the agent
may *propose* changes (write them to `GATE_QUEUE.md`) but may **not** silently edit them
at any tier, including T4. The seeds below are deliberately tiny — the agent's first job
in a campaign is to *grow* them (with human approval) into a real evaluation set, then
freeze them.

The orchestrator runs the model on the target to produce outputs/log-probs and passes
them to `eval/runner.py`; these files define the fixed inputs and the grading keys.

## Files & schemas

### `corpus.txt`  — for bits-per-byte (Tier 1)
Plain UTF-8 prose the candidate models are unlikely to have memorized verbatim. BPB is
summed −log2 P(token) over this text divided by its raw byte length, so it's
tokenizer-independent and cross-model comparable. Prefer recent, niche, or
locally-authored text over canonical web text to limit contamination. Rotate only with
human approval.

### `math.jsonl`  — exact-match math (Tier 1)
One JSON object per line:
```json
{"id": "m001", "prompt": "What is 17 * 23? Give only the number.", "answer": 391}
```
Grading: the **last number** in the model output must equal `answer` (numeric compare).

### `code.jsonl`  — unit-tested code (Tier 1)
```json
{"id": "c001", "prompt": "Write a Python function `add(a, b)` that returns their sum.", "tests": "assert add(2, 3) == 5\nassert add(-1, 1) == 0"}
```
Grading: model output (code block extracted) + `tests` is executed in a subprocess
sandbox; exit 0 = pass. Tests must be self-contained and deterministic.

### `pairwise_prompts.jsonl`  — open-ended prompts for the judge (Tier 2)
```json
{"id": "p001", "prompt": "Explain why batch-1 LLM decode on CPU is memory-bandwidth-bound, in 3 sentences."}
```
Used only for front contenders. The session judges two models' outputs **pairwise**
against `judge_rubric.md`.

### `judge_rubric.md`  — the judging instructions
What "better" means, what to ignore (length, position, verbosity). Read by the session
when acting as judge. Frozen so the criteria can't drift to flatter a favored model.

### `reference_pair.json`  — judge-drift check
A frozen A/B with a known expected winner. Periodically re-judged; if the session starts
disagreeing with the expected verdict, judge drift is visible in the audit trail.
```json
{"id": "ref001", "prompt": "...", "output_a": "<clearly better answer>", "output_b": "<clearly worse answer>", "expected_winner": "a"}
```

## Agentic benchmarks (v0.4) — the quality composite

From v0.4 the **ranked quality coordinate is an agentic composite** (`runner.agentic_score`,
doctrine/01+02). These frozen sets are auto-graded (no judge) and each is recorded so a config is
comparable to public leaderboards. Still invariant-kernel — propose changes, don't silently edit.

### `toolcall.jsonl`  — tool / function calling (BFCL-style, the center; weight 0.40)
```json
{"id": "t001", "prompt": "What's the weather in Paris?", "tools": [{"name": "get_weather", "description": "...", "parameters": {"city": "string"}}], "expected": {"name": "get_weather", "args": {"city": "Paris"}}}
```
Grading: the model is prompted with the tool schemas + request and must emit ONE JSON tool call;
score = 0.5·(name correct) + 0.5·(fraction of expected args correct). Single-turn call emission.

### `ifeval.jsonl`  — instruction following (IFEval-style; weight 0.25)
```json
{"id": "if001", "prompt": "List exactly three benefits ... start with '-' ... no commas.", "checks": [{"type": "bullets", "n": 3, "at_least": false}, {"type": "no_commas"}]}
```
Grading: each output scores the fraction of its programmatic `checks` satisfied. Check `type`s:
`contains`/`not_contains`/`regex`/`min_words`/`max_words`/`bullets`/`numbered`/`json`/`ends_with`/
`starts_with`/`no_commas`/`keyword_count`/`all_caps`/`lowercase`/`max_sentences`.

### `gsm8k.jsonl`  — grade-school reasoning (exact-match; weight 0.20)
```json
{"id": "g001", "prompt": "A baker made 48 muffins and sold three-quarters ... Give only the number.", "answer": 12}
```
Grading: same last-number exact-match as `math.jsonl` (reuses `runner.grade_math`).
