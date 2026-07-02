#!/usr/bin/env python3
"""
Target-side eval driver: Tier-0 degeneracy + Tier-1 math/code for ONE (model,config).
Runs on the disposable target (code grader execs model code). Uses llama-completion for
generation and scaffold/eval/runner.py for grading. Emits one JSON blob to stdout.

CHAT-TEMPLATE ROUTING (doctrine: harness fix, not a one-model patch):
  Instruct/agentic GGUFs embed a `tokenizer.chat_template`. Feeding them RAW prompts via
  llama-completion (--no-cnv) skips that template, producing degenerate/empty outputs (an
  instruct model fed a bare prompt often emits an immediate end-of-generation). Every
  template-bearing GGUF must instead be generated through the model's embedded jinja
  template. We detect the template in the GGUF metadata and route accordingly:
    - template present  -> `-cnv -st --jinja`  (apply embedded template, single-turn, exit)
    - no template (true base/completion model) -> `--no-cnv` (raw prompt, as before)
  `-st` (single-turn) is mandatory: it makes conversation mode exit after one turn instead
  of looping on EOF (the documented llama-cli /dev/null runaway footgun, MEMORY H1).

Usage:
  python3 eval_config.py <llama-completion-bin> <model.gguf> <assets_dir> <runner_dir> [threads]
"""
import json, os, shutil, subprocess, sys, time
sys.path.insert(0, sys.argv[4])  # runner_dir
import runner

COMP, MODEL, ASSETS, RUNNER_DIR = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
THREADS = sys.argv[5] if len(sys.argv) > 5 else "2"
NGL = os.environ.get("CRUCIBLE_NGL")  # set to "99" for GPU; unset = CPU


def has_chat_template(model_path, scan_bytes=32 * 1024 * 1024):
    """True iff the GGUF carries an embedded chat template. GGUF stores all metadata
    (including tokenizer.chat_template) in the file header before the tensor data, so a
    bounded head scan for the key is sufficient and avoids loading the model."""
    key = b"tokenizer.chat_template"
    try:
        with open(model_path, "rb") as f:
            return key in f.read(scan_bytes)
    except OSError:
        return False


TEMPLATE = has_chat_template(MODEL)


# ---- engine compat (bug C — GATED, doctrine/04; see GATE_PROPOSALS.md) --------------------
# The eval kernel must not hardcode a binary name or flags: stock llama.cpp often ships
# `llama-cli` (not `llama-completion`), and ik_llama.cpp differs again. Resolve the binary, and
# only pass flags the build actually advertises, so a run never dies on `unknown argument: -st`.
# STAGED FOR GATED REVIEW — verify on the real target engine before relying on it.
def _resolve_bin(comp):
    if os.path.exists(comp) or shutil.which(comp):
        return comp
    d = os.path.dirname(comp) or "."
    for cand in ("llama-completion", "llama-cli"):
        p = os.path.join(d, cand)
        if os.path.exists(p):
            return p
    return comp  # nothing resolved -> let the run fail loudly with the original name

def _help_text(binary):
    try:
        p = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=30)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""

def _flag_ok(help_text, flag):
    # only pass a flag the binary advertises; empty help (probe failed) -> don't block (pass it)
    return (flag in help_text) if help_text else True

BIN = _resolve_bin(COMP)
HELP = _help_text(BIN)


def gen(prompt, n=160, temp=0.0, retries=2):
    cmd = [BIN, "-m", MODEL, "-p", prompt, "-n", str(n),
           "--temp", str(temp), "-s", "1", "-t", THREADS]
    if _flag_ok(HELP, "--no-display-prompt"):
        cmd += ["--no-display-prompt"]
    if TEMPLATE:
        # instruct/chat GGUF: apply the model's embedded jinja template, single-turn then exit.
        # Only pass flags this build advertises (bug C: -st / -cnv / --jinja are rejected on some
        # engines); if none are available, warn and fall back to raw (may degenerate).
        tmpl = [f for f in ("-cnv", "-st", "--jinja") if _flag_ok(HELP, f)]
        if tmpl:
            cmd += tmpl
        else:
            print("eval_config: WARNING template-bearing GGUF but this build lacks -cnv/-st/--jinja; "
                  "raw prompt may degenerate — verify engine compat (bug C)", file=sys.stderr)
    else:
        if _flag_ok(HELP, "--no-cnv"):
            cmd += ["--no-cnv"]
    if NGL:
        cmd += ["-ngl", NGL]
    # Generation is greedy+seeded (deterministic), so an empty result is NOT the model's
    # real output — it's a transient process/GPU-reload hiccup (the harness reloads the full
    # model per prompt). Retry empties a few times; a fresh process recovers the real output.
    out = ""
    for _ in range(retries + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            continue
        out = p.stdout or ""
        if out.strip():
            break
    return out

def load_jsonl(path):
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out

math_items = load_jsonl(os.path.join(ASSETS, "math.jsonl"))
code_items = load_jsonl(os.path.join(ASSETS, "code.jsonl"))

t0 = time.time()
math_out = [gen(it["prompt"], n=120) for it in math_items]
code_out = [gen(it["prompt"], n=320) for it in code_items]

# Tier-0 degeneracy across all generated samples
degens = []
for o in math_out + code_out:
    bad, reason = runner.is_degenerate(o)
    if bad:
        degens.append(reason)

math_pass = runner.grade_math(math_out, math_items)
code_pass = runner.grade_code(code_out, code_items)

result = {
    "model": os.path.basename(MODEL),
    "threads": int(THREADS),
    "chat_template_applied": bool(TEMPLATE),
    "tier0_degenerate": bool(degens),
    "tier0_reasons": degens,
    "math_pass": math_pass,
    "math_n": len(math_items),
    "code_pass": code_pass,
    "code_n": len(code_items),
    "eval_seconds": round(time.time() - t0, 1),
    "samples": {
        "math": [{"prompt": it["prompt"][:60], "want": it["answer"], "got": (o or "")[:120]}
                 for it, o in zip(math_items, math_out)],
        "code": [{"prompt": it["prompt"][:60], "got": (o or "")[:200]}
                 for it, o in zip(code_items, code_out)],
    },
}
print(json.dumps(result, indent=2))
