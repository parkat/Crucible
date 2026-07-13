#!/usr/bin/env bash
# crucible remote_run.sh — launch a long remote job DETACHED under the box lock, then poll it.
#
# Fixes recurring friction (bug H): units hand-rolled `setsid nohup … & poll`, several polls read
# a half-written JSON result and crashed (JSONDecodeError / KeyError / NoneType.__format__), and
# one unit ran `exec 9>"$LOCK"` with $LOCK UNSET — dropping a literal `"$LOCK"` file in the repo
# root. This helper ALWAYS resolves the lock via `boxpaths.py --lock-path` and signals completion
# with an ATOMIC `DONE rc=<n>` sentinel (written to a temp name, then renamed), so a poller never
# races a half-written file: it either sees no DONE (still running) or a complete one.
#
# Usage:
#   remote_run.sh boxes/<nick> start "<remote shell command>"   # detached under flock; prints job id
#   remote_run.sh boxes/<nick> poll  [job]                      # rc 0 = DONE (prints "DONE rc=N"); rc 2 = running
#   remote_run.sh boxes/<nick> wait  [job] [interval_s]         # block until DONE; exits with the job's rc
#   remote_run.sh boxes/<nick> log   [job]                      # cat the remote stdout/stderr so far
# [job] defaults to the most recent job. NOTE: staged for review — verify SSH quoting on the target.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 1
BOX="${1:?usage: remote_run.sh boxes/<nick> <start|poll|wait|log> [args]}"
SUB="${2:?usage: remote_run.sh boxes/<nick> <start|poll|wait|log> [args]}"
shift 2
BOX="${BOX%/}"
[ -f "$BOX/campaign.json" ] || { echo "remote_run: not a box (no campaign.json): $BOX" >&2; exit 2; }
SSH="$(python3 scaffold/boxpaths.py "$BOX" --ssh)" || { echo "remote_run: cannot resolve --ssh" >&2; exit 2; }
LOCK="$(python3 scaffold/boxpaths.py "$BOX" --lock-path)" || { echo "remote_run: cannot resolve --lock-path" >&2; exit 2; }
[ -n "$LOCK" ] || { echo "remote_run: resolved an empty lock path — refusing (this is the bug-H class)" >&2; exit 2; }
RDIR='$HOME/crucible/work/remote_run'   # remote job root (expanded on the target shell)

case "$SUB" in
  start)
    JOBSPEC="${1:?remote_run start needs a remote command}"
    JOB="job_$(date +%s)"
    # base64 the job command so ANY quoting inside it (single quotes, $( ), awk/sed scripts) survives
    # the trip through the remote login shell AND the flock -c '...' wrapper (finding #13). The old
    # code pasted $JOBSPEC verbatim between the single quotes of `flock -c '...'`, so one single quote
    # in the command closed the -c string early: the job died with a syntax error, never wrote DONE,
    # was misreported as "box busy", and a subsequent `wait` hung forever. The b64 blob is
    # [A-Za-z0-9+/=] only, so it embeds safely; the remote decodes it and runs it via bash.
    JOBSPEC_B64="$(printf '%s' "$JOBSPEC" | base64 | tr -d '\n')"
    # flock -n = single-tenant; if the box lock is held, fail fast rather than contend.
    if $SSH "mkdir -p $RDIR/$JOB && flock -n '$LOCK' -c '{ echo $JOBSPEC_B64 | base64 -d | bash ; echo DONE rc=\$? > $RDIR/$JOB/.done && mv -f $RDIR/$JOB/.done $RDIR/$JOB/DONE ; } > $RDIR/$JOB/out.log 2>&1 & echo \$! > $RDIR/$JOB/pid'"; then
      echo "$JOB"
    else
      echo "remote_run: box busy (lock held) or launch failed" >&2; exit 3
    fi
    ;;
  log)
    JOB="${1:-$($SSH "ls -1t $RDIR 2>/dev/null | head -1")}"
    [ -n "$JOB" ] || { echo "remote_run: no jobs found" >&2; exit 2; }
    $SSH "cat $RDIR/$JOB/out.log 2>/dev/null"
    ;;
  poll|wait)
    JOB="${1:-$($SSH "ls -1t $RDIR 2>/dev/null | head -1")}"
    INT="${2:-15}"
    MAXWAIT="${REMOTE_RUN_MAXWAIT:-21600}"     # 6h ceiling so a wedged job can't loop forever (#31)
    [ -n "$JOB" ] || { echo "remote_run: no jobs found" >&2; exit 2; }
    START="$(date +%s)"
    while : ; do
      # ONE round-trip: report the DONE sentinel if written, else whether the job pid is still ALIVE
      # or DEAD. Distinguishes a finished job, a live job, and a job that died without writing DONE
      # (crash/reboot/OOM) — the old loop treated all three (and an unreachable box) as "running" (#31).
      STATUS="$($SSH "if [ -s $RDIR/$JOB/DONE ]; then echo DONEFILE:\$(cat $RDIR/$JOB/DONE); elif kill -0 \$(cat $RDIR/$JOB/pid 2>/dev/null) 2>/dev/null; then echo ALIVE; else echo DEAD; fi")"
      SSH_RC=$?
      if [ "$SSH_RC" -ne 0 ]; then
        # transport failure (asleep box / network), NOT 'still running'.
        [ "$SUB" = "poll" ] && { echo "unreachable ($JOB): ssh rc=$SSH_RC" >&2; exit 3; }
        echo "remote_run: box unreachable (ssh rc=$SSH_RC), retrying under the ${MAXWAIT}s ceiling" >&2
      else
        case "$STATUS" in
          DONEFILE:*)
            DONE="${STATUS#DONEFILE:}"; echo "$DONE"
            RC="${DONE##*rc=}"; [ "$SUB" = "wait" ] && exit "${RC:-0}"; exit 0 ;;
          DEAD)
            echo "remote_run: job $JOB died without writing DONE (crash/reboot/OOM)" >&2; exit 4 ;;
          ALIVE)
            [ "$SUB" = "poll" ] && { echo "running ($JOB)"; exit 2; } ;;
        esac
      fi
      if [ "$(( $(date +%s) - START ))" -ge "$MAXWAIT" ]; then
        echo "remote_run: wait exceeded ${MAXWAIT}s for $JOB — giving up" >&2; exit 5
      fi
      sleep "$INT"
    done
    ;;
  *) echo "remote_run: unknown subcommand '$SUB' (want start|poll|wait|log)" >&2; exit 2 ;;
esac
