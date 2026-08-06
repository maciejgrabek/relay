# Extreme Insane Mode - Design Spec

**Date:** 2026-08-06
**Status:** Approved (design)

## Problem

Insane mode auto-approves permission prompts, but a session still stops
whenever its turn simply ends: it asks an open question in prose, or posts
a done-summary and hands the next action to the human ("you playtest and
report back"). Both look identical to the watcher - `idle` at the Claude
prompt - and today nothing pushes past them. The operator wants an opt-in
tier above insane where relay keeps such a session moving by injecting a
configured continuation prompt ("do the highest-value thing you can do
without me") whenever it idles.

Two boundaries are deliberate:

1. **Genuine decision points stay human.** The `blocked` state (a real
   multi-choice question relay refuses to answer) is untouched. Extreme
   only fires on `idle`; it never touches a permission or chooser UI.
2. **Armed only from the relay TUI.** Not from the iTerm2 status-bar RPC,
   not from spawn pre-arming (`arm_request`), not from `a` (arm-all). A
   session that never stops can do harm; entering that state requires a
   deliberate per-session act in the TUI.

## Design

Approach: `extreme` is a real fifth mode value (the control surface), with
two in-memory per-session fields carrying what the enum cannot (the prompt
and the remaining fire budget), firing through the same payload + `\r`
injection path timers use.

### 1. The mode

- `extreme` joins the mode vocabulary in `watcher.py` (SessionInfo.mode
  semantics comment) and `db.py`. It is a strict superset of insane:
  every `mode == "insane"` behavior branch in `Watcher._handle` becomes
  `mode in ("insane", "extreme")`.
- `_MODE_CYCLE` is unchanged (`off -> safe -> wild -> insane -> off`).
  Extreme is only reachable via the TUI `E E` flow, and only from a
  session currently in `insane`. Any mode change out of extreme
  (`space`, `s`, `d`, `set_mode`) simply leaves it; the stored prompt is
  retained for convenient re-arming.
- `ARM_REQUEST_MODES` stays `("safe", "wild", "insane")` - spawn
  pre-arming cannot request extreme. `set_all` never sets extreme.
  The status-bar arm RPC refuses `extreme` if asked.
- **Extreme does not survive a relay restart.** Unlike the other armed
  modes (which `_persist_mode` writes and the first-sight restore
  replays), extreme is in-memory only: `extreme_prompt` and
  `extreme_fires_left` live on `SessionInfo`, never in the DB. When
  `_persist_mode` runs for an extreme session it writes `"insane"` (the
  floor it falls back to), and the restore whitelist
  (`safe/wild/insane/shadow`) never gains `"extreme"` - a natural second
  guard. Restarting relay is therefore a panic button for extreme:
  every pushed session comes back as plain insane and must be re-armed
  with `E E`.

### 2. Arming: `E E` + a one-line form

Same double-press pattern as `R`/`W`/`Z`/`Q` (`_CONFIRM_WINDOW`, armed
flag, auto-cancel timer), then a form instead of an action:

- First `E` on the selected session: if its mode is not `insane` (or
  already `extreme`), log `EXTREME requires INSANE first` and do not arm.
  Otherwise arm and log
  `extreme ARMED: press E again to configure the push prompt`.
- Second `E` opens an `Input` the way the timer payload form does
  (mount, focus, Escape cancels, Enter submits), prefilled with the
  session's stored `extreme_prompt` if any, placeholder:
  `do the highest-value thing you can do without me; don't wait for my review`.
- Submit with text: store the prompt, set `extreme_fires_left` to the
  configured cap, set mode `extreme`, persist, log.
- Submit empty: disarm back to `insane` (prompt column cleared).
- On an already-extreme session, `E E` reopens the form to edit the
  prompt (submitting also refills the fire budget) or disarm.
- Overlay guard (`_any_overlay_open`) applies as for every other
  session-mutating key.

Cap and dwell are config, not form fields:

```
[swarm]
extreme_fires = 5    # auto-continues per arming
extreme_dwell = 45   # seconds a session must sit idle before a fire
```

### 3. Firing

A new per-tick check in the watcher, gated exactly like inbox delivery
plus a dwell:

- session mode is `extreme` and `extreme_fires_left > 0`
- `state == "idle"` and `claude_prompt_ready(last_screen)`
- not paused, not relay's own tab, no foreground shell job
- **inbox empty** - queued messages always win; `_deliver` runs first
- **prompt line empty** - if the Claude input line already contains
  typed text (an operator composing a message in the tab), skip the
  fire entirely; injecting would append to their draft and submit it
- idle continuously for `extreme_dwell` seconds. The watcher records
  `idle_since` per session (set on transition into `idle`, cleared on
  any other state); fire when `now - idle_since >= dwell`. The dwell
  also keeps relay from stomping on the operator typing into the tab.

A fire is: audit-log first (house rule: audit before act), then inject
`extreme_prompt` via `send_keys`, sleep 0.3, standalone `"\r"` - the
same shape as a timer fire. Decrement `extreme_fires_left`, persist,
reset `idle_since`.

**Exhaustion:** when the counter hits 0, revert mode to `insane`,
persist, log `EXTREME exhausted on <name>`, and pulse attention the way
completions do. Re-arm with `E E`.

### 4. Display

- `MODE_STYLE` gains `extreme` (e.g. `☢ EXTREME`); the mode cell shows
  the remaining budget, `EXTREME 3`.
- Status bar: new `MODE_CIRCLE`/`MODE_TEXT` entries (purple circle,
  `RELAY:extreme`) - display only, the badge never sets it.
- `KEYBAR` gains `("E×2", "extreme")`; `help_text()` explains the tier,
  the insane prerequisite, and the cap.

## Testing

- `watcher`: fire requires idle + prompt-ready + empty inbox + dwell
  elapsed; no fire when `blocked`/`prompting`/paused/own-tab/queued
  mail; decrement and persist per fire; exhaustion reverts to insane
  and logs; `insane` behavior branches also accept `extreme`;
  `idle_since` resets on state change and on fire.
- `watcher` (persistence boundary): `_persist_mode` on an extreme
  session writes `"insane"`; first-sight restore never yields
  `"extreme"`; a fire is skipped when the prompt line holds typed text.
- `db`: arm-request with `extreme` is rejected; no schema change.
- `app`: `E` binding present; first press on a non-insane session logs
  and does not arm; second press opens the form; empty submit disarms;
  submit stores prompt + budget and flips mode; overlay guard holds.
- `statusbar`: label renders for `extreme`.

## Out of scope

- Heuristic detection of "asked a question" vs "posted a summary" -
  extreme fires on idle, full stop.
- Per-session cap/dwell overrides in the form (config only).
- Any non-TUI arming surface: CLI verb, status-bar RPC, spawn
  `arm_request`, `set_all`.
- Auto-answering `blocked` (genuine multi-choice) states - permanently
  out, not deferred.
- Prompt template library / multiple named prompts per session.
