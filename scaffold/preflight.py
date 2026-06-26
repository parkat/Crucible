#!/usr/bin/env python3
"""
crucible preflight — structure validator + self-repair.

Goal: you can dump every crucible file into ONE folder (or a slightly mis-nested one)
and the agent runs this once to rebuild the correct tree. It knows the canonical layout,
finds each file by name (disambiguating same-named files by a content marker), and moves
misplaced files into place. Critical files missing entirely are reported precisely so the
human can supply them.

This does NOT create per-box campaign folders — that's startup.md step 5. Preflight only
fixes the apparatus layout itself.

Usage:
    python3 preflight.py [root]            # validate AND repair (default root = cwd)
    python3 preflight.py [root] --check    # validate only, no moves (exit 1 if not OK)

Exit code 0 = structure OK (after any repairs). Exit 1 = critical files still missing.
"""
from __future__ import annotations
import argparse, os, shutil, sys

# canonical_path : (tier, marker)
#   tier   = "critical" (apparatus can't run without it) | "seed" (regenerable; warn only)
#   marker = a short substring that uniquely identifies the file's content, used to
#            disambiguate same-named files (e.g. the two README.md). None = match by name.
MANIFEST: dict[str, tuple[str, str | None]] = {
    "startup.md":                              ("critical", "ENTRY POINT"),
    "doctrine/00_PRIME_DIRECTIVE.md":          ("critical", "PRIME DIRECTIVE"),
    "doctrine/01_RUBRIC.md":                   ("critical", "01 — RUBRIC"),
    "doctrine/02_EVAL_FUNNEL.md":              ("critical", "02 — EVAL FUNNEL"),
    "doctrine/03_PROPOSER_PLAYBOOK.md":        ("critical", "03 — PROPOSER PLAYBOOK"),
    "doctrine/04_AUTONOMY_TIERS.md":           ("critical", "04 — AUTONOMY TIERS"),
    "doctrine/05_SAFETY_RECOVERY.md":          ("critical", "05 — SAFETY"),
    "doctrine/06_OPERATIONS.md":               ("critical", "06 — OPERATIONS"),
    "scaffold/preflight.py":                   ("critical", "crucible preflight"),
    "scaffold/ledger.py":                      ("critical", "crucible ledger"),
    "scaffold/hardware_scan.sh":               ("critical", "crucible hardware_scan.sh"),
    "scaffold/roofline.py":                    ("critical", "crucible roofline"),
    "scaffold/correctness.py":                 ("critical", "crucible correctness"),
    "scaffold/verify.py":                      ("critical", "INDEPENDENT VERIFIER"),
    "scaffold/eval/runner.py":                 ("critical", "crucible eval/runner.py"),
    "scaffold/dashboard/server.py":            ("critical", "crucible dashboard server"),
    "scaffold/dashboard/index.html":           ("critical", "crucible · campaign monitor"),
    "templates/MEMORY.template.md":            ("critical", "MEMORY — <NICKNAME>"),
    "templates/campaign.template.json":        ("critical", "winddown_margin_frac"),
    "templates/GATE_QUEUE.template.md":        ("critical", "GATE QUEUE"),
    "templates/gitignore":                     ("critical", "Secrets — NEVER commit"),
    # seeds — regenerable from doctrine if absent
    "scaffold/eval/assets/README.md":          ("seed", "Frozen eval assets"),
    "scaffold/eval/assets/corpus.txt":         ("seed", None),
    "scaffold/eval/assets/math.jsonl":         ("seed", '"answer"'),
    "scaffold/eval/assets/code.jsonl":         ("seed", '"tests"'),
    "scaffold/eval/assets/pairwise_prompts.jsonl": ("seed", None),
    "scaffold/eval/assets/judge_rubric.md":    ("seed", "Pairwise judge rubric"),
    "scaffold/eval/assets/reference_pair.json":("seed", "expected_winner"),
    # optional — orientation only; tracked so it isn't confused with assets/README.md
    "README.md":                               ("optional", "# crucible"),
}

# basenames that map to >1 canonical path -> must disambiguate by marker
def _basename(p): return os.path.basename(p)
_BY_NAME: dict[str, list[str]] = {}
for _p in MANIFEST:
    _BY_NAME.setdefault(_basename(_p), []).append(_p)


def _read_head(path: str, n: int = 4000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(n)
    except OSError:
        return ""

def _matches(path: str, canonical: str) -> bool:
    """Does the file at `path` belong at `canonical`? Check the marker if there is one,
    and make sure it doesn't actually belong to a different same-named entry."""
    marker = MANIFEST[canonical][1]
    head = _read_head(path)
    if marker is not None:
        return marker in head
    # no marker: name is unique, but guard against grabbing a sibling that has its own marker
    siblings = [c for c in _BY_NAME[_basename(canonical)] if c != canonical]
    for sib in siblings:
        sm = MANIFEST[sib][1]
        if sm and sm in head:
            return False
    return True


def _scan(root: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "boxes")]
        for fn in filenames:
            found.append(os.path.join(dirpath, fn))
    return found


def run(root: str, repair: bool) -> int:
    root = os.path.abspath(root)
    all_files = _scan(root)
    # index found files by basename (also map ".gitignore" -> "gitignore" for the template)
    by_name: dict[str, list[str]] = {}
    for p in all_files:
        by_name.setdefault(_basename(p), []).append(p)
        if _basename(p) == ".gitignore":
            by_name.setdefault("gitignore", []).append(p)

    present, repaired, missing_critical, missing_seed, notes = [], [], [], [], []

    for canonical, (tier, _marker) in MANIFEST.items():
        target = os.path.join(root, canonical)
        # already correctly placed?
        if os.path.isfile(target) and _matches(target, canonical):
            present.append(canonical)
            continue
        # locate a candidate elsewhere by basename + marker
        candidates = [c for c in by_name.get(_basename(canonical), []) if _matches(c, canonical)]
        # never treat the canonical target itself as a "move" source
        candidates = [c for c in candidates if os.path.abspath(c) != os.path.abspath(target)]
        if candidates:
            src = candidates[0]
            if repair:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(src, target)
                repaired.append(f"{os.path.relpath(src, root)}  ->  {canonical}")
            else:
                notes.append(f"misplaced: {os.path.relpath(src, root)} should be {canonical}")
                (missing_critical if tier == "critical" else
                 missing_seed if tier == "seed" else notes).append(canonical) if tier != "optional" else None
            if len(candidates) > 1:
                notes.append(f"note: multiple matches for {_basename(canonical)}; used {os.path.relpath(src, root)}")
        else:
            if tier == "critical":
                missing_critical.append(canonical)
            elif tier == "seed":
                missing_seed.append(canonical)
            # optional missing -> silent

    # ---- report ----
    print(f"crucible preflight — root: {root}")
    print(f"  present:  {len(present)}/{len(MANIFEST)} tracked files in place")
    if repaired:
        print(f"  repaired: {len(repaired)} moved into place")
        for r in repaired:
            print(f"            {r}")
    if missing_seed:
        print(f"  seeds missing ({len(missing_seed)}) — regenerable from doctrine/02 + assets/README:")
        for m in missing_seed:
            print(f"            {m}")
    if notes:
        for n in notes:
            print(f"  ! {n}")
    if missing_critical:
        print(f"  CRITICAL MISSING ({len(missing_critical)}) — supply these and re-run:")
        for m in missing_critical:
            print(f"            {m}")
        print("  -> structure NOT ready.")
        return 1

    if not repair and (missing_seed or notes):
        # check-only mode surfaced issues but no criticals
        print("  -> apparatus files OK (check-only; seeds/notes above are non-blocking).")
        return 0

    print("  -> structure OK. Safe to proceed with startup.md.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--check", action="store_true", help="validate only; do not move files")
    a = ap.parse_args()
    return run(a.root, repair=not a.check)


if __name__ == "__main__":
    raise SystemExit(main())
