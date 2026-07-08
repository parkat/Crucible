# DOCTRINE INDEX — load a rule on demand; don't preload the corpus

Each unit already carries the operational rules it needs (`scaffold/prompts/unit.md`). Consult doctrine
only when a call is **novel or ambiguous**: find the file below, then `grep` the specific rule out of it
— do **not** read all of `doctrine/` every unit (token-opt; the full corpus is ~12k tokens).
`00_PRIME_DIRECTIVE` governs any novel situation not covered elsewhere.

| file | governs | grep for |
|---|---|---|
| `00_PRIME_DIRECTIVE.md` | the mission + how to act in novel / uncovered situations | mission, salvage, novel, prime directive |
| `01_RUBRIC.md` | scoring axes + the quality coordinate | decode / prefill / quality axes, roofline efficiency, quality floor, **agentic composite**, status vocab |
| `02_EVAL_FUNNEL.md` | how a model is evaluated | Tier 0/1/1.5/2, degeneracy, BPB, math/code, **agentic battery**, pairwise judge, frozen assets |
| `03_PROPOSER_PLAYBOOK.md` | what to try + the web-research phase | search levels L0–L3, landscape snapshot, research phase, hypothesis, MoE / speculative / SSM |
| `04_AUTONOMY_TIERS.md` | what you may do unattended + what is gated | tiers T1–T4, auto-promote vs gate, GATE_QUEUE, eval-kernel gate, git before/after self-mod |
| `05_SAFETY_RECOVERY.md` | safety, secrets, the independent verifier, recovery | secrets / password discard, `verify.py`, resume, rollback |
| `06_OPERATIONS.md` | the loop, windows, resume protocol | run_window, window / deadline, resume protocol, box lock, relauncher |

Example: to check whether a change needs human approval →
`grep -i -A3 'eval.*kernel\|gate' doctrine/04_AUTONOMY_TIERS.md` (not a full read).
