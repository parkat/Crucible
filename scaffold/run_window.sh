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
    *)         if [ -z "$BOX" ]; then BOX="${a%/}"; else HOURS="$a"; fi ;;
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
  DEADLINE="$(( NOW + HOURS * 3600 ))"
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
read -r DISK_START DISK_DEADLINE MARGIN FRONT_STALL_K < <(python3 -c "import json;d=json.load(open('$BOX/campaign.json'));print(d.get('start_epoch') or 0, d.get('deadline_epoch') or 0, d.get('winddown_margin_frac') if d.get('winddown_margin_frac') is not None else 0.10, d.get('front_stall_K') or 0)")
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
m = re.search(r"###\s*TAKEABLE NOW.*?(\[BOX\]|\[HOST\]|\[EITHER\])", head, re.S | re.I)
if (m.group(1).upper() if m else "[HOST]") != "[BOX]":
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
  fi
  echo   "  bg wait ceiling (ms) = $CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
  exit 0
fi

# ---- the loop -----------------------------------------------------------------
command -v claude >/dev/null 2>&1 || { echo "run_window: 'claude' CLI not on PATH" >&2; exit 1; }
mkdir -p "$LOGDIR"
echo "run_window: $(date -Is) START box=$BOX winddown=$WINDDOWN deadline=$DEADLINE" >> "$LOG"

UNIT=0
while : ; do
  NOW="$(date +%s)"
  if [ "$WINDDOWN" -ne 0 ] && [ "$NOW" -ge "$WINDDOWN" ]; then
    echo "run_window: $(date -Is) WINDDOWN reached -> consolidate" | tee -a "$LOG"
    break
  fi
  UNIT=$(( UNIT + 1 ))
  UNIT_FILE="$LOGDIR/unit_$(date +%s)_$UNIT.jsonl"
  # research is gated behind an EMPTY QUEUE: the worker writes the QUEUE_EMPTY sentinel when the
  # takeable queue is empty; that unit is spent refilling it (>= REFILL_MIN items) and clearing
  # the sentinel. Otherwise, grind the queue top.
  if [ -f "$SENTINEL" ]; then
    THIS_RESEARCH=1
    MODEL="$(pick_model research)"
    echo "run_window: $(date -Is) launch unit #$UNIT [RESEARCH — queue empty, refill >= $REFILL_MIN] [model=$MODEL]" | tee -a "$LOG"
    render_research_refill "$UNIT_PROMPT" | claude -p "$(cat)" --model "$MODEL" --dangerously-skip-permissions $OUTFMT \
      > "$UNIT_FILE" 2>>"$LOG" \
      || echo "run_window: $(date -Is) unit #$UNIT (research) exited non-zero (logged; continuing)" | tee -a "$LOG"
  else
    THIS_RESEARCH=0
    MODEL="$(pick_model unit)"
    echo "run_window: $(date -Is) launch unit #$UNIT [model=$MODEL]" | tee -a "$LOG"
    render "$UNIT_PROMPT" | claude -p "$(cat)" --model "$MODEL" --dangerously-skip-permissions $OUTFMT \
      > "$UNIT_FILE" 2>>"$LOG" \
      || echo "run_window: $(date -Is) unit #$UNIT exited non-zero (logged; continuing)" | tee -a "$LOG"
  fi
  # session-limit backoff: if this unit was a limit no-op, drop it (don't count it) and sleep
  # until the reset instead of tight-relaunching against the cap.
  SLEEP="$(limit_sleep_secs "$UNIT_FILE")"
  if [ "${SLEEP:-0}" -gt 0 ]; then
    echo "run_window: $(date -Is) SESSION LIMIT on unit #$UNIT -> back off ${SLEEP}s (until reset); no-op dropped, not counted" | tee -a "$LOG"
    rm -f "$UNIT_FILE"
    UNIT=$(( UNIT - 1 ))
    sleep "$SLEEP"
    continue
  fi
  # empty-queue exhaustion guard: a research-refill unit MUST push >= REFILL_MIN items and clear
  # the sentinel. If it ran (not a limit no-op) yet the sentinel is STILL present, the campaign is
  # genuinely out of tractable directions -> consolidate.
  if [ "$THIS_RESEARCH" -eq 1 ] && [ -f "$SENTINEL" ]; then
    echo "run_window: $(date -Is) research refill left the queue empty -> consolidate (campaign exhausted)" | tee -a "$LOG"
    break
  fi
done

# ---- consolidate ONCE, then exit ----------------------------------------------
MODEL="$(pick_model consolidate)"
echo "run_window: $(date -Is) consolidate [model=$MODEL]" | tee -a "$LOG"
render "$CONSOLIDATE_PROMPT" | claude -p "$(cat)" --model "$MODEL" --dangerously-skip-permissions $OUTFMT \
  > "$LOGDIR/consolidate_$(date +%s).jsonl" 2>>"$LOG" \
  || echo "run_window: $(date -Is) consolidate exited non-zero (logged)" | tee -a "$LOG"
python3 -c "import json;p='$BOX/campaign.json';d=json.load(open(p));d['state']='completed';json.dump(d,open(p,'w'),indent=2)"
rm -f "$SENTINEL"
# render the just-sealed FINAL_*.md report(s) to PDF so the dashboard panel is ready immediately
python3 scaffold/dashboard/report_pdf.py "$BOX" >/dev/null 2>&1 || true
echo "run_window: $(date -Is) DONE box=$BOX units=$UNIT" | tee -a "$LOG"
