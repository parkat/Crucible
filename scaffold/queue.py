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
import argparse, os, re, sys, tempfile
from datetime import datetime
try:
    import fcntl                       # Unix orchestrator: advisory inter-process lock
except ImportError:                    # Windows host: no fcntl -> the atomic replace still applies
    fcntl = None

QUEUE_HDR = re.compile(r"^#{2,3}\s+(Queue|Open hypotheses)\b", re.I)
ANY_HDR = re.compile(r"^#{1,3}\s+")
ITEM = re.compile(r"^\s*(\d+[.)]\s+|[-*]\s+|\*\*\s*→)")


def _read(p: str) -> str:
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _read_strict(p: str) -> str:
    """Read for a MUTATING op: return '' ONLY if the file genuinely doesn't exist. If it exists but
    can't be read (transient EACCES/drvfs lock during a concurrent unit edit), RAISE — else inject
    would treat a real 250-line notebook as empty and truncate it to a 4-line stub (finding #28)."""
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


def _atomic_write(path: str, content: str) -> None:
    """Write via a temp file in the same dir + os.replace so a concurrent reader/writer never sees a
    torn file (findings #9/#28). os.replace is atomic on POSIX and same-volume Windows."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


class _FileLock:
    """Advisory exclusive lock on a sidecar .state.lock so the tool-writers (queue.py, steer.py,
    dashboard injects) serialize among themselves (finding #9). No-op on Windows (fcntl absent);
    the atomic write still prevents torn files there."""
    def __init__(self, target: str):
        # in work/ (gitignored) so the lockfile never lands in the box repo
        self.lockpath = os.path.join(os.path.dirname(os.path.abspath(target)) or ".", "work", ".state.lock")
        self._fh = None
    def __enter__(self):
        if fcntl is not None:
            os.makedirs(os.path.dirname(self.lockpath), exist_ok=True)
            self._fh = open(self.lockpath, "w")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self
    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()


def inject(md_path: str, text: str, tag: str | None) -> str:
    with _FileLock(md_path):
        lines = _read_strict(md_path).splitlines()             # re-read UNDER the lock (not a stale snapshot)
        tagstr = f" `[{tag.upper()}]`" if tag else ""
        item = f"**→ [INJECT]{tagstr}** — {text.strip()}"      # arrow style => parsed as the takeable top
        hdr = next((i for i, l in enumerate(lines) if QUEUE_HDR.match(l.strip())), None)
        if hdr is None:                                        # no queue section yet -> create one up top
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
        _atomic_write(md_path, "\n".join(lines).rstrip() + "\n")
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
            if re.search(r"\b(CLOSED|DONE|NO-GO|REFUTED|RETRACTED)\b", l):
                continue                       # a finished item isn't the takeable top (finding #57)
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
