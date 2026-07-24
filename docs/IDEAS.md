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

## 2. Status/mode prefix in the tab name, e.g. "[INSANE][BLOCKED] api-server" (2026-07-15) - SHIPPED (2026-07-15)

Done: `iterm/titles.py` (pure render/strip), a `[titles] style` config key
(off/glyphs/words/hybrid), and the watcher write path (`_apply_title` /
`_restore_titles`) that strips on read so the UNIT column + swarm registry
always see bare names, writes only on change, restores on quit, and is inert in
dry-run. The strip-on-read + self-heal makes it crash-safe. Later evolution
added a shadow-mode glyph and swapped the stale glyph to one users can't type.
See docs/specs/2026-07-15-tab-title-prefixes-design.md (status: Implemented).

## Open follow-ups (2026-07-21 review sweep)

- ~~`wipe --project X --all` does not remove relay-created worktrees~~ FIXED
  (2026-07-24): `--all` now cleans up worktrees via swarm.worktree_removals,
  keeping dirty ones.
- ~~Header `msgs queued` and the quit-guard stakes count across the WHOLE DB~~
  FIXED (2026-07-24): both scope to live sessions via swarm.live_* helpers.
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
