# startup.md — ENTRY POINT (interactive setup wizard)

This is the front door. Drop the crucible files into one folder, open a Claude Code session
**in that folder**, and say:

> follow startup.md

The session then runs the **setup wizard** below — it asks you for everything it needs
(target box, autonomy tier, campaign length), wires up the box, launches the dashboard, and
hands the research loop to the external relauncher. You don't pre-edit this file.

> **If you cloned the repo from GitHub:** copy this template first — `cp startup.template.md
> startup.md` — then say "follow startup.md". (The downloadable release zip already includes a
> ready `startup.md`.) Either way the wizard is identical.

---

## FOR THE CLAUDE SESSION — run this in order

### Step 0 · Verify & repair the file structure

Files may have been dumped flat, mis-nested, or already structured. Locate `preflight.py` (it
may be at the folder root, or at `scaffold/preflight.py` if already structured) and run it from
inside the crucible folder:

```bash
python3 preflight.py .          # or: python3 scaffold/preflight.py .
```

It rebuilds the canonical tree (moving misplaced files into `doctrine/`, `scaffold/`,
`scaffold/eval/assets/`, `templates/`, `scaffold/dashboard/`), disambiguating same-named files
by content. If it prints **CRITICAL MISSING**, stop and ask the human to supply those files. If
it prints **structure OK**, proceed. (Seed eval assets, if missing, are regenerable from
`doctrine/02` + `scaffold/eval/assets/README.md` — note them and move on.)

### Step 1 · Load the constitution

Read every file in `doctrine/` before doing anything else. `00_PRIME_DIRECTIVE.md` governs all
novel situations; `05_SAFETY_RECOVERY.md` governs the credential handling in Step 3.

### Step 2 · Run the pickers (ask the human — do not guess)

Collect the campaign settings **interactively** (use the session's question UI; ask in one or a
few prompts). Don't write any of these into a file yet:

1. **Target box — how to reach it** (the machine that will be benchmarked):
   - `TARGET_IP` — host/IP of the target box
   - `SSH_USER` — the SSH username
   - `SSH_PASS` — the SSH password, used **once** to install a key, then discarded (Step 3).
     **Never write this to disk or commit it.** If the human prefers, they can install the key
     themselves and you skip the password entirely.
   - `NICKNAME` — a short name for this box (e.g. `westmere-01`); becomes `boxes/<NICKNAME>/`.
   - `LAN_MODEL_STORE` *(optional)* — a LAN path/host where large models are staged, to avoid
     re-downloading. Blank = fetch over the internet as needed.

2. **Autonomy tier** (default **T4**; see `doctrine/04`):
   - **T1 Conservative** — config-space search only, no code edits
   - **T2 Standard** — + compile/flag tuning + engine-fork swaps
   - **T3 Aggressive** — + kernel writing (correctness-gated) + spec-decode + arch swaps
   - **T4 Unleashed** — + harness self-modification + green-field engines *(default)*

3. **Campaign length** — `1h` · `6h` · `1day` · `3day` *(default)* · `open` (continuous, no
   deadline; ends only when the queue empties).

### Step 3 · Establish SSH + install a dedicated key

Ensure `sshpass` and `ssh-keygen` exist on the host (install `sshpass` if missing). Generate a
dedicated keypair and install the public key on the target using the password **once**:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/crucible_<NICKNAME>
sshpass -p "$SSH_PASS" ssh-copy-id -i ~/.ssh/crucible_<NICKNAME>.pub \
    -o StrictHostKeyChecking=accept-new "$SSH_USER@$TARGET_IP"
```

Then grant passwordless sudo on the target (the one-time use of the password):

```bash
sshpass -p "$SSH_PASS" ssh "$SSH_USER@$TARGET_IP" \
  "echo '$SSH_PASS' | sudo -S sh -c 'echo \"$SSH_USER ALL=(ALL) NOPASSWD:ALL\" > /etc/sudoers.d/crucible && chmod 440 /etc/sudoers.d/crucible'"
```

From here on, connect with the **key** only — `ssh -i ~/.ssh/crucible_<NICKNAME>
"$SSH_USER@$TARGET_IP"`. **Discard the plaintext password; never persist it anywhere**
(`doctrine/05`).

### Step 4 · Write the connection contract

Write `boxes/<NICKNAME>/connection.json` — HOW TO REACH the box, so no prompt or script ever
hardcodes a host, key, or remote path:

```json
{
  "host": "<TARGET_IP>", "user": "<SSH_USER>",
  "ssh_key_path": "~/.ssh/crucible_<NICKNAME>", "ssh_opts": "-o IdentitiesOnly=yes",
  "remote_root": "~/crucible", "remote_engine_dir": "~/crucible/engines/llama.cpp"
}
```

The password is **not** written here. Verify the resolver reads it:
`python3 scaffold/boxpaths.py boxes/<NICKNAME> --ssh` must print your keyed SSH line.
connection.json is the first leg of the **contract trio** (connection = how to reach /
hardware = what it is / campaign = the current window) that `scaffold/boxpaths.py` resolves for
every box operation.

### Step 5 · Scan the hardware

Copy `scaffold/hardware_scan.sh` to the target, run it, and save its JSON to
`boxes/<NICKNAME>/hardware.json`. This drives the ISA guard and every roofline (`doctrine/01`,
`05`). If bandwidth couldn't be measured (no compiler on the target), that's a logged
"unknown — needs probe", not a failure.

### Step 6 · Scaffold the box folder from `templates/`

```
boxes/<NICKNAME>/
  MEMORY.md          <- from templates/MEMORY.template.md
  campaign.json      <- from templates/campaign.template.json; start_epoch=`date +%s`,
                        deadline_epoch from the chosen length (null if open)
  STEERING.md        <- from templates/STEERING.template.md (operator steering inbox)
  connection.json    <- Step 4
  hardware.json      <- Step 5
  ledger.jsonl       <- empty
  GATE_QUEUE.md      <- from templates/GATE_QUEUE.template.md (header only)
  blessed/           <- empty
  engines/           <- empty (forks/source under test; git-tracked)
  work/              <- scratch (gitignored)
  reports/           <- empty
  .gitignore         <- from templates/gitignore
```

Init the box folder as its own git repo (`doctrine/04` — git is the undo for T4 self-mod) and
make the first commit (`scaffold <NICKNAME>`).

### Step 7 · Stand up the dashboard (on the host, never the target)

```bash
python3 scaffold/dashboard/server.py boxes/<NICKNAME>     # one box
python3 scaffold/dashboard/server.py boxes                # whole fleet, one panel per box
```

Report the printed local URL (default `http://127.0.0.1:8787/`).

### Step 8 · Seed MEMORY, then launch the external loop

Run the opening **web-research phase** (`doctrine/03`) and seed `boxes/<NICKNAME>/MEMORY.md`
with findings + an initial, takeable, resource-tagged (`[BOX]`/`[HOST]`/`[EITHER]`) hypothesis
queue (the dated landscape snapshot in `03` is your starting prior — refresh it first).
Suggested openers: `ik_llama.cpp` + a small-active MoE + speculative decoding (the three moves
that shift the bandwidth wall). Confirm `campaign.json` has the `start_epoch`/`deadline_epoch`
from the picker. Then **hand the loop to the external relauncher** (the session no longer runs
the loop itself):

```bash
./scaffold/run_window.sh boxes/<NICKNAME>
```

It drives bounded units → winddown → consolidate → exit, reading the clock from `campaign.json`
and every box detail from the resolver. To re-arm a fresh window later, pass hours:
`./scaffold/run_window.sh boxes/<NICKNAME> 24`.

### Step 9 · Announce readiness

Report: the scanned hardware summary (ISA flags + measured bandwidth), the chosen tier and
deadline, the dashboard URL, and the first few hypotheses the loop will test.

---

## Steering it later (from anywhere, even your phone)

Drop a research direction into the box's inbox and the running worker picks it up at the start
of its next unit (see `README.md` → "Steering a live campaign"):

```bash
python3 scaffold/steer.py boxes/<NICKNAME> "look into Mamba-2 SSD CPU-decode kernels" --tag HOST --research
```

## To resume later

Point a fresh session at the box folder and say **"resume this campaign"**. It reloads doctrine,
reads `MEMORY.md`, tails `ledger.jsonl`, checks the clock, and continues (`doctrine/06` resume
protocol). You do **not** re-run this wizard on resume.
