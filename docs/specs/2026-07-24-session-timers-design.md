# Session Timers (cron-for-sessions) - Design Spec

**Date:** 2026-07-24
**Status:** Approved for planning

## Summary

Per-session scheduled injection. The operator attaches one or more **timers** to
any session; each fires a **payload string** (a script name, a "check PRs"
nudge, arbitrary text) into that session on a fixed **interval of 1-90 minutes**.
A session can carry multiple independent timers. This gives relay cron-like
behavior scoped to individual terminal/Claude sessions, driven by the same
"inject when idle" machinery relay already uses for swarm message delivery.

Origin: operator request. Distinct from swarm messages (which are worker->worker
and require a registered name); timers work on ANY session, including a plain
Claude tab or a bare shell.

## 1. Behavior model (the locked decisions)

- **Interval:** 1-90 minutes, per timer. Elapsed-time based (no wall-clock
  alignment): a timer is due when `now >= last_fired_at + interval`. First fire
  is one interval after it is enabled/restored; firing (or "fire now") resets
  the clock.
- **Payload:** a single-line string. Sent as text, then a discrete Enter (the
  same bracketed-paste + standalone `\r` pattern `_deliver` uses). Embedded
  newlines are stripped at save time so a payload can never submit early.
- **Per-timer busy mode:**
  - `idle` - queue and inject the moment the session is idle at Claude's input
    box (reuses the `claude_prompt_ready` + `state == "idle"` gate from
    `_deliver`); never interrupts a running turn. A 5-min `idle` timer means
    "about every 5 min, but only at a clean prompt."
  - `now` - inject immediately when due, whatever is on screen. True cron
    precision; can land mid-turn or in a running command. The operator chose the
    sharpness deliberately.
- **Gating:**
  - A **global** policy `timers require an armed session` (config
    `[timers] require_armed`, default `false` = independent). When `false`, a
    timer fires on whatever session it is attached to, even a MANUAL/unarmed
    tab - arming governs auto-approving Claude's prompts, an orthogonal concern.
    When `true`, a timer only fires while its session is armed
    (safe/wild/insane); disarming suspends its timers.
  - **Pause always freezes every timer.** Non-negotiable: `p` must mean "relay's
    hands are fully off." A frozen timer does not advance toward "missed"; it
    resumes its schedule on unpause (it does not fire a backlog).
- **Dry-run:** timers "would-fire" (audited as `would-fire`) but never inject,
  identical to how `_deliver` behaves in dry-run.
- **Audit before act:** every fire writes an audit record (`timer-fired`,
  session title, payload, timer id) BEFORE the keystrokes go out - the same
  log-before-act contract as approvals and deliveries. An audit-write failure
  suppresses the fire (retries next tick), debounced by the notify cooldown.
- **Relay's own panel tab** can never carry a timer (same guard as delivery:
  `session_id == own_sid` is rejected in the editor and skipped in the engine).

## 2. Data model

New `timers` table in the swarm DB (the runtime source of truth):

| column | type | meaning |
| --- | --- | --- |
| `id` | INTEGER PK | |
| `source` | TEXT | `tui` or `file` - who owns the definition (see 5.3) |
| `file_key` | TEXT | stable key for a file-defined timer (its section name); NULL for `tui` |
| `iterm_session_id` | TEXT | the bound tab; NULL while a file timer is unresolved |
| `target_match` | TEXT | for `file` timers: the session title/swarm-name to bind to; NULL for `tui` |
| `label` | TEXT | session title snapshot - display + the restore prompt |
| `interval_min` | INTEGER | 1-90 (clamped) |
| `payload` | TEXT | the single-line string to send |
| `mode` | TEXT | `idle` or `now` |
| `enabled` | INTEGER | 0/1 - operator on/off |
| `active` | INTEGER | 0/1 - has it been restored/armed this relay run (see 6) |
| `last_fired_at` | REAL | drives next-due; set on enable/restore/fire |
| `created_at` | REAL | |

Schema is added via the same additive-migration path relay already uses for new
tables/columns (see `db.py`'s `_SCHEMA` + `ALTER` migrations). `timers` is
independent of `sessions`/`tasks`; deleting a session row does not cascade
(timers are retained dormant and re-attach on the tab's return).

## 3. The engine

A new pure module `iterm/timers.py` (no iterm2/sqlite imports, unit-tested like
`gates.py`/`swarm.py`/`statusbar.py`). It owns the decisions; the watcher only
executes them.

- `due_timers(timers, now) -> list` - enabled, active timers whose
  `last_fired_at + interval*60 <= now`.
- `firable(timer, *, session_state, session_ready, mode, paused, armed,
  require_armed) -> bool` - the full gate: not paused, arm-policy satisfied, and
  for `idle` mode the session is idle at a ready prompt; for `now` mode always.
- `next_due_in(timer, now) -> float` - seconds until next fire, for the
  countdown display.
- `clamp_interval(n) -> int` - 1..90.
- `sanitize_payload(s) -> str` - strip newlines, trim.

Watcher integration (in the existing 2s poll loop, per present session, after
`_check_stale`):

1. Gather that session's due timers via `timers.due_timers`.
2. For the first one that is `firable` (at most **one fire per session per
   tick**, preserving observability like delivery), audit `timer-fired`, then
   send `payload` + Enter, then set `last_fired_at = now`. `now`-mode fires
   immediately; `idle`-mode simply won't be `firable` until the session is idle,
   so it naturally waits.
3. Pause / dry-run / arm-policy handled by `firable` + the same dry-run branch as
   `_deliver`.

Multiple timers due on one session trickle out one-per-tick (2s apart) - fine.

## 4. Authoring surface A: the TUI overlay

A new full-screen overlay (like settings/swarm/help), opened with **`t`** on the
selected session. It manages ONLY that session's timers.

Layout: a list of the session's timers, one row each -
`⏲ every 5m  idle  ● on   next in 3m12s   "check open PRs"`. Below, a key hint
bar.

Keys inside the overlay:
- `a` - add a timer (enters the add/edit flow).
- `enter` / `e` - edit the highlighted timer.
- `space` - enable/disable.
- `x` / `delete` - remove.
- `g` - fire now (resets the clock).
- `r` - restore (only shown for pending-restore timers, see 6).
- `esc` / `t` - close.

Add/edit flow (three fields, arrow-key + one text input):
- **interval**: left/right cycles 1-90 (coarse steps, e.g. 1,2,3,5,10,15,20,30,
  45,60,90, plus fine +/-).
- **payload**: a Textual `Input` widget - relay's FIRST free-text field.
  Single-line; newlines stripped on save.
- **mode**: left/right toggles `idle`/`now`.
- `enter` saves, `esc` cancels.

File-defined timers (source=`file`) are shown but **structurally read-only** in
the TUI (you may `space` toggle / `g` fire-now / `x` delete-from-runtime, but
interval/payload/mode are edited in the file) - see 5.3. This keeps the two
surfaces coherent without bidirectional sync.

Because this is relay's first text input, the overlay owns focus while open:
list-navigation keys are inert during the payload Input, and the global
session-mutating keys are inert while any overlay is open (existing
`_any_overlay_open` guard, extended to include the timers overlay).

## 5. Authoring surface B: the config file

### 5.1 Format

A dedicated INI file `~/.relay/timers` (override `RELAY_TIMERS`), parsed with
`configparser` - consistent with `~/.relay/config`. One section per timer; the
section name is the stable `file_key`:

```ini
[timer check-prs]
session  = api-worker        ; match by swarm name OR tab title
interval = 10                ; minutes, 1-90
payload  = check open PRs and summarize
mode     = idle              ; idle | now
enabled  = true

[timer nightly-lint]
session  = bff-worker
interval = 30
payload  = ./scripts/lint.sh
mode     = now
enabled  = true
```

### 5.2 Import (seed, not sync)

On startup and on an explicit "reload timers" action, relay reads the file and
**upserts** each section into the `timers` table keyed by `file_key`:
- Resolve `session` to a currently-present tab (swarm name first, then exact tab
  title). Found -> bind `iterm_session_id`. Not present -> store with
  `iterm_session_id = NULL` and `target_match = session`; the engine resolves it
  when a matching session appears.
- A `file_key` present in the DB but absent from the file is removed (the file
  is authoritative for its own timers).
- `tui`-source timers are never touched by import.

This is deliberately **one-way (file -> DB), not bidirectional.** Two-way
mirroring of a live table and a hand-edited file is a coherence trap for little
gain. The file is for declarative, version-controllable, long-payload timers;
the TUI is the live editor for ad-hoc ones.

### 5.3 Coherence rule

`source` partitions ownership: `file` timers are re-derived from the file on
every import (structural edits happen in the file); `tui` timers live only in
the DB. In the TUI overlay, `file` timers render with a small `[file]` tag and
their interval/payload/mode are read-only there.

## 6. Restore-on-restart (the safety gate)

The `active` flag separates "defined" from "armed this run". A timer created or
edited in the TUI during a live run, or imported by an explicit mid-run reload,
is `active = 1` immediately - creating it IS the deliberate act. The restore gate
below applies ONLY to timers loaded at startup.

Timers persist, but **never auto-fire after a relay restart.** On startup:

- Every saved timer loads with `active = 0`.
- For each present session that has saved timers, relay surfaces them as
  **pending restore**: a `⏲?` indicator on the row, and a one-line startup note
  ("2 session(s) have saved timers - open `t` to restore").
- In the `t` overlay, pending timers show a `restore` action (`r` = restore all
  for this session). Restoring sets `active = 1` and `last_fired_at = now` (so
  the first post-restore fire is a full interval away). Declined timers stay
  dormant (`active = 0`); they are not deleted and can be restored later.
- A timer whose bound tab is not currently open stays dormant until the tab
  returns (then it is offered for restore).

Escape hatch for genuine automation: a global `[timers] autostart` (default
`false`). When `true`, saved timers for present sessions activate on startup
**without** the prompt - for operators who intentionally maintain a timers file
and want set-and-forget. Default keeps the safe, prompt-first behavior.

This mirrors relay's existing `R x2` restore philosophy: unattended action after
a restart is a deliberate human choice, not a silent resume.

## 7. Global config settings

Two new keys, in a `[timers]` section of `~/.relay/config`, both surfaced in the
`,` settings editor as toggles:

```ini
[timers]
require_armed = false   ; timers only fire on an armed session
autostart     = false   ; skip the restore prompt; activate saved timers on start
```

They join the existing `config.Config` dataclass + `settings.SETTINGS` list the
same way `statusbar_enabled` did (`toggle` kind, live where applicable).

## 8. Visibility

- **List:** a `⏲N` glyph (N = active timers) on any session row that has active
  timers; `⏲?` when it has pending-restore timers. Placed so it never reorders
  or widens columns disruptively (follows the stable-list-order rule).
- **Preview / self panel:** a "TIMERS" block for the selected session listing
  each timer's interval, mode, next-fire countdown, and payload - so a firing
  session is never a surprise.
- **Log:** each fire emits a feed line (`TIMER -> api-worker: check open PRs`),
  like deliveries.

## 9. Safety and edge cases

- **Pause** freezes all timers; unpause resumes the schedule (no backlog burst).
- **Own panel tab:** never eligible (editor rejects, engine skips).
- **Session closed:** timers retained dormant; re-attach + re-offer restore when
  the tab returns.
- **Two timers due same tick:** at most one fires per session per tick; the rest
  fire on subsequent ticks.
- **`now` into a running command:** documented sharp edge; the operator opted in
  via `mode = now`.
- **Payload newlines:** stripped at save (single-line only in v1).
- **Interval out of range:** clamped to 1-90 (file values clamped with a
  warning, like other config validation).
- **Audit-write failure:** fire is suppressed and retried; notify debounced by
  the session's notify cooldown (same as delivery).
- **Dead session_id after iTerm2 restart:** if the UUID no longer resolves, the
  timer is dormant/pending until a matching tab (by `target_match`, when set)
  reappears; TUI-only timers with a vanished UUID surface as "orphaned - tab
  gone" and can be deleted or left dormant.

## 10. Module layout

```
iterm/timers.py         # pure: due/firable/next_due/clamp/sanitize + file parse
iterm/test_timers.py    # table-driven tests for the above
iterm/db.py             # MODIFY: timers table + CRUD + file-import upsert
iterm/config.py         # MODIFY: [timers] require_armed + autostart
iterm/settings.py       # MODIFY: two toggles in SETTINGS
iterm/watcher.py        # MODIFY: _fire_timers in the poll loop; restore-on-start
iterm/app.py            # MODIFY: 't' overlay (list + add/edit Input + actions),
                        #         timer indicators, preview TIMERS block
iterm/test_*.py         # MODIFY: db/config/settings/watcher/app coverage
README.md               # MODIFY: session-timers section + config keys + keymap
```

## 11. Testing

- `test_timers.py` - due calculation across intervals; `firable` truth table
  over (mode, paused, armed, require_armed, ready); clamp; sanitize; file parse
  (valid, bad interval, bad mode, missing fields, malformed -> warnings).
- `test_db.py` - timers CRUD; file-import upsert idempotency; file_key removal;
  name/title resolution; dormant (NULL session) handling.
- `test_config.py` - `[timers]` keys default/parse/bad-value/round-trip.
- `test_settings.py` - the two toggles cycle + render.
- `test_watcher.py` - fire path (idle waits for ready; now fires immediately);
  audit-before-act; pause freeze; dry-run would-fire; one-per-tick; require_armed
  gate; restore leaves timers inactive until confirmed.
- `test_app.py` - piloted `t` overlay: open, add (interval cycle + payload Input
  + mode), enable/disable, fire-now, delete, restore; indicator renders.

## 12. Out of scope (v1)

- Multi-line / scripted payloads (single-line only for now).
- Cron expressions / wall-clock schedules (fixed elapsed intervals only).
- Bidirectional file<->DB sync (import is one-way).
- Timers on non-relay-visible windows.
- Per-timer arm gating (global policy only, per the agreed knob split).
- Backlog/catch-up firing after pause or downtime.
