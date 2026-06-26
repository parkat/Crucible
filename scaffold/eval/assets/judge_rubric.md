# Pairwise judge rubric (Tier 2)

You are comparing two model outputs (A and B) to the **same** prompt. Decide which is
better, or tie. Return `{"winner": "a"|"b"|"tie", "rationale": "<one line>"}`.

Judge on, in priority order:
1. **Correctness** — factually and technically right. A confident wrong answer loses to
   a correct one, always.
2. **Directness & relevance** — answers the actual question; no padding, no preamble.
3. **Clarity** — a knowledgeable reader understands it on first read.

Explicitly **ignore**:
- Length. Longer is not better; do not reward verbosity.
- Position. A vs B ordering carries no information.
- Style flourish that doesn't add correctness or clarity.

If both are correct and equally clear, call it a **tie** rather than splitting hairs.
The objective tiers (BPB, math, code) outrank your verdict on any conflict (doctrine/02).
