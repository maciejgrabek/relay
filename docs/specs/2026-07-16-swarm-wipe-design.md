# Swarm Wipe - Design Spec

**Date:** 2026-07-16
**Status:** Implemented (see docs/plans/2026-07-16-swarm-wipe.md)

## Problem

Relay can now *restore* dead workers (continue their work) and *clean* them
(reset their tasks to unowned `todo`). Missing is the third disposition:
**delete** the work so you can restart it from scratch. After `clean`, the old
task rows survive as todo; sometimes you want them gone - a blank board to
re-plan against, or a whole project nuked to start over.

`wipe` is the delete-counterpart to `clean`:

- `restore` - revive the worker, keep and continue the tasks.
- `clean` - drop the worker, RESET its non-done tasks to unowned `todo`.
- `wipe` - drop the worker, DELETE its tasks outright.

## Verb

`relay wipe [names...] [--project P] [--all] [--yes] [--dry-run]`

Two scopes:

### Orphaned (default, safe)

`relay wipe [--project P]` or `relay wipe <names> [--project P]`

Targets **closed sessions only** (same candidate set as `clean`): for each
closed session (optionally filtered to `names` / `--project`), delete every
task it owns (any state, including its subtasks that it owns), delete its
queued (undelivered) messages, and delete the ghost session row. Live sessions
and any task owned by a live session are never touched.

This is `clean` with delete-instead-of-reset. Message *history* (delivered
rows) is kept, consistent with `clean`/`delete_session`.

### Whole project (escalated)

`relay wipe --project P --all`

A true blank slate for one project: delete ALL tasks, ALL sessions, and ALL
messages whose project is P, regardless of liveness. Requires `--project`;
`--all` without `--project` is an error (so a single command can never nuke
every project at once). Even a live session's row for P is removed - the user
is explicitly starting that project over.

## Guards

- Prints a plan with exact counts before doing anything: `WIPE PLAN` then, per
  scope, `delete N task(s), M session(s), K queued message(s)` (orphaned lists
  each session name; `--all` shows the project totals).
- `--dry-run` prints the plan and stops.
- Otherwise, unless `--yes`, confirm from stdin. The confirm line states the
  counts and that it is a DELETE (`permanently delete N tasks + M sessions?
  [y/N]`), so it reads differently from `clean`'s confirm.
- Blocked-dependency warning: if a task being wiped is listed in the
  `blocked_by` of a task that is NOT being wiped, the plan prints
  `WARNING: #X is a blocker of #Y (not being wiped) - #Y may never unblock`.
  Informational; does not stop the wipe.

## Pure planning + DB layer

- `swarm.wipe_candidates(sessions, tasks, names=None) -> list[dict]` - closed
  sessions (optionally filtered to `names`), each `{name, task_ids}` where
  task_ids = ALL tasks it owns (not just non-done - wipe deletes done ones too).
- `swarm.wipe_blocker_warnings(cands, tasks) -> list[str]` - the dangling-
  blocker warnings above (pure).
- `swarm.wipe_plan_text(cands, project_all=None) -> str` - plan text for the
  orphaned scope; `project_all=(n_tasks, n_sessions, n_messages)` renders the
  `--all` totals form.
- `db.delete_tasks_for_owner(conn, owner) -> int` - `DELETE FROM tasks WHERE
  owner=?`.
- `db.wipe_project(conn, project) -> (int, int, int)` - delete all tasks,
  sessions, messages for a project; returns the three counts.
- Reuse `db.delete_session`, `db.delete_undelivered_to` (from restore/clean).

## CLI flow (cmd_wipe)

1. If `--all`: require `--project`, else error. Compute counts for the project
   (a dry SELECT), print the `--all` plan.
2. Else (orphaned): build `wipe_candidates` from `db.closed_sessions` +
   `db.list_tasks`, print `wipe_plan_text` + any blocker warnings.
3. `--dry-run` -> return 0. Else confirm (unless `--yes`); on no/EOF, abort.
4. Apply:
   - orphaned: per candidate, `delete_tasks_for_owner`, `delete_undelivered_to`,
     `delete_session`.
   - `--all`: `wipe_project(P)`.
   Pure DB; no iTerm2 - fully headless. `bin/relay` dispatches `wipe`; the `-h`
   header lists it.

## TUI

- A new `W` binding, orphaned scope only (never `--all` from a keystroke).
- Active only when `watcher.orphan_count > 0` (same gate as `R`).
- Two-press confirm like `R`: first `W` arms and logs "press W again within 3s
  to DELETE orphaned work"; second `W` shells out to `bin/relay wipe --yes`.
- The subtitle orphan hint gains the option: `... press R to restore, W to
  wipe, or 'relay clean'`.

## Testing

- `swarm`: `wipe_candidates` (closed-only, names filter, includes done tasks),
  `wipe_blocker_warnings` (fires only across the wipe boundary), `wipe_plan_text`
  (orphaned + `--all` totals form, and empty -> "(nothing to wipe)").
- `db`: `delete_tasks_for_owner` (deletes only that owner's tasks, leaves others),
  `wipe_project` (removes all three row types for the project, leaves other
  projects intact) with counts.
- `cli`: `wipe --dry-run` (no changes), `wipe --yes` orphaned (tasks+session
  gone, live untouched), `wipe --project P --all` (project emptied, other
  project intact), `--all` without `--project` -> error.
- TUI `W` binding load validated by the existing test_app render; live two-press
  deferred to human.

## Out of scope

Undo (deletion is permanent - that is the point; `--dry-run` + confirm are the
guardrails); wiping across projects in one command; wiping by task id
(disposition is per-session, matching restore/clean); recovering the deleted
tasks' file work (it lives in git / on disk, untouched by relay).
