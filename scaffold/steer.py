#!/usr/bin/env python3
"""
crucible steer.py — drop an operator steering note into a box's STEERING.md inbox.

The research loop (scaffold/run_window.sh -> prompts/unit.md) has the worker read
`<box>/STEERING.md` at the START of every unit and treat each Inbox note as a top-priority
hypothesis, then move it out of the Inbox (-> Picked up / Dropped) the same unit. This helper
just appends a well-formed note to the TOP of the Inbox, so a remote Claude Code session (or
you, from a phone) can steer a live campaign without hand-editing markdown or risking a
malformed entry the worker can't parse.

It NEVER touches MEMORY.md, the queue, or the loop — it only adds to the inbox. The running
worker is what folds the note in and empties the inbox. Adding while a unit is mid-flight is
safe: the note is picked up at the next unit's Orient.

Usage:
    python3 scaffold/steer.py boxes/<nick> "look into Mamba-2 SSD CPU-decode kernels"
    python3 scaffold/steer.py boxes/<nick> "try IQ2_XXS on the 70B" --tag BOX
    python3 scaffold/steer.py boxes/<nick> "survey 2026 SSM forks" --research --note "saw a paper claiming 2x decode"
    python3 scaffold/steer.py boxes/<nick> --list
"""
from __future__ import annotations
import argparse, json, os, re, sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TEMPLATE_PATH = os.path.join(ROOT, "templates", "STEERING.template.md")
INBOX_RE = re.compile(r"^##\s+inbox\b", re.I)
ANY_H2_RE = re.compile(r"^##\s+")

_FALLBACK_TEMPLATE = """# Steering — operator-injected research directions

Operator notes that steer the next unit. The worker reads this at the start of every unit,
treats each Inbox note as a top-priority hypothesis, then moves it out of the Inbox the same
unit (-> Picked up / Dropped). The Inbox only ever holds notes not yet acted on.

## Inbox (unprocessed)

<!-- newest first; the worker consumes these and moves them below. -->

## Picked up

## Dropped
"""


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _template() -> str:
    t = _read(TEMPLATE_PATH)
    return t if t.strip() else _FALLBACK_TEMPLATE


def ensure_file(path: str) -> None:
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_template())


def add_note(path: str, text: str, tag: str | None, research: bool, note: str | None) -> str:
    ensure_file(path)
    lines = _read(path).splitlines()
    ts = datetime.now().isoformat(timespec="minutes")
    bullet = f"- [{ts}] **{text.strip()}**"
    if tag:
        bullet += f" [{tag.upper()}]"
    if research:
        bullet += " (research)"
    body = [bullet]
    if note:
        body += ["  " + note.strip()]

    # find the Inbox header, then the first real content line under it (skip blanks/comments)
    hdr = next((i for i, l in enumerate(lines) if INBOX_RE.match(l.strip())), None)
    if hdr is None:  # no Inbox section — create one at the end
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["## Inbox (unprocessed)", ""] + body + [""]
    else:
        j = hdr + 1
        while j < len(lines) and (not lines[j].strip() or lines[j].lstrip().startswith("<!--")):
            j += 1
        ins = list(body)
        if j >= len(lines) or ANY_H2_RE.match(lines[j]):  # inbox is empty -> pad before next section
            ins = body + [""]
        lines[j:j] = ins
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return bullet


def list_inbox(path: str) -> list[str]:
    md = _read(path)
    lines = md.splitlines()
    hdr = next((i for i, l in enumerate(lines) if INBOX_RE.match(l.strip())), None)
    if hdr is None:
        return []
    out = []
    for l in lines[hdr + 1:]:
        if ANY_H2_RE.match(l):
            break
        if re.match(r"^[-*]\s+", l.strip()):
            out.append(re.sub(r"^[-*]\s+", "", l.strip()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="add an operator steering note to a box's STEERING.md inbox")
    ap.add_argument("box", help="box folder, e.g. boxes/<nick>")
    ap.add_argument("text", nargs="?", help="the steering note (the avenue to look into)")
    ap.add_argument("--tag", choices=["BOX", "HOST", "EITHER", "box", "host", "either"],
                    help="resource hint for the worker (default: let it decide)")
    ap.add_argument("--research", action="store_true",
                    help="mark it as a web-research probe (worker spends a research phase on it)")
    ap.add_argument("--note", help="optional extra context line")
    ap.add_argument("--list", action="store_true", help="print the pending inbox and exit")
    a = ap.parse_args()

    box = a.box.rstrip("/")
    if not os.path.isfile(os.path.join(box, "campaign.json")):
        print(f"steer: not a box (no campaign.json): {box}", file=sys.stderr)
        return 2
    path = os.path.join(box, "STEERING.md")

    if a.list:
        items = list_inbox(path)
        if not items:
            print(f"steer: inbox empty — {path}")
        else:
            print(f"steer: {len(items)} pending in {path}:")
            for it in items:
                print("  - " + it)
        return 0

    if not a.text or not a.text.strip():
        print("steer: provide a note, e.g.  steer.py boxes/<nick> \"look into X\"  (or --list)", file=sys.stderr)
        return 2

    bullet = add_note(path, a.text, a.tag, a.research, a.note)
    n = len(list_inbox(path))
    print(f"steer: queued to {path} ({n} pending) — picked up at the next unit's Orient:")
    print("  " + bullet)
    print("\nnote: this only adds to the inbox; the running worker folds it in and empties it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
