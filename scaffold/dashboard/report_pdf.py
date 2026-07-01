#!/usr/bin/env python3
"""
report_pdf.py — turn FINAL_*.md campaign reports into nicely formatted PDFs.

No REQUIRED third-party deps. It renders the markdown to a styled, print-ready HTML document
(headings, bold/italic, inline+block code, lists, block-quotes, GFM tables, rules) and then
prints that to PDF with whatever high-fidelity engine is available, tried in order:

  1. a headless Chromium / Chrome / Edge  (best fidelity; found on PATH, or under /mnt/c on WSL)
  2. weasyprint            (if importable)
  3. wkhtmltopdf           (if on PATH)

If none is available it still writes the styled `<stem>.html` next to the report as a fallback
you can open and Ctrl-P → "Save as PDF". Everything is idempotent: a report is (re)rendered only
when its .pdf is missing or older than the .md, unless force=True.

CLI:
    python3 scaffold/dashboard/report_pdf.py boxes                 # every box under boxes/
    python3 scaffold/dashboard/report_pdf.py boxes/Precision390    # one box
    python3 scaffold/dashboard/report_pdf.py boxes --force         # re-render even if up to date

Importable:
    import report_pdf
    report_pdf.ensure_pdf("boxes/<nick>/reports/FINAL_2026-07-01_window.md")   # -> pdf path or None
"""
from __future__ import annotations
import os, re, sys, html as _html, shutil, subprocess, tempfile

# ─────────────────────────────────────────────────────────────────────────────
# markdown → HTML  (small, dependency-free; enough for these reports incl. tables)
# ─────────────────────────────────────────────────────────────────────────────
def _inline(s: str) -> str:
    """Inline spans on an ALREADY html-escaped string: code, bold, italic, links."""
    # code spans first, and protect their contents from further substitution
    stash: list[str] = []
    def _stash(m):
        stash.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(stash)-1}\x00"
    s = re.sub(r"`([^`]+)`", _stash, s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*(?!\s)([^*]+?)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\b(_)(?!_)([^_]+?)_\b", r"<em>\2</em>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?:[^)\s]+)\)",
               r'<a href="\2">\1</a>', s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)
    return s

def _is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", line))

def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]

def md_to_html_body(src: str) -> str:
    lines = src.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    in_ul = in_ol = False
    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul: out.append("</ul>"); in_ul = False
        if in_ol: out.append("</ol>"); in_ol = False
    while i < n:
        raw = lines[i]
        # fenced code
        if raw.strip().startswith("```"):
            close_lists()
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(_html.escape(lines[i]))
                i += 1
            i += 1  # closing fence
            out.append('<pre class="code"><code>' + "\n".join(buf) + "</code></pre>")
            continue
        line = raw.rstrip()
        if not line.strip():
            close_lists()
            i += 1
            continue
        # GFM table: header row followed by a separator row
        if "|" in line and i + 1 < n and _is_table_sep(lines[i + 1]):
            close_lists()
            head = _split_row(line)
            i += 2
            body_rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                body_rows.append(_split_row(lines[i]))
                i += 1
            t = ['<table><thead><tr>']
            t += [f"<th>{_inline(_html.escape(c))}</th>" for c in head]
            t.append("</tr></thead><tbody>")
            for r in body_rows:
                t.append("<tr>" + "".join(f"<td>{_inline(_html.escape(c))}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        # heading
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(_html.escape(m.group(2).strip()))}</h{lvl}>")
            i += 1
            continue
        # horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line.strip()):
            close_lists(); out.append("<hr>"); i += 1; continue
        # blockquote
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            close_lists()
            out.append(f"<blockquote>{_inline(_html.escape(m.group(1)))}</blockquote>")
            i += 1
            continue
        # unordered list
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            if in_ol: out.append("</ol>"); in_ol = False
            if not in_ul: out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline(_html.escape(m.group(2)))}</li>")
            i += 1
            continue
        # ordered list
        m = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
        if m:
            if in_ul: out.append("</ul>"); in_ul = False
            if not in_ol: out.append("<ol>"); in_ol = True
            out.append(f"<li>{_inline(_html.escape(m.group(2)))}</li>")
            i += 1
            continue
        # paragraph
        close_lists()
        out.append(f"<p>{_inline(_html.escape(line))}</p>")
        i += 1
    close_lists()
    return "\n".join(out)

_CSS = """
@page { size: Letter; margin: 16mm 15mm 18mm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font: 10.5pt/1.55 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif; color:#1a1d21;
       margin:0; background:#fff; }
.report { max-width: 760px; margin: 0 auto; padding: 8px 0 24px; }
.masthead { border-bottom: 2px solid #c99a3a; padding-bottom: 8px; margin-bottom: 18px; }
.masthead .kicker { font: 600 9pt/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       letter-spacing:.18em; text-transform:uppercase; color:#b07d1e; }
.masthead .title { font-size: 20pt; font-weight: 700; margin: 6px 0 2px; color:#14171a; }
.masthead .meta { font: 9.5pt ui-monospace,Menlo,Consolas,monospace; color:#6b7178; }
h1,h2,h3,h4,h5,h6 { line-height:1.25; margin:18px 0 8px; color:#14171a; page-break-after:avoid; }
h1 { font-size:16pt; border-bottom:1px solid #e2e5e9; padding-bottom:5px; color:#a9781c; }
h2 { font-size:13.5pt; }
h3 { font-size:11.5pt; color:#2f6f66; }
h4,h5,h6 { font-size:10.5pt; color:#444; }
p { margin:7px 0; }
ul,ol { margin:7px 0; padding-left:22px; }
li { margin:2px 0; }
a { color:#1c6b8c; text-decoration:none; }
strong { color:#0f1214; }
hr { border:0; border-top:1px solid #e2e5e9; margin:16px 0; }
blockquote { border-left:3px solid #c99a3a; margin:8px 0; padding:2px 0 2px 12px; color:#555; background:#faf6ee; }
code { font:9.5pt ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; background:#f2f0eb; color:#8a5a12;
       padding:1px 4px; border-radius:3px; }
pre.code { background:#f6f8fa; border:1px solid #e2e5e9; border-radius:5px; padding:10px 12px;
       overflow:auto; margin:9px 0; page-break-inside:avoid; }
pre.code code { background:none; color:#24292f; padding:0; font-size:9pt; white-space:pre-wrap; word-break:break-word; }
table { border-collapse:collapse; width:100%; margin:10px 0; font-size:9.5pt; page-break-inside:avoid; }
th,td { border:1px solid #dfe3e8; padding:5px 8px; text-align:left; vertical-align:top; }
th { background:#f3ede1; font-weight:600; color:#5a4415; }
tbody tr:nth-child(even) td { background:#fafbfc; }
"""

def render_html_doc(md: str, title: str, subtitle: str = "") -> str:
    body = md_to_html_body(md)
    safe_title = _html.escape(title)
    sub = _html.escape(subtitle) if subtitle else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title><style>{_CSS}</style></head><body><div class='report'>"
        "<div class='masthead'>"
        "<div class='kicker'>Crucible · sealed window report</div>"
        f"<div class='title'>{safe_title}</div>"
        + (f"<div class='meta'>{sub}</div>" if sub else "")
        + "</div>"
        + body
        + "</div></body></html>"
    )

# ─────────────────────────────────────────────────────────────────────────────
# HTML → PDF  (find the best available renderer)
# ─────────────────────────────────────────────────────────────────────────────
_WIN_BROWSERS = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
]
_LINUX_BROWSERS = ["chromium", "chromium-browser", "google-chrome", "chrome", "brave-browser"]

def _win_path(p: str) -> str:
    """A Windows path for a WSL path (so a Windows browser can read it)."""
    p = os.path.abspath(p)
    try:
        return subprocess.check_output(["wslpath", "-w", p], text=True).strip()
    except Exception:
        if p.startswith("/mnt/") and len(p) > 6:
            return p[5].upper() + ":" + p[6:].replace("/", "\\")
        return p

def _file_url_win(win_path: str) -> str:
    return "file:///" + win_path.replace("\\", "/").replace(" ", "%20")

def find_browser() -> tuple[str, bool] | None:
    """Return (exe, is_windows) for a headless-capable browser, or None."""
    for b in _LINUX_BROWSERS:
        exe = shutil.which(b)
        if exe:
            return (exe, False)
    for p in _WIN_BROWSERS:
        if os.path.isfile(p):
            return (p, True)
    return None

def _browser_print(exe: str, is_win: bool, html_path: str, pdf_path: str) -> bool:
    if is_win:
        src = _file_url_win(_win_path(html_path))
        out = _win_path(pdf_path)
    else:
        src = "file://" + os.path.abspath(html_path)
        out = os.path.abspath(pdf_path)
    cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--no-pdf-header-footer", f"--print-to-pdf={out}", src]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    except Exception:
        # older builds want the legacy headless flag
        try:
            cmd[1] = "--headless"
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        except Exception:
            return False
    return os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0

def _weasyprint(html_str: str, pdf_path: str) -> bool:
    try:
        import weasyprint  # type: ignore
        weasyprint.HTML(string=html_str).write_pdf(pdf_path)
        return os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0
    except Exception:
        return False

def _wkhtmltopdf(html_path: str, pdf_path: str) -> bool:
    exe = shutil.which("wkhtmltopdf")
    if not exe:
        return False
    try:
        subprocess.run([exe, "-q", "--enable-local-file-access", html_path, pdf_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        return os.path.isfile(pdf_path) and os.path.getsize(pdf_path) > 0
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────────────────────
# top-level: md file -> pdf (idempotent)
# ─────────────────────────────────────────────────────────────────────────────
def _title_for(stem: str) -> tuple[str, str]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
    date = m.group(1) if m else ""
    return (f"Final report — {date}" if date else stem.replace("_", " "),
            f"{stem}")

def ensure_pdf(md_path: str, force: bool = False) -> str | None:
    """Render md_path -> sibling .pdf (idempotent). Returns the pdf path, or None if no
    renderer was available (in which case a styled <stem>.html fallback is written)."""
    md_path = os.path.abspath(md_path)
    if not os.path.isfile(md_path):
        return None
    d = os.path.dirname(md_path)
    stem = os.path.basename(md_path)[:-3] if md_path.endswith(".md") else os.path.basename(md_path)
    pdf_path = os.path.join(d, stem + ".pdf")
    if (not force and os.path.isfile(pdf_path)
            and os.path.getmtime(pdf_path) >= os.path.getmtime(md_path)):
        return pdf_path
    md = open(md_path, encoding="utf-8", errors="ignore").read()
    title, subtitle = _title_for(stem)
    doc = render_html_doc(md, title, subtitle)

    br = find_browser()
    if br:
        exe, is_win = br
        # temp html must be readable by the (possibly Windows) browser; keep it beside the md
        tmp_html = os.path.join(d, "." + stem + ".render.html")
        open(tmp_html, "w", encoding="utf-8").write(doc)
        ok = _browser_print(exe, is_win, tmp_html, pdf_path)
        try:
            os.remove(tmp_html)
        except OSError:
            pass
        if ok:
            return pdf_path
    # try pure-python / other engines
    if _weasyprint(doc, pdf_path):
        return pdf_path
    tmp_html = os.path.join(d, "." + stem + ".render.html")
    open(tmp_html, "w", encoding="utf-8").write(doc)
    ok = _wkhtmltopdf(tmp_html, pdf_path)
    try:
        os.remove(tmp_html)
    except OSError:
        pass
    if ok:
        return pdf_path
    # no engine — leave a durable styled HTML fallback next to the report
    open(os.path.join(d, stem + ".html"), "w", encoding="utf-8").write(doc)
    return None

def convert_tree(paths: list[str], force: bool = False) -> dict:
    """Find FINAL_*.md under each path (a box dir or a parent of box dirs) and render each."""
    md_files: list[str] = []
    for p in paths:
        p = os.path.abspath(p)
        rd = os.path.join(p, "reports")
        if os.path.isdir(rd):
            md_files += [os.path.join(rd, f) for f in os.listdir(rd)
                         if re.match(r"FINAL_.*\.md$", f)]
        else:  # a parent of box dirs
            try:
                for name in os.listdir(p):
                    rd = os.path.join(p, name, "reports")
                    if os.path.isdir(rd):
                        md_files += [os.path.join(rd, f) for f in os.listdir(rd)
                                     if re.match(r"FINAL_.*\.md$", f)]
            except OSError:
                pass
    md_files = sorted(set(md_files))
    made, fell_back = [], []
    for md in md_files:
        res = ensure_pdf(md, force=force)
        (made if res else fell_back).append(md)
    return {"pdf": made, "html_fallback": fell_back, "engine": (find_browser() or ["none"])[0]}

def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv[1:]
    if not args:
        args = ["boxes"]
    r = convert_tree(args, force=force)
    eng = r["engine"]
    print(f"[report_pdf] engine: {os.path.basename(eng) if eng!='none' else 'NONE (wrote .html fallbacks)'}")
    for m in r["pdf"]:
        print("  PDF  ", os.path.relpath(m).replace(".md", ".pdf"))
    for m in r["html_fallback"]:
        print("  HTML*", os.path.relpath(m).replace(".md", ".html"), "(no PDF engine)")
    print(f"[report_pdf] {len(r['pdf'])} pdf(s), {len(r['html_fallback'])} html fallback(s)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
