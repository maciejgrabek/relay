# Relay Onboarding + Swarm Resilience - Change Plan

> Batch of small, mostly-surgical UX and robustness fixes. Per the working
> agreement for this batch: implement directly (no per-change TDD cycle), then
> run the full suite once at the end. Motivated by watching real first-contact
> failures: a new user launches relay with only itself running and is stuck;
> a swarm worker stalled silently for an hour; a relay restart cold-disarmed a
> live swarm.

## A. Skills fixes (skills/*.md)

Root cause observed: the swarm works in the happy path but does not survive a
worker going dark, and a coordinator following the skill literally spawns
workers that cannot act.

- **A1 (worker): never go silent.** Add a rule: before a turn ends with a task
  still `doing`, or on hitting a question it cannot self-answer, the worker
  must `relay send <coordinator>` a status and mark `blocked` rather than
  stopping. Silent stalls break walk-away autonomy.
- **A2 (coordinator): arm the workers you spawn.** The spawn example must pass
  `--arm wild` (or note `spawn_arm` in config). An unarmed worker stalls at its
  first permission prompt.
- **A3 (coordinator): watch for silent stalls.** Add a rule to periodically
  sweep `relay task list` for tasks stuck in `doing` and to treat a STALE flag
  as a signal to investigate, since a dead worker sends no message.
- **A4 (worker): spec-or-description.** "Read the spec file" becomes "read the
  spec file, or the task description if there is none."
- **A5 (worker): split only when warranted.** Subtask decomposition is for
  epics large enough to need it, not every task.
- **A6 (worker): status is a heartbeat.** Tie "keep status fresh" to the STALE
  mechanism: a long task with no status updates gets flagged STALE.

## B. Smart empty state (iterm/app.py)

Relay controls OTHER sessions; with only itself running there is nothing to do,
and the preview pane is dead space. Detect and teach.

- **B1.** Relay identifies its own tab via `$ITERM_SESSION_ID` (bare UUID),
  and computes `controllable = sessions excluding self`.
- **B2.** When `controllable == 0`, the preview pane renders a getting-started
  panel (what relay is, how to add sessions, key hints, the `relay spawn` line)
  instead of `[ no signal ]` / blank.
- **B3.** When there are sessions but `armed == 0`, the subtitle carries a hint
  ("press Space to arm one, then walk away"). Normal behavior otherwise.
- **B4.** Relay's own row is tagged (a dim `(this panel)` marker) so nobody
  wonders what the mystery unit is.

## C. Empty swarm view (iterm/swarm.py render_swarm)

TAB with no registered sessions currently renders a bare skeleton. When there
are zero sessions AND zero tasks, render a one-screen "no swarm yet - spawn
workers with `relay spawn --name w1 --arm wild \"task\"`" hint instead.

## D. Docs mental-model fix (README.md)

The "Install and run" section jumps to `--dry-run` without ever stating the
core model. Add up top: "Relay is a control panel for OTHER terminal sessions -
open some first; it has nothing to do with only itself running." Mirror the
empty-state panel's wording so the live TUI and the docs agree.

## E. Restart-disarm resilience (iterm/db.py, iterm/watcher.py) - LARGER

Observed: restarting relay showed `0 armed` on a live swarm because arm state
lives only in the watcher's memory and `arm_request` is consumed once. A
restart should not cold-disarm a running swarm.

- **E1.** Persist a session's current mode to the `sessions` table (new column
  `mode`, schema v3 migration) whenever `set_mode`/`toggle` changes it.
- **E2.** On startup, the watcher restores each registered session's mode from
  the DB when it first sees the sid.
- **E3.** Interaction with the spawn-arm guard: a restored mode is NOT an
  escalation (it was already granted in a prior run and recorded), so it is
  applied without the grace-window check; only a fresh `arm_request` still goes
  through first-sight + grace. Document the residual (a persisted insane mode
  survives restart by design; clear it by disarming before quit if unwanted).

E is the one item that touches schema and startup ordering; it gets real tests
when we resume the test cycle. A-D are surgical.

## Additional suggestions (proposed, decide before building)

- **F. `relay status --all` / `relay doctor`:** a CLI that prints, from outside
  the TUI, whether relay is running, how many sessions/armed, and any STALE
  workers - a lifeline for the "I launched it and I'm stuck" user who cannot
  read the TUI state programmatically.
- **G. First-launch one-liner:** if relay starts and finds zero controllable
  sessions, also print a single stderr line before the TUI takes over, so even
  a user who quits immediately saw the mental model once.
- **H. `relay spawn` default arm from config in the coordinator flow:** ensure
  `spawn_arm` is surfaced in `relay spawn --help` and the coordinator skill so
  arming is the obvious default, not an easily-missed flag.

## Test plan (deferred to end of batch)

Run `./test/run.sh` once after A-D (+E if built). New/updated tests: empty-state
render helper (pure, given a controllable count), swarm-view empty render,
db mode persistence + restore (if E), and skills docs verified against
`cli.py` flags. No em-dash anywhere.
