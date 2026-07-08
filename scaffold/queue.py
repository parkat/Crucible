#!/usr/bin/env python3
"""
crucible queue.py — peek at, or inject into, a box's takeable queue (MEMORY.md's queue section).

Peek prints the current open queue. Inject prepends a takeable item to the TOP so the next unit
takes it — a direct alternative to steering. It makes a minimal edit to the '### Queue' section that
the loop + dashboard parser both read (arrow-style item -> recognized as the takeable top).

  python3 scaffold/queue.py boxes/<nick> --list
  python3 scaffold/queue.py boxes/<nick> "value-verify conv.conv.weight reshape vs bf16 ref" --tag HOST
"""
from __future__ import annotations
import argparse, os, re, sys
from datetime import datetime

QUEUE_HDR = re.compile(r"^#{2,3}\s+(Queue|Open hypotheses)\b", re.I)
ANY_HDR = re.compile(r"^#{1,3}\s+")
ITEM = re.compile(r"^\s*(\d+[.)]\s+|[-*]\s+|\*\*\s*→)")


def _read(p: str) -> str:
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def inject(md_path: str, text: str, tag: str | None) -> str:
    lines = _read(md_path).splitlines()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    tagstr = f" `[{tag.upper()}]`" if tag else ""
    item = f"**→ [INJECT]{tagstr}** — {text.strip()}"          # arrow style => parsed as the takeable top
    hdr = next((i for i, l in enumerate(lines) if QUEUE_HDR.match(l.strip())), None)
    if hdr is None:                                             # no queue section yet -> create one up top
        lines = ["### Queue (takeable top)", "", item, ""] + lines
    else:
        first_item = None
        for k in range(hdr + 1, len(lines)):
            if ANY_HDR.match(lines[k]):
                break
            if ITEM.match(lines[k]):
                first_item = k
                break
        at = first_item if first_item is not None else hdr + 1
        lines[at:at] = [item, ""]
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return item


def peek(md_path: str) -> list[str]:
    lines = _read(md_path).splitlines()
    hdr = next((i for i, l in enumerate(lines) if QUEUE_HDR.match(l.strip())), None)
    if hdr is None:
        return []
    out = []
    for l in lines[hdr + 1:]:
        if ANY_HDR.match(l):
            break
        if ITEM.match(l):
            out.append(re.sub(r"[*`]", "", l.strip())[:140])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="peek at / inject into a box's takeable queue")
    ap.add_argument("box", help="box folder, e.g. boxes/<nick>")
    ap.add_argument("text", nargs="?", help="the takeable item to inject at the queue top")
    ap.add_argument("--tag", choices=["BOX", "HOST", "EITHER", "box", "host", "either"],
                    help="resource tag for the injected item")
    ap.add_argument("--list", action="store_true", help="print the current open queue and exit")
    a = ap.parse_args()

    box = a.box.rstrip("/")
    if not os.path.isfile(os.path.join(box, "campaign.json")):
        print(f"queue: not a box (no campaign.json): {box}", file=sys.stderr)
        return 2
    md = os.path.join(box, "MEMORY.md")

    if a.list or not a.text:
        items = peek(md)
        if not items:
            print(f"queue: empty (or no queue section) — {md}")
        else:
            print(f"queue: {len(items)} item(s), top first:")
            for i, it in enumerate(items, 1):
                print(f"  {i}. {it}")
        return 0

    item = inject(md, a.text, a.tag)
    print(f"queue: injected as the takeable top of {md}:")
    print("  " + re.sub(r"[*`]", "", item))
    print("queue: the next unit takes this before the rest of the queue.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
