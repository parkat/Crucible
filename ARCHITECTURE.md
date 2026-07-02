# How Crucible works

Crucible turns one salvage box into a self-driving inference-optimization lab. The
controlling idea is **open hypothesis space, protected evaluation**: the search may rewrite
almost anything (engines, kernels, quant, models, even its own orchestration at T4), but a
small **invariant kernel** — correctness gating, measurement hygiene, the clock, the ledger,
recovery, and the frozen eval assets — is protected so the optimizer can't Goodhart its own ruler.

## 1 · Campaign control flow

The loop lives **outside** any model session: the external relauncher owns the clock and
launches one bounded unit at a time, so a dead or derailed session can never run the campaign
off the rails. Each unit does exactly one queue item and stops. 🔒 marks invariant-kernel steps.

```mermaid
flowchart TD
    classDef inv fill:#fde2e2,stroke:#c0392b,stroke-width:2px,color:#111
    classDef persist fill:#e2ecfd,stroke:#2c5db0,color:#111
    classDef human fill:#fff3cd,stroke:#b8860b,color:#111
    classDef target fill:#e6f5e6,stroke:#2e7d32,color:#111

    START(["follow startup.md"]) --> A1

    subgraph SETUP["① Setup — one-time, human-guided"]
        direction TB
        A1["Pick target box · autonomy tier T1–T4 · campaign length"]
        A2["Install SSH key + passwordless sudo · discard password"]
        A3["Scan hardware → hardware.json<br/>ISA flags + MEASURED STREAM bandwidth"]
        A4["Scaffold box folder · seed MEMORY.md<br/>opening web-research + hypothesis queue"]
        A1 --> A2 --> A3 --> A4
    end

    A4 --> LOOP

    subgraph HOST["② HOST — relauncher (run_window.sh) owns the loop & clock"]
        direction TB
        LOOP{"date +%s vs deadline"}
        LOOP -->|"wind-down / QUEUE_EMPTY"| DONE["Consolidate → FINAL report → state = completed"]
        LOOP -->|"front stalled ≥ K measured records"| RESEARCH["Launch research unit"]
        LOOP -->|"otherwise"| NORMAL["Launch normal unit"]
    end

    RESEARCH --> B0
    NORMAL --> B0

    subgraph UNIT["③ ONE bounded unit (claude -p) — does ONE queue item, then STOPs"]
        direction TB
        B0["0 · STEER — STEERING.md inbox preempts the queue"]
        B1["1 · DECIDE — ledger + Pareto front + roofline class"]
        B2["2 · BUILD — for target's scanned ISA · objdump guard"]
        B3["3 · SMOKE + CORRECTNESS gate 🔒"]
        B4["4 · MEASURE — prefill & decode SEPARATELY · median+variance 🔒"]
        B5["5 · EVALUATE — Tier 0/1/2 funnel 🔒"]
        B6["6 · RECORD + LEARN"]
        B0 --> B1 --> B2 --> B3 --> B4 --> B5 --> B6
    end

    B3 -->|"fails equivalence / SIGILL"| FAILED["status: failed<br/>(never benchmarked)"]
    FAILED --> B6
    B6 --> LOOP

    B6 --> LEDGER[("ledger.jsonl<br/>append-only")]
    B6 --> MEM[("MEMORY.md<br/>brain transplant")]
    B6 -->|"auto-promote (per tier)"| BLESSED[("blessed/")]
    B6 -->|"kernel / eval / self-mod change"| GATE["GATE_QUEUE.md<br/>⟵ human approval (survives T4)"]

    B2 -.->|"SSH, resolved by boxpaths.py"| TARGET["🎯 TARGET box — disposable<br/>build · run · bench · watchdog recovery"]
    B3 -.-> TARGET
    B4 -.-> TARGET

    BLESSED -.->|"re-checked OUTSIDE the loop"| VERIFY["verify.py<br/>independent verifier"]
    LEDGER -.->|"resume from disk — session is mortal, campaign is not"| LOOP
    MEM -.-> LOOP

    class B3,B4,B5 inv
    class LEDGER,MEM,BLESSED persist
    class GATE,VERIFY human
    class TARGET target
```

## 2 · The roofline router + the recursion ladder

The roofline is the brain: it classifies every result as memory-bound or kernel-bound and
routes the next proposal there. Escalation up the L0→L3 ladder is triggered by **evidence**
(front stall or roofline class), not whim — and a winning kernel/family becomes the new
baseline, so the inner search restarts on top of it.

```mermaid
flowchart LR
    classDef mb fill:#e2ecfd,stroke:#2c5db0,color:#111
    classDef kb fill:#fde2e2,stroke:#c0392b,color:#111

    R{"Roofline<br/>efficiency = achieved decode ÷ bandwidth ceiling"}
    R -->|"≥ 0.6 · near the memory wall"| MB
    R -->|"< 0.6 · perf left on the floor"| KB

    subgraph MB["MEMORY-BOUND — reduce bytes / token"]
        direction TB
        M1["small-active MoE"]
        M2["lower-bit quant (KLD-guarded)"]
        M3["speculative decoding"]
        M4["KV-cache quant"]
    end
    subgraph KB["KERNEL-BOUND — better code"]
        direction TB
        K1["thread-count knee"]
        K2["integer-SIMD kernels + prefetch"]
        K3["compile flags + PGO"]
        K4["engine fork / R4 repack"]
    end

    MB --> ESC
    KB --> ESC
    ESC{"front gaining ground?"}
    ESC -->|"yes"| L1["L0–L1 · config-space sampler<br/>NSGA-II / TPE — automatic, cheap"]
    ESC -->|"no · escalate"| L2["L2 · write / modify kernels"]
    L2 --> L3["L3 · switch model / quant / arch family"]
    L1 -.->|"re-measure"| R
    L2 -.->|"new baseline engine"| R
    L3 -.->|"re-baseline everything"| R
    class MB,M1,M2,M3,M4 mb
    class KB,K1,K2,K3,K4 kb
```

## 3 · The evaluation funnel (why a self-modifying agent can't cheat)

Cheap **objective** filters gate entry to the expensive **subjective** judge, so the judge can
never single-handedly elevate something the objective signals call garbage. Everything that *is
the ruler* — the frozen corpus, item sets, and judge rubric — is invariant-kernel and can only
be changed through the human gate.

```mermaid
flowchart TD
    classDef obj fill:#e2ecfd,stroke:#2c5db0,color:#111
    classDef subj fill:#fff3cd,stroke:#b8860b,color:#111
    classDef frozen fill:#eeeeee,stroke:#555,color:#111

    IN(["output for a (model, config)"]) --> T0
    T0["Tier 0 · degeneracy filter — ≈free, every config<br/>repetition · NaN · UTF-8 · length + KLD-vs-own-fp16"]
    T0 -->|"broken"| DEG["degenerate → logged, off-front"]
    T0 -->|"passes"| T1
    T1["Tier 1 · cheap objective anchor — per model<br/>BPB on frozen corpus · math exact-match · code unit-tests"]
    T1 -->|"else"| REC["objective floor recorded"]
    T1 -->|"fast enough to threaten the front"| T2
    T2["Tier 2 · pairwise judge — contenders only<br/>A vs B → Bradley-Terry / Elo"]
    T2 --> Q(["single quality coordinate<br/>Q(M,c) = Elo − penalty(KLD)"])

    FROZEN["🔒 frozen eval assets — corpus · math/code sets · judge rubric<br/>invariant · human-gated to change, even at T4"]
    FROZEN -.-> T0
    FROZEN -.-> T1
    FROZEN -.-> T2

    class T0,T1,REC obj
    class T2,Q subj
    class FROZEN,DEG frozen
```

## The four recorded axes (no scalar collapse)

Results land on a **Pareto front**, not a weighted sum — the optimizer chases hypervolume so
the human picks the operating point later:

1. **Decode throughput** (batch-1 tok/s) — the memory-bandwidth-bound axis
2. **Prefill throughput + TTFT** — the compute/SIMD-bound axis, kept separate on purpose
3. **Quality** — one cross-model coordinate (Elo-anchored, KLD-interpolated)
4. **Perf/watt** — recorded, never gating

Peak RSS and roofline efficiency ride along as context.
