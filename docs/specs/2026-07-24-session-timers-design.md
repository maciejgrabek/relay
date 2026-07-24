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
| `iterm_session_id` | TEXT | the bound tab (stable while the tab lives) |
| `label` | TEXT | session title snapshot - display + the restore prompt (lets a human spot a mismatched re-binding) |
| `interval_min` | INTEGER | 1-90 (clamped) |
| `payload` | TEXT | the single-line string to send |
| `mode` | TEXT | `idle` or `now` |
| `enabled` | INTEGER | 0/1 - operator on/off |
| `active` | INTEGER | 0/1 - has it been restored/armed this relay run (see 6) |
| `last_fired_at` | REAL | drives next-due; set on enable/restore/fire |
| `bound_at` | REAL | when this timer was bound/confirmed to its session; drives the stale-binding re-confirmation (see 6) |
| `created_at` | REAL | |

Schema is added via `db.py`'s `_SCHEMA` (`CREATE TABLE IF NOT EXISTS`) - a
brand-new table needs no ALTER migration, same as `tasks` was introduced.
`timers` is independent of `sessions`/`tasks`; deleting a session row does not
cascade (timers are retained dormant and re-attach on the tab's return).

**Note on authoring surface:** the config-file importer (a declarative
`~/.relay/timers` INI) is a **deferred extension**, not part of this build - see
§5 and §12. The TUI overlay is the whole feature. The schema above deliberately
omits the file-specific columns (`source`/`file_key`/`target_match`); adding
them later is additive.

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

## 4. Authoring: the TUI overlay (the config file is deferred)

The declarative `~/.relay/timers` config file - and its one-way import into the
DB - is a **deferred extension** (§9/§12), NOT part of this build. It adds real
complexity (name/title resolution, upsert/prune, a `source` partition to keep
two surfaces coherent) for a convenience layer; the overlay below delivers the
entire feature on its own. When built later it is purely additive.

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

Because this is relay's first text input, the overlay owns focus while open:
list-navigation keys are inert during the payload Input, and the global
session-mutating keys are inert while any overlay is open (existing
`_any_overlay_open` guard, extended to include the timers overlay).

## 5. Stale-binding protection (recycled session ids)

An iTerm2 session UUID can outlive the real session, and over a long-running
relay a closed tab's UUID could in principle be reused by a different tab. A
week-old timer bound to `SID` might then point at a session that is not what the
operator meant. Two layers guard this:

1. **Restart gate (§6):** every relay restart already re-confirms (timers load
   inactive; the human restores per session, seeing the `label` snapshot). This
   covers the common case - relay is usually restarted more often than a UUID
   could plausibly recycle.
2. **Bind-age re-confirmation (the long-run case):** each timer records
   `bound_at` (set on create and on restore). A pure
   `needs_reconfirm(timer, now, reconfirm_days)` returns true once
   `now - bound_at > reconfirm_days * 86400`. When the engine sees a due timer
   whose binding is that old, it **deactivates** it (`active = 0`, back to
   pending restore) instead of firing - forcing the operator to re-confirm the
   session. Config `[timers] reconfirm_days` (default `7`; `0` disables the
   check). The `label` shown in the restore prompt lets a human catch a
   mismatched binding at that moment.

This is deliberately simple (a time threshold, no cross-run session
fingerprinting) and reuses the existing restore UX as the re-confirmation
surface.

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
**without** the prompt - for operators who want set-and-forget. Default keeps the
safe, prompt-first behavior. (Even with `autostart`, the bind-age
re-confirmation in §5 still applies.)

This mirrors relay's existing `R x2` restore philosophy: unattended action after
a restart is a deliberate human choice, not a silent resume.

## 7. Global config settings

Two new keys, in a `[timers]` section of `~/.relay/config`, both surfaced in the
`,` settings editor as toggles:

```ini
[timers]
require_armed = false   ; timers only fire on an armed session
autostart     = false   ; skip the restore prompt; activate saved timers on start
reconfirm_days = 7      ; re-confirm a timer binding older than this (0 = never)
```

`require_armed` and `autostart` are `toggle` settings; `reconfirm_days` is a
`number` setting. They join the existing `config.Config` dataclass +
`settings.SETTINGS` list the same way `statusbar_enabled` did.

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
- **Session closed then a NEW tab reuses the UUID:** the restart gate + bind-age
  re-confirmation (§5) force a human re-confirm before firing into a possibly
  different session.
- **TUI-only timer whose tab is gone:** surfaces as "orphaned - tab gone"; can be
  deleted or left dormant (re-offered for restore if a tab with that UUID
  returns).

## 10. Module layout

```
iterm/timers.py         # pure: due/firable/next_due/needs_reconfirm/clamp/sanitize
iterm/test_timers.py    # table-driven tests for the above
iterm/db.py             # MODIFY: timers table + CRUD (no file import in v1)
iterm/config.py         # MODIFY: [timers] require_armed + autostart + reconfirm_days
iterm/settings.py       # MODIFY: two toggles + one number in SETTINGS
iterm/watcher.py        # MODIFY: _fire_timers in the poll loop; restore-on-start
iterm/app.py            # MODIFY: 't' overlay (list + add/edit Input + actions),
                        #         timer indicators, preview TIMERS block
iterm/test_*.py         # MODIFY: db/config/settings/watcher/app coverage
README.md               # MODIFY: session-timers section + config keys + keymap
```

## 11. Testing

- `test_timers.py` - due calculation across intervals; `firable` truth table
  over (mode, paused, armed, require_armed, ready); `needs_reconfirm` at the
  bind-age boundary (and disabled when `reconfirm_days = 0`); clamp; sanitize.
- `test_db.py` - timers CRUD; `bound_at` set on add/restore; deactivate-all +
  restore-session; dormant (NULL session) handling.
- `test_config.py` - `[timers]` keys default/parse/bad-value/round-trip.
- `test_settings.py` - the two toggles + the number cycle + render.
- `test_watcher.py` - fire path (idle waits for ready; now fires immediately);
  audit-before-act; pause freeze; dry-run would-fire; one-per-tick; require_armed
  gate; restore leaves timers inactive until confirmed; a past-reconfirm timer
  deactivates instead of firing.
- `test_app.py` - piloted `t` overlay: open, add (interval cycle + payload Input
  + mode), enable/disable, fire-now, delete, restore; indicator renders.

## 12. Out of scope (v1) / deferred extensions

- **Config-file authoring (`~/.relay/timers` import)** - a real extension we
  chose to defer; the TUI overlay is the whole feature. Additive when built
  (new `source`/`file_key`/`target_match` columns + a one-way import step).
- Multi-line / scripted payloads (single-line only for now).
- Cron expressions / wall-clock schedules (fixed elapsed intervals only).
- Timers on non-relay-visible windows.
- Per-timer arm gating (global policy only, per the agreed knob split).
- Backlog/catch-up firing after pause or downtime.
- Cross-run session fingerprinting (the bind-age threshold + restart gate are
  the chosen protection instead).
