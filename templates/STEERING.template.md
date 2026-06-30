# Steering — operator-injected research directions

Notes you (the human) drop here to point the campaign at a front **you** found — e.g. from
AI-research news — without stopping the run. The worker reads this at the **start of every
unit** and treats each **Inbox** note as a TOP-PRIORITY hypothesis: it folds the note into
`MEMORY.md`'s queue (splitting it to a small takeable item if large), pursues it ahead of the
stale queue top, then **moves the note out of the Inbox in the same unit** — to **Picked up**
(with what it queued/measured) or **Dropped** (with a one-line reason if it isn't viable).

> **STEERING INVARIANT:** the Inbox only ever holds notes the worker has **not yet acted on**.
> Anything consumed is moved below the same unit, so stale steering can never re-poison a
> later research round.

Add a note with the helper (preferred — well-formed, safe from a phone session) or by hand:

```bash
python3 scaffold/steer.py boxes/<nick> "look into Mamba-2 SSD CPU-decode kernels" --tag HOST --research
python3 scaffold/steer.py boxes/<nick> --list      # show what's still pending
```

## Inbox (unprocessed)

<!-- newest first; the worker consumes these and moves them below. Empty = nothing pending. -->

## Picked up

<!-- worker appends: - [ts] **<note>** → queued as [ID] / ledger <id> -->

## Dropped

<!-- worker appends: - [ts] **<note>** → not viable: <one-line reason> -->
