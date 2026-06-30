# crucible

A recursive, self-improving research harness that drives a single throwaway target
box over SSH to discover **maximally fast and intelligent batch-1 LLM inference** on
**AVX-less, memory-bandwidth-limited salvage hardware**.

One Claude Code session (Opus 4.8) on a host machine is the orchestrator, the
proposer, and the judge. It SSHes into one target box, scans it, and then runs an
open-ended search over engines, models, quantizations, kernels, compile flags, and
algorithms — extending its own search code as it goes — to push a Pareto frontier of
speed vs. quality. The mission is to advance local AI for people whose only hardware
is salvage.

## The one idea that organizes everything

**Open hypothesis space, protected evaluation.**

- *Everything worth trying is discovered, never hardcoded.* Which engine, which fork,
  which model, which architecture (dense / MoE / SSM / whatever the agent finds),
  which kernel, which quant, which compile flags, the search algorithm itself, and —
  at the top autonomy tier — the orchestration code. The action space is genuinely
  open; the agent populates it by web research, testing, and random hypotheses.
- *A tiny kernel of method is protected from the search.* Five invariants
  (correctness gating, measurement hygiene, time discipline, resumability, target
  recovery) and the frozen evaluation assets. These are **not optimization avenues**.
  They exist because any optimizer that can rewrite both its engine and its own
  evaluation will, with certainty, find ways to make the number go up that aren't
  real progress. The kernel protects the thing you actually want — real progress —
  from being Goodharted by the search.

Freedom in *what to try*; discipline in *how it's judged*.

## The physics anchor

Batch-1 **decode** is memory-bandwidth-bound: `tok/s ≈ effective_BW / active_bytes_per_token`.
**Prefill** is compute/SIMD-bound. AVX-lessness mostly costs prefill and the
dequant+dot-product hot path; it barely moves the decode ceiling because that ceiling
is the DRAM bus. The two phases are optimized separately, always. This single fact
dictates what is even worth searching — see `doctrine/03_PROPOSER_PLAYBOOK.md`.

## Requirements

- **Host** (where the Claude Code session runs — never the target): `python3` (≥3.10, stdlib
  only — the dashboard has zero third-party deps), `git`, `ssh`/`ssh-keygen`, and `sshpass`
  (installed once to seed the key). The host drives everything and serves the dashboard.
- **Target** (the disposable salvage box): reachable over SSH with a sudo-capable user. Nothing
  is pre-installed — the agent builds the engine on the target itself. The box may be asleep;
  if `hardware.json` records Wake-on-LAN, the resolver can wake it.
- **Models**: fetched over the internet as needed, or staged on a LAN store (`LAN_MODEL_STORE`
  in `startup.md`) to avoid re-downloading.

## Start fresh — one paste, one instruction

1. **Dump every `.md` (and the rest of the apparatus) flat into one folder.** Files may be
   flat, mis-nested, or already structured — it does not matter.
2. **Paste your creds** into the fill-in block in `startup.md` (target IP, SSH user/pass, a
   nickname for the box; optional LAN model store). Pick an autonomy tier and a campaign length.
3. Tell the Claude Code session:

   > follow startup.md

That one flow now, end to end: **repairs the layout** (`preflight.py` rebuilds the canonical
tree from the flat dump) → **installs a dedicated SSH key and discards the password** →
**scans the hardware into a COMPLETE contract** (ISA, measured bandwidth, GPU `sm_NN`/VRAM,
Wake-on-LAN) → **writes `connection.json`** → **scaffolds the box folder** (its own git repo) →
**starts the dashboard** → **seeds `MEMORY.md`** with research + a takeable hypothesis queue →
**launches the loop**. One paste, one instruction.

## Run / resume the campaign

The research loop runs **outside** the session, in an external relauncher — so a dead or
derailed session can never run the campaign off the rails. The loop lives on disk:

```bash
./scaffold/run_window.sh boxes/<nick>          # continue the current window as-is
./scaffold/run_window.sh boxes/<nick> 24       # RE-ARM a fresh 24-hour window, then run
./scaffold/run_window.sh boxes/<nick> --dry-run # preview the epoch math + the exact invocation
```

`hours` re-arms a fresh window (new start/deadline, same wind-down margin); omit it to
continue the current one. Everything is read from the three contracts — nothing depends on
prose or on a session's memory. **The session is ephemeral; the campaign lives on disk.**

## Add a second box (fleet parallelism)

Each box is driven by its own independent relauncher — run as many as you have hardware for:

```bash
# 1. paste creds for the new nickname into startup.md, then:  "follow startup.md"
# 2. drive it (in its own terminal):
./scaffold/run_window.sh boxes/<newnick> 24
```

Results compare across boxes via **roofline efficiency** (achieved ÷ ceiling), so a fast box
and a slow box are judged on the same scale.

## The contract trio + the resolver

Nothing hardcodes how to reach or build on a box. Each box folder carries three contracts,
and `scaffold/boxpaths.py` turns them into concrete commands:

| contract | answers | lives at |
|---|---|---|
| `connection.json` | **how to reach** the box (host, user, key, remote paths) | `boxes/<nick>/` |
| `hardware.json`   | **what the box is** (ISA, bandwidth, GPU, Wake-on-LAN) | `boxes/<nick>/` |
| `campaign.json`   | **the current window** (start/deadline, thresholds, state) | `boxes/<nick>/` |

```bash
python3 scaffold/boxpaths.py boxes/<nick> --ssh        # the SSH prefix (append a remote command)
python3 scaffold/boxpaths.py boxes/<nick> --build      # remote build bin dir (build-cuda<NN> if GPU)
python3 scaffold/boxpaths.py boxes/<nick> --build --cpu # force the CPU build dir
python3 scaffold/boxpaths.py boxes/<nick> --lock-path  # the target-side box lock
python3 scaffold/boxpaths.py boxes/<nick> --wake       # wake a sleeping box (WoL / PowerShell helper)
```

Every loop prompt resolves the box through this tool, so the whole apparatus is
host/target/hardware-agnostic: point it at a different box folder and every command
re-resolves. (The password is **never** stored — it is used once at setup, then discarded.)

## Loop behavior

The relauncher launches one **bounded unit** at a time (`claude -p` with the box injected).
Each unit does exactly ONE takeable queue item, gates it (KLD &lt; 0.02 for kernels; Tier-0 +
chat template for instruct models), records to the ledger, leaves the queue's top takeable,
and **stops**. The relauncher then decides whether to launch the next:

```
bounded units ──▶ wind-down (deadline − margin) ──▶ consolidate once ──▶ exit
        └────────▶ queue empty (work/QUEUE_EMPTY sentinel) ──▶ consolidate ──▶ stop
```

**Research is automatic, not a separate session.** The opening web-research phase seeds the
queue at startup; thereafter `doctrine/03`'s "research whenever the front stalls" is enforced by
the relauncher: when the Pareto front hasn't gained ground in `front_stall_K` **measured**
records, the next unit is spent on a web-research phase (refresh the landscape, push 1–3 fresh
takeable hypotheses to the queue) instead of grinding the stale queue — never two research units
in a row, so a normal unit acts on the new hypotheses before the next research pass. You don't
run a separate session to refill the queue; units both execute and refill it.

`winddown_epoch = deadline − (deadline − start) × winddown_margin_frac`. **Wind-down ≠ stop:**
units keep running until wind-down; only then does the session switch to consolidation-only
(make `MEMORY.md` current, leave the queue clean + tagged + takeable-top, write the FINAL
report). To resume later, just run `run_window.sh` again — it reconstructs state from disk
(`MEMORY.md` + `ledger.jsonl` + the clock).

## Watch it live — the dashboard

A read-only, auto-refreshing monitor runs on the **host** (it never polls the target — that
would perturb the very numbers the campaign measures). It renders entirely from host-side
artifacts and tolerates files being appended to mid-read.

```bash
python3 scaffold/dashboard/server.py boxes/<nick>     # one box
python3 scaffold/dashboard/server.py boxes            # the whole fleet (one panel per box)
# then open the printed http://127.0.0.1:8787/   (override with --port / --host)
```

Per box it shows, all from `boxes/<nick>/`:

- **Window status** — phase (`running` / `winddown` / `done`) with live countdowns to both
  wind-down and the deadline, from `campaign.json`.
- **Live agent activity** — the current task (the takeable-top item, or "web-research phase"
  when stalled) with the time it **started** and a running elapsed; units completed this window,
  cumulative `total_cost_usd`, and the `QUEUE_EMPTY` sentinel — from the `run_window` driver log
  and each unit's `claude -p` output.
- **Live transcript** — the running unit's thinking + every tool/SSH command + results,
  streamed as it happens. `run_window.sh` runs units with `--output-format stream-json`, so
  each unit's `.jsonl` grows live and the dashboard tails it (host-side; the box is never
  polled). The panel auto-follows the newest event unless you scroll up.
- **Research progress** — the Pareto front, every measured model, the roofline gauge, the
  iteration timeline, and key findings (each stamped with when it was logged), from `ledger.jsonl`.
- **Latest research round** — the results summary + fresh hypotheses from the most recent
  stall-triggered web-research unit, with when it ran.
- **Queue** — the `[BOX]`/`[HOST]`/`[EITHER]`-tagged hypothesis queue from `MEMORY.md`, with
  the takeable-top item highlighted and closed items listed.
- **Hardware contract** — measured STREAM bandwidth (the roofline denominator), ISA, GPU, RAM.

The fleet bar at the top shows one card per box (phase, countdowns, units, cost); click a card
to focus its detail panels.

## Layout

```
crucible/
  README.md              <- you are here
  startup.md             <- entry point: fill-ins + pickers + boot procedure
  doctrine/              <- the agent's constitution (read in full before acting)
    00_PRIME_DIRECTIVE.md    mission, physics, the invariant kernel
    01_RUBRIC.md             the Pareto axes + how quality is scored
    02_EVAL_FUNNEL.md        how "any model" is evaluated cheaply and un-gameably
    03_PROPOSER_PLAYBOOK.md  roofline router, action space, recursion levels
    04_AUTONOMY_TIERS.md     T1-T4 and what each unlocks
    05_SAFETY_RECOVERY.md    ISA guard, watchdog, the independent verifier
    06_OPERATIONS.md         the iteration loop, time discipline, resume protocol
  templates/             <- instantiated once per box
  scaffold/              <- runnable spine (ledger, scan, roofline, verifier, eval, dashboard)
    boxpaths.py            the box resolver: contracts -> concrete ssh/build/lock/wake commands
    run_window.sh          the external relauncher: owns the bounded-unit research loop
    hardware_scan.sh       scans a box into a COMPLETE contract (ISA, BW, GPU, Wake-on-LAN)
    prompts/               unit.md + consolidate.md (box-agnostic; injected per run)
    dashboard/             host-side live monitor (server.py + index.html; fleet-aware)
    ledger.py roofline.py correctness.py verify.py eval/   <- scoring + the frozen eval funnel
  boxes/<nickname>/      <- created per target box; the live campaign state
    connection.json        how to reach it   | hardware.json  what it is
    campaign.json          the current window | MEMORY.md + ledger.jsonl  the brain + log
```

## Safety posture

Target boxes are disposable salvage. Bricking, SIGILL, and hard hangs are acceptable
outcomes — but a wedged box must auto-recover or checkpoint so the campaign survives
(`doctrine/05_SAFETY_RECOVERY.md`). The host is never the target. Credentials stay
local and gitignored.
