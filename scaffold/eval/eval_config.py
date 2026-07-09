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
import json, os, re, shutil, subprocess, sys, time
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


def _build_cmd(prompt, n, temp):
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
    return cmd


def _run_once(cmd):
    # A degenerate/aggressively-quantized model can emit raw bytes that aren't valid UTF-8;
    # text=True decodes with strict errors by default and would crash the whole battery on
    # one bad sample (K258) instead of grading that sample as garbage. errors="replace" keeps
    # the run alive and lets Tier-0 degeneracy grading see the mangled output as mangled.
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                           stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired:
        return ""
    return p.stdout or ""


# K250/K252: a `<think>...</think>` reasoning preamble (DeepSeek-R1-distill and similar) can
# consume the whole token budget before the model ever emits its actual answer — an open,
# never-closed <think> tag is a harness truncation artifact, not a real (bad) response, and
# must not be graded as one. Generic to any reasoning-tagged model, not a one-model patch.
# K304: EXAONE-Deep tags its reasoning span <thought>, not <think> -- same failure mode, different
# tag name, so the open/close pairs are a list, not a single hardcoded pair.
REASONING_TAGS = [("<think>", "</think>"), ("<thought>", "</thought>")]
REASONING_RETRY_MULT = 4


def gen(prompt, n=160, temp=0.0, retries=2, answer_probe=None):
    cmd = _build_cmd(prompt, n, temp)
    # Generation is greedy+seeded (deterministic), so an empty result is NOT the model's
    # real output — it's a transient process/GPU-reload hiccup (the harness reloads the full
    # model per prompt). Retry empties a few times; a fresh process recovers the real output.
    out = ""
    for _ in range(retries + 1):
        out = _run_once(cmd)
        if out.strip():
            break
    # Proposal-E: escalate the token budget when the ANSWER is missing. The old check only caught an
    # unclosed <think>; but plain verbose CoT (no tags) truncates the same way, so we also fire on a
    # battery-specific probe that finds no extractable answer. One shot at a much larger budget.
    need_more = any(open_tag in out and close_tag not in out for open_tag, close_tag in REASONING_TAGS) or \
                (answer_probe is not None and not answer_probe(out))
    if need_more:
        bigger = _run_once(_build_cmd(prompt, n * REASONING_RETRY_MULT, temp))
        if bigger.strip() and (answer_probe is None or answer_probe(bigger) or len(bigger) > len(out)):
            out = bigger
    return out

def load_jsonl(path):
    out = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    return out

def load_jsonl_opt(path):
    """Like load_jsonl but returns [] if the file is absent — the v0.4 agentic assets may not
    exist in an older box's staged asset dir, so degrade gracefully instead of crashing."""
    return load_jsonl(path) if os.path.exists(path) else []

def _toolcall_prompt(it):
    """Single-shot function-calling prompt: present the tool schemas + the user request and ask
    for ONE JSON tool call. Tests call EMISSION (BFCL AST-style), not a multi-step tool loop."""
    tools = json.dumps(it.get("tools", []), ensure_ascii=False)
    return ("You can call these tools (JSON schemas):\n" + tools +
            "\n\nUser request: " + it["prompt"] +
            "\n\nRespond with ONE JSON tool call and nothing else, exactly like: "
            '{"name": "<tool_name>", "arguments": {<args>}}')

def _math_prompt(it):
    """Proposal-E: standardize the answer format so extraction is reliable across terse AND verbose
    models — ask for reasoning THEN a marked final line. grade_math prefers the '#### N' marker, and
    the marker's absence is what tells the harness a long CoT truncated before its answer."""
    return (it["prompt"] +
            "\n\nShow your reasoning, then end with the final answer on its own line exactly as:\n#### <number>")

math_items = load_jsonl(os.path.join(ASSETS, "math.jsonl"))
code_items = load_jsonl(os.path.join(ASSETS, "code.jsonl"))
# agentic benchmarks (v0.4) — optional so an older staged asset dir still runs
ifeval_items = load_jsonl_opt(os.path.join(ASSETS, "ifeval.jsonl"))
gsm8k_items  = load_jsonl_opt(os.path.join(ASSETS, "gsm8k.jsonl"))
tool_items   = load_jsonl_opt(os.path.join(ASSETS, "toolcall.jsonl"))

t0 = time.time()
# Proposal-E: bigger budgets on the reasoning-heavy batteries + a per-battery answer probe that
# escalates when the final answer didn't fit. For math/gsm8k the probe is the ANSWER MARKER (not "any
# number") — a truncated CoT still contains intermediate numbers, so only the marker's absence reliably
# signals "truncated before the answer." This is the real fix for the verbose-CoT gsm8k inversion.
_MARK     = lambda o: any(re.search(p, runner._strip_think(o or ""), re.I) for p in runner._ANS_MARKERS)
_has_code = lambda o: "```" in (o or "") or bool(re.search(r"\bdef\s+\w+\s*\(", runner._strip_think(o or "")))
_has_call = lambda o: runner._extract_toolcall(o or "") is not None
math_out   = [gen(_math_prompt(it), n=256, answer_probe=_MARK)  for it in math_items]
code_out   = [gen(it["prompt"], n=512, answer_probe=_has_code)  for it in code_items]
ifeval_out = [gen(it["prompt"], n=320)                           for it in ifeval_items]
gsm8k_out  = [gen(_math_prompt(it), n=512, answer_probe=_MARK)  for it in gsm8k_items]
tool_out   = [gen(_toolcall_prompt(it), n=220, answer_probe=_has_call) for it in tool_items]

# Tier-0 degeneracy across ALL generated samples
degens = []
for o in math_out + code_out + ifeval_out + gsm8k_out + tool_out:
    bad, reason = runner.is_degenerate(o)
    if bad:
        degens.append(reason)

math_pass = runner.grade_math(math_out, math_items)
code_pass = runner.grade_code(code_out, code_items)
ifeval_pass   = runner.grade_ifeval(ifeval_out, ifeval_items) if ifeval_items else None
gsm8k_pass    = runner.grade_math(gsm8k_out, gsm8k_items) if gsm8k_items else None
toolcall_pass = runner.grade_toolcall(tool_out, tool_items) if tool_items else None
# v0.4 quality coordinate = agentic composite (tool-use + instruction-following + reasoning + code)
agentic = runner.agentic_score({"toolcall": toolcall_pass, "ifeval": ifeval_pass,
                                "gsm8k": gsm8k_pass, "code": code_pass})

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
    # agentic funnel (v0.4) — agentic_score is the ranked quality coordinate (ledger._quality_coord)
    "agentic_score": agentic,
    "toolcall_pass": toolcall_pass,
    "toolcall_n": len(tool_items),
    "ifeval_pass": ifeval_pass,
    "ifeval_n": len(ifeval_items),
    "gsm8k_pass": gsm8k_pass,
    "gsm8k_n": len(gsm8k_items),
    "eval_seconds": round(time.time() - t0, 1),
    "samples": {
        "math": [{"prompt": it["prompt"][:60], "want": it["answer"], "got": (o or "")[:120]}
                 for it, o in zip(math_items, math_out)],
        "code": [{"prompt": it["prompt"][:60], "got": (o or "")[:200]}
                 for it, o in zip(code_items, code_out)],
        "toolcall": [{"prompt": it["prompt"][:60], "want": it.get("expected"), "got": (o or "")[:160]}
                     for it, o in zip(tool_items, tool_out)],
    },
}
print(json.dumps(result, indent=2))
