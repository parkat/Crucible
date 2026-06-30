#!/usr/bin/env python3
"""
crucible dashboard server — runs on the HOST (never the target).

Zero third-party dependencies (stdlib http.server) so it runs on a minimal host. Serves
the single-page UI and a /data endpoint that reads the box folder's ledger.jsonl,
campaign.json, and MEMORY.md and returns the live campaign state as JSON. Read-only: it
never writes to the campaign.

Usage:
    python3 scaffold/dashboard/server.py boxes/<nickname> [--port 8787]
Then open http://localhost:8787/
"""
from __future__ import annotations
import argparse, json, os, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCAFFOLD = os.path.dirname(HERE)
sys.path.insert(0, SCAFFOLD)
import ledger  # noqa: E402  (read-only use of the same front logic the agent writes)

BOX = None  # set in main()

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""

def _section(md: str, header: str) -> str:
    """Pull the body under a '## <header>' up to the next '## '. Robust to absence."""
    m = re.search(rf"^##\s+{re.escape(header)}\s*$(.*?)(?=^##\s|\Z)", md,
                  re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    body = m.group(1).strip()
    # strip HTML comments and our template placeholders
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
    return body

def _hypotheses(md: str) -> list[str]:
    body = _section(md, "Open hypotheses (prioritized queue)") or _section(md, "Open hypotheses")
    out = []
    for ln in body.splitlines():
        ln = ln.strip()
        m = re.match(r"^(?:\d+\.|[-*])\s+(.*)", ln)
        if m and m.group(1):
            out.append(m.group(1))
    return out[:8]

def _phase(md: str) -> dict:
    body = _section(md, "Current phase")
    fields = {}
    for ln in body.splitlines():
        m = re.match(r"^[-*]\s*\*\*(.+?):\*\*\s*(.*)$", ln.strip()) or \
            re.match(r"^[-*]\s*(.+?):\s*(.*)$", ln.strip())
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
    return fields

_QUANT_SUFFIX = re.compile(r"[-_](Q\d[_A-Za-z0-9]*|IQ\d[_A-Za-z0-9]*|TQ\d[_A-Za-z0-9]*|f16|bf16|fp16)$", re.I)

def _norm_model(name: str) -> str:
    """Collapse a model id to its family by stripping a trailing quant token."""
    n = name or "?"
    for _ in range(2):  # strip up to two trailing quant tokens (e.g. ...-Instruct-Q4_0)
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
    """One row per (model-family, quant): merge speed (CPU+GPU) and quality across records."""
    groups: dict[tuple, dict] = {}
    for r in recs:
        cfg = r.get("config") or {}
        raw = cfg.get("model")
        if not raw:
            continue  # finding/experiment record with no model — skip here
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
        # a single sortable speed: best decode seen (gpu preferred, else cpu)
        g["best_decode"] = g["gpu_decode"] if g["gpu_decode"] is not None else g["cpu_decode"]
        # quality display: Elo if present, else objective composite (0..100) from math/code
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
        # status precedence for the badge
        order = ["blessed", "contender", "degenerate", "couldnt_load", "failed"]
        g["status"] = next((s for s in order if s in g["statuses"]), (g["statuses"] or [None])[0])
    rows.sort(key=lambda g: (g["best_decode"] is None, -(g["best_decode"] or 0)))
    return rows

_FIND_RE = re.compile(r"\b(NOVEL|KEY FINDING|VERDICT|DEFINITIVE|SYNTHESIS|HEADLINE|WIN|DEAD END|"
                      r"CONFIRMED|NEGATIVE|ANTIDOTE|CONTRIBUTION|TEED UP)\b")

def _findings(recs: list[dict]) -> list[dict]:
    """Surface the marked discoveries (blessed records + notes flagged with caps markers)."""
    out = []
    for r in recs:
        notes = r.get("notes", "") or ""
        mark = _FIND_RE.search(notes)
        if r.get("status") == "blessed" or mark:
            cfg = r.get("config") or {}
            title = cfg.get("finding") or cfg.get("experiment") or cfg.get("model") or "finding"
            # first sentence of the note as the lede
            lede = re.split(r"(?<=[.)])\s", notes.strip(), maxsplit=1)[0][:240]
            out.append({"id": r.get("id"), "status": r.get("status"),
                        "tag": (mark.group(1) if mark else "BLESSED"),
                        "title": str(title)[:60], "note": lede, "epoch": r.get("epoch")})
    out.sort(key=lambda f: -(f["epoch"] or 0))
    return out[:12]

def build_data() -> dict:
    camp = {}
    try:
        camp = json.loads(_read(os.path.join(BOX, "campaign.json")) or "{}")
    except json.JSONDecodeError:
        pass
    recs = ledger.load(os.path.join(BOX, "ledger.jsonl"))
    front = ledger.pareto_front(recs)
    md = _read(os.path.join(BOX, "MEMORY.md"))

    now = time.time()
    deadline = camp.get("deadline_epoch")
    remaining = (deadline - now) if deadline else None

    def axis_best(key):
        vals = [r[key] for r in front if r.get(key) is not None]
        if not vals:
            return None
        winner = max(front, key=lambda r: r.get(key) or -1)
        return {"value": max(vals), "config": winner.get("config", {}),
                "id": winner.get("id")}

    # status tallies for the timeline strip
    from collections import Counter
    tally = Counter(r.get("status") for r in recs)

    # the "current best" decode config carries the roofline gauge
    top = max(front, key=lambda r: r.get("decode_tok_s") or -1) if front else None

    # optional measured depth-curve dataset (the headline finding), read if present
    depth = None
    try:
        depth = json.loads(_read(os.path.join(BOX, "depth.json")) or "null")
    except json.JSONDecodeError:
        depth = None

    models = _models_table(recs)
    findings = _findings(recs)

    return {
        "now": now,
        "campaign": camp,
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
        "hypotheses": _hypotheses(md),
        "memory_present": bool(md),
        "models": models,
        "findings": findings,
        "depth": depth,
        "windows": camp.get("prior_windows", []),
    }


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
    global BOX
    ap = argparse.ArgumentParser()
    ap.add_argument("box", help="path to boxes/<nickname>")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    BOX = os.path.abspath(a.box)
    if not os.path.isdir(BOX):
        print(f"[dashboard] box folder not found: {BOX}", file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[dashboard] serving {BOX}\n[dashboard] open http://{a.host}:{a.port}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
