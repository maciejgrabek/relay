# Swarm Restore + Clean - Design Spec

**Date:** 2026-07-15
**Status:** Approved for planning

## Problem

A worker or coordinator's Claude session ends (crash, closed tab, hit an
unanswerable prompt). Its `sessions` row lingers, and the tasks it owned stay
`doing` forever - visible on the board, but nobody is working them and the DB
cannot tell the owner is dead. There is no way to bring the work back, and no
way to tidy it up. Worse, `relay spawn --dir` is used to launch a tab but the
working directory is **never persisted**, so even if we wanted to resume a
dead worker we would not know where to relaunch it.

Observed live: tasks #1 (designer) and #2 (critic) sit `doing` with both
owners dead.

## Goals

1. Persist enough context per session to relaunch it in the right place.
2. Give relay a trustworthy "this session is gone" signal.
3. `relay restore` - bring dead workers back, in their original workdir, under
   their own name, to finish their own tasks.
4. `relay clean` - the opposite: give up on dead workers, reset their work to
   unowned `todo`, and remove the ghost rows.
5. Surface orphaned work proactively in the TUI and offer restore.

`restore` and `clean` are opposite resolutions of the same orphan: restore
keeps the work owned and revives the worker; clean unowns the work and deletes
the worker. `clean` destroys the context `restore` needs (the workdir on the
session row), so you choose one per orphan - and if you might want either, run
`restore` first.

## 1. Schema (v4 migration)

Add three columns to `sessions`:

- `workdir TEXT NOT NULL DEFAULT ''` - absolute path the session was spawned
  in (empty for sessions that self-registered without one).
- `spawn_prompt TEXT NOT NULL DEFAULT ''` - the original mission string, so a
  resumed session gets context beyond just its task ids.
- `closed_at REAL NOT NULL DEFAULT 0` - 0 = alive; a unix timestamp = when the
  watcher last confirmed the tab is gone.

`db.connect()` migrates v3 -> v4 via the existing step-migration ladder.

## 2. Persisting context

- `relay spawn` records `workdir` (the resolved `--dir`) and `spawn_prompt`
  (the mission) on the session row it creates.
- `relay register` gains an optional `--dir <path>` so a self-registering
  session can record its own workdir; without it, `workdir` stays empty and
  that session is "reset-only" (clean can reclaim its tasks, restore skips it
  with a note that it has no known workdir).
- A new `db.set_session_context(conn, name, workdir, spawn_prompt)` writes both.

## 3. Liveness (`closed_at`)

The watcher is the authority on which iTerm2 tabs exist. Each tick (and on the
first pass at startup), for every registered session:

- iterm_session_id NOT among the live tabs (`self.sessions`) and `closed_at==0`
  -> stamp `closed_at = now`, log `CLOSED <name>`.
- iterm_session_id IS live and `closed_at != 0` -> clear it (the tab
  reappeared, e.g. a restore respawned it).

`db.register()` also clears `closed_at` (a respawn under the same name revives
it). New helpers: `db.mark_closed(conn, name, ts)`, `db.clear_closed(conn,
name)`, `db.closed_sessions(conn, project=None)`.

Startup coverage: because the watcher does a full pass on startup, a death
that happened while relay was closed is detected the next time relay runs.

## 4. `relay restore [--project P] [--yes] [--dry-run]`

Resume abandoned work.

1. Find candidates: sessions with `closed_at != 0` that still own at least one
   non-`done` task. Group by session (name, role, workdir).
2. Print a plan, one line per candidate: `restore <name> (<role>) in <workdir>
   - N task(s): #a #b`. A candidate whose `workdir` is empty is listed as
   `SKIP <name> - no known workdir (use relay clean, or re-run in the dir)`.
3. Stop here if `--dry-run`. Otherwise, unless `--yes`, ask for confirmation
   (`restore N session(s)? [y/N]`) reading from stdin.
4. For each confirmed candidate, `spawn_worker(name, project, resume_prompt,
   workdir, role, arm=<config spawn_arm>)`. Reusing the same name rebinds the
   session and clears `closed_at`; the tasks are already owned by that name.

The resume prompt is minimal and points at the skill:

    Invoke the relay-<role> skill. You are '<name>' on project '<project>',
    RESUMING work a previous session left unfinished. Run `relay task list
    --mine` and `relay inbox`, then continue the in-progress task(s) from where
    they were left. Original mission: <spawn_prompt>

## 5. `relay clean [--project P] [--yes] [--dry-run]`

Give up on dead workers and tidy the board.

1. Find the same candidates (closed sessions owning non-done tasks) PLUS closed
   sessions owning nothing.
2. Print a plan: for each, `reset K task(s) to todo` (the non-done tasks it
   owns) and `remove session <name>`.
3. `--dry-run` stops here; otherwise confirm unless `--yes`.
4. Apply: for every non-`done` task owned by a closed session, set
   `state='todo'` and `owner=NULL` (unowned + claimable). Then delete the
   closed session rows. New helpers: `db.reset_owner_tasks(conn, name)`,
   `db.delete_session(conn, name)`.

`clean` never touches a live session or a `done` task.

## 6. Automatic message retention (separate, small)

Independent of clean: on TUI launch (where audit pruning already happens),
prune **delivered** messages older than `RELAY_MSG_RETENTION_DAYS` (default 7).
Queued (undelivered) messages are always kept. `db.prune_messages(conn,
older_than_days, now)`.

## 7. TUI: surface orphaned work + offer restore

- The watcher exposes `orphan_count` (closed sessions owning non-done tasks).
- When `orphan_count > 0`, the subtitle carries a hint:
  `N task(s) orphaned by closed sessions - press R to restore, or 'relay clean'`.
- A new `R` binding runs restore from the TUI: it spawns the candidates (same
  code path as the CLI verb, armed per config) after a one-key confirm. Because
  spawning opens tabs, `R` shows a brief confirm state first (press R again to
  proceed), not an instant fan-out.
- The swarm view (TAB) marks a task whose owner is closed with a `!` and the
  owner dimmed, so orphans are visible on the board.

## 8. CLI wiring

`bin/relay` dispatches `restore` and `clean` to `cli.py`. `restore` and
`clean` need iTerm2 only for the actual spawn (restore) - the planning and
`clean` are pure DB and work headless. `relay doctor` gains an "orphans: N"
line and lists closed sessions owning work.

## 9. Testing

- `db`: v3->v4 migration; set_session_context; mark_closed/clear_closed/
  closed_sessions; reset_owner_tasks + delete_session; prune_messages.
- `swarm`/pure: candidate grouping and the restore-plan / clean-plan text as
  pure functions over rows (no iTerm2), so the plans are unit-tested.
- `watcher`: closed_at stamping (tab present vs absent), clear on reappear,
  orphan_count.
- `cli`: restore --dry-run plan output; clean --dry-run and applied against a
  temp DB (state reset + rows removed); no-workdir candidate skipped by
  restore.
- Live (human checklist, deferred): kill a worker tab, confirm it goes CLOSED,
  `relay restore` brings it back in the right dir and it resumes its task.

## 10. Out of scope

Restoring the exact conversation/context of the dead Claude session (relay
only knows tasks + mission, not the transcript); auto-restore without any
confirmation; restoring across machines; snapshotting partial file work
(that lives in git / on disk already).
