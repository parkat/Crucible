#!/usr/bin/env bash
# crucible run_window.sh — the EXTERNAL relauncher that owns the research loop.
#
# This REPLACES the agent running the loop "in its own head". The agent's volition is
# bounded to ONE unit per launch; this script is the thing that decides to launch the
# next one, watches the clock, and switches to consolidation at wind-down. Moving the
# loop out here means a dead/derailed session can't run the campaign off the rails — the
# loop lives on disk and in this driver, not in the model's context.
#
# Usage:
#   ./scaffold/run_window.sh boxes/<nick>            # continue the current window as-is
#   ./scaffold/run_window.sh boxes/<nick> 24         # RE-ARM a fresh 24h window, then run
#   ./scaffold/run_window.sh boxes/<nick> --dry-run  # print epoch math + the exact claude -p
#   ./scaffold/run_window.sh boxes/<nick> 6 --dry-run
#
# Everything box-specific (host, build dir, lock, wake) is resolved INSIDE the prompts via
# scaffold/boxpaths.py — there is ZERO hardcoded host/build/lock here.
set -u

# ---- run from the crucible root (dir holding scaffold/ + boxes/) --------------
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { echo "run_window: cannot cd to crucible root $ROOT" >&2; exit 1; }

UNIT_PROMPT="scaffold/prompts/unit.md"
CONSOLIDATE_PROMPT="scaffold/prompts/consolidate.md"

# ---- parse args ---------------------------------------------------------------
BOX=""; HOURS=""; DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *[!0-9]*)  [ -z "$BOX" ] && BOX="${a%/}" || { echo "run_window: unexpected arg '$a'" >&2; exit 2; } ;;
    *)         if [ -z "$BOX" ]; then BOX="${a%/}"; elif [ -z "$HOURS" ]; then HOURS="$a"; else echo "run_window: unexpected extra arg '$a'" >&2; exit 2; fi ;;
  esac
done
[ -n "$BOX" ] || { echo "usage: run_window.sh boxes/<nick> [DURATION_HOURS] [--dry-run]" >&2; exit 2; }
[ -d "$BOX" ] || { echo "run_window: box folder not found: $BOX" >&2; exit 2; }
[ -f "$BOX/campaign.json" ] || { echo "run_window: missing $BOX/campaign.json" >&2; exit 2; }
for p in "$UNIT_PROMPT" "$CONSOLIDATE_PROMPT"; do
  [ -f "$p" ] || { echo "run_window: missing prompt $p (preflight should place it)" >&2; exit 2; }
done

# ---- long benches must not be reaped mid-run ----------------------------------
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-21600000}"  # 6h

NOW="$(date +%s)"

# ---- (re)arm the window -------------------------------------------------------
# --dry-run MUST be side-effect-free (bug D): compute the epochs either way so a dry-run can
# preview them, but only WRITE campaign.json on a real run.
if [ -n "$HOURS" ]; then
  START="$NOW"
  DEADLINE="$(( NOW + 10#$HOURS * 3600 ))"     # 10# forces base-10 so HOURS=08/09 isn't invalid octal (#48)
  if [ "$DRY" -eq 0 ]; then
    python3 - "$BOX/campaign.json" "$START" "$DEADLINE" "$HOURS" <<'PY'
import json, sys
path, start, deadline, hours = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
d = json.load(open(path))
d["start_epoch"] = start
d["deadline_epoch"] = deadline          # winddown_margin_frac & all else preserved
d["duration_label"] = f"{hours}hr"      # keep the label in sync with the epochs (bug F)
d["state"] = "running"
json.dump(d, open(path, "w"), indent=2)
PY
    echo "run_window: RE-ARMED $BOX for ${HOURS}h (start=$START deadline=$DEADLINE)"
  else
    echo "run_window: DRY-RUN would RE-ARM $BOX for ${HOURS}h (start=$START deadline=$DEADLINE) — campaign.json NOT written"
  fi
fi

# ---- read the window ----------------------------------------------------------
read -r DISK_START DISK_DEADLINE MARGIN FRONT_STALL_K < <(python3 -c "import json;d=json.load(open('$BOX/campaign.json'));print(d.get('start_epoch') or 0, d.get('deadline_epoch') or 0, d.get('winddown_margin_frac') if d.get('winddown_margin_frac') is not None else 0.10, d.get('front_stall_K') or 0)" 2>/dev/null || echo "ERR")
# A torn/corrupt campaign.json must NOT silently degrade to an open-ended window that never
# consolidates (finding #27) — the unguarded read used to leave DEADLINE empty -> WINDDOWN=0.
if [ "${DISK_START:-ERR}" = "ERR" ] || [ -z "${DISK_START:-}" ]; then
  echo "run_window: cannot parse $BOX/campaign.json (torn/corrupt?) — refusing to start" >&2
  exit 5
fi
# A real re-arm already wrote these (disk == computed). A --dry-run re-arm deliberately did NOT
# write (bug D), so keep the computed would-be epochs from the re-arm block for the preview.
if [ "$DRY" -eq 1 ] && [ -n "$HOURS" ]; then
  :   # keep computed START/DEADLINE
else
  START="$DISK_START"; DEADLINE="$DISK_DEADLINE"
fi

if [ "${DEADLINE:-0}" -eq 0 ]; then
  # open-ended campaign: no deadline -> never auto-winddown; an empty queue triggers a research
  # refill (and only ends if that refill also comes up empty).
  WINDDOWN=0
else
  WINDDOWN="$(python3 -c "print(int($DEADLINE - ($DEADLINE - $START) * $MARGIN))")"
fi

# ---- helper: build the box-injected prompt for `claude -p` ---------------------
render() { sed "s|{{BOX}}|$BOX|g" "$1"; }

# ---- research is gated behind an EMPTY QUEUE (not a stalled front) --------------------
# When the worker signals the takeable queue is empty (it writes the QUEUE_EMPTY sentinel), the
# relauncher spends ONE unit on a research pass that must REFILL the queue with >= REFILL_MIN
# takeable items and then clear the sentinel, after which grinding resumes. If research cannot
# refill, it leaves the sentinel and the loop consolidates. This replaces the old front-stall
# trigger, which churned research while a non-empty queue still had takeable work.
REFILL_MIN="${CRUCIBLE_REFILL_MIN:-5}"

# ---- anti-rabbit-hole: novelty pivot when an avenue goes stale (operator standing policy) ----------
# If the CURRENT avenue of exploration has been ground for >= NOVELTY_PIVOT_K K-items (measured as
# distinct K-ids in ledger notes, else record count) since the last NEW blessed config OR since the
# last pivot — whichever is more recent — the relauncher spends ONE unit ABANDONING that avenue: it
# declares it dead, GARBAGE-COLLECTS its failed on-disk experiments (box + host), then researches and
# refills the queue with a genuinely NOVEL avenue. This keeps one dead-end thread (e.g. a quant-bridge
# bug hunt) from eating an entire window without ever producing a serveable/blessed config. The loop
# focuses serially on one dominant avenue, so "K-items since last win, reset on each deliberate pivot"
# is a robust proxy for "this avenue has been ground N items with nothing to show".
NOVELTY_PIVOT_K="${CRUCIBLE_NOVELTY_PIVOT_K:-100}"

render_research_refill() {
  cat <<EOF
‼ QUEUE EMPTY — RESEARCH-REFILL UNIT (injected by run_window.sh: the takeable queue is empty).
For THIS unit ONLY, run a web-research phase per doctrine/03 to REFILL the queue — do NOT bench:
  - FIRST fold in any operator notes from "${BOX}/STEERING.md" (Inbox) and empty them per the
    steering invariant — a human-pointed front outranks an automated search direction;
  - search the CURRENT ($(date +%Y)) landscape for AVX-less / bandwidth-bound CPU inference, new
    architectures (SSM/RWKV/Mamba/ternary & successors), engine forks, and recent papers;
  - write findings into "${BOX}/MEMORY.md" and refresh its dated landscape snapshot;
  - push **AT LEAST ${REFILL_MIN}** NEW small, takeable, resource-tagged ([BOX]/[HOST]/[EITHER])
    hypotheses onto the queue (a single small takeable item at the TAKEABLE-NOW top) so the next
    units have concrete directions;
  - log a research record to the ledger;
  - **THEN clear the empty-queue sentinel so grinding resumes:** \`rm -f "${BOX}/work/QUEUE_EMPTY"\`.
    ONLY if you genuinely cannot find ${REFILL_MIN} tractable directions, LEAVE the sentinel in
    place — the relauncher will then consolidate and end the campaign.
  Do NOT bench on this unit. Then STOP.

--- the standard unit contract follows (format, gating, recording, resolver use) ---
EOF
  render "$1"
}

# where the driver remembers the ledger length at the last pivot (resets the staleness baseline so a
# fresh avenue gets its own full NOVELTY_PIVOT_K budget before it can be declared a rabbit hole too).
PIVOT_STATE="$BOX/work/last_pivot.json"

# avenue_metrics — echoes "<avenue_len> <total_records> <dominant_model>":
#   avenue_len  = K-items ground on the CURRENT avenue (distinct K-ids in notes since the later of the
#                 last blessed record and the last pivot; falls back to a record count if no K-ids).
#   dominant    = the model most frequently recorded on this avenue (names the thread to declare dead).
# Robust to a missing/empty/torn ledger and a missing pivot-state file (echoes "0 0 ").
avenue_metrics() {
  python3 - "$BOX/ledger.jsonl" "$PIVOT_STATE" <<'PY' 2>/dev/null || echo "0 0 "
import json, sys, re
from collections import Counter
led, pivot_state = sys.argv[1], sys.argv[2]
recs = []
try:
    for ln in open(led, encoding="utf-8", errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except Exception:
            pass
except OSError:
    pass
total = len(recs)
bpos = -1
for i, r in enumerate(recs):
    if r.get("status") == "blessed":
        bpos = i
try:
    last_pivot_len = int(json.load(open(pivot_state)).get("ledger_len_at_pivot", 0))
except Exception:
    last_pivot_len = 0
base = max(bpos + 1, last_pivot_len)
avenue = recs[base:]
kids = set()
for r in avenue:
    for m in re.findall(r"\bK(\d+)\b", r.get("notes", "") or ""):
        kids.add(int(m))
alen = len(kids) if kids else len(avenue)
# name the CURRENT thread from the most recent avenue records (what the loop is stuck on NOW),
# not the whole-avenue mode (which skews to an older sub-thread with more records).
models = Counter((r.get("config") or {}).get("model", "") for r in avenue[-25:] if (r.get("config") or {}).get("model"))
dom = models.most_common(1)[0][0] if models else ""
print(alen, total, dom)
PY
}

render_novelty_pivot() {   # $1 = unit prompt ; $2 = dominant stale model/thread ; $3 = avenue length (K-items)
  cat <<EOF
‼ ANTI-RABBIT-HOLE / NOVELTY PIVOT — injected by run_window.sh (operator standing policy).
The CURRENT avenue of exploration has been ground for **$3 K-items with NO new blessed config**
(threshold ${NOVELTY_PIVOT_K}); its dominant thread is: **${2:-<identify from MEMORY.md "Current phase">}**.
ABANDON this avenue for THIS unit and pivot to a genuinely NOVEL one. Do NOT continue the stale thread;
do NOT bench it. This one unit does, in order:

  1. DECLARE THE STALE AVENUE DEAD. Confirm the dominant avenue above against MEMORY.md's "Current
     phase" + the ledger's recent records. Move its open queue items and its current-phase thread into
     "Tried & ruled out", each with a one-line locate-and-redirect residual, then REMOVE those items
     from the takeable queue so no later unit resumes them.
  2. CLEAN UP ITS FAILED EXPERIMENTS (free disk on BOTH resources). Using MEMORY's tried-and-ruled-out
     list + the ledger's failed/degenerate/couldnt_load records for this avenue, delete the dead
     on-disk artifacts it left behind — staged model files, dequant/bridge scratch, work/ temp — on the
     [HOST] sandbox and, if it holds reclaimable disk, the [BOX] (wake it only if needed; resolve every
     path via scaffold/boxpaths.py, NEVER hardcode a host/path). Report bytes freed per resource.
     HARD SAFETY RAILS — NEVER delete: anything under "$BOX/blessed/", the ledger, MEMORY*.md, git
     history/.git, the hardware/connection/campaign json, or any file MEMORY flags as live/reused
     provenance or a reference for a STILL-OPEN thread. When unsure about a file, LEAVE IT and note it —
     a missed cleanup is cheap; a deleted blessed config or reference is not.
  3. RESEARCH A GENUINELY NOVEL AVENUE (doctrine/03 web-research phase; no benching). It MUST be
     materially different from the avenue you just killed AND from everything already in
     "Tried & ruled out" — a different architecture family / engine / technique, not a re-skin of the
     dead thread. FIRST fold in any operator notes from "$BOX/STEERING.md" (Inbox) and empty them per
     the steering invariant — a human-pointed front outranks an automated search direction. Refresh
     MEMORY's dated landscape snapshot with what you find.
  4. REFILL THE QUEUE with **AT LEAST ${REFILL_MIN}** NEW small, takeable, resource-tagged
     ([BOX]/[HOST]/[EITHER]) hypotheses for the NOVEL avenue — a single small takeable item at the top.
  5. LOG a ledger record for the pivot (a null/contender result is fine — it IS research output: what
     died, what disk was reclaimed, what novel avenue replaces it) and COMMIT the box repo.
  6. CLEAR the empty-queue sentinel if present: \`rm -f "$BOX/work/QUEUE_EMPTY"\`. ONLY if you truly
     cannot find ${REFILL_MIN} tractable NOVEL directions, LEAVE the sentinel in place (\`touch
     "$BOX/work/QUEUE_EMPTY"\`) — the relauncher then consolidates and ends the campaign.
  Do NOT bench on this unit. Then STOP.

--- the standard unit contract follows (format, gating, recording, resolver use) ---
EOF
  render "$1"
}

# ---- session-limit backoff -----------------------------------------------------
# A hit Claude session/usage limit comes back as a ~1s, $0 no-op unit whose result line is
# is_error=true with a "…limit… resets <time>" message. Tight-relaunching then burns hundreds
# of no-op units against the cap (and wastes the window). limit_sleep_secs() inspects a just-
# written unit file and echoes how many seconds to sleep until the reset (0 = not a limit hit,
# so a real unit is never mistaken for one). Parses "resets 6pm"/"resets 6:30am"; falls back to
# 15m if the time is unparseable; clamps to [30s, 6h] so a mis-parse can never wedge the loop.
limit_sleep_secs() {  # $1 = unit output file ; echoes seconds to sleep (0 if not a limit hit)
  python3 - "$1" <<'PY'
import sys, json, re, datetime
try:
    last = None
    with open(sys.argv[1], encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if o.get("type") == "result":
                last = o
    if not last or not last.get("is_error"):
        print(0); raise SystemExit
    msg = last.get("result") or ""
    if "limit" not in msg.lower():
        print(0); raise SystemExit
    now = datetime.datetime.now().astimezone()
    m = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", msg, re.I)
    if m:
        hh = int(m.group(1)) % 12
        if m.group(3).lower() == "pm":
            hh += 12
        target = now.replace(hour=hh, minute=int(m.group(2) or 0), second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)
        secs = int((target - now).total_seconds()) + 20   # small buffer past the reset
    else:
        secs = 900                                        # unparseable -> 15 min
    print(max(30, min(secs, 6 * 3600)))
except SystemExit:
    raise
except Exception:
    print(0)                                              # never wedge the loop
PY
}

# ---- non-limit failure detection + interruptible backoff -----------------------
# unit_is_error: exit 0 (true) if the unit crashed or its result line is is_error — a NON-limit
# failure (auth expiry, bad model id, API 5xx). Lets the loop tell "unit did work" from "unit
# failed" so a persistent instant-failure backs off instead of tight-spinning (finding #1), and
# a crashed research/pivot unit is retried, not misread as "campaign exhausted" (finding #2).
unit_is_error() {  # $1 = unit output file
  python3 - "$1" <<'PY'
import sys, json
last = None
try:
    for ln in open(sys.argv[1], encoding="utf-8", errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try: o = json.loads(ln)
        except Exception: continue
        if o.get("type") == "result":
            last = o
except OSError:
    pass
# error if there is no result line at all (crash/empty) OR it is flagged is_error
raise SystemExit(0 if (last is None or last.get("is_error")) else 1)
PY
}

# _interruptible_sleep: sleep up to $1 seconds but wake early on a STOP flag or a passed deadline,
# so a long backoff never ignores operator control for hours (finding #24).
_interruptible_sleep() {  # $1 = seconds
  local remain="${1:-0}" step dl
  while [ "$remain" -gt 0 ]; do
    [ -f "$STOP_FLAG" ] && return 0
    dl="$(python3 -c "import json;print(json.load(open('$BOX/campaign.json')).get('deadline_epoch') or 0)" 2>/dev/null || echo 0)"
    [ "${dl:-0}" -ne 0 ] && [ "$(date +%s)" -ge "$dl" ] && return 0
    step=10; [ "$remain" -lt 10 ] && step="$remain"
    sleep "$step"; remain=$(( remain - step ))
  done
}

# Units run with --output-format stream-json --verbose so the dashboard can render the
# live transcript (thinking + each tool/SSH command + results) as the unit runs. Output is
# JSONL that grows in real time; the final {"type":"result"} line carries the same summary
# (total_cost_usd, is_error, num_turns) the single-blob `json` format used to emit.
OUTFMT="--output-format stream-json --verbose"

# ---- token-opt: per-unit model routing ----------------------------------------
# Opus only where its intelligence changes the result — a [BOX] measurement bench on an IDLE box.
# Sonnet for research / consolidate / [HOST] recon / staging. Any uncertainty -> Sonnet (cheaper,
# safe): a wrong guess never blocks work, it just costs a bit more or a bit less of the weekly cap.
OPUS="${CRUCIBLE_MODEL_OPUS:-claude-opus-4-8}"
SONNET="${CRUCIBLE_MODEL_SONNET:-claude-sonnet-5}"
pick_model() {  # $1 = research|consolidate|unit ; echoes a model id
  case "$1" in
    research|consolidate) echo "$SONNET"; return ;;
  esac
  python3 - "$BOX" "$OPUS" "$SONNET" <<'PY'
import sys, re, os, subprocess
box, opus, sonnet = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    head = open(os.path.join(box, "MEMORY.md"), encoding="utf-8", errors="ignore").read()
except OSError:
    print(sonnet); raise SystemExit
# Route on the takeable queue top's resource tag. The queue lives under a '### Queue' / '### Open
# hypotheses' header; the FIRST non-closed item's [BOX]/[HOST]/[EITHER] tag decides. The old regex
# looked for a literal '### TAKEABLE NOW' header the format never emits, so it always fell through to
# [HOST]->Sonnet, silently downgrading EVERY [BOX] bench off Opus (finding #34).
tag = "[HOST]"
lines = head.splitlines()
hdr = next((i for i, l in enumerate(lines)
            if re.match(r"^#{2,3}\s+(Queue|Open hypotheses)\b", l.strip(), re.I)), None)
if hdr is not None:
    for l in lines[hdr + 1:]:
        if re.match(r"^#{1,3}\s+", l):                                  # next section -> stop
            break
        if not re.match(r"^\s*(\d+[.)]\s+|[-*]\s+|\*\*\s*→)", l):       # not an item line
            continue
        if re.search(r"\b(CLOSED|DONE|NO-GO|REFUTED|RETRACTED)\b", l):  # skip closed items
            continue
        mm = re.search(r"(\[BOX\]|\[HOST\]|\[EITHER\])", l, re.I)
        if mm:
            tag = mm.group(1).upper()
        break                                                          # first non-closed item decides
if tag != "[BOX]":
    print(sonnet); raise SystemExit           # not a [BOX] bench -> Sonnet
# a [BOX] bench earns Opus only if the box is actually idle; probe best-effort, fail -> sonnet.
idle = False
try:
    ssh = subprocess.check_output(["python3", "scaffold/boxpaths.py", box, "--ssh"],
                                  text=True, timeout=15).split()
    free = subprocess.run(ssh + ["nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=20)
    srv = subprocess.run(ssh + ["pgrep -x llama-server"], capture_output=True, text=True, timeout=20)
    tok = free.stdout.strip().split()
    idle = free.returncode == 0 and tok and int(tok[0]) >= 2400 and srv.returncode != 0
except Exception:
    idle = False
print(opus if idle else sonnet)
PY
}

claude_cmd() {  # $1 = prompt file ; $2 = model ; echoes the exact argv (for --dry-run + logging)
  printf 'claude -p "<%s, {{BOX}}=%s>" --model %s --dangerously-skip-permissions %s' \
    "$(basename "$1")" "$BOX" "${2:-$OPUS}" "$OUTFMT"
}

SENTINEL="$BOX/work/QUEUE_EMPTY"
LOGDIR="$BOX/work/run_window"; LOG="$BOX/work/run_window.log"
STOP_FLAG="$BOX/work/STOP"; CURPID_FILE="$BOX/work/current_unit.pid"
STOP_REQUESTED=0

# run ONE unit: read the rendered prompt on stdin, background `claude` so its PID is recorded to
# work/current_unit.pid (window.py's quick-stop kills that PID to halt fast), then wait + clean up.
# Returns claude's exit code so each caller's `|| echo ...exited non-zero` still fires.
run_unit() {   # $1 = model id ; stdin = rendered prompt
  claude -p "$(cat)" --model "$1" --dangerously-skip-permissions $OUTFMT > "$UNIT_FILE" 2>>"$LOG" &
  UNIT_PID=$!
  echo "$UNIT_PID" > "$CURPID_FILE"
  wait "$UNIT_PID"; local rc=$?
  rm -f "$CURPID_FILE"
  return $rc
}

# ---- dry-run: show the math + the exact invocation, then exit -----------------
if [ "$DRY" -eq 1 ]; then
  echo   "run_window DRY-RUN — box=$BOX"
  echo   "  now           = $NOW"
  echo   "  start_epoch   = $START"
  echo   "  deadline_epoch= $DEADLINE"
  echo   "  winddown_frac = $MARGIN"
  echo   "  winddown_epoch= $WINDDOWN   (= deadline - (deadline-start)*frac)"
  if [ "$WINDDOWN" -ne 0 ] && [ "$NOW" -ge "$WINDDOWN" ]; then
    echo "  phase         = WINDDOWN reached -> would run consolidate ONCE then exit"
    echo "  would invoke  : $(claude_cmd "$CONSOLIDATE_PROMPT" "$(pick_model consolidate)")"
  else
    [ "$DEADLINE" -ne 0 ] && echo "  seconds to winddown = $(( WINDDOWN - NOW ))"
    echo "  phase         = RUNNING -> grind the queue until winddown; empty queue -> research refill (>= $REFILL_MIN)"
    echo "  model routing = research/consolidate=$SONNET ; [BOX]-idle bench=$OPUS ; else=$SONNET"
    echo "  would invoke  : $(claude_cmd "$UNIT_PROMPT" "$(pick_model unit)")"
    echo "  sentinel      = $SENTINEL (empty queue -> research refill >= $REFILL_MIN, not early-exit)"
    read -r DR_AVLEN DR_TOT DR_DOM < <(avenue_metrics)
    echo "  novelty-pivot = current avenue $DR_AVLEN K-items since last blessed/pivot (dom='$DR_DOM'); threshold $NOVELTY_PIVOT_K"
    if [ "${DR_AVLEN:-0}" -ge "$NOVELTY_PIVOT_K" ]; then
      echo "                  -> NEXT UNIT WOULD PIVOT: abandon '${DR_DOM:-current avenue}', GC its dead experiments, research+refill a NOVEL avenue"
    fi
  fi
  echo   "  bg wait ceiling (ms) = $CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
  exit 0
fi

# ---- the loop -----------------------------------------------------------------
command -v claude >/dev/null 2>&1 || { echo "run_window: 'claude' CLI not on PATH" >&2; exit 1; }
mkdir -p "$LOGDIR"
# refuse to start a SECOND driver on the same box (two loops would race the queue/ledger/box
# lock and duplicate multi-minute benches, finding #25). A stale pidfile — dead PID, or a reused
# PID that isn't a run_window process — is ignored.
if [ -f "$BOX/work/driver.pid" ]; then
  OLD_PID="$(cat "$BOX/work/driver.pid" 2>/dev/null || echo "")"
  if [ -n "$OLD_PID" ] && [ "$OLD_PID" != "$$" ] && kill -0 "$OLD_PID" 2>/dev/null \
     && grep -qa run_window "/proc/$OLD_PID/cmdline" 2>/dev/null; then
    echo "run_window: a live driver (pid $OLD_PID) already owns $BOX — refusing to start a second loop" >&2
    exit 4
  fi
fi
echo "$$" > "$BOX/work/driver.pid"   # so window.py hard-kill can find the driver
# clear STALE control flags left by a previous (now-dead) driver: a lingering STOP would
# instantly no-op this fresh window (units=0), a lingering PAUSE would hang it (finding #3).
rm -f "$STOP_FLAG" "$BOX/work/PAUSE"
echo "run_window: $(date -Is) START box=$BOX winddown=$WINDDOWN deadline=$DEADLINE" >> "$LOG"

UNIT=0
FAIL_STREAK=0
REPEATED_FAILURE=0
MAX_FAIL_STREAK="${CRUCIBLE_MAX_FAIL_STREAK:-6}"
while : ; do
  NOW="$(date +%s)"
  # operator/agent quick-stop: window.py drops work/STOP -> halt now, but STILL consolidate + write
  # the FINAL report (we break to the same consolidate block a natural winddown uses).
  if [ -f "$STOP_FLAG" ]; then
    echo "run_window: $(date -Is) STOP requested (window.py) -> consolidate + final report, then exit" | tee -a "$LOG"
    rm -f "$STOP_FLAG"; STOP_REQUESTED=1
    break
  fi
  # operator PAUSE (window.py): hold here without launching a unit (no cap spent) until resumed. A
  # STOP during pause still wins (breaks to consolidate on the next top-of-loop check).
  if [ -f "$BOX/work/PAUSE" ]; then
    echo "run_window: $(date -Is) PAUSED (window.py) -> waiting for resume (or STOP)" | tee -a "$LOG"
    while [ -f "$BOX/work/PAUSE" ] && [ ! -f "$STOP_FLAG" ]; do sleep 10; done
    echo "run_window: $(date -Is) RESUMED" | tee -a "$LOG"
    continue
  fi
  # live clock: re-read the deadline every iteration so window.py add-hours/remove-time takes effect
  # mid-run (START is fixed for the window; only DEADLINE/WINDDOWN move).
  read -r DEADLINE MARGIN < <(python3 -c "import json;d=json.load(open('$BOX/campaign.json'));print(d.get('deadline_epoch') or 0, d.get('winddown_margin_frac') if d.get('winddown_margin_frac') is not None else 0.10)" 2>/dev/null || echo "$DEADLINE $MARGIN")
  if [ "${DEADLINE:-0}" -eq 0 ]; then WINDDOWN=0; else WINDDOWN="$(python3 -c "print(int($DEADLINE - ($DEADLINE - $START) * $MARGIN))" 2>/dev/null || echo "$WINDDOWN")"; fi
  if [ "$WINDDOWN" -ne 0 ] && [ "$NOW" -ge "$WINDDOWN" ]; then
    echo "run_window: $(date -Is) WINDDOWN reached -> consolidate" | tee -a "$LOG"
    break
  fi
  UNIT=$(( UNIT + 1 ))
  UNIT_FILE="$LOGDIR/unit_$(date +%s)_$UNIT.jsonl"
  # Unit routing, in PRECEDENCE order:
  #   (1) NOVELTY PIVOT — the current avenue has been ground >= NOVELTY_PIVOT_K K-items with no new
  #       blessed config: abandon it, GC its dead experiments, research+refill a novel avenue. This
  #       OUTRANKS grinding and the empty-queue refill, because a stale avenue can still have a full
  #       (but dead-end) queue — that is exactly the rabbit hole this guards against.
  #   (2) RESEARCH REFILL — the queue emptied (QUEUE_EMPTY sentinel): refill >= REFILL_MIN and clear it.
  #   (3) GRIND — take the queue top.
  THIS_RESEARCH=0; THIS_PIVOT=0
  read -r AVENUE_LEN TOTAL_REC DOM_MODEL < <(avenue_metrics)
  if [ "${AVENUE_LEN:-0}" -ge "$NOVELTY_PIVOT_K" ]; then
    THIS_PIVOT=1
    MODEL="$(pick_model research)"        # a research/cleanup unit, not a bench -> research-class model
    echo "run_window: $(date -Is) launch unit #$UNIT [NOVELTY PIVOT — avenue stale ${AVENUE_LEN} K-items >= $NOVELTY_PIVOT_K w/o new blessed; dom='${DOM_MODEL}'] [model=$MODEL]" | tee -a "$LOG"
    render_novelty_pivot "$UNIT_PROMPT" "$DOM_MODEL" "$AVENUE_LEN" | run_unit "$MODEL" \
      || echo "run_window: $(date -Is) unit #$UNIT (novelty-pivot) exited non-zero (logged; continuing)" | tee -a "$LOG"
    # reset the staleness baseline to now so the FRESH avenue gets its own full NOVELTY_PIVOT_K budget
    # before it too can be declared a rabbit hole (prevents re-pivoting every unit while it ramps).
    python3 -c "import json,sys;open('$PIVOT_STATE','w').write(json.dumps({'ledger_len_at_pivot': ${TOTAL_REC:-0}}))" 2>>"$LOG" || true
  elif [ -f "$SENTINEL" ]; then
    THIS_RESEARCH=1
    MODEL="$(pick_model research)"
    echo "run_window: $(date -Is) launch unit #$UNIT [RESEARCH — queue empty, refill >= $REFILL_MIN] [model=$MODEL]" | tee -a "$LOG"
    render_research_refill "$UNIT_PROMPT" | run_unit "$MODEL" \
      || echo "run_window: $(date -Is) unit #$UNIT (research) exited non-zero (logged; continuing)" | tee -a "$LOG"
  else
    MODEL="$(pick_model unit)"
    echo "run_window: $(date -Is) launch unit #$UNIT [model=$MODEL]" | tee -a "$LOG"
    render "$UNIT_PROMPT" | run_unit "$MODEL" \
      || echo "run_window: $(date -Is) unit #$UNIT exited non-zero (logged; continuing)" | tee -a "$LOG"
  fi
  # session-limit backoff: if this unit was a limit no-op, drop it (don't count it) and sleep
  # until the reset instead of tight-relaunching against the cap.
  SLEEP="$(limit_sleep_secs "$UNIT_FILE")"
  if [ "${SLEEP:-0}" -gt 0 ]; then
    echo "run_window: $(date -Is) SESSION LIMIT on unit #$UNIT -> back off ${SLEEP}s (until reset); no-op dropped, not counted" | tee -a "$LOG"
    rm -f "$UNIT_FILE"
    UNIT=$(( UNIT - 1 ))
    _interruptible_sleep "$SLEEP"     # wake early on STOP / deadline (finding #24)
    continue
  fi
  # non-limit failure handling (findings #1/#2): a unit that crashed or returned is_error (auth
  # expiry, bad model id, API 5xx) did NO work and returns in ~1s; tight-relaunching it spins the
  # whole window at max rate against the cap. Count consecutive failures, back off exponentially,
  # and abort after a hard cap instead of spinning forever.
  if unit_is_error "$UNIT_FILE"; then
    FAIL_STREAK=$(( FAIL_STREAK + 1 ))
    if [ "$FAIL_STREAK" -ge "$MAX_FAIL_STREAK" ]; then
      echo "run_window: $(date -Is) ABORT after $FAIL_STREAK consecutive unit failures (auth/API/config?) — stopping so a broken loop can't burn the window on a zero-work spin" | tee -a "$LOG"
      REPEATED_FAILURE=1
      break
    fi
    BACKOFF=$(( 15 * (1 << (FAIL_STREAK - 1)) )); [ "$BACKOFF" -gt 300 ] && BACKOFF=300
    echo "run_window: $(date -Is) unit #$UNIT failed (streak $FAIL_STREAK/$MAX_FAIL_STREAK) -> backoff ${BACKOFF}s" | tee -a "$LOG"
    _interruptible_sleep "$BACKOFF"
    continue     # retry; a crashed research/pivot unit must NOT fall through to the exhaustion guard (#2)
  fi
  FAIL_STREAK=0
  # exhaustion guard: only a SUCCESSFUL research/pivot unit that STILL left the sentinel means the
  # campaign is genuinely out of tractable directions (a crashed one was retried above, #2).
  if { [ "$THIS_RESEARCH" -eq 1 ] || [ "$THIS_PIVOT" -eq 1 ]; } && [ -f "$SENTINEL" ]; then
    echo "run_window: $(date -Is) research/pivot refill left the queue empty -> consolidate (campaign exhausted)" | tee -a "$LOG"
    break
  fi
done

# ---- consolidate ONCE, then exit ----------------------------------------------
if [ "${REPEATED_FAILURE:-0}" -eq 1 ]; then
  # claude itself is failing (auth/API/config) — a consolidate unit would just fail too. Skip it,
  # mark the campaign failed, and exit so a human can investigate rather than spinning (finding #1).
  echo "run_window: $(date -Is) skipping consolidate (repeated unit failures) — state -> failed" | tee -a "$LOG"
  FINAL_STATE="failed"
else
  MODEL="$(pick_model consolidate)"
  echo "run_window: $(date -Is) consolidate [model=$MODEL]" | tee -a "$LOG"
  render "$CONSOLIDATE_PROMPT" | claude -p "$(cat)" --model "$MODEL" --dangerously-skip-permissions $OUTFMT \
    > "$LOGDIR/consolidate_$(date +%s).jsonl" 2>>"$LOG" \
    || echo "run_window: $(date -Is) consolidate exited non-zero (logged)" | tee -a "$LOG"
  FINAL_STATE="completed"; [ "${STOP_REQUESTED:-0}" -eq 1 ] && FINAL_STATE="stopped"
fi
python3 -c "import json;p='$BOX/campaign.json';d=json.load(open(p));d['state']='$FINAL_STATE';json.dump(d,open(p,'w'),indent=2)"
rm -f "$SENTINEL" "$CURPID_FILE" "$STOP_FLAG" "$BOX/work/driver.pid" "$BOX/work/PAUSE"
# render the just-sealed FINAL_*.md report(s) to PDF so the dashboard panel is ready immediately
python3 scaffold/dashboard/report_pdf.py "$BOX" >/dev/null 2>&1 || true
echo "run_window: $(date -Is) DONE box=$BOX units=$UNIT" | tee -a "$LOG"
