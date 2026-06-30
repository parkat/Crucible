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
import argparse, json, os, re, sys, time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCAFFOLD = os.path.dirname(HERE)
sys.path.insert(0, SCAFFOLD)
import ledger  # noqa: E402  (read-only use of the same front logic the agent writes)

BOXES: list[str] = []  # set in main()

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
    """Parse one top-level queue bullet into {id, title, tags, status, section}."""
    text = re.sub(r"^[-*]\s+", "", block.strip())
    status = None
    m = re.match(r"^([✅❌◐◔◑◕✓✗])\s*", text)
    if m:
        status = m.group(1)
        text = text[m.end():]
    # the item id is the first bracket token that is NOT a resource tag (e.g. [K1], [LFM2-B])
    bid = None
    for mm in re.finditer(r"\[([^\]]+)\]", text):
        if mm.group(1).strip().upper() not in _RES_TAGS:
            bid = mm.group(1).strip()
            break
    tm = re.search(r"\*\*(.+?)\*\*", text, re.S)
    title = tm.group(1) if tm else (text.splitlines() or [""])[0]
    title = re.sub(r"\s+", " ", re.sub(r"[*`]", "", title)).strip()[:140]
    tags = sorted({t.upper() for t in re.findall(r"\[(BOX|HOST|EITHER)\]", text, re.I)})
    return {"id": bid, "title": title, "tags": tags, "status": status, "section": section}

def _bullets(sec: str) -> list[str]:
    """Top-level '- ' bullet blocks (indented sub-units stay attached to their parent)."""
    blocks, cur = [], None
    for ln in sec.splitlines():
        if re.match(r"^[-*] ", ln):
            if cur is not None:
                blocks.append("\n".join(cur))
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]

def _queue(md: str) -> dict:
    """The tagged hypothesis queue: open items (tagged), closed items, takeable-top id."""
    body = _section(md, "Open hypotheses (prioritized queue)") or _section(md, "Open hypotheses")
    out = {"open": [], "closed": [], "takeable_id": None}
    if not body:
        return out
    parts = re.split(r"^###\s+(.*)$", body, flags=re.MULTILINE)
    # parts = [pre, head1, body1, head2, body2, ...]; ignore the pre-amble
    it = iter(parts[1:])
    for head, sec in zip(it, it):
        head = head.strip()
        up = head.upper()
        is_closed = "CLOSED" in up or "DONE" in up
        is_takeable = "TAKEABLE" in up
        for idx, blk in enumerate(_bullets(sec)):
            item = _queue_item(blk, head)
            if is_closed:
                out["closed"].append(item)
            else:
                out["open"].append(item)
                if is_takeable and idx == 0 and out["takeable_id"] is None:
                    out["takeable_id"] = item["id"] or item["title"]
    return out

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
                               "quality": None, "math_pass": None, "code_pass": None,
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
        for f in ("quality", "math_pass", "code_pass", "roofline_eff"):
            src = "roofline_efficiency" if f == "roofline_eff" else f
            v = r.get(src)
            if v is not None and g[f] is None:
                g[f] = v
    rows = list(groups.values())
    for g in rows:
        g["best_decode"] = g["gpu_decode"] if g["gpu_decode"] is not None else g["cpu_decode"]
        if g["quality"] is not None:
            g["quality_kind"] = "elo"
            g["quality_score"] = g["quality"]
        elif g["math_pass"] is not None or g["code_pass"] is not None:
            m = g["math_pass"] or 0.0
            c = g["code_pass"] or 0.0
            g["quality_kind"] = "objective"
            g["quality_score"] = round((m + c) / 2 * 100, 1)
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
        p = os.path.join(rundir, fn)
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        rec = {"name": fn, "path": p, "kind": m.group(1), "epoch": int(m.group(2)),
               "num": int(m.group(3)) if m.group(3) else None,
               "empty": sz == 0, "torn": False, "has_result": False,
               "cost": None, "is_error": None, "duration_ms": None,
               "num_turns": None, "terminal_reason": None, "subtype": None}
        if sz > 0:
            objs = _jsonl(_tail(p, 96_000))  # the result line is at the tail
            result = next((o for o in reversed(objs) if o.get("type") == "result"), None)
            if result:
                rec["has_result"] = True
                rec.update(cost=result.get("total_cost_usd"), is_error=result.get("is_error"),
                           duration_ms=result.get("duration_ms"), num_turns=result.get("num_turns"),
                           terminal_reason=result.get("terminal_reason") or result.get("stop_reason"),
                           subtype=result.get("subtype"))
            elif not objs:
                rec["torn"] = True  # non-empty but nothing parseable yet
        recs.append(rec)
    recs.sort(key=lambda r: (r["epoch"], r["num"] or 0))

    win_start = win_start or 0
    for r in recs:
        if r["cost"]:
            info["cost_all_usd"] += r["cost"]
            if r["epoch"] >= win_start:
                info["cost_window_usd"] += r["cost"]
    window_units = [r for r in recs if r["epoch"] >= win_start]
    info["units_launched"] = max(info["current_unit"] or 0, len(window_units))
    info["units_completed"] = sum(1 for r in window_units if r["has_result"])
    newest = recs[-1] if recs else None
    in_flight = bool(newest and not newest["has_result"])
    info["running"] = (terminal is None) and in_flight
    done = [r for r in recs if r["has_result"]]
    if done:
        info["last_unit"] = {k: done[-1][k] for k in
                             ("name", "kind", "num", "epoch", "cost", "is_error",
                              "duration_ms", "num_turns", "terminal_reason", "subtype")}
    info["recent_units"] = [
        {k: r[k] for k in ("kind", "num", "epoch", "cost", "is_error",
                           "duration_ms", "num_turns", "empty", "torn", "has_result")}
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
        "hardware": _hardware(box),
        "remaining_s": remaining,
        "counts": dict(tally),
        "n_records": len(recs),
        "front": [{
            "id": r.get("id"),
            "decode_tok_s": r.get("decode_tok_s"),
            "prefill_tok_s": r.get("prefill_tok_s"),
            "ttft_s": r.get("ttft_s"),
            "quality": r.get("quality"),
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
    return {"now": now, "boxes": boxes}


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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))

    def do_GET(self):
        if self.path.startswith("/data"):
            try:
                self._send(200, json.dumps(build_data()), "application/json")
            except Exception as e:  # never 500 the panel; show the error in-band
                self._send(200, json.dumps({"error": str(e)}), "application/json")
        elif self.path in ("/", "/index.html"):
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
