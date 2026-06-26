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

def grade_math(outputs: list[str], items: list[dict]) -> float:
    """
    items: [{"id","prompt","answer"}]; exact numeric match on the LAST number emitted
    (models usually conclude with the answer). Returns pass fraction.
    """
    if not items:
        return 0.0
    ok = 0
    for out, it in zip(outputs, items):
        nums = _NUM.findall(out or "")
        if not nums:
            continue
        got = nums[-1]
        want = str(it["answer"]).strip()
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
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return m.group(1) if m else text


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
    assert grade_code(
        ["```python\ndef add(a,b):\n  return a+b\n```"],
        [{"id": "1", "prompt": "", "tests": "assert add(2,3)==5"}]) == 1.0
    e = Elo(tempfile.mktemp())
    e.update("fast_moe", "big_dense", "fast_moe")
    assert e.rating("fast_moe") > e.rating("big_dense")
    print("eval/runner.py self-test: all objective tiers pass ✓")
