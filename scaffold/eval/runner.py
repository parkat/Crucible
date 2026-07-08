#!/usr/bin/env python3
"""
crucible eval/runner.py — the evaluation funnel (doctrine/02).

Cheap objective filters gate the expensive subjective judge, so the judge can never
single-handedly elevate something the objective signals call garbage. The frozen assets
in assets/ are invariant-kernel: propose changes, don't silently edit (doctrine/04).

Tiers:
  T0 degeneracy   (free, every config)   -> repetition / garbage / utf-8 / length
  T1 cross-model  (cheap, per model)     -> BPB on frozen corpus + auto-graded math/code
  T2 pairwise     (expensive, contenders)-> session judges A vs B, Bradley-Terry/Elo

The objective tiers are fully implemented here. The judge in T2 IS the orchestrating
Opus session: the runner stages the prompt and the two outputs and records the verdict
the session returns, then updates Elo. Output collection (running the model over SSH)
is done by the orchestrator and passed in — this module is the metric/bookkeeping layer.

Stdlib only.
"""
from __future__ import annotations
import json, math, os, re, subprocess, sys, tempfile
from collections import Counter

# ============================ TIER 0: degeneracy =============================
def is_degenerate(text: str, *, max_repeat_ngram: int = 4, repeat_thresh: float = 0.30,
                  min_len: int = 1) -> tuple[bool, str]:
    """Return (degenerate?, reason). Cheap structural checks (doctrine/02 T0)."""
    if text is None:
        return True, "null output"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True, "invalid utf-8"
    if "\ufffd" in text:
        return True, "replacement char (garbage tokens)"
    toks = text.split()
    if len(toks) < min_len:
        return True, "empty/too short"
    # n-gram cycle detection: fraction of repeated n-grams
    if len(toks) >= max_repeat_ngram * 2:
        ngrams = [tuple(toks[i:i+max_repeat_ngram]) for i in range(len(toks)-max_repeat_ngram+1)]
        c = Counter(ngrams)
        repeated = sum(v for v in c.values() if v > 1)
        if repeated / max(len(ngrams), 1) > repeat_thresh:
            return True, f"repetition loop ({repeated}/{len(ngrams)} ngrams repeat)"
    # single-token spam
    if len(set(toks)) <= 2 and len(toks) > 8:
        return True, "single-token spam"
    return False, "ok"


# ============================ TIER 1: BPB ====================================
def bits_per_byte(sum_neg_log2_prob: float, corpus_byte_len: int) -> float:
    """
    BPB = (sum over tokens of -log2 P(token)) / (raw bytes of the text).
    Tokenizer-independent because the denominator is BYTES, not tokens -> genuinely
    cross-model comparable (doctrine/02). The orchestrator computes the per-token
    log-probs over the frozen corpus on the target and passes the summed nats/bits +
    the corpus byte length here. Lower is better.
    """
    if corpus_byte_len <= 0:
        return float("inf")
    return sum_neg_log2_prob / corpus_byte_len

def nats_to_bits(sum_neg_ln_prob: float) -> float:
    return sum_neg_ln_prob / math.log(2)


# ====================== TIER 1: auto-graded math =============================
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_NUM_ANY = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# Explicit final-answer markers, preferred over "last number" — a verbose or truncated CoT ends on an
# intermediate number, so the last-number heuristic scored the scratch work (Proposal-E: gsm8k inversion).
_ANS_MARKERS = (
    r"####\s*(-?\d[\d,]*(?:\.\d+)?)",                                                # GSM8K canonical
    r"\\boxed\{\s*(-?\d[\d,]*(?:\.\d+)?)\s*\}",                                      # \boxed{N}
    r"(?:final answer|the answer is|answer\s*[:=])\s*\$?\s*(-?\d[\d,]*(?:\.\d+)?)",  # phrase
)

def _strip_think(text: str) -> str:
    """Drop a <think>...</think> reasoning span (or a dangling, unclosed <think>… preamble) so the
    graders read the ANSWER, not the scratch work. Generic to any reasoning-tagged model (R1-distill…)."""
    if not text:
        return text or ""
    t = re.sub(r"<think>.*?</think>", " ", text, flags=re.S | re.I)   # closed reasoning spans
    t = re.sub(r"<think>.*$", " ", t, flags=re.S | re.I)             # truncated/unclosed preamble
    return t

def _extract_answer_number(text: str):
    """The model's FINAL numeric answer: prefer an explicit marker (####, \\boxed, 'the answer is'),
    else the last number — after stripping <think> so intermediate scratch numbers can't win. Returns a
    comma-normalized numeric string, or None."""
    t = _strip_think(text or "")
    for pat in _ANS_MARKERS:
        ms = list(re.finditer(pat, t, re.I))
        if ms:
            return ms[-1].group(1).replace(",", "")
    nums = _NUM_ANY.findall(t)
    return nums[-1].replace(",", "") if nums else None

def grade_math(outputs: list[str], items: list[dict]) -> float:
    """
    items: [{"id","prompt","answer"}]. Extracts the model's FINAL answer (marker-preferred, <think>
    stripped) and exact-numeric-matches it. Returns pass fraction. (Proposal-E hardening: the old
    "last number emitted" grader scored a verbose/truncated CoT on an intermediate number.)
    """
    if not items:
        return 0.0
    ok = 0
    for out, it in zip(outputs, items):
        got = _extract_answer_number(out or "")
        if got is None:
            continue
        want = str(it["answer"]).strip().replace(",", "")
        try:
            if abs(float(got) - float(want)) < 1e-6:
                ok += 1
        except ValueError:
            if got == want:
                ok += 1
    return ok / len(items)


# ====================== TIER 1: auto-graded code =============================
def grade_code(outputs: list[str], items: list[dict], timeout: int = 10) -> float:
    """
    items: [{"id","prompt","tests"}] where tests is python asserting the solution.
    The model output is expected to define the required function(s); we exec the output
    + tests in a subprocess sandbox. Returns pass fraction. (Run this on a disposable
    target or a contained host; it executes model-written code.)
    """
    if not items:
        return 0.0
    ok = 0
    for out, it in zip(outputs, items):
        code = _extract_code(out or "")
        prog = code + "\n\n" + it["tests"] + "\n"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
            tf.write(prog); path = tf.name
        try:
            r = subprocess.run([sys.executable, path], capture_output=True,
                               text=True, timeout=timeout)
            if r.returncode == 0:
                ok += 1
        except subprocess.TimeoutExpired:
            pass
        finally:
            os.unlink(path)
    return ok / len(items)

def _extract_code(text: str) -> str:
    t = _strip_think(text or "")                                    # ignore reasoning scratch
    m = re.search(r"```(?:python)?\s*(.*?)```", t, re.DOTALL)
    if m:
        return m.group(1)
    m2 = re.search(r"(?m)^(?:from |import |def |class |@)", t)      # unfenced fallback: from the first code line
    return t[m2.start():] if m2 else t


# ====================== shared: robust JSON extraction =======================
def _first_json(text: str):
    """First parseable JSON object/array in text (a ```json fence or a balanced {...} span).
    Returns the parsed value or None. Used by the tool-call + ifeval-json graders.
    (Proposal-E: <think> scratch is stripped first so its braces don't shadow the real tool call.)"""
    text = _strip_think(text or "")
    if not text:
        return None
    cands = []
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    if m:
        cands.append(m.group(1))
    depth = 0; start = -1
    for i, ch in enumerate(text):        # balanced-brace scan for the first complete {...}
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                cands.append(text[start:i + 1])
    for c in cands:
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


# ============ AGENTIC (v0.4): instruction-following (IFEval-style) ===========
def _ifeval_check(text: str, chk: dict) -> bool:
    """One programmatic instruction-constraint check. Un-gameable: a constraint either holds
    or it doesn't. Unknown check types fail closed (never credited)."""
    t = text or ""
    typ = chk.get("type")
    ci = chk.get("ci", True)
    if typ == "contains":
        return (chk["value"].lower() in t.lower()) if ci else (chk["value"] in t)
    if typ == "not_contains":
        return (chk["value"].lower() not in t.lower()) if ci else (chk["value"] not in t)
    if typ == "regex":
        return re.search(chk["pattern"], t, re.S | (re.I if ci else 0)) is not None
    if typ == "min_words":
        return len(t.split()) >= int(chk["n"])
    if typ == "max_words":
        return len(t.split()) <= int(chk["n"])
    if typ == "bullets":
        n = sum(1 for ln in t.splitlines() if re.match(r"\s*[-*•]\s+", ln))
        return (n >= int(chk["n"])) if chk.get("at_least", True) else (n == int(chk["n"]))
    if typ == "numbered":
        n = sum(1 for ln in t.splitlines() if re.match(r"\s*\d+[.)]\s+", ln))
        return (n >= int(chk["n"])) if chk.get("at_least", True) else (n == int(chk["n"]))
    if typ == "json":
        return _first_json(t) is not None
    if typ == "ends_with":
        return t.rstrip().endswith(chk["value"])
    if typ == "starts_with":
        return t.lstrip().startswith(chk["value"])
    if typ == "no_commas":
        return "," not in t
    if typ == "keyword_count":
        return t.lower().count(chk["value"].lower()) >= int(chk["n"])
    if typ == "all_caps":
        letters = [c for c in t if c.isalpha()]
        return bool(letters) and all(c.isupper() for c in letters)
    if typ == "lowercase":
        letters = [c for c in t if c.isalpha()]
        return bool(letters) and all(c.islower() for c in letters)
    if typ == "max_sentences":
        return len([x for x in re.split(r"[.!?]+", t) if x.strip()]) <= int(chk["n"])
    return False

def grade_ifeval(outputs: list[str], items: list[dict]) -> float:
    """
    items: [{"id","prompt","checks":[{type,...}]}]. Each output scores the FRACTION of its
    instruction-checks satisfied (programmatic, un-gameable); returns the mean over items (0..1).
    Instruction-following is a core agentic capability (doctrine/01 v0.4).
    """
    if not items:
        return 0.0
    total = 0.0
    for out, it in zip(outputs, items):
        checks = it.get("checks") or []
        if checks:
            total += sum(1 for c in checks if _ifeval_check(out or "", c)) / len(checks)
    return total / len(items)


# ============ AGENTIC (v0.4): tool / function calling (BFCL-style) ===========
def _norm(v) -> str:
    return str(v).strip().strip("\"'").lower()

def _extract_toolcall(text: str):
    """
    Pull ONE function/tool call from a model output: a JSON object with name+arguments (various
    key spellings, optionally wrapped in tool_call/function_call), or a `name(arg=val,...)` literal.
    Returns {"name": str, "args": dict} or None. Single-turn AST-style match — a multi-step tool
    LOOP (tau-bench-style) is a future plugin, not this (doctrine/02 v0.4).
    """
    if not text:
        return None
    obj = _first_json(text)
    if isinstance(obj, dict):
        for w in ("tool_call", "function_call", "tool", "function"):
            if isinstance(obj.get(w), dict):
                obj = obj[w]; break
        name = obj.get("name") or obj.get("function") or obj.get("tool") or obj.get("tool_name")
        args = obj.get("arguments")
        if args is None:
            args = obj.get("args")
        if args is None:
            args = obj.get("parameters")
        if isinstance(args, str):
            j = _first_json(args); args = j if isinstance(j, dict) else {}
        if name:
            return {"name": str(name), "args": args if isinstance(args, dict) else {}}
    m = re.search(r"([A-Za-z_]\w*)\s*\(([^)]*)\)", text)   # fallback: name(a=1, b="x")
    if m:
        args = {}
        for kv in m.group(2).split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                args[k.strip()] = v.strip().strip("\"'")
        return {"name": m.group(1), "args": args}
    return None

def grade_toolcall(outputs: list[str], items: list[dict]) -> float:
    """
    items: [{"id","prompt","tools":[...],"expected":{"name","args":{...}}}]. Per-item score =
    0.5*name_correct + 0.5*(fraction of expected args correct); mean over items (0..1). Tests
    function-call EMISSION — the central agentic axis (doctrine/01 v0.4).
    """
    if not items:
        return 0.0
    total = 0.0
    for out, it in zip(outputs, items):
        exp = it.get("expected") or {}
        call = _extract_toolcall(out or "")
        if not call:
            continue
        name_ok = 1.0 if _norm(call["name"]) == _norm(exp.get("name", "")) else 0.0
        exp_args = exp.get("args") or {}
        if exp_args:
            got = call.get("args") or {}
            args_frac = sum(1 for k, v in exp_args.items() if _norm(got.get(k)) == _norm(v)) / len(exp_args)
        else:
            args_frac = 1.0
        total += 0.5 * name_ok + 0.5 * args_frac
    return total / len(items)


# ============ AGENTIC (v0.4): the composite = the quality coordinate =========
# Weights CENTER the score on agentic capability (doctrine/01 v0.4). Agent-revisable prior:
# tool-use dominates, then instruction-following, then reasoning, then code. Missing axes drop
# out and the remaining weights renormalize, so a partial eval still yields a score.
AGENTIC_WEIGHTS = {"toolcall": 0.40, "ifeval": 0.25, "gsm8k": 0.20, "code": 0.15}

def agentic_score(scores: dict, weights: dict | None = None):
    """
    scores: {"toolcall","ifeval","gsm8k","code": 0..1 or None}. Weighted mean over the PRESENT
    axes (renormalized), or None if none present. This is the single quality coordinate the
    Pareto front ranks on in v0.4 (ledger._quality_coord).
    """
    w = weights or AGENTIC_WEIGHTS
    num = den = 0.0
    for k, wt in w.items():
        v = scores.get(k)
        if v is not None:
            num += wt * float(v); den += wt
    return (num / den) if den > 0 else None


# ====================== TIER 2: pairwise Elo =================================
class Elo:
    """Bradley-Terry via online Elo. Quality coordinate anchor (doctrine/01,02)."""
    def __init__(self, path: str, k: float = 24.0, base: float = 1000.0):
        self.path, self.k, self.base = path, k, base
        self.r: dict[str, float] = {}
        if os.path.exists(path):
            self.r = json.load(open(path))

    def rating(self, model_id: str) -> float:
        return self.r.get(model_id, self.base)

    def update(self, a: str, b: str, winner: str) -> None:
        """winner in {a, b, 'tie'}."""
        ra, rb = self.rating(a), self.rating(b)
        ea = 1.0 / (1.0 + 10 ** ((rb - ra) / 400))
        sa = 1.0 if winner == a else 0.0 if winner == b else 0.5
        self.r[a] = ra + self.k * (sa - ea)
        self.r[b] = rb + self.k * ((1 - sa) - (1 - ea))
        json.dump(self.r, open(self.path, "w"), indent=2)

def judge_request(prompt: str, output_a: str, output_b: str, rubric: str) -> dict:
    """
    Stage a pairwise comparison for the orchestrating session to judge. The session
    reads this, applies the frozen rubric, and returns a verdict {'winner': 'a'|'b'|'tie',
    'rationale': str}. (The judge is the Opus session itself — doctrine/02.)
    """
    return {"rubric": rubric, "prompt": prompt,
            "output_a": output_a, "output_b": output_b,
            "instructions": "Return winner in {a,b,tie} with a one-line rationale. "
                            "Judge on the rubric only; ignore length and position."}


# ====================== quality synthesis ====================================
def quality_coordinate(base_elo: float, kld_vs_fp16: float | None,
                       penalty_slope: float = 4000.0) -> float:
    """
    Q(M,c) = Q_base(M) - penalty(KLD).  Default prior: linear, penalty = slope*KLD
    (Elo points). Recalibrate `penalty_slope` from directly-judged lossy configs
    (doctrine/01). KLD None (e.g. a freshly judged fp16 config) -> Q = base.
    """
    if kld_vs_fp16 is None:
        return base_elo
    return base_elo - penalty_slope * kld_vs_fp16


# ====================== degeneracy self-test =================================
if __name__ == "__main__":
    # tiny smoke test of the objective tiers so the file is runnable standalone
    assert is_degenerate("the the the the the the the the the")[0] is True
    assert is_degenerate("A clear, varied sentence about inference on old hardware.")[0] is False
    assert grade_math(["the answer is 42"], [{"id": "1", "prompt": "", "answer": 42}]) == 1.0
    # Proposal-E: <think> stripped, explicit marker preferred over a trailing/intermediate number
    assert grade_math(["<think>try 7... no 13</think> #### 42"], [{"id": "2", "prompt": "", "answer": 42}]) == 1.0
    assert grade_math(["#### 42\n(see also problem 99)"], [{"id": "3", "prompt": "", "answer": 42}]) == 1.0
    assert _extract_answer_number("<think>1000</think> the answer is 1,024") == "1024"
    assert _extract_toolcall('<think>maybe {"name":"wrong"}</think> {"name":"get_weather","arguments":{"city":"Paris"}}')["name"] == "get_weather"
    assert grade_code(
        ["```python\ndef add(a,b):\n  return a+b\n```"],
        [{"id": "1", "prompt": "", "tests": "assert add(2,3)==5"}]) == 1.0
    e = Elo(tempfile.mktemp())
    e.update("fast_moe", "big_dense", "fast_moe")
    assert e.rating("fast_moe") > e.rating("big_dense")
    # agentic (v0.4)
    assert grade_ifeval(["- a\n- b\n- c"],
                        [{"id": "1", "prompt": "", "checks": [{"type": "bullets", "n": 3},
                                                              {"type": "no_commas"}]}]) == 1.0
    assert grade_ifeval(["one, two, three"],
                        [{"id": "1", "prompt": "", "checks": [{"type": "no_commas"}]}]) == 0.0
    assert grade_toolcall(['{"name":"get_weather","arguments":{"city":"Paris"}}'],
                          [{"id": "1", "prompt": "",
                            "expected": {"name": "get_weather", "args": {"city": "Paris"}}}]) == 1.0
    assert _extract_toolcall('call get_weather(city="Paris")')["name"] == "get_weather"
    assert abs(agentic_score({"toolcall": 1.0, "ifeval": 1.0, "gsm8k": 1.0, "code": 1.0}) - 1.0) < 1e-9
    assert abs(agentic_score({"toolcall": 1.0}) - 1.0) < 1e-9   # renormalizes over present axes
    assert agentic_score({}) is None
    print("eval/runner.py self-test: objective + agentic tiers pass ✓")
