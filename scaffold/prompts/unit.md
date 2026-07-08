# CRUCIBLE — UNIT PROMPT (one bounded item, then STOP)

Launched by `scaffold/run_window.sh`. Do **exactly ONE** queue item, then **STOP** — the relauncher
launches the next. No loops, no second item, no "while I'm here".

## Box: resolve everything, hardcode nothing
`export BOX={{BOX}}` is injected. Resolve all else via `scaffold/boxpaths.py` — **zero hardcoded
host / IP / build-dir / lock-path**:
```bash
SSH="$(python3 scaffold/boxpaths.py "$BOX" --ssh)"        # ssh prefix; append a remote cmd:  $SSH 'uptime'
BUILD="$(python3 scaffold/boxpaths.py "$BOX" --build)"     # GPU bin dir  (add --cpu for the CPU bin dir)
LOCK="$(python3 scaffold/boxpaths.py "$BOX" --lock-path)"  # target box lock
python3 scaffold/boxpaths.py "$BOX" --wake                 # wake box before any [BOX] op (no-op-safe)
```

## Orient (read state from disk; you have no memory and no clock)
- `cat "$BOX/campaign.json"`; check the clock with `date +%s` vs `deadline_epoch` (always shell `date`).
- `"$BOX/MEMORY.md"` = working head (phase, queue, Pareto front, recent findings, landscape snapshot).
  **grep `MEMORY_ARCHIVE.md` on demand — never read it wholesale** (token-opt).
- `python3 scaffold/ledger.py tail "$BOX/ledger.jsonl" 15` and `... front "$BOX/ledger.jsonl"`.
- `"$BOX/STEERING.md"` (see below). **Doctrine is grep-on-demand:** `doctrine/INDEX.md` maps rule→file;
  grep the specific rule — don't preload all doctrine. `00_PRIME_DIRECTIVE` governs any novel call.

## Steering preempts the queue
Notes under `## Inbox (unprocessed)` in `STEERING.md` **OUTRANK** the queue. Take the **top** note as
this unit's item:
- **viable** → fold into the MEMORY queue as the takeable top (split if large); if tagged `(research)`,
  spend the unit on a web-research phase (`doctrine/03`), no benching.
- **not viable** → drop with a one-line reason (you may reject a human note).

**INVARIANT — empty what you consume**, in this unit's commit (never leave behind or silently delete):
- acted → `## Picked up`: `- [<ts>] **<note>** → queued as [ID] / ledger <id>`
- rejected → `## Dropped`: `- [<ts>] **<note>** → not viable: <reason>`

Only the **top** note this unit; untouched notes stay pending. Empty Inbox → take the queue top.

## Pick ONE takeable top, by resource tag
- `[HOST]` — codegen / web-research / build on the host; **no box**.
- `[BOX]` — needs the idle target; **wake it and hold the lock** for the whole measurement:
  `$SSH "flock -n $LOCK -c '<bench using the resolved $BUILD on the target>'"`
- `[EITHER]` — either resource.

Not actually takeable (ambiguous / too big / blocked)? Your unit is to **split it** into ordered
sub-units with crisp completion states, then stop.

## Do the work, then GATE (non-negotiable)
- **kernel / engine change** → numerical equivalence **KLD < 0.02** on the correctness corpus *before*
  any speed claim; fail → `failed`/`degenerate`, not a contender.
- **instruct GGUF** → Tier-0 degeneracy check **and** the chat-template path (never raw-prompt a
  template-bearing model).
- **quality = agentic composite (v0.4)** → run the agentic battery via `eval_config.py` (tool-calling +
  IFEval + GSM8K + code); `agentic_score` is the ranked quality coordinate (`doctrine/01`+`02`). bpb/Elo
  stay recorded as context, not the ranker.
- **measurement [BOX]** → pin the governor, box idle, discard warmup, `llama-bench` (median + variance).
  GPU: resolved `--build` + `-ngl 99`; CPU: `--build --cpu` (silently ignores `-ngl`).
- **engine flags** → probe `<bin> --help` once, record the working invocation under MEMORY's "Known-good
  flags per engine". (Rejected on some builds: `-st`, `--no-cnv`, `--no-display-prompt`, `--version`/`-d`
  on llama-bench. Stock uses `llama-cli`/`llama-completion`; ik_llama.cpp differs.)

## Record + commit (append-only)
Log every result — incl. `failed`/`couldnt_load`/`degenerate` and negatives (they're research output):
```bash
echo '{"status":"contender","parent":"<id>","config":{"engine":"...","model":"...","quant":"..."},
  "decode_tok_s":0.0,"prefill_tok_s":0.0,"agentic_score":0.0,"toolcall_pass":0.0,"ifeval_pass":0.0,
  "gsm8k_pass":0.0,"code_pass":0.0,"bpb":0.0,"notes":"..."}' | python3 scaffold/ledger.py record "$BOX/ledger.jsonl"
```
Record `agentic_score` + sub-scores when you have them (it, not bpb/quality, ranks the front); keep
`bpb` as context. Update `"$BOX/MEMORY.md"` to current truth, then commit the box repo:
`git -C "$BOX" add -A && git -C "$BOX" commit -m "<what this unit established>"`

## Locate-and-redirect (on every negative / closing result)
Before closing a negative, state in MEMORY **what it localizes the bottleneck TO** (not "didn't work"),
and if the residual is tractable **queue a new bounded item** for it. A negative rules out a
neighborhood and points at the residual — don't discard that.
(e.g. "TQ1_0 no faster → bottleneck is *load* BW, not compute; queue: bench with weights pre-faulted".)

## Leave the queue takeable, then STOP
The queue top must again be a single small takeable item (QUEUE INVARIANT). If you genuinely emptied it:
`mkdir -p "$BOX/work" && touch "$BOX/work/QUEUE_EMPTY"` — the relauncher then spends the next unit
refilling **≥5** takeable hypotheses (it consolidates only if that too finds nothing). Never write the
sentinel while a takeable item remains. One unit. **STOP.**
