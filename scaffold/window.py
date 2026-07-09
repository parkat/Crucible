#!/usr/bin/env python3
"""
crucible window.py — operator/agent control over a LIVE campaign window.

Companion to steer.py. Where steer.py adds a *research direction* to the inbox, window.py
controls the WINDOW ITSELF: stop it cleanly, or add/remove time — without hand-editing
campaign.json or hunting down the driver PID. Safe to call while the loop is running:
scaffold/run_window.sh re-reads the deadline every iteration and checks the STOP flag at the
top of every iteration, so a change takes effect at the next unit boundary.

  python3 scaffold/window.py boxes/<nick> status
  python3 scaffold/window.py boxes/<nick> stop            # QUICK-STOP: kill the in-flight unit,
                                                          #   then the loop consolidates + writes the
                                                          #   FINAL report and exits (state -> stopped)
  python3 scaffold/window.py boxes/<nick> stop --graceful # let the current unit finish first
  python3 scaffold/window.py boxes/<nick> add-hours 6     # extend the window by 6h
  python3 scaffold/window.py boxes/<nick> add-hours -2    # remove 2h (shorten)

Only ever writes: campaign.json (deadline_epoch + duration_label on add-hours) and, on stop,
work/STOP + a kill of work/current_unit.pid. It NEVER touches MEMORY / the ledger / the queue.
The report is still written on stop because the loop breaks into its normal consolidate step.
"""
from __future__ import annotations
import argparse, json, os, signal, subprocess, sys, time


def _load(box: str):
    p = os.path.join(box, "campaign.json")
    with open(p, encoding="utf-8") as f:
        return p, json.load(f)


def _fmt_dur(secs: float) -> str:
    secs = int(secs)
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    return f"{sign}{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _winddown(start: int, dl: int, frac: float) -> int:
    return 0 if dl == 0 else int(dl - (dl - start) * frac)


def _pidfile(box: str) -> str:
    return os.path.join(box, "work", "current_unit.pid")


def _current_pid(box: str):
    """PID of the in-flight unit if one is genuinely alive, else None."""
    try:
        pid = int(open(_pidfile(box)).read().strip())
        os.kill(pid, 0)  # probe existence (raises if gone)
        return pid
    except Exception:
        return None


def _kill_tree(pid: int) -> None:
    """Best-effort terminate the unit's `claude` process and its tool children."""
    try:
        subprocess.run(["pkill", "-TERM", "-P", str(pid)], capture_output=True)
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def cmd_status(box: str, d: dict) -> int:
    now = int(time.time())
    start = int(d.get("start_epoch") or 0)
    dl = int(d.get("deadline_epoch") or 0)
    frac = d.get("winddown_margin_frac")
    frac = 0.10 if frac is None else float(frac)
    wd = _winddown(start, dl, frac)
    print(f"box            : {box}")
    print(f"state          : {d.get('state')}")
    print(f"duration_label : {d.get('duration_label')}")
    print(f"start_epoch    : {start}")
    print(f"deadline_epoch : {dl}" + ("" if dl else "   (open-ended)"))
    if dl:
        print(f"winddown_epoch : {wd}   (grind stops here -> consolidate)")
        print(f"elapsed        : {_fmt_dur(now - start)}")
        print(f"to winddown    : {_fmt_dur(wd - now)}")
        print(f"to deadline    : {_fmt_dur(dl - now)}")
    print(f"unit in flight : {'yes (pid ' + str(_current_pid(box)) + ')' if _current_pid(box) else 'no'}")
    return 0


def cmd_stop(box: str, graceful: bool) -> int:
    workdir = os.path.join(box, "work")
    os.makedirs(workdir, exist_ok=True)
    # No live driver? A STOP flag has nobody to consume it and would AMBUSH the next window —
    # a fresh run_window.sh sees it on iteration 1, consolidates once, exits units=0 (finding
    # #3). Instead mark the campaign stopped directly and leave no dangling flag.
    if _driver_pid(box) is None:
        path = os.path.join(box, "campaign.json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except OSError as e:
            print(f"window: no live driver and cannot read campaign.json ({e}).", file=sys.stderr)
            return 2
        if d.get("state") == "running":
            d["state"] = "stopped"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
            print(f"window: no live driver — marked {box} state -> stopped (no STOP flag left behind).")
        else:
            print(f"window: no live driver and state is {d.get('state')!r}; nothing to stop.")
        return 0
    open(os.path.join(workdir, "STOP"), "w").close()
    print(f"window: STOP flag set for {box}.")
    if graceful:
        print("window: --graceful — the current unit finishes, then the loop stops.")
    else:
        pid = _current_pid(box)
        if pid:
            _kill_tree(pid)
            print(f"window: killed in-flight unit (pid {pid}) for a fast stop.")
        else:
            print("window: no in-flight unit to kill; loop stops at the next unit boundary.")
    print("window: the loop will consolidate + write the FINAL report, then exit (state -> stopped).")
    print("window: (if no driver is running nothing consumes the flag — delete work/STOP to cancel.)")
    return 0


def cmd_add_hours(box: str, path: str, d: dict, hours: float, force: bool) -> int:
    now = int(time.time())
    start = int(d.get("start_epoch") or 0)
    dl = int(d.get("deadline_epoch") or 0)
    was_open_ended = (dl == 0)
    base = dl if dl else now           # open-ended window -> anchor the new deadline from now
    new_dl = int(base + hours * 3600)
    if not force:
        if new_dl <= now:
            print(f"window: refusing — new deadline ({new_dl}) is at/before now. "
                  "Use `stop` to end the window now, or pass --force.", file=sys.stderr)
            return 2
        if start and new_dl <= start:
            print("window: refusing — new deadline would precede start_epoch. Pass --force to override.",
                  file=sys.stderr)
            return 2
    d["deadline_epoch"] = new_dl
    if was_open_ended:
        # Bounding a previously open-ended window (start_epoch was 0): we MUST set start=now,
        # else run_window computes winddown = dl - (dl - 0)*frac, which lands ~decades in the
        # past, so the loop winddowns instantly on the next iteration (finding #4).
        d["start_epoch"] = now
        start = now
    if start:
        d["duration_label"] = f"{round((new_dl - start) / 3600)}hr"   # keep the label truthful (run_window invariant)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    verb = "extended" if hours >= 0 else "shortened"
    print(f"window: {verb} {box} by {abs(hours)}h -> deadline_epoch={new_dl} "
          f"({_fmt_dur(new_dl - now)} from now); duration_label={d.get('duration_label')}.")
    print("window: a running loop re-reads the deadline each iteration; the change applies at the next unit.")
    return 0


def _driver_pid(box: str):
    """PID of the run_window.sh driver if alive, else None (run_window writes work/driver.pid)."""
    try:
        pid = int(open(os.path.join(box, "work", "driver.pid")).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def cmd_pause(box: str) -> int:
    os.makedirs(os.path.join(box, "work"), exist_ok=True)
    open(os.path.join(box, "work", "PAUSE"), "w").close()
    print(f"window: PAUSE set for {box} — the loop finishes the current unit, then waits (no cap spent while paused).")
    print("window: run `resume` to continue; a `stop` during pause still stops cleanly.")
    return 0


def cmd_resume(box: str) -> int:
    p = os.path.join(box, "work", "PAUSE")
    if os.path.exists(p):
        os.remove(p)
        print(f"window: resumed {box} — the loop picks up on its next check (≤10s).")
    else:
        print(f"window: {box} was not paused (no PAUSE flag).")
    return 0


def cmd_hard_kill(box: str, path: str, d: dict) -> int:
    """Escape hatch: terminate driver + in-flight unit immediately, NO consolidate/report."""
    killed = []
    dp = _driver_pid(box)
    if dp:
        _kill_tree(dp); killed.append(f"driver {dp}")
    up = _current_pid(box)
    if up:
        _kill_tree(up); killed.append(f"unit {up}")
    for f in ("STOP", "PAUSE", "current_unit.pid", "driver.pid", "QUEUE_EMPTY"):
        try:
            os.remove(os.path.join(box, "work", f))
        except OSError:
            pass
    d["state"] = "killed"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)
    print(f"window: HARD KILL — terminated {', '.join(killed) if killed else 'nothing (no live processes found)'}.")
    print("window: NO consolidate/report ran; state -> killed. Re-arm with run_window.sh to restart.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="control a live crucible campaign window (stop / add-remove time / status)")
    ap.add_argument("box", help="box folder, e.g. boxes/<nick>")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="print the window clock + state")
    sp = sub.add_parser("stop", help="quick-stop: halt the loop, still consolidate + write the FINAL report")
    sp.add_argument("--graceful", action="store_true", help="let the in-flight unit finish first (default: kill it)")
    ap_add = sub.add_parser("add-hours", help="add (or, negative, remove) hours to the active window")
    ap_add.add_argument("hours", type=float, help="hours to add; negative removes time")
    ap_add.add_argument("--force", action="store_true", help="skip the deadline>now / >start guardrails")
    sub.add_parser("pause", help="pause after the current unit (resumable; no cap spent while waiting)")
    sub.add_parser("resume", help="resume a paused loop")
    sub.add_parser("hard-kill", help="terminate driver+unit NOW, no consolidate/report (escape hatch)")
    a = ap.parse_args()

    box = a.box.rstrip("/")
    if not os.path.isfile(os.path.join(box, "campaign.json")):
        print(f"window: not a box (no campaign.json): {box}", file=sys.stderr)
        return 2
    path, d = _load(box)

    if a.cmd == "status":
        return cmd_status(box, d)
    if a.cmd == "stop":
        return cmd_stop(box, a.graceful)
    if a.cmd == "add-hours":
        return cmd_add_hours(box, path, d, a.hours, a.force)
    if a.cmd == "pause":
        return cmd_pause(box)
    if a.cmd == "resume":
        return cmd_resume(box)
    if a.cmd == "hard-kill":
        return cmd_hard_kill(box, path, d)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
