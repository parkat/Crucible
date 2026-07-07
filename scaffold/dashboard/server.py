#!/usr/bin/env python3
"""
crucible dashboard server — runs on the HOST (never the target).

Zero third-party dependencies (stdlib http.server) so it runs on a minimal host. Serves
the single-page UI and a /data endpoint that reads, for EACH box under the given path(s),
the host-side artifacts the live campaign produces — campaign.json, hardware.json,
ledger.jsonl, MEMORY.md, and the run_window driver log + per-unit `claude -p` JSON — and
returns the live fleet state as JSON. Read-only: it NEVER writes to a campaign and NEVER
touches the target box (host-side artifacts only).

Fleet-ready: pass either a single box folder, several box folders, or the parent `boxes/`
dir; the server enumerates every child that has a campaign.json and renders one panel per
box.

Usage:
    python3 scaffold/dashboard/server.py boxes/<nickname> [--port 8787]
    python3 scaffold/dashboard/server.py boxes            # whole fleet
    python3 scaffold/dashboard/server.py boxes/a boxes/b  # explicit set
Then open http://localhost:8787/
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCAFFOLD = os.path.dirname(HERE)
sys.path.insert(0, SCAFFOLD)
import ledger  # noqa: E402  (read-only use of the same front logic the agent writes)
sys.path.insert(0, HERE)
try:
    import report_pdf  # sibling: FINAL_*.md -> PDF, generated lazily on request
except Exception:
    report_pdf = None

BOXES: list[str] = []  # set in main()

# a report filename is always FINAL_<...>.<md|pdf|html> — no slashes, no traversal
_REPORT_NAME_RE = re.compile(r"^FINAL_[A-Za-z0-9_.\-]+\.(md|pdf|html)$")

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""

def _read_json(path: str, default=None):
    try:
        return json.loads(_read(path) or "null")
    except json.JSONDecodeError:
        return default

# ─────────────────────────────────────────────────────────────────────────────
# MEMORY.md section helpers (the queue + phase narrative live here)
# ─────────────────────────────────────────────────────────────────────────────
def _section(md: str, header: str) -> str:
    """Pull the body under a '## <header>' up to the next '## '. Robust to absence."""
    m = re.search(rf"^##\s+{re.escape(header)}\b.*?$(.*?)(?=^##\s|\Z)", md,
                  re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    body = m.group(1).strip()
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
    return body

def _phase(md: str) -> dict:
    body = _section(md, "Current phase")
    fields = {}
    for ln in body.splitlines():
        m = re.match(r"^[-*]\s*\*\*(.+?):\*\*\s*(.*)$", ln.strip()) or \
            re.match(r"^[-*]\s*(.+?):\s*(.*)$", ln.strip())
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
    return fields

_RES_TAGS = ("BOX", "HOST", "EITHER")

def _queue_item(block: str, section: str) -> dict:
    """Parse one queue item into {id, title, tags, status, section}. Robust to the formats the
    MEMORY head evolves through: '- '/'* ' bullets, '1. '/'1) ' numbered items, and '**→ [Kxx]**';
    ids as a [Kxx] bracket OR a leading bold token (**K235**, **K230-poll**); tags [BOX]/[HOST]/
    [EITHER] (backtick-wrapped ok). Title prefers the text after the first em/en-dash."""
    text = re.sub(r"^\s*(\d+[.)]\s+|[-*]\s+)", "", block.strip())   # strip a list marker
    text = text.replace("→", " ")
    status = None
    m = re.match(r"^\s*([✅❌◐◔◑◕✓✗])\s*", text)
    if m:
        status = m.group(1); text = text[m.end():]
    # id: first bracket token that is NOT a resource tag; else a leading bold/backtick K-id
    bid = None
    for mm in re.finditer(r"\[([^\]]+)\]", text):
        if mm.group(1).strip().upper() not in _RES_TAGS:
            bid = mm.group(1).strip(); break
    if not bid:
        m = re.search(r"\*\*\s*`?\[?\s*([A-Za-z]+-?\d[\w./-]*)", text)   # **K235**, **K230-poll**
        if m:
            bid = m.group(1).strip()
    tags = sorted({t.upper() for t in re.findall(r"\[(BOX|HOST|EITHER)\]", text, re.I)})
    dm = re.search(r"[—–]\s+(.+)", text, re.S)                          # text after the em/en-dash
    if dm:
        title = dm.group(1)
    else:
        tm = re.search(r"\*\*(.+?)\*\*", text, re.S)
        title = tm.group(1) if tm else (text.splitlines() or [""])[0]
    title = re.sub(r"\s+", " ", re.sub(r"[*`]", "", title)).strip()[:160]
    return {"id": bid, "title": title, "tags": tags, "status": status, "section": section}

def _queue_blocks(sec: str) -> list[str]:
    """Item blocks under a queue section: lines starting with '- '/'* ', 'N. '/'N) ', or '**→'.
    Continuation lines stay attached to their item; a leading blockquote '>' is stripped."""
    blocks, cur = [], None
    for ln in sec.splitlines():
        s = re.sub(r"^\s*>+\s?", "", ln)
        if re.match(r"^\s*(\d+[.)]\s+|[-*]\s+|\*\*\s*→)", s):
            if cur is not None:
                blocks.append("\n".join(cur))
            cur = [s]
        elif cur is not None:
            cur.append(s)
    if cur is not None:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]

_bullets = _queue_blocks   # back-compat alias: _steering() and others still call the old name

_QUEUE_HEADERS = ("Queue (takeable top)", "Queue", "Open hypotheses (prioritized queue)", "Open hypotheses")

def _queue(md: str) -> dict:
    """The tagged hypothesis queue: open items (tagged), closed items, takeable-top id. Finds the
    queue section at ## OR ### level under any known header name (live head uses '### Queue
    (takeable top)'; the template uses '## Open hypotheses ...')."""
    out = {"open": [], "closed": [], "takeable_id": None}
    body = ""
    for name in _QUEUE_HEADERS:
        m = re.search(rf"^#{{2,3}}\s+{re.escape(name)}\b.*?$(.*?)(?=^#{{1,3}}\s|\Z)",
                      md, re.MULTILINE | re.DOTALL)
        if m and m.group(1).strip():
            body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL).strip()
            break
    if not body:
        return out
    # optional '### TAKEABLE NOW / ### CLOSED' sub-sections; else a flat list (numbered or bulleted)
    parts = re.split(r"^###\s+(.*)$", body, flags=re.MULTILINE)
    pairs = list(zip(parts[1::2], parts[2::2])) if len(parts) > 1 else [("", body)]
    for head, sec in pairs:
        up = head.upper()
        section_closed = "CLOSED" in up or "DONE" in up
        is_takeable = "TAKEABLE" in up or head == ""     # a flat queue's first open item is the top
        for blk in _queue_blocks(sec):
            item = _queue_item(blk, head or "queue")
            # done PER ITEM, checked on the FIRST line only (the verbose numbered-queue descriptions
            # mention 'closed'/'done' about OTHER things — never scan the whole block for it).
            first = (blk.strip().splitlines() or [""])[0].upper()
            item_done = item.get("status") in ("✅", "❌", "✓", "✗") or \
                bool(re.search(r"\b(DONE|NO-GO|SUBSUMED|DEPRECATED)\b|DEAD END", first))
            if section_closed or item_done:
                out["closed"].append(item)
            else:
                out["open"].append(item)
                if is_takeable and out["takeable_id"] is None:
                    out["takeable_id"] = item["id"] or item["title"]
    open_ids = {i["id"] for i in out["open"] if i["id"]}
    out["closed"] = _dedup_items(i for i in out["closed"] if not (i["id"] and i["id"] in open_ids))
    out["open"] = _dedup_items(out["open"])
    return out

def _dedup_items(items) -> list[dict]:
    seen, keep = set(), []
    for i in items:
        k = i["id"] or i["title"]
        if k in seen:
            continue
        seen.add(k)
        keep.append(i)
    return keep

def _steering(box: str) -> dict:
    """Operator steering inbox (STEERING.md): pending notes + recently consumed.

    The worker reads STEERING.md at each unit's Orient, acts on the top Inbox note, and moves
    every consumed note out of the Inbox (-> Picked up / Dropped). So `pending` is exactly what
    the human has queued that the worker has not yet touched — the thing to glance at from a
    phone to confirm a steer was received and what became of it."""
    md = _read(os.path.join(box, "STEERING.md"))
    if not md.strip():
        return {"present": False, "pending": [], "recent": [], "picked_n": 0, "dropped_n": 0}

    def _items(header: str) -> list[dict]:
        out = []
        for blk in _bullets(_section(md, header)):
            text = re.sub(r"^[-*]\s+", "", blk.strip())
            head = text.split("\n", 1)[0]
            cont = " ".join(l.strip() for l in text.split("\n")[1:] if l.strip())
            tsm = re.match(r"\[([^\]]+)\]\s*", head)
            ts = tsm.group(1) if tsm else None
            if tsm:
                head = head[tsm.end():]
            tagm = re.search(r"\[(BOX|HOST|EITHER)\]", head, re.I)
            tag = tagm.group(1).upper() if tagm else None
            research = bool(re.search(r"\(research\)", head, re.I))
            tm = re.search(r"\*\*(.+?)\*\*", head, re.S)
            title = tm.group(1) if tm else head
            title = re.sub(r"\s+", " ", re.sub(r"[*`]", "", title)).strip()[:160]
            rm = re.search(r"(?:→|->)\s*(.+)$", head)          # the "→ what the worker did" tail
            note = (rm.group(1).strip() if rm else cont) or None
            out.append({"ts": ts, "tag": tag, "research": research,
                        "title": title, "note": (note[:160] if note else None)})
        return out

    picked, dropped = _items("Picked up"), _items("Dropped")
    return {
        "present": True,
        "pending": _items("Inbox"),
        "picked_n": len(picked),
        "dropped_n": len(dropped),
        "recent": ([{**p, "state": "picked"} for p in picked[:4]]
                   + [{**d, "state": "dropped"} for d in dropped[:4]]),
    }

# ─────────────────────────────────────────────────────────────────────────────
# ledger → models table + findings (carried over from the single-box dashboard)
# ─────────────────────────────────────────────────────────────────────────────
_QUANT_SUFFIX = re.compile(r"[-_](Q\d[_A-Za-z0-9]*|IQ\d[_A-Za-z0-9]*|TQ\d[_A-Za-z0-9]*|f16|bf16|fp16)$", re.I)

def _norm_model(name: str) -> str:
    n = name or "?"
    for _ in range(2):
        n2 = _QUANT_SUFFIX.sub("", n)
        if n2 == n:
            break
        n = n2
    return n

def _is_gpu(cfg: dict) -> bool:
    b = (cfg.get("backend") or "").upper()
    if "CUDA" in b or "GPU" in b:
        return True
    ngl = cfg.get("n_gpu_layers")
    return bool(ngl) and ngl not in (0, "0")

def _models_table(recs: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for r in recs:
        cfg = r.get("config") or {}
        raw = cfg.get("model")
        if not raw:
            continue
        model = _norm_model(raw)
        quant = cfg.get("quant", "") or ""
        key = (model, quant)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"model": model, "quant": quant,
                               "cpu_decode": None, "gpu_decode": None,
                               "cpu_prefill": None, "gpu_prefill": None,
                               "agentic_score": None,
                               "quality": None, "bpb": None, "math_pass": None, "code_pass": None,
                               "roofline_eff": None, "depth_drop_pct": None,
                               "statuses": [], "n": 0, "last_epoch": 0}
        g["n"] += 1
        g["statuses"].append(r.get("status"))
        g["last_epoch"] = max(g["last_epoch"], r.get("epoch") or 0)
        gpu = _is_gpu(cfg)
        def _hi(field, val):
            if val is None:
                return
            cur = g[field]
            g[field] = val if cur is None else max(cur, val)
        _hi("gpu_decode" if gpu else "cpu_decode", r.get("decode_tok_s"))
        _hi("gpu_prefill" if gpu else "cpu_prefill", r.get("prefill_tok_s"))
        for f in ("agentic_score", "quality", "bpb", "math_pass", "code_pass", "roofline_eff"):
            src = "roofline_efficiency" if f == "roofline_eff" else f
            v = r.get(src)
            if v is not None and g[f] is None:
                g[f] = v
    rows = list(groups.values())
    for g in rows:
        g["best_decode"] = g["gpu_decode"] if g["gpu_decode"] is not None else g["cpu_decode"]
        # bug A: label the quality number by its TRUE source. BPB is the cross-model ruler
        # (lower = better); math/code is the objective grader; the raw `quality` scalar is a
        # single-model number and is NOT Elo — never blanket-label a populated `quality` "elo".
        if g["agentic_score"] is not None:
            g["quality_kind"] = "agentic"                    # v0.4 ranked coordinate (0..100 display)
            g["quality_score"] = round(g["agentic_score"] * 100, 1)
        elif g["bpb"] is not None:
            g["quality_kind"] = "bpb"
            g["quality_score"] = round(g["bpb"], 4)
        elif g["math_pass"] is not None or g["code_pass"] is not None:
            m = g["math_pass"] or 0.0
            c = g["code_pass"] or 0.0
            g["quality_kind"] = "objective"
            g["quality_score"] = round((m + c) / 2 * 100, 1)
        elif g["quality"] is not None:
            g["quality_kind"] = "quality"      # single-model scalar; NOT cross-model Elo
            g["quality_score"] = g["quality"]
        else:
            g["quality_kind"] = None
            g["quality_score"] = None
        order = ["blessed", "contender", "degenerate", "couldnt_load", "failed"]
        g["status"] = next((s for s in order if s in g["statuses"]), (g["statuses"] or [None])[0])
    rows.sort(key=lambda g: (g["best_decode"] is None, -(g["best_decode"] or 0)))
    return rows

_FIND_RE = re.compile(r"\b(NOVEL|KEY FINDING|VERDICT|DEFINITIVE|SYNTHESIS|HEADLINE|WIN|DEAD END|"
                      r"CONFIRMED|NEGATIVE|ANTIDOTE|CONTRIBUTION|TEED UP)\b")

def _findings(recs: list[dict]) -> list[dict]:
    out = []
    for r in recs:
        notes = r.get("notes", "") or ""
        mark = _FIND_RE.search(notes)
        if r.get("status") == "blessed" or mark:
            cfg = r.get("config") or {}
            title = cfg.get("finding") or cfg.get("experiment") or cfg.get("model") or "finding"
            lede = re.split(r"(?<=[.)])\s", notes.strip(), maxsplit=1)[0][:240]
            out.append({"id": r.get("id"), "status": r.get("status"),
                        "tag": (mark.group(1) if mark else "BLESSED"),
                        "title": str(title)[:60], "note": lede, "epoch": r.get("epoch")})
    out.sort(key=lambda f: -(f["epoch"] or 0))
    return out[:12]

# ─────────────────────────────────────────────────────────────────────────────
# window phase + live agent activity (the run_window driver writes these host-side)
# ─────────────────────────────────────────────────────────────────────────────
def _window(camp: dict, now: float) -> dict:
    start = camp.get("start_epoch") or 0
    deadline = camp.get("deadline_epoch") or 0
    margin = camp.get("winddown_margin_frac")
    margin = 0.10 if margin is None else margin
    winddown = int(deadline - (deadline - start) * margin) if deadline else 0
    state = camp.get("state") or "running"
    if state in ("completed", "done") or (deadline and now >= deadline):
        phase = "done"
    elif winddown and now >= winddown:
        phase = "winddown"
    else:
        phase = "running"
    return {
        "phase": phase,
        "state": state,
        "start_epoch": start or None,
        "deadline_epoch": deadline or None,
        "winddown_epoch": winddown or None,
        "to_winddown_s": (winddown - now) if winddown else None,
        "to_deadline_s": (deadline - now) if deadline else None,
    }

_UNIT_RE = re.compile(r"^(unit|consolidate)_(\d+)(?:_(\d+))?\.jsonl?$")  # .json (old) or .jsonl (stream)

def _tail(path: str, nbytes: int) -> str:
    """Read at most the last nbytes of a file, dropping a leading partial line if we seeked.
    Lets us find the trailing result line / recent events in a large growing stream without
    re-reading megabytes every poll."""
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as f:
            if sz > nbytes:
                f.seek(sz - nbytes)
            data = f.read()
        text = data.decode("utf-8", "ignore")
        return text.split("\n", 1)[-1] if sz > nbytes else text
    except OSError:
        return ""

def _jsonl(text: str) -> list[dict]:
    """Parse JSONL, tolerating a torn trailing line (concurrent append)."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            pass
    return out

def _iso_epoch(s):
    """Parse a `date -Is` driver-log timestamp (e.g. 2026-06-30T13:47:19-07:00) to epoch."""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None

def _tool_brief(name, inp: dict) -> str:
    """The command/target a tool_use is about — the 'commands' the agent is running."""
    inp = inp or {}
    if name == "Bash":
        return inp.get("command", "")
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return inp.get("file_path", "") or inp.get("notebook_path", "")
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if name == "Task":
        return inp.get("description", "")
    try:
        return json.dumps(inp)
    except (TypeError, ValueError):
        return str(inp)

def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                c = b.get("content", b.get("text", ""))
                parts.append(c if isinstance(c, str) else json.dumps(c))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return ""

def _unit_events(objs: list[dict], max_events: int = 80, max_chars: int = 700) -> list[dict]:
    """Flatten stream-json into a renderable transcript: thinking / text / tool_use / tool_result."""
    ev = []
    for o in objs:
        t = o.get("type")
        if t == "assistant":
            for b in (o.get("message", {}).get("content") or []):
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    ev.append({"kind": "thinking", "text": (b.get("thinking") or "")[:max_chars]})
                elif bt == "text":
                    txt = (b.get("text") or "").strip()
                    if txt:
                        ev.append({"kind": "text", "text": txt[:max_chars]})
                elif bt == "tool_use":
                    ev.append({"kind": "tool", "name": b.get("name"),
                               "cmd": _tool_brief(b.get("name"), b.get("input"))[:max_chars]})
        elif t == "user":
            txt = _result_text(o.get("message", {}).get("content")).strip()
            if txt:
                ev.append({"kind": "result", "text": txt[:max_chars]})
    return ev[-max_events:]

def _token_tele(result: dict, objs: list) -> dict:
    """Per-unit token / context / tok-s telemetry from a stream-json result line (+ the tailed
    events for an approximate peak context fill). Every field tolerates absence -> 0/None."""
    u = result.get("usage") or {}
    mu = result.get("modelUsage") or {}
    out = u.get("output_tokens") or 0
    dapi = result.get("duration_api_ms") or 0
    models, ctx_window = {}, 0
    for mid, mv in mu.items():                 # per-model split (main vs sub-agent, e.g. sonnet vs haiku)
        models[mid] = {"input": mv.get("inputTokens") or 0, "output": mv.get("outputTokens") or 0,
                       "cache_read": mv.get("cacheReadInputTokens") or 0,
                       "cache_creation": mv.get("cacheCreationInputTokens") or 0,
                       "cost": mv.get("costUSD") or 0.0, "ctx_window": mv.get("contextWindow") or 0}
        ctx_window = max(ctx_window, mv.get("contextWindow") or 0)
    ctx_peak = 0                               # late-run assistant turns in the tail ~= peak context fill
    for o in objs:
        if o.get("type") == "assistant":
            au = (o.get("message") or {}).get("usage") or {}
            ctx_peak = max(ctx_peak, (au.get("input_tokens") or 0) + (au.get("cache_read_input_tokens") or 0)
                           + (au.get("cache_creation_input_tokens") or 0))
    return {"input": u.get("input_tokens") or 0, "output": out,
            "cache_read": u.get("cache_read_input_tokens") or 0,
            "cache_creation": u.get("cache_creation_input_tokens") or 0,
            "dapi": dapi, "tok_s": round(out / (dapi / 1000.0), 1) if (out and dapi) else None,
            "ctx_peak": ctx_peak, "ctx_window": ctx_window, "models": models}


_EST_DIV = 4  # chars-per-token heuristic (dependency-free; ~English/mixed). Approximate — surfaced as such.

def _est_tokens(text: str) -> int:
    return round(len(text) / _EST_DIV)

def _ingest_costs(boxes: list) -> list:
    """Approximate token cost to ingest the key context files (dep-free chars/4). Lets the
    orchestrating agent see what reading each file 'costs' its context window."""
    import glob as _glob
    root = os.path.dirname(SCAFFOLD)
    paths = sorted(_glob.glob(os.path.join(root, "doctrine", "*.md")))
    paths += [os.path.join(root, "scaffold", "prompts", p) for p in ("unit.md", "consolidate.md")]
    for box in boxes:
        paths += [os.path.join(box, f) for f in ("MEMORY.md", "campaign.json", "STEERING.md")]
    out = []
    for p in paths:
        txt = _read(p)
        if not txt:
            continue
        out.append({"file": os.path.relpath(p, root).replace("\\", "/"),
                    "bytes": len(txt.encode("utf-8")), "est_tokens": _est_tokens(txt)})
    return out


_UNIT_CACHE: dict = {}   # path -> ((mtime_ns, size), rec). Historical unit files are immutable, so
                         # after the first scan only the growing in-flight file is re-parsed — turns a
                         # ~10s all-files scan (1097 files / 266MB) into a sub-100ms poll.

def _parse_unit(path: str, fn: str, m):
    """Parse one unit jsonl into its rec dict (cost/tokens/tele/…), memoized by (mtime, size).
    A unit with a result line is COMPLETE and immutable, so once cached it's returned WITHOUT even
    a stat() — the key perf win on WSL/DrvFs, where 1097 per-file stat syscalls otherwise dominate."""
    c = _UNIT_CACHE.get(path)
    if c is not None and c[1].get("has_result"):
        return c[1]
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if c is not None and c[0] == key:
        return c[1]
    sz = st.st_size
    rec = {"name": fn, "path": path, "kind": m.group(1), "epoch": int(m.group(2)),
           "num": int(m.group(3)) if m.group(3) else None,
           "empty": sz == 0, "torn": False, "has_result": False,
           "cost": None, "is_error": None, "duration_ms": None,
           "num_turns": None, "terminal_reason": None, "subtype": None, "tele": None}
    if sz > 0:
        objs = _jsonl(_tail(path, 96_000))  # the result line is at the tail
        result = next((o for o in reversed(objs) if o.get("type") == "result"), None)
        if result:
            rec["has_result"] = True
            rec.update(cost=result.get("total_cost_usd"), is_error=result.get("is_error"),
                       duration_ms=result.get("duration_ms"), num_turns=result.get("num_turns"),
                       terminal_reason=result.get("terminal_reason") or result.get("stop_reason"),
                       subtype=result.get("subtype"))
            rec["tele"] = _token_tele(result, objs)
        elif not objs:
            rec["torn"] = True  # non-empty but nothing parseable yet
    _UNIT_CACHE[path] = (key, rec)
    return rec


def _agent(box: str, win_start) -> dict:
    """Live agent activity from the per-box driver log + per-unit `claude -p` JSON.

    Tolerant of concurrent writes: a 0-byte file = a unit launched but not yet finished;
    a non-JSON body = a torn trailing write. Neither crashes the panel.
    """
    work = os.path.join(box, "work")
    logp = os.path.join(work, "run_window.log")
    rundir = os.path.join(work, "run_window")
    sentinel = os.path.join(work, "QUEUE_EMPTY")
    info = {
        "log_present": os.path.exists(logp),
        "queue_empty": os.path.exists(sentinel),
        "current_window_line": None,
        "current_unit": None,
        "driver_terminal": None,
        "running": False,
        "units_launched": 0,
        "units_completed": 0,
        "cost_window_usd": 0.0,
        "cost_all_usd": 0.0,
        "last_unit": None,
        "recent_units": [],
        "tokens_window": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "tokens_all": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "tok_s_window": None, "ctx_peak_window": 0, "ctx_window": 0, "models_window": {},
    }

    # ---- driver log: isolate the current window (everything after the last START) ----
    lines = [ln for ln in _read(logp).splitlines() if ln.strip()]
    start_idx = None
    for i, ln in enumerate(lines):
        if " START box=" in ln:
            start_idx = i
    window_lines = lines[start_idx:] if start_idx is not None else []
    maxunit, terminal, cur_research, research_units = 0, None, False, 0
    cur_started_iso = None
    research_started_iso, research_unit_num = None, None
    for ln in window_lines:
        m = re.search(r"run_window:\s+(\S+)\s+launch unit #(\d+)", ln)
        if m:
            iso, n, is_r = m.group(1), int(m.group(2)), ("RESEARCH" in ln)
            if is_r:
                research_units += 1
                research_started_iso, research_unit_num = iso, n
            if n >= maxunit:
                maxunit, cur_research, cur_started_iso = n, is_r, iso
        if "WINDDOWN reached" in ln:
            terminal = "winddown"
        elif "QUEUE_EMPTY sentinel" in ln:
            terminal = "queue_empty"
        elif " DONE box=" in ln:
            terminal = "done"
    if window_lines:
        info["current_window_line"] = window_lines[0].strip()
    info["current_unit"] = maxunit or None
    info["current_is_research"] = cur_research
    info["research_units"] = research_units
    info["current_started_iso"] = cur_started_iso
    info["current_started_epoch"] = _iso_epoch(cur_started_iso)
    info["driver_terminal"] = terminal

    # ---- per-unit claude -p output (JSONL stream, or legacy single-object json) ----
    # A unit is COMPLETE once its file contains a {"type":"result"} line; until then it is
    # in-flight (stream-json grows it live). Legacy single-object .json files are one result
    # line, so they parse identically — backward compatible.
    try:
        files = sorted(os.listdir(rundir))
    except OSError:
        files = []
    recs = []
    for fn in files:
        m = _UNIT_RE.match(fn)
        if not m:
            continue
        rec = _parse_unit(os.path.join(rundir, fn), fn, m)
        if rec is not None:
            recs.append(rec)
    recs.sort(key=lambda r: (r["epoch"], r["num"] or 0))

    win_start = win_start or 0
    for r in recs:
        if r["cost"]:
            info["cost_all_usd"] += r["cost"]
            if r["epoch"] >= win_start:
                info["cost_window_usd"] += r["cost"]
    # token / context telemetry aggregation (v0.4): sum tokens window+all, tok/s window, per-model split
    dapi_win = 0.0
    for r in recs:
        t = r.get("tele")
        if not t:
            continue
        in_win = r["epoch"] >= win_start
        for k in ("input", "output", "cache_read", "cache_creation"):
            info["tokens_all"][k] += t[k]
            if in_win:
                info["tokens_window"][k] += t[k]
        if in_win:
            dapi_win += t["dapi"]
            info["ctx_peak_window"] = max(info["ctx_peak_window"], t["ctx_peak"])
            info["ctx_window"] = max(info["ctx_window"], t["ctx_window"])
            for mid, mv in t["models"].items():
                agg = info["models_window"].setdefault(
                    mid, {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "cost": 0.0})
                for k in ("input", "output", "cache_read", "cache_creation", "cost"):
                    agg[k] += mv[k]
    ow = info["tokens_window"]["output"]
    info["tok_s_window"] = round(ow / (dapi_win / 1000.0), 1) if (ow and dapi_win) else None
    for mv in info["models_window"].values():
        mv["cost"] = round(mv["cost"], 4)
    window_units = [r for r in recs if r["epoch"] >= win_start]
    info["units_launched"] = max(info["current_unit"] or 0, len(window_units))
    info["units_completed"] = sum(1 for r in window_units if r["has_result"])
    newest = recs[-1] if recs else None
    in_flight = bool(newest and not newest["has_result"])
    info["running"] = (terminal is None) and in_flight
    done = [r for r in recs if r["has_result"]]
    if done:
        info["last_unit"] = {**{k: done[-1][k] for k in
                             ("name", "kind", "num", "epoch", "cost", "is_error",
                              "duration_ms", "num_turns", "terminal_reason", "subtype")},
                             "tele": done[-1].get("tele")}
    info["recent_units"] = [
        {**{k: r[k] for k in ("kind", "num", "epoch", "cost", "is_error",
                              "duration_ms", "num_turns", "empty", "torn", "has_result")},
         "tele": r.get("tele")}
        for r in recs[-8:]]

    # ---- live transcript: the newest unit's stream (in-flight if running, else last done) ----
    if newest and not newest["empty"]:
        objs = _jsonl(_tail(newest["path"], 300_000))
        info["live"] = {"name": newest["name"], "kind": newest["kind"], "num": newest["num"],
                        "running": in_flight, "events": _unit_events(objs)}
    else:
        info["live"] = {"name": (newest["name"] if newest else None),
                        "num": (newest["num"] if newest else None),
                        "running": bool(newest), "events": []}

    # ---- latest research round: the newest [RESEARCH] unit's summary + freshness ----
    rr = None
    if research_unit_num is not None:
        rfile = next((r for r in reversed(recs)
                      if r["num"] == research_unit_num and r["epoch"] >= win_start), None)
        summary, running_r = "", False
        if rfile:
            running_r = (research_unit_num == maxunit) and not rfile["has_result"]
            robjs = _jsonl(_tail(rfile["path"], 300_000))
            result = next((o for o in reversed(robjs) if o.get("type") == "result"), None)
            if result and isinstance(result.get("result"), str):
                summary = result["result"][:1800]
            else:  # not finished yet — show the most recent narration
                texts = [e["text"] for e in _unit_events(robjs) if e["kind"] == "text"]
                summary = "\n\n".join(texts[-3:])[:1800]
        rr = {"unit": research_unit_num, "started_iso": research_started_iso,
              "started_epoch": _iso_epoch(research_started_iso),
              "running": running_r, "summary": summary}
    info["research_round"] = rr

    info["cost_window_usd"] = round(info["cost_window_usd"], 4)
    info["cost_all_usd"] = round(info["cost_all_usd"], 4)
    return info

# ─────────────────────────────────────────────────────────────────────────────
# hardware contract summary (the roofline denominator + ISA/GPU/RAM)
# ─────────────────────────────────────────────────────────────────────────────
def _hardware(box: str) -> dict:
    hw = _read_json(os.path.join(box, "hardware.json"), {}) or {}
    isa = hw.get("isa") or {}
    topo = hw.get("topology") or {}
    mem = hw.get("memory") or {}
    gpu = hw.get("gpu") or {}
    total = mem.get("total_bytes")
    return {
        "bandwidth_gbps": hw.get("bandwidth_gbps"),
        "bandwidth_reason": hw.get("bandwidth_reason"),
        "isa": [k.upper() for k, v in isa.items() if v],
        "cpu": hw.get("model_name"),
        "arch": hw.get("arch"),
        "cores": topo.get("nproc"),
        "ram_gb": round(total / 1e9, 1) if total else None,
        "ram_speed": mem.get("dimm_speed"),
        "gpu": (gpu.get("name") if gpu.get("present") else None),
        "gpu_arch": gpu.get("arch"),
        "vram_mib": gpu.get("vram_total_mib"),
    }

# ─────────────────────────────────────────────────────────────────────────────
# per-box payload
# ─────────────────────────────────────────────────────────────────────────────
def _conclusion(box: str, md: str, win: dict) -> dict:
    """The sealed window document, surfaced the moment the run concludes.

    Wind-down procedure (scaffold/run_window.sh + prompts/consolidate.md): at wind-down the
    consolidate unit reconciles MEMORY.md against the ledger and writes a FINAL report, then
    the relauncher sets campaign.json.state = "completed" -> _window() reports phase "done".
    So once `phase == "done"` the box MEMORY.md IS the consolidated, sealed document; we ship
    its full markdown (and a pointer to the FINAL report) so the dashboard can show it in the
    window the instant the run ends. Kept empty while running to keep the live payload light.
    """
    concluded = win.get("phase") == "done"
    report = None
    try:
        finals = sorted(fn for fn in os.listdir(os.path.join(box, "reports"))
                        if fn.startswith("FINAL_") and fn.endswith(".md"))
        report = finals[-1] if finals else None
    except OSError:
        pass
    return {
        "concluded": concluded,
        "memory_md": md if (concluded and md) else "",
        "report_name": report,
    }

def _reports(box: str) -> list[dict]:
    """Every FINAL_*.md in the box's reports/ dir, newest first, with whether a PDF exists.

    The dashboard links each to /report/<box_dir>/<stem>.pdf; the server renders the PDF
    lazily on first open (report_pdf) if it isn't there yet. `pdf_ready` lets the panel show
    'generates on open' for reports not yet rendered."""
    rd = os.path.join(box, "reports")
    out = []
    try:
        mds = sorted((fn for fn in os.listdir(rd) if re.match(r"FINAL_.*\.md$", fn)), reverse=True)
    except OSError:
        return out
    for idx, md in enumerate(mds):
        stem = md[:-3]
        try:
            st = os.stat(os.path.join(rd, md))
            mtime, size_kb = st.st_mtime, round(st.st_size / 1024, 1)
        except OSError:
            mtime, size_kb = None, None
        m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
        pdf_ready = os.path.isfile(os.path.join(rd, stem + ".pdf"))
        out.append({
            "stem": stem, "md": md, "pdf": stem + ".pdf",
            "html": (stem + ".html") if os.path.isfile(os.path.join(rd, stem + ".html")) else None,
            "date": m.group(1) if m else None,
            "pdf_ready": pdf_ready, "mtime": mtime, "size_kb": size_kb,
            "is_latest": idx == 0,
        })
    return out

def build_box(box: str, now: float) -> dict:
    camp = _read_json(os.path.join(box, "campaign.json"), {}) or {}
    recs = ledger.load(os.path.join(box, "ledger.jsonl"))
    front = ledger.pareto_front(recs)
    md = _read(os.path.join(box, "MEMORY.md"))
    win = _window(camp, now)

    deadline = camp.get("deadline_epoch")
    remaining = (deadline - now) if deadline else None

    def axis_best(key):
        vals = [r[key] for r in front if r.get(key) is not None]
        if not vals:
            return None
        winner = max(front, key=lambda r: r.get(key) or -1)
        return {"value": max(vals), "config": winner.get("config", {}), "id": winner.get("id")}

    tally = Counter(r.get("status") for r in recs)
    top = max(front, key=lambda r: r.get("decode_tok_s") or -1) if front else None
    depth = _read_json(os.path.join(box, "depth.json"), None)
    nick = camp.get("nickname") or os.path.basename(box.rstrip("/"))

    return {
        "nick": nick,
        "box_dir": os.path.basename(box.rstrip("/")),
        "campaign": camp,
        "window": win,
        "agent": _agent(box, win.get("start_epoch")),
        "queue": _queue(md),
        "steering": _steering(box),
        "hardware": _hardware(box),
        "hardware_live": _read_json(os.path.join(box, "work", "hw_status.json"), None),  # last on-demand probe
        "paused": os.path.exists(os.path.join(box, "work", "PAUSE")),
        "remaining_s": remaining,
        "counts": dict(tally),
        "n_records": len(recs),
        "front": [{
            "id": r.get("id"),
            "decode_tok_s": r.get("decode_tok_s"),
            "prefill_tok_s": r.get("prefill_tok_s"),
            "ttft_s": r.get("ttft_s"),
            "quality": r.get("quality"),
            "agentic_score": r.get("agentic_score"),
            "perf_per_watt": r.get("perf_per_watt"),
            "roofline_efficiency": r.get("roofline_efficiency"),
            "roofline_ceiling_tok_s": r.get("roofline_ceiling_tok_s"),
            "status": r.get("status"),
            "model": (r.get("config") or {}).get("model", "?"),
            "quant": (r.get("config") or {}).get("quant", ""),
        } for r in front],
        "timeline": [{
            "epoch": r.get("epoch"), "status": r.get("status"),
            "decode_tok_s": r.get("decode_tok_s"),
        } for r in recs[-200:]],
        "best": {
            "decode": axis_best("decode_tok_s"),
            "prefill": axis_best("prefill_tok_s"),
            "quality": axis_best("quality"),
            "agentic": axis_best("agentic_score"),
        },
        "roofline_now": None if not top else {
            "efficiency": top.get("roofline_efficiency"),
            "achieved": top.get("decode_tok_s"),
            "ceiling": top.get("roofline_ceiling_tok_s"),
            "model": (top.get("config") or {}).get("model", "?"),
        },
        "phase": _phase(md),
        "memory_present": bool(md),
        "conclusion": _conclusion(box, md, win),
        "reports": _reports(box),
        "models": _models_table(recs),
        "findings": _findings(recs),
        "depth": depth,
        "windows": camp.get("prior_windows", []),
    }

def build_data() -> dict:
    now = time.time()
    boxes = []
    for b in BOXES:
        try:
            boxes.append(build_box(b, now))
        except Exception as e:  # one bad box must not blank the whole fleet
            boxes.append({"nick": os.path.basename(b.rstrip("/")),
                          "box_dir": os.path.basename(b.rstrip("/")), "error": str(e)})
    return {"now": now, "boxes": boxes, "ingest": _ingest_costs(BOXES)}


# ─────────────────────────────────────────────────────────────────────────────
# box discovery
# ─────────────────────────────────────────────────────────────────────────────
def discover_boxes(path: str) -> list[str]:
    """A path is either a box (has campaign.json) or a parent dir of boxes. Enumerate."""
    path = os.path.abspath(path)
    if os.path.isfile(os.path.join(path, "campaign.json")):
        return [path]
    out = []
    try:
        for name in sorted(os.listdir(path)):
            d = os.path.join(path, name)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "campaign.json")):
                out.append(d)
    except OSError:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# write-API (0.4): the Control page POSTs /action/<name> here; each shells to a vetted CLI. The server
# binds 127.0.0.1 (localhost-only); the box is validated against the served set; every arg is passed as
# an argv LIST (never a shell string), so there is no command injection.
# ─────────────────────────────────────────────────────────────────────────────
def _canon_box(box):
    want = str(box or "").rstrip("/")
    for b in BOXES:
        if b.rstrip("/") == want or os.path.basename(b.rstrip("/")) == want:
            return b
    return None

def _run(argv, timeout=30):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return {"rc": p.returncode, "out": (p.stdout or "").strip(), "err": (p.stderr or "").strip()}

def _do_action(action, params):
    box = _canon_box(params.get("box"))
    if not box:
        return {"ok": False, "error": f"unknown box: {params.get('box')!r}"}
    PY = ["python3"]
    try:
        if action == "steer":
            text = (params.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty steering note"}
            argv = PY + ["scaffold/steer.py", box, text]
            if params.get("tag"):
                argv += ["--tag", str(params["tag"])]
            if params.get("research"):
                argv += ["--research"]
            r = _run(argv); return {"ok": r["rc"] == 0, "message": r["out"] or r["err"]}
        if action == "steer-delete":
            r = _run(PY + ["scaffold/steer.py", box, "--delete", str(int(params.get("n") or 0))])
            return {"ok": r["rc"] == 0, "message": r["out"] or r["err"]}
        if action == "queue-inject":
            text = (params.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty queue item"}
            argv = PY + ["scaffold/queue.py", box, text]
            if params.get("tag"):
                argv += ["--tag", str(params["tag"])]
            r = _run(argv); return {"ok": r["rc"] == 0, "message": r["out"] or r["err"]}
        if action == "window-add":
            argv = PY + ["scaffold/window.py", box, "add-hours", str(float(params.get("hours")))]
            if params.get("force"):
                argv += ["--force"]
            r = _run(argv); return {"ok": r["rc"] == 0, "message": r["out"] or r["err"]}
        if action in ("pause", "resume", "hard-kill"):
            r = _run(PY + ["scaffold/window.py", box, action])
            return {"ok": r["rc"] == 0, "message": r["out"] or r["err"]}
        if action == "stop":                        # clean = graceful (finish unit); force = default (kill unit)
            argv = PY + ["scaffold/window.py", box, "stop"] + (["--graceful"] if params.get("graceful") else [])
            r = _run(argv); return {"ok": r["rc"] == 0, "message": r["out"] or r["err"]}
        if action == "run-start":
            camp = _read_json(os.path.join(box, "campaign.json"), {}) or {}
            if camp.get("state") == "running":
                return {"ok": False, "error": "a run is already active — stop it first"}
            hours = params.get("hours")
            argv = ["bash", "scaffold/run_window.sh", box] + ([str(int(hours))] if hours else [])
            logp = os.path.join(box, "work", "run_window.nohup.log")
            os.makedirs(os.path.dirname(logp), exist_ok=True)
            subprocess.Popen(argv, stdout=open(logp, "a"), stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
            return {"ok": True, "message": f"run started ({hours}h) — detached" if hours else "run continuing current window"}
        if action == "hw-refresh":
            subprocess.run(PY + ["scaffold/boxpaths.py", box, "--wake"], capture_output=True, timeout=20)  # best-effort wake
            try:
                ssh = subprocess.check_output(PY + ["scaffold/boxpaths.py", box, "--ssh"], text=True, timeout=15).split()
            except Exception as e:
                return {"ok": False, "error": f"resolver: {e}"}
            try:
                with open(os.path.join(SCAFFOLD, "hw_probe.sh")) as f:
                    r = subprocess.run(ssh + ["bash -s"], stdin=f, capture_output=True, text=True, timeout=35)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "probe timed out — box unreachable/asleep"}
            if r.returncode != 0 or not r.stdout.strip():
                return {"ok": False, "error": (r.stderr or "probe failed")[:200]}
            try:
                hw = json.loads(r.stdout)
            except Exception:
                return {"ok": False, "error": "probe returned non-JSON"}
            try:
                json.dump(hw, open(os.path.join(box, "work", "hw_status.json"), "w"))
            except OSError:
                pass
            return {"ok": True, "hw": hw}
        return {"ok": False, "error": f"unknown action: {action}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "action timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def _send_file(self, data: bytes, ctype: str, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_report(self):
        """GET /report/<box_dir>/<FINAL_*.pdf|md|html> — serve a report file (PDF inline).

        A requested .pdf that doesn't exist yet is rendered on the fly from the sibling .md;
        if no PDF engine is available it falls back to the styled .html. Strictly sandboxed to
        each box's reports/ dir (validated name + realpath containment)."""
        from urllib.parse import urlparse, unquote
        parts = [unquote(p) for p in urlparse(self.path).path.split("/") if p]
        if len(parts) != 3:                      # ["report", box_dir, filename]
            return self._send(404, "not found", "text/plain")
        _, box_dir, fname = parts
        if not _REPORT_NAME_RE.match(fname):
            return self._send(404, "bad report name", "text/plain")
        box = {os.path.basename(b.rstrip("/")): b for b in BOXES}.get(box_dir)
        if not box:
            return self._send(404, "unknown box", "text/plain")
        rd = os.path.realpath(os.path.join(box, "reports"))
        target = os.path.realpath(os.path.join(rd, fname))
        if target != rd and not target.startswith(rd + os.sep):   # traversal guard
            return self._send(404, "not found", "text/plain")
        ext = fname.rsplit(".", 1)[-1].lower()
        if ext == "pdf" and not os.path.isfile(target) and report_pdf is not None:
            md = os.path.join(rd, fname[:-4] + ".md")             # lazy render from the .md
            if os.path.isfile(md):
                try:
                    report_pdf.ensure_pdf(md)
                except Exception:
                    pass
            if not os.path.isfile(target):                        # engine missing -> html fallback
                alt = target[:-4] + ".html"
                if os.path.isfile(alt):
                    target, ext = alt, "html"
        if not os.path.isfile(target):
            return self._send(404, "report not available", "text/plain")
        ctype = {"pdf": "application/pdf",
                 "html": "text/html; charset=utf-8",
                 "md": "text/markdown; charset=utf-8"}.get(ext, "application/octet-stream")
        try:
            with open(target, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, "not found", "text/plain")
        self._send_file(data, ctype, os.path.basename(target))

    def do_POST(self):
        if self.path.split("?")[0].startswith("/action/"):
            action = self.path.split("?")[0][len("/action/"):]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                params = json.loads(self.rfile.read(n) if n else b"{}")
            except Exception:
                params = {}
            res = _do_action(action, params if isinstance(params, dict) else {})
            self._send(200 if res.get("ok") else 400, json.dumps(res), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_GET(self):
        if self.path.startswith("/data"):
            try:
                self._send(200, json.dumps(build_data()), "application/json")
            except Exception as e:  # never 500 the panel; show the error in-band
                self._send(200, json.dumps({"error": str(e)}), "application/json")
        elif self.path.startswith("/report/"):
            try:
                self._serve_report()
            except Exception as e:
                self._send(500, "report error: " + str(e), "text/plain")
        elif self.path.split("?")[0] in ("/", "/index.html"):   # tolerate ?cache-bust query strings
            self._send(200, _read(os.path.join(HERE, "index.html")), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")


def main() -> int:
    global BOXES
    ap = argparse.ArgumentParser(description="crucible fleet dashboard (host-side, read-only)")
    ap.add_argument("paths", nargs="+",
                    help="box folder(s) and/or the parent boxes/ dir to enumerate")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    seen, BOXES = set(), []
    for p in a.paths:
        for b in discover_boxes(p):
            if b not in seen:
                seen.add(b)
                BOXES.append(b)
    if not BOXES:
        print(f"[dashboard] no boxes (campaign.json) found under: {a.paths}", file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[dashboard] fleet: {', '.join(os.path.basename(b) for b in BOXES)}")
    print(f"[dashboard] open http://{a.host}:{a.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
