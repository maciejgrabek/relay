# Delight batch - design

Date: 2026-07-21
Status: approved (chosen from the adoption brainstorm; mascot form picked by
Maciej: header core-creature)

## Phase 0 - review fixes (before any new feature)

1. `bin/relay` dry-run guard matches any of `--dry-run|--dryrun|-n` in ANY
   argv position (mirrors `iterm/app.py:895`), not just literal argv[1].
2. Statusbar provider liveness: the AutoLaunch provider touches
   `~/.relay/statusbar-provider.alive` (mtime heartbeat, ~2s cadence via its
   render callback, throttled). The watcher skips in-process registration
   only when that heartbeat is FRESH (<15s), not merely because the symlink
   exists. Symlinked-but-not-started no longer produces an ERROR badge.
3. Escalation ping rate limit: at most one sound+notification per
   `notify_cooldown` window; a burst becomes one ping naming the count
   ("3 escalations pending, first from bff"). All burst ids still marked
   pinged.
4. Header attention count includes blocked: `awaiting` counts
   `prompting + blocked` so the header never contradicts the strip.
5. `action_hide` cursor restore uses the same nearest-occurrence rule as
   `_refresh` (shared helper `_row_index_near(sid, near_row)`).
6. Worktree git helpers gain `_git`-grade hardening: 8s timeout,
   never-raise (failure -> dirty/error result).

## F1 - `relay demo`

One command that shows the whole loop in ~60s. `relay demo`:
- requires iTerm2 ($ITERM_SESSION_ID) and warns loudly if the relay TUI is
  not running (delivery needs it);
- registers the CURRENT session as coordinator `demo-coord` on project
  `demo`, spawns one worker `demo-w1` (--arm wild) in a fresh temp dir,
  adds the haiku task assigned to it;
- prints what to watch (panel: worker arms itself; TAB: task moves;
  your prompt: the haiku arrives) and the cleanup line
  (`relay wipe --project demo --all --yes`).
Thin orchestration of existing verbs; no new machinery.

## F2 - header mascot

A phosphor creature at the end of the CORE TEMP line, driven by real state
(pure function `mascot_frame(tick, band, alarmed, working) -> str`):
- alarmed (any non-own session prompting): `(⊙_⊙)!`
- reactor CRITICAL: `(x_x)`
- working (an approval/delivery/log event within the last ~3 ticks):
  alternating `(◕‿◕)⌁` / `(◕‿◕)`
- idle: `(－‿－)` with a periodic blink frame `(￣‿￣)`
Priority: alarmed > critical > working > idle. Rendered by the existing
0.5s reactor timer; no new timers. `RELAY_NO_REACTOR=1` hides it too.

## F3 - audit viewer

Select a session, press `v`: the preview pane switches to that session's
audit history (what relay auto-approved / escalated / delivered while you
were away), newest last: `HH:MM verdict command`. `v` again (or moving off
with Esc-like behavior: `v` is a toggle) returns to the live feed.
- `iterm/audit.py` gains `read_tail(limit=500) -> list[dict]` (tolerant of
  garbled lines).
- Pure formatter `audit_view_text(entries, title, width)` filters by the
  session title and renders; empty -> teaches what the audit log is.

## F4 - safety presets

`~/.relay/config`: `[danger] preset = default | paranoid` (default:
default). The watcher exports `RELAY_DANGER_PRESET` when invoking
`lib/danger.sh`; in `paranoid`, ONLY the read-only allowlist classifies
safe - everything else escalates. No new regexes; it reuses the two
existing rule sets, just flips the default-allow to default-deny.
Documented in README's safety-boundary section; danger_test.sh gains
paranoid cases.

## F5 - `?` help overlay

`?` toggles a full-width overlay (same mechanism as the swarm view) with
the key map and an arm-level cheat sheet (off/safe/wild/insane in one line
each). Any of `?`/`q`-on-overlay/TAB closes it back to the control view.

## F6 - themes

`~/.relay/config`: `[theme] name = phosphor | amber | ice`. All hardcoded
hex colors in app.py collapse into one `THEME` palette dict (roles: accent,
bright, dim, cyan, warn, danger, hidden) resolved at startup from config;
CSS phosphor values come from the same palette. Unknown theme -> phosphor
with one warning line. The three palettes ship in-code; no user-defined
palettes (YAGNI).

## Order

Phase 0 -> F5 -> F2 -> F3 -> F1 -> F4 -> F6, each with tests, committed
separately on one branch, merged + pushed at the end.

## Out of scope

Homebrew/tap packaging, first-run diagnosis wizard, tmux support, README
hero GIF (superseded by the in-TUI mascot for now).
