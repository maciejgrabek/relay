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

The watcher is the authority on which iTerm2 tabs exist. Each tick, for every
registered session:

- iterm_session_id NOT among the live tabs (`self.sessions`) -> increment an
  in-memory miss counter for that sid; once it reaches **2 consecutive misses**
  and `closed_at==0`, stamp `closed_at = now` and log `CLOSED <name>`.
- iterm_session_id IS live -> reset the miss counter, and if `closed_at != 0`
  clear it (the tab reappeared, e.g. a restore respawned it).

**Startup-race guard (self-review finding).** The closed marking runs ONLY on
a tick whose roster sync succeeded (a flag set after `_sync_sessions` returns
without raising). Without this, a first tick that hasn't yet enumerated tabs -
or a transient iTerm2 error that empties `self.sessions` - would falsely mark
every registered session closed, flap `closed_at`, and let a `restore` run in
that window try to revive live sessions. The 2-miss debounce plus the
sync-succeeded gate together make a false "closed" require two consecutive
good syncs that both lack the tab.

`db.register()` also clears `closed_at` (a respawn under the same name revives
it). New helpers: `db.mark_closed(conn, name, ts)`, `db.clear_closed(conn,
name)`, `db.closed_sessions(conn, project=None)`.

Startup coverage: because the watcher does a full pass on startup, a death
that happened while relay was closed is detected the next time relay runs.

## 4. `relay restore [names...] [--project P] [--yes] [--dry-run]`

Resume abandoned work.

**Two entry modes (self-review finding: the common case is stalled, not
closed).** A worker often does not *close* its tab - it ends its turn or sits
on a question, tab still open, task stuck `doing`. `closed_at` never fires for
those, so auto-restore alone would miss them:

- **Auto (no names):** candidates are sessions with `closed_at != 0` that own a
  non-`done` task - unambiguously dead tabs, safe to revive without asking
  which.
- **Manual (`relay restore designer critic`):** restore the named sessions
  regardless of `closed_at`, so a stalled-but-open worker can be revived on the
  user's judgement. If the old tab is still open, the respawn rebinds the name
  to the NEW tab; the old tab becomes a harmless unregistered zombie the user
  can close. The plan output warns when a named target is still live.

Flow:

1. Resolve candidates (auto or named). Group by session (name, role, workdir).
2. Print a plan, one line per candidate: `restore <name> (<role>) in <workdir>
   - N task(s): #a #b`, with `[tab still open - old tab left as a zombie]`
   appended when applicable. A candidate whose `workdir` is empty is listed as
   `SKIP <name> - no known workdir (use relay clean, or re-run relay in the
   dir)`. If `spawn_arm` resolves to `off`, print a one-line warning that
   restored workers will not act unattended (arm them or set `spawn_arm`).
3. Stop here if `--dry-run`. Otherwise, unless `--yes`, ask for confirmation
   (`restore N session(s)? [y/N]`) from stdin.
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
- A new `R` binding runs restore from the TUI. To avoid entangling tab-spawning
  with the Textual event loop, `R` **shells out** to `bin/relay restore --yes
  --project <active>` as a subprocess (the same verb, which spawns in its own
  process). Because spawning opens tabs, `R` shows a brief confirm state first
  (press R again within a couple seconds to proceed), not an instant fan-out.
  Auto-restore only (closed sessions); manual by-name restore stays a CLI action.
- The swarm view (TAB) marks a task whose owner is closed with a `!` and the
  owner dimmed, so orphans are visible on the board.

## 8. CLI wiring

`bin/relay` dispatches `restore` and `clean` to `cli.py`. `restore` takes
optional positional `names`. Planning and `clean` are pure DB and work
headless; only restore's spawn step needs iTerm2. `relay doctor` gains an
"orphans: N" line and lists closed sessions owning work.

Also: `clean` drops **undelivered** messages addressed TO a session it deletes
(they could never be delivered once the session row is gone); message history
keeps the name strings on remaining rows since there is no foreign key.

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
(that lives in git / on disk already); relay auto-closing a stalled worker's
old tab on manual restore (it is left as a zombie for the user to close -
relay closing a user's tab is too aggressive for v1).

## Self-review summary (findings folded in above)

1. **Stalled != closed.** The live failure case is a worker whose tab is still
   open but idle; `closed_at` misses it. Fixed by adding manual
   `relay restore <names>` alongside closed-only auto-restore (section 4).
2. **closed_at startup race.** A first/failed roster sync could false-mark
   everything closed. Fixed with a 2-miss debounce + sync-succeeded gate
   (section 3).
3. **Restore into `spawn_arm=off`** revives workers that immediately re-stall
   unarmed. Fixed with a warning in the plan (section 4).
4. **TUI spawning on the Textual loop** is fragile. Fixed by shelling out to
   the CLI verb (section 7).
5. **clean vs restore ordering** (clean destroys restore's workdir context) -
   already called out in the summary at the top; both verbs confirm first.
