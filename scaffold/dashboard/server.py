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
    print(f"[dashboard] serving {BOX}\n[dashboard] open http://{a.host}:{a.port}/")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
