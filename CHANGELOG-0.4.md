# Crucible v0.4 — in progress (branch `release/0.4`)

Efficiency-focused release: agent-driven window control, token/model telemetry, agentic-centered
scoring, and a ground-up **System-3 dashboard** (interactive, with a localhost write-API). **Not yet
cut** — merge `release/0.4` → `main` + tag `v0.4` when the operator says so.

## Base (already on `main`)
- **Anti-rabbit-hole novelty pivot** (`run_window.sh`) — after >100 K-items on one avenue with no new
  blessed config, abandon it, GC the dead experiments, and research a novel avenue. `c45ddba`.

## On `release/0.4`

### Agent window control — `d1ec221`
- New `scaffold/window.py` (companion to `steer.py`):
  - `stop` — **quick-stop**: drop `work/STOP` + kill the in-flight unit; the loop still consolidates and
    writes the FINAL report before exiting (`state → stopped`).
  - `stop --graceful` — let the current unit finish first.
  - `add-hours N` — extend the window (negative removes time); refreshes `duration_label`; guardrails
    (new deadline must be > now and > start).
  - `status` — window clock + state.
- `run_window.sh` — check the STOP flag at the top of every iteration; **re-read `deadline_epoch` each
  iteration** so time changes take effect live; capture the unit PID (`work/current_unit.pid`) via a
  `run_unit()` launcher so `window.py` can kill it; flip `state → stopped` on an operator stop.

### Agentic scoring — quality axis becomes an agentic composite — `4954380`
- Ranked quality coordinate is now **`agentic_score`** = tool-calling **0.40** / IFEval **0.25** /
  GSM8K **0.20** / code **0.15** (agent-revisable). Front stays 3-D {decode, prefill, agentic-quality}.
- `runner.py` — `grade_ifeval`, `grade_toolcall` (+`_extract_toolcall`, `_first_json`), `agentic_score`.
- `eval_config.py` — runs the batteries on the target, emits `agentic_score` + sub-scores.
- `ledger.py` — `_quality_coord` ranks on `agentic_score`; bpb/Elo = recorded context + legacy fallback.
- New FROZEN assets: `toolcall.jsonl` (10), `ifeval.jsonl` (12), `gsm8k.jsonl` (12).
- Doctrine 01/02, `assets/README.md`, `unit.md`, `GATE_PROPOSALS.md` amended (human-authored
  eval-kernel change per doctrine/04).

### Dashboard telemetry — `e801cc4` (server) + `e92ddac` (client)
- `server.py` — per-unit + per-window token telemetry from the stream-json `usage`/`modelUsage`:
  input/output/cache-read/cache-creation tokens, tok/s, approx peak context fill vs the model context
  window, and a per-model cost/token split (main vs sub-agent). New `_ingest_costs()` — approximate
  (chars/4) token cost to ingest key context files → `/data.ingest`. Agentic quality label.
- `index.html` — **Spend & tokens** panel (cost chart + token/context/per-model breakdown) and a new
  **Context ingest cost** panel. Quality meter relabeled elo → agentic. Theme-aware.

### Token efficiency — how agents read the scaffolding — `5f5232e`, `b42f47d`, `8aec98f` (+ box-repo compaction)
Input tokens are paid on every unit; this pass cuts the per-unit read ~half.
- **Compressed prompts** — `unit.md` 2,172→1,332 tok (−39%, −840 **every unit**), `consolidate.md` 886→533,
  hybrid (YAML/tables for structured rules + tight prose for rationale). Comprehension A/B verified (a
  fresh agent reproduced all 11 rules from the compressed `unit.md` alone — no regression).
- **Doctrine grep-on-demand** — new `doctrine/INDEX.md` (rule→file map); prompts now grep the one rule
  they need instead of preloading the ~12k-tok corpus. Cuts the boot read + prevents full-file reads.
- **MEMORY head budget + one-time compaction** — the head is read in full every unit and had ballooned
  to ~28k. Set a concrete budget (≤~12k; the "Known-good flags" block ~5.9k is the floor) and compacted
  Precision390's live head **27,989→13,411 tok (−52%, ~14.5k saved/unit)**, relocating superseded
  LFM/bitnet sagas to `MEMORY_ARCHIVE.md` verbatim (0 deletions — nothing lost).
- **Net per-unit read (unit.md + MEMORY head): ~30.2k → ~14.7k (≈−51%).**
- Deferred: doctrine prose compression (low leverage post-grep-on-demand); moving the flags block +
  snapshot to a grep-on-demand reference file (would take the head to ~6–7k).

### Dashboard restructure + fixes — `375f725`, `e8ce4d7`
- **Load time**: `_agent` re-scanned all 1,097 unit files (266MB) per request (~10–30s). Memoized per-file
  parse; completed units are immutable so they return without a stat() (the win on WSL/DrvFs) →
  **warm polls 10s→0.24s**. Cold first-load covered by a theme-aware **loading overlay**.
- **Queue panel** (was empty): parser only matched the template `## Open hypotheses` + `- ` bullets, but
  the live head uses `### Queue (takeable top)` with a `1. **K235**` numbered/bold format. Rewrote it
  robust to `##`/`###` headers, numbered/bulleted/arrow items, bracket-or-bold ids → 5 items, K235 top.
- **Research panel**: replaced stale `front_stall_K` copy with the real triggers (queue-empty refill /
  novelty pivot); it was data-correct-empty, just mislabeled.
- **Full viz inspection**: 20/22 panels healthy; only the queue was genuinely broken (timeline/ledger use
  CSS bars, not SVG — false alarms).
- **Density restructure**: tab bar (Overview / Performance / Research & Queue / Cost & Tokens / System /
  All) + **click-to-collapse** on every panel; **Overview = curated 7-panel landing** (was 22). Tab +
  collapse state persist in localStorage; active tab is URL-routable (`#performance`). Built on the
  existing prefs/drawer.
- **Deferred**: interactive/write API (design TBD); dashboard style-bible refresh (later).

### Interactive Control page + write-API — `d92f58e`, `69cffd0`, `d71731f`
The read-only dashboard gains a write layer (localhost-only) and a **Control tab** (command-deck layout).
- **Control primitives** (reuse the CLI pattern): `window.py` gains pause/resume/hard-kill; `run_window.sh`
  writes `work/driver.pid` + honors a PAUSE flag; `steer.py --delete` retracts an inbox note; new
  `queue.py` (peek + inject-at-top) and `hw_probe.sh` (on-demand box disk/mem/cpu/temp/gpu, null-safe).
- **Write-API**: `server.py` `do_POST` → `_do_action` routes `/action/<name>` to those CLIs. Safety:
  binds 127.0.0.1; box validated against the served set (blocks path injection); args passed as argv
  lists (no shell injection); `run-start` guarded; `hw-refresh` auto-wakes then probes, fails gracefully.
- **Control page** (`index.html`): full-width run-control command bar (adaptive Start → Pause/Resume +
  Clean/Force stop + Hard-kill, each behind a confirm modal) + cards for steering (add + retract),
  time-window, queue (peek + inject), live hardware (Refresh), and a live-agent cockpit. Toast on every
  action; state driven by a new `/data` `paused`/`hardware_live`. Verified in-browser end-to-end (a
  steering add→retract round-trip through the live server; no console errors).

### System 3.0 skin — classic Macintosh dashboard theme
A monochrome re-skin of the live dashboard in the classic Mac **System 3.0 Finder** style (from a
UI-kit reference), applied after a mockup pass. Contained + fully reversible:
- **Theme layer** appended to `index.html` — a clearly delimited `SYSTEM 3.0 THEME` CSS block + a
  paired `<script id="sys3-skin">`. It remaps the design tokens to B&W (the existing light/dark toggle
  now drives black-on-white vs inverted white-on-black), swaps in Chicago / Geneva / Monaco type + a
  dither desktop, and restyles panels as Mac windows (pinstripe title bars with close/zoom boxes, hard
  1px drop shadows), tabs as file tabs, and controls as classic push-buttons / fields / gauges. Charts
  inherit B&W through the tokens.
- **Backup**: `scaffold/dashboard/index.pre-system3.backup.html` (the prior Precision-Lab style),
  **gitignored**. Revert = restore it, or delete the two System-3 blocks.
- **Latent bug fixed en route**: the Control page's `renderHardware(hw, fromAction)` collided by name
  with the base `renderHardware(box)` (last declaration wins), so once `hardware_live` was populated
  `renderControl` invoked the wrong one and threw (`hw.cpu.replace`), stalling every poll behind the
  loading overlay. Renamed the Control-page one to `renderHwLive`. Verified in-browser: both themes
  render with real data, overlay clears, no console errors.

### System 3.0 desktop mode — menu bar + draggable windows + Apple-menu launcher
Turns the reskinned dashboard into a full classic-Mac **desktop** (self-contained: `body.sys3-desktop`
CSS + `<script id=sys3-desktop>`; remove both to drop back to the tabbed skin).
- Panels + the run command-bar become absolutely-positioned, **draggable, closable windows** — a real
  close box on each pinstripe title bar; window positions + the open-set persist in localStorage;
  z-order rises on focus.
- A **menu bar** with an **Apple-menu program launcher** listing every window (✓ = open) + About
  Crucible + Control Panel (opens the customize drawer); a **View** menu (Light / Dark / Clean Up
  Windows / Close All); and a live box + clock readout. Tabs / masthead / fleet-bar are hidden here.
- **Chicago webfont** plumbing in place — `server.py` serves `scaffold/dashboard/fonts/` at `/fonts/`;
  `@font-face ChicagoFLF` activates the moment the face is dropped in (falls back to system until then).
Verified in-browser: default 6 windows open with real data, launcher lists all 30 windows with correct
checkmarks, drag/close/focus work, no console errors. (Fixed en route: the open Apple-menu inherited
white-on-white text — forced the dropdown's ink color.)

### System 3.0 desktop — window model, Control Panel, pattern editor, bitmap font
Second pass on the desktop, from operator feedback:
- **Real window model** — each panel/cmdbar becomes a flex window: a fixed pinstripe **title bar** with
  **close / zoom / resize** all working, a scrollable **`.win-body`** with a **classic dithered
  scrollbar**, and the base collapse-on-titlebar-click neutralized so dragging is clean. Fixed a bug
  where the window-model `display` rule overrode the closed-panel `display:none`, so *every* panel was
  visible and piled at the origin — now only `.win-open` windows render.
- **Neat tiled default** — the six/seven default windows open at explicit tiled coordinates (no more
  cascade pile-up); each shows its live content. "Clean Up Windows" re-tiles.
- **Bitmap font** — dropped-in OFL **sysfont** (Chicago-style) served from `fonts/`, wired via the
  `ChicagoFLF` `@font-face`; `--serif` (titles/UI) + `--sans` lead with it, `font-smoothing:none`
  forces aliased pixel text.
- **Classic Control Panel** (replaces the old drawer) — boxed sections (Desktop Pattern, Appearance,
  Autonomy Tier, Window Defaults, Search) styled after the System 6/7 panel, incl. an **8×8 desktop-
  pattern editor**: draw in the grid and it renders to a canvas tile that repeats across the desktop
  (persisted; re-themes with dark mode).
- **Desktop icons** — a Reports floppy + a Read Me doc; double-click opens the reports window / a
  System-3-rendered README (with the 1-bit campaign flowchart).

### Dashboard rebuilt from the ground up — the shipped client (supersedes the three System 3.0 skin/desktop entries above)
The skin-over-the-old-design approach above was bloated (a foreign layout wearing a Mac costume), so the
client was **rebuilt from scratch** as a System-3-native app — this is what v0.4 ships. The Python backend
(`/data` + the `/action` write-API) is unchanged; only the client was replaced.
- **Stack** — vendored **system.css** (System-6 monochrome components + Chicago/Geneva/Monaco faces) +
  **interact.js** (drag/resize/snap) under `scaffold/dashboard/vendor/`; vendored icon sets (pixelarticons
  MIT + 12 authentic classic-Mac recreations, `vendor/.../NOTICE.txt`).
- **Architecture** — one file, module-shaped: `DATA` (poll `/data`) · `WM` (window manager on system.css
  `.window` + interact.js) · `PANELS` (data-driven render registry) · `Draw` (one 1-bit SVG kit —
  progress/gauge/bar/scatter/sparkline with dither/hatch fills) · `SHELL` (menu bar + Apple launcher +
  boot) · `Actions`/`Cockpit` (write-API + confirms + toasts) · `Theme`/`UIScale`/`Pattern`/`Desktop`.
- **Desktop metaphor** — hybrid tiled/floating windows (drag, resize, zoom, Clean-Up; positions persist),
  Apple-menu launcher, richer desktop icons (Crucible HD / About + Dogcow, Control Panels, Reports floppy,
  Read Me, Pareto/Findings aliases, Trash), a fuller boot sequence + a Web-Audio startup chime.
- **Control Panel** — theme (true 1-bit / Night-invert / Amber & Green phosphor CRT via a whole-desktop
  CSS filter) · an 8×8 draw-your-own desktop-pattern editor (+ presets) · **Text Size** (UI scale) ·
  campaign settings. All persisted.
- **Reports / Read Me** — the Reports floppy opens a Finder-list of FINAL reports; double-click renders one
  as a System-3 document (mini-markdown). Read Me renders the README with the 1-bit campaign flowchart.
- **Polish (operator feedback)** — dropdown-icon invert bug (white-on-white) scoped to the Apple logo;
  monochrome `::selection` (was the OS accent orange); centered Apple glyph + taller menu bar; and the
  text-scale fix (base font sizes raised + zoom-aware drag/resize + a storage-key bump so a stale small
  scale can't pin the default).
- **Swap** — `scaffold/dashboard/index.html` **is** the rebuild now; the old skinned dashboard and its
  gitignored backup were removed from the branch; `server.py` serves it at `/` (with `/next` as an alias).

## Verification done
- `window.py` — functional test (status / add-hours ± / stop / --graceful / guardrails / open-ended).
- `run_window.sh` — `bash -n` + dry-run intact.
- `runner.py` — self-test (objective + agentic graders). `ledger` — ranking test (agentic drives front,
  dominated config drops off, bpb fallback works).
- `server.py` — `/data` smoke test against real unit files (tokens, tok/s, per-model, ctx, ingest).
- `index.html` — JS brace/paren balance identical to the working baseline (no imbalance introduced);
  dashboard serves `/` (200) and `/data` on the new code.
- **Dashboard rebuild** — eyeballed in-browser (Chrome) against the live server across the build: every
  panel renders on real `/data`; Control Panel themes (Amber/Green CRT) + pattern editor + Text Size;
  Reports + Read Me render; drag tracks the cursor 1:1 under zoom; boot + chime; no console errors. Swap
  verified: `/` serves the rebuild (system.css present, old skin absent), `/next` aliases, `server.py`
  compiles.

## Pending / deferred
- **On-box (Proposal E confirm-before-merge)**: re-run `eval_config.py`'s agentic battery against the four
  blessed staged GGUFs on the target (needs the box awake) — the 3B GSM8K scores should jump, confirming
  the truncation fix. Until then treat `agentic_score` as plumbing-verified, not capability-valid.
- **Proposal E asset growth** (approved, not yet done): grow the agentic seed sets 10–16 → ≥50 items /
  battery to shrink the per-item quantum + variance, then re-freeze.
- **Future plugins** on the same benchmark registry: heavier multi-step agentic harnesses
  (tau-bench / SWE-bench).
