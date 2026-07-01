# Crucible Dashboard — Redesign Brief

**Task for you (the UI model):** redesign the *look and feel* of the Crucible campaign
dashboard. Make it beautiful, clearer, better organized. You have **full freedom over CSS,
layout, typography, spacing, color, animation, and HTML structure.**

**But this is a live instrument wired to a backend you must not break.** Read this whole file
before touching anything. The single hard rule: **the page must keep rendering every piece of
data it renders today, driven by the same `/data` JSON, with no new network dependencies.**

---

## 1. What this is

A **single self-contained HTML file** (`index.baseline.html` here — your starting point). It is
served verbatim at `/` by a zero-dependency Python stdlib server (`../server.py`) that runs on
an **offline / air-gapped host**. The page:

1. `fetch("/data")` every **4 seconds** → gets a JSON snapshot of the whole campaign.
2. Writes that data into fixed DOM elements **by `id`**.
3. Runs a **1-second `tick()`** that recomputes only *time-derived* UI (countdowns, "N ago",
   the rotating hype line) from a synced server clock — **without** re-fetching.

Everything else — the charts, tables, transcript — is inline SVG/HTML built in JS. There is no
build step, no framework, no bundler.

### Files in this folder
- `index.baseline.html` — the current, working dashboard. **Edit a copy of this.**
- `sample-data.json` — a **real captured `/data` payload**. This is your ground truth for the
  data shape and realistic values. Open it alongside your work.
- `BRIEF.md` — this file.

### Deliverable
One file: `index.html` (self-contained). The human will diff it against the baseline and drop
it into `scaffold/dashboard/index.html`. If you also update the `<script>`, keep it in the same
single file.

---

## 2. Hard constraints (breaking any of these breaks the dashboard)

1. **Single file, fully self-contained.** All CSS in one `<style>`, all JS in one `<script>`,
   inline in the `.html`. No external files.
2. **No external network resources.** The host is offline. **No** CDN links, Google Fonts,
   web-font fetches, external images, `@import url(http…)`, or any `https://` asset. Use system
   font stacks and inline SVG only. (A remote `<link rel=stylesheet>` will simply fail to load.)
3. **Do not change the data contract.** The page must `fetch("/data", {cache:"no-store"})`. Do
   not rename the endpoint, add auth, or expect any other route. The only routes that exist are
   `/` (this page) and `/data` (JSON). The server is **read-only** and returns the shape in
   `sample-data.json`.
4. **Keep both timers:** a `~4000ms` poll (fetch) and a `~1000ms` tick (time-derived redraw, no
   fetch). Countdowns must update every second but the network poll stays at ~4s.
5. **Keep the server-clock sync.** Countdowns are computed against the server's clock, not the
   browser's: on each poll, `serverNow = data.now; syncAt = performance.now()`, and live time is
   `snow() = serverNow + (performance.now()-syncAt)/1000`. Do **not** replace this with
   `Date.now()` — the browser and the campaign host may disagree, and honesty matters here.
6. **Keep HTML-escaping on every data-derived string.** The server does **zero** escaping — all
   safety is client-side (`escapeHtml` / `escapeXml`). Model names, notes, the live agent
   transcript, and the sealed `MEMORY.md` are agent/LLM-generated text and **will** contain
   `< > &`. Every value interpolated into innerHTML/SVG must go through the escape helpers.
   (Exception: the sealed doc is intentionally rendered via `mdToHtml`, which escapes *first*
   then adds a small, fixed set of tags. Preserve that ordering.)
7. **Every element `id` the script reads/writes must still exist** with the same meaning — or
   you must update the script to match. See §4 for the full list. Renaming a panel's heading is
   fine; deleting the `id="win-wd"` span it writes into is not.

---

## 3. The `/data` payload (what you're visualizing)

Top level: `{ now: <server epoch seconds>, boxes: [ Box, … ] }`. The UI focuses one box at a
time (fleet cards switch between them). Every field a box can carry is in `sample-data.json`;
below is the map with meaning. **Any field can be `null`/absent — render "—", never crash.**

### Box
| field | meaning |
|---|---|
| `nick`, `box_dir` | display name / folder. `nick` is the identity used for fleet selection. |
| `error` | if present, this box failed to load — show an error card, skip the rest. |
| `campaign` | raw campaign config: `autonomy_tier` ("T4"), `nickname`, thresholds, `prior_windows`, `notes`. |
| `window` | `{phase, state, start_epoch, deadline_epoch, winddown_epoch, to_winddown_s, to_deadline_s}`. Phases: `running` → `winddown` → `done`. **Recompute phase live** from epochs vs `snow()`; the server value is a snapshot. |
| `agent` | live driver state — see below. The heart of the "is it working right now" story. |
| `queue` | `{open:[QItem], closed:[QItem], takeable_id}`. The hypothesis queue. |
| `steering` | operator inbox: `{present, pending:[Note], recent:[Note+state], picked_n, dropped_n}`. |
| `hardware` | `{bandwidth_gbps, isa:[…], cpu, arch, cores, ram_gb, ram_speed, gpu, gpu_arch, vram_mib}`. |
| `counts` | `{status: count}` tally across all records (blessed/contender/degenerate/failed…). |
| `n_records` | total ledger records. |
| `front` | Pareto frontier: `[{id, decode_tok_s, prefill_tok_s, ttft_s, quality, perf_per_watt, roofline_efficiency, roofline_ceiling_tok_s, status, model, quant}]`. Drives the scatter plot. |
| `timeline` | `[{epoch, status, decode_tok_s}]` last ~200 records — the bar strip. |
| `best` | `{decode:{value,config,id}, prefill:{…}, quality:{…}}` — best per axis (the "Readout" meters). |
| `roofline_now` | `{efficiency, achieved, ceiling, model}` for the best-decode config — the gauge. `efficiency≥0.60` = "memory-bound", else "kernel-bound". |
| `phase` | parsed key/values from `MEMORY.md`'s "Current phase" — freeform strings. |
| `conclusion` | `{concluded, memory_md, report_name}`. When `concluded` is true, `memory_md` is the full sealed markdown to render. Empty while running. |
| `models` | table rows: `{model, quant, cpu_decode, gpu_decode, cpu_prefill, gpu_prefill, quality_kind, quality_score, math_pass, code_pass, roofline_eff, status, best_decode, arch?}`. |
| `findings` | `[{id, status, tag, title, note, epoch}]` newest first (max ~12). |
| `depth` | optional `{depths:[int], series:[{arch, backend, label, tok_s:[float]}]}` — the decode-vs-context-depth chart. Hidden if absent. |

### `agent` (the live driver state)
`log_present`, `queue_empty`, `running`, `current_unit`, `current_is_research`,
`current_started_epoch`, `driver_terminal` (null | "winddown" | "queue_empty" | "done"),
`units_launched`, `units_completed`, `research_units`, `cost_window_usd`, `cost_all_usd`,
`last_unit:{kind,num,is_error,duration_ms,num_turns,cost,terminal_reason}`,
`recent_units:[{kind,num,is_error,empty,torn,has_result}]` (the little outcome squares),
`live:{kind,num,running,events:[Event]}` (the streamed transcript),
`research_round:{unit,started_epoch,running,summary}` (latest stall-research unit).

**Event** (transcript line): `{kind:"thinking"|"text"|"tool"|"result", text?, name?, cmd?}`.
`tool` events have `name` (tool) + `cmd` (the command/target). Render each kind distinctly.

### QItem / Note
- **QItem**: `{id, title, tags:["BOX"|"HOST"|"EITHER"], status, section}`. Highlight the one whose
  `id` (or `title`) equals `queue.takeable_id` — that's what the agent will do next.
- **Note** (steering): `{ts, tag, research:bool, title, note}`; recent notes also carry
  `state:"picked"|"dropped"`.

---

## 4. The element-`id` contract

The `<script>` locates DOM nodes with `$(id)=getElementById`. **If you restructure the HTML,
each of these ids must still exist and hold the same kind of content — or update the JS.** They
group by panel:

- **Status strip:** `nick`, `tier`, `phase-chip`, `count-chip`, `clock`, `clock-label`,
  `livedot`, `livetext`
- **Fleet bar:** `fleet` (JS builds `.fcard[data-nick]` cards inside; click/Enter/Space selects
  a box — preserve `data-nick` + the delegated listeners)
- **Window status:** `win-sub`, `win-phase`, `win-wd`, `win-dl`, `win-bar`, `win-pre`,
  `win-post`, `win-head`, `win-edge-k`, `win-edge`
- **Live agent activity:** `agent`, `agent-recent`
- **Hardware:** `hw`
- **Operator steering:** `steer-panel`, `steer-tag`, `steer-pending`, `steer-empty`,
  `steer-recent-wrap`, `steer-recent`
- **Live transcript:** `live`, `live-sub`, `live-tag`
- **Hype board:** `brag-flex`, `brag-hero`, `brag-hero-txt`, `brag-list`
- **Pareto plot / Readout:** `plot`, `m-decode`(+`-cfg`), `m-prefill`(+`-cfg`),
  `m-quality`(+`-cfg`), `g-fill`, `g-knee`, `g-eff`, `g-bound`, `g-ach`, `g-ceil`
- **Timeline:** `timeline`, `tl-sub`
- **Depth chart:** `depth-panel`, `depthplot`, `depth-legend`
- **Models table:** `models-body`, `models-sub`, and the `<th data-k="…">` headers (clicking a
  header sorts by that key — keep `data-k` on each sortable header)
- **Findings:** `finds`
- **Research round:** `research-panel`, `research-sub`, `research-body`, `research-tag`
  (`research-ago` is created by JS at runtime)
- **Current activity:** `act-task`, `activity` (`act-elapsed` created by JS)
- **Queue:** `q`, `q-sub`, `q-empty`, `q-closed-wrap`, `q-closed`
- **Conclusion doc:** `conclusion-panel`, `concl-sub`, `concl-doc`
- **Footer:** `foot-box`, `foot-poll`

Panels that **start hidden and reveal conditionally** (keep this behavior — don't force them
visible): `conclusion-panel` (only when a run has concluded), `steer-panel` (only when
`steering.present`), `depth-panel` (only when depth data exists), plus the `live-tag` /
`research-tag` "● LIVE/RUNNING" badges.

---

## 5. Behaviors that must survive the redesign

- **Fleet switching:** clicking (or Enter/Space on) a fleet card focuses that box and re-renders
  everything. Multi-box "fleet" is real — don't hard-code a single box.
- **Sortable models table:** clicking a column header toggles/sets sort. Keep `data-k` headers.
- **Live-transcript auto-follow:** the transcript pane auto-scrolls to newest *unless* the user
  has scrolled up. Nice-to-keep.
- **Rotating hype line:** the hero "brag" line cross-fades to the next every ~5s. Cosmetic —
  restyle freely, but it reads from data already in the payload (no server change).
- **Graceful nulls & empty states:** every panel has a "no data yet" state (awaiting first
  measurement, no run_window.log, inbox clear, etc.). Preserve empty states — early in a
  campaign most panels are empty and that must look intentional, not broken.
- **Connection-lost state:** on a failed poll, the footer shows "connection lost — retrying"
  and the live dot goes offline. Keep some equivalent.

## 6. What you are free to change
Everything visual: the whole color system, layout/grid, panel order and grouping, typography,
iconography, chart styling (the SVG is hand-built — you may restyle or redraw it), spacing,
motion, responsive breakpoints, dark/light, adding a proper `<!doctype html>`/`<html>` wrapper.
You may refactor the JS for clarity as long as the data-in → DOM-out contract above holds.

## 7. Sanity check before you hand it back
- [ ] One `.html` file, no external URLs of any kind.
- [ ] Still `fetch("/data")`; ~4s poll + ~1s tick both present.
- [ ] Server-clock (`snow()`) still drives countdowns.
- [ ] Every id in §4 present (or JS updated to match).
- [ ] All data-derived strings escaped.
- [ ] Open it against `sample-data.json` mentally: every field in §3 has somewhere to render,
      and every panel degrades cleanly when its data is `null`.

> Tip for testing locally (the human can do this): from the repo root,
> `python3 scaffold/dashboard/server.py boxes --port 8788` then open `http://localhost:8788/`.
> Or serve `sample-data.json` as `/data` from any static server to preview without a live campaign.
