# Relay - idea backlog

Captured ideas, not commitments. Each gets its own brainstorm/spec before any
implementation.

## 1. Switch arm mode from the iTerm2 tab itself (2026-07-15) - SHIPPED (2026-07-24)

Done via the status-bar badge: the `com.relay.arm` component renders each tab's
arm mode and a CLICK cycles off/safe/wild/insane from the tab itself, no trip to
the relay panel. Signalling channel is the click queue
(`~/.relay/statusbar-clicks.jsonl`), which relay consumes each tick with its
usual guards; safety holds because a click is an un-spoofable human action (a
Claude session cannot click its own status bar). The always-on AutoLaunch
provider now self-heals: relay / install / `relay doctor` start it when it's
installed but not running (see statusbar_ensure.py), and the provider heartbeats
on a timer so liveness is honest even when the badge isn't on screen.

## 2. Status/mode prefix in the tab name, e.g. "[INSANE][BLOCKED] api-server" (2026-07-15)

Relay rewrites each session's tab title so mode + state are visible on the tab
bar itself - glanceable without the TUI. Notes:

- Watcher already knows mode + state per session; iTerm2 allows setting names
  via async_set_name.
- CONFLICT to solve first: relay's UNIT column and the swarm registry read the
  user-set titleOverride as the session's name, and `relay spawn` sets tab
  names too. If relay also WRITES prefixes into the same field, it must
  reliably strip/re-apply its own prefix (idempotent, crash-safe) and never
  clobber the user's actual name. Probably: store the bare name, render
  "[MODE][STATE] bare-name", and strip the bracket prefix when reading.
- Opt-in flag (env var) at first; restore original titles on quit.

## Open follow-ups (2026-07-21 review sweep)

- `wipe --project X --all` does not remove relay-created worktrees (only the
  per-session wipe path does) - bulk-wiping a worktree-heavy project orphans
  them on disk.
- Header `msgs queued` and the quit-guard stakes count across the WHOLE DB,
  not scoped to live sessions' projects - stale projects can cry wolf.
- Efficiency pass: `_check_escalations` and `_check_gone` each scan
  undelivered per tick (fetch once, share); `_statusbar_publish` writes the
  state file every tick even when unchanged; `_render_swarm_view` queries
  every 1s while the data changes at 2s; the launcher pays a python startup
  per launch just to read the update-check stamp.
- Cleanup pass: `kind_of` vs `_get` duplication in swarm.py; a third
  mode-glyph table (swarm._MODE_GLYPH vs app.MODE_STYLE vs
  statusbar.MODE_CIRCLE); progress-bar math in both swarm.py and
  `_tick_reactor`; `cmd_update`'s repeated `auto` ternaries; own-sid
  special-casing sprinkled across app.py (consider one filtered view).
- Adoption ideas parked: Homebrew tap / one-line install; first-run
  prerequisite diagnosis in the getting-started panel; tmux support (the
  big strategic fork - decide deliberately).
