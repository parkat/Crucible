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
  [ -f "$p" ] || { echo "run_window: missing prompt $p (Task 5 / preflight should place it)" >&2; exit 2; }
done

# ---- long benches must not be reaped mid-run ----------------------------------
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-21600000}"  # 6h

NOW="$(date +%s)"

# ---- (re)arm the window -------------------------------------------------------
if [ -n "$HOURS" ]; then
  START="$NOW"
  DEADLINE="$(( NOW + HOURS * 3600 ))"
  python3 - "$BOX/campaign.json" "$START" "$DEADLINE" <<'PY'
import json, sys
path, start, deadline = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
d = json.load(open(path))
d["start_epoch"] = start
d["deadline_epoch"] = deadline          # winddown_margin_frac & all else preserved
d["state"] = "running"
json.dump(d, open(path, "w"), indent=2)
PY
  echo "run_window: RE-ARMED $BOX for ${HOURS}h (start=$START deadline=$DEADLINE)"
fi

# ---- read the window ----------------------------------------------------------
read -r START DEADLINE MARGIN FRONT_STALL_K < <(python3 -c "import json;d=json.load(open('$BOX/campaign.json'));print(d.get('start_epoch') or 0, d.get('deadline_epoch') or 0, d.get('winddown_margin_frac') if d.get('winddown_margin_frac') is not None else 0.10, d.get('front_stall_K') or 0)")

if [ "${DEADLINE:-0}" -eq 0 ]; then
  # open-ended campaign: no deadline -> never auto-winddown; QUEUE_EMPTY still ends it.
  WINDDOWN=0
else
  WINDDOWN="$(python3 -c "print(int($DEADLINE - ($DEADLINE - $START) * $MARGIN))")"
fi

# ---- helper: build the box-injected prompt for `claude -p` ---------------------
render() { sed "s|{{BOX}}|$BOX|g" "$1"; }

# ---- stall trigger: turn doctrine/03's "research whenever the front stalls" into real
#      automation. front_stalled() echoes 1 when the Pareto front has not gained ground in
#      >= FRONT_STALL_K *measured* records (research/finding rows don't count, so a research
#      unit can't keep re-triggering itself). Anything unexpected -> 0 (never wedge the loop).
front_stalled() {
  [ "${FRONT_STALL_K:-0}" -gt 0 ] || { echo 0; return; }
  python3 - "$BOX/ledger.jsonl" "$FRONT_STALL_K" <<'PY'
import sys
sys.path.insert(0, "scaffold")
try:
    import ledger
    path, K = sys.argv[1], int(sys.argv[2])
    recs = ledger.load(path)
    front = ledger.pareto_front(recs)
    if not front:
        print(0); raise SystemExit          # no front yet -> still building, not stalled
    fids = {id(f) for f in front}
    last = max((i for i, r in enumerate(recs) if id(r) in fids), default=-1)
    since = sum(1 for r in recs[last + 1:]
                if r.get("decode_tok_s") is not None or r.get("prefill_tok_s") is not None)
    print(1 if since >= K else 0)
except SystemExit:
    raise
except Exception:
    print(0)
PY
}

# When stalled, the next unit is spent on a web-research phase instead of grinding the stale
# queue: prepend a directive, then the standard unit contract (for gating/recording/resolver).
render_research() {
  cat <<EOF
‼ FRONT STALLED — RESEARCH UNIT (injected by run_window.sh: the Pareto front has not improved
in >= ${FRONT_STALL_K} measured records). For THIS unit ONLY, run a web-research phase per
doctrine/03 instead of taking the stale queue top:
  - FIRST fold in any operator notes from "${BOX}/STEERING.md" (Inbox) and empty them per the
    steering invariant — a human-pointed front outranks an automated search direction;
  - search the CURRENT ($(date +%Y)) landscape for AVX-less / bandwidth-bound CPU inference,
    new architectures (SSM/RWKV/Mamba & successors), relevant engine forks, and recent papers;
  - write findings into "${BOX}/MEMORY.md" and refresh its dated landscape snapshot;
  - push 1–3 NEW small, takeable, resource-tagged ([BOX]/[HOST]/[EITHER]) hypotheses to the TOP
    of the queue so the next units have fresh directions;
  - log a research record to the ledger. Do NOT bench on this unit. Then STOP.

--- the standard unit contract follows (format, gating, recording, resolver use) ---
EOF
  render "$1"
}

# Units run with --output-format stream-json --verbose so the dashboard can render the
# live transcript (thinking + each tool/SSH command + results) as the unit runs. Output is
# JSONL that grows in real time; the final {"type":"result"} line carries the same summary
# (total_cost_usd, is_error, num_turns) the single-blob `json` format used to emit.
OUTFMT="--output-format stream-json --verbose"

claude_cmd() {  # $1 = prompt file ; echoes the exact argv (for --dry-run + logging)
  printf 'claude -p "<%s, {{BOX}}=%s>" --dangerously-skip-permissions %s' \
    "$(basename "$1")" "$BOX" "$OUTFMT"
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
    echo "  would invoke  : $(claude_cmd "$CONSOLIDATE_PROMPT")"
  else
    [ "$DEADLINE" -ne 0 ] && echo "  seconds to winddown = $(( WINDDOWN - NOW ))"
    echo "  phase         = RUNNING -> would loop bounded units until winddown/QUEUE_EMPTY"
    echo "  would invoke  : $(claude_cmd "$UNIT_PROMPT")"
    echo "  sentinel      = $SENTINEL (early -> consolidate)"
  fi
  echo   "  bg wait ceiling (ms) = $CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
  exit 0
fi

# ---- the loop -----------------------------------------------------------------
command -v claude >/dev/null 2>&1 || { echo "run_window: 'claude' CLI not on PATH" >&2; exit 1; }
mkdir -p "$LOGDIR"
echo "run_window: $(date -Is) START box=$BOX winddown=$WINDDOWN deadline=$DEADLINE" >> "$LOG"

UNIT=0; LAST_RESEARCH=0
while : ; do
  NOW="$(date +%s)"
  if [ "$WINDDOWN" -ne 0 ] && [ "$NOW" -ge "$WINDDOWN" ]; then
    echo "run_window: $(date -Is) WINDDOWN reached -> consolidate" | tee -a "$LOG"
    break
  fi
  if [ -f "$SENTINEL" ]; then
    echo "run_window: $(date -Is) QUEUE_EMPTY sentinel -> consolidate early" | tee -a "$LOG"
    break
  fi
  UNIT=$(( UNIT + 1 ))
  # stall trigger: spend this unit on research when the front has stalled — but never two in a
  # row, so the research output gets a normal unit to act on it before we research again.
  if [ "$LAST_RESEARCH" -eq 0 ] && [ "$(front_stalled)" = "1" ]; then
    echo "run_window: $(date -Is) launch unit #$UNIT [RESEARCH — front stalled >= $FRONT_STALL_K measured records]" | tee -a "$LOG"
    render_research "$UNIT_PROMPT" | claude -p "$(cat)" --dangerously-skip-permissions $OUTFMT \
      > "$LOGDIR/unit_$(date +%s)_$UNIT.jsonl" 2>>"$LOG" \
      || echo "run_window: $(date -Is) unit #$UNIT (research) exited non-zero (logged; continuing)" | tee -a "$LOG"
    LAST_RESEARCH=1
  else
    echo "run_window: $(date -Is) launch unit #$UNIT" | tee -a "$LOG"
    render "$UNIT_PROMPT" | claude -p "$(cat)" --dangerously-skip-permissions $OUTFMT \
      > "$LOGDIR/unit_$(date +%s)_$UNIT.jsonl" 2>>"$LOG" \
      || echo "run_window: $(date -Is) unit #$UNIT exited non-zero (logged; continuing)" | tee -a "$LOG"
    LAST_RESEARCH=0
  fi
done

# ---- consolidate ONCE, then exit ----------------------------------------------
echo "run_window: $(date -Is) consolidate" | tee -a "$LOG"
render "$CONSOLIDATE_PROMPT" | claude -p "$(cat)" --dangerously-skip-permissions $OUTFMT \
  > "$LOGDIR/consolidate_$(date +%s).jsonl" 2>>"$LOG" \
  || echo "run_window: $(date -Is) consolidate exited non-zero (logged)" | tee -a "$LOG"
python3 -c "import json;p='$BOX/campaign.json';d=json.load(open(p));d['state']='completed';json.dump(d,open(p,'w'),indent=2)"
rm -f "$SENTINEL"
echo "run_window: $(date -Is) DONE box=$BOX units=$UNIT" | tee -a "$LOG"
