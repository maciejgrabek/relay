# Wipe Carries Messages + Zap Project - Design Spec

**Date:** 2026-08-05
**Status:** Implemented (see docs/plans/2026-08-05-wipe-messages-and-zap.md)

## Problem

Two gaps surfaced by a real incident (a session took over another session's
name and spoke under it; the stale transcript made the roleplay convincing):

1. **Wiping a session leaves its voice behind.** `relay wipe` (and `clean`)
   delete the session row and its *undelivered* mail, but every delivered
   message it sent or received survives as history until prune retention.
   Worse, a *queued* message FROM a wiped session TO a live one still
   delivers later - ghost mail from a sender that no longer exists. The
   2026-07-16 wipe spec kept delivered history deliberately ("consistent
   with clean"); the incident showed that consistency preserves exactly the
   material an impersonator needs.

2. **No shortcut for a full project clear.** The TUI's `W W` handles only
   closed sessions' work, and when nothing is orphaned it tells the operator
   to go type `relay wipe --project <p> --all` in a shell. Clearing the
   board between runs is a routine operation; it should not require leaving
   the TUI.

## Design

### 1. Session-scoped wipe deletes the session's messages

`db.delete_messages_for(conn, name) -> int`:

```sql
DELETE FROM messages
 WHERE (from_name = ? OR to_name = ?)
   AND (thread_id IS NULL
        OR thread_id NOT IN (SELECT id FROM threads WHERE state = 'open'))
```

Sent or received, delivered or queued - all gone. One carve-out: posts in a
still-**open** discussion are kept, consistent with the existing rule that
open threads are live state and are never deleted however old
(`prune_threads` docstring). They age out via `prune_messages` after the
thread closes. Wiping one dead participant must not hole out a transcript
other participants can still read.

Call sites, replacing `delete_undelivered_to`:

- `cmd_wipe` (session-scoped path): per candidate, `delete_tasks_by_ids`,
  `delete_messages_for`, `delete_session`.
- `cmd_clean`: same substitution. `clean` also deletes the session row;
  leaving its transcript behind is the same ghost-mail hole.

`delete_undelivered_to` itself stays - restore still uses it.

**Plan + confirm honesty.** `db.count_messages_for(conn, name) -> int`
(same WHERE as the delete). `cmd_wipe`/`cmd_clean` stamp `msg_count` on each
candidate; `swarm.wipe_plan_text` renders `+ K message(s)` per line, and the
confirm prompt becomes `permanently DELETE N task(s) + M session(s) + K
message(s)?`. `clean`'s confirm gains the message count the same way.

### 2. TUI `Z Z` - zap the whole project

New binding `Z` -> `action_zap`, same double-press arming pattern as `W`:

- **Project resolution:** the TUI is project-agnostic, so resolve from the
  DB: `db.list_projects(conn)` - distinct non-empty project values across
  sessions, tasks, and messages. Exactly one project -> target it. Zero ->
  log "nothing to zap". More than one -> log the list plus the CLI command
  and do nothing (no picker; guessing a target for a permanent delete is
  worse than a shell round-trip).
- **First press** arms `_zap_armed` (auto-cancel after `_CONFIRM_WINDOW`,
  same as wipe) and logs: `zap ARMED: press Z again to DELETE ALL of
  project '<p>' (N tasks + M sessions + K messages)`. Counts come from the
  same dry SELECTs `cmd_wipe --all` uses.
- **Second press** runs `relay wipe --project <p> --all --yes` through
  `_shell_verb`, which learns to take extra argv (currently verb-only).
  The `--all` path already deletes messages and removes clean worktrees;
  dirty worktrees are kept, as today.
- The `W`-with-nothing-orphaned hint and the header key legend gain `Z`.

### 3. Docs

README swarm section, `W` hint string, header legend `("Z×2", "zap")`,
and the help overlay if it lists keys. `bin/relay` CLI surface is
unchanged (no new verb; `wipe --all` already exists).

## Testing

- `db`: `delete_messages_for` deletes sent+received, queued+delivered;
  keeps open-thread posts; deletes closed-thread posts; leaves other
  sessions' messages. `count_messages_for` matches. `list_projects` unions
  the three tables, skips empty strings.
- `cli`: `wipe --yes` on a closed session removes its messages (and ghost
  queued mail to a live peer), leaves the live peer's other messages;
  plan text and confirm include message counts; `clean` same.
- `app`: `Z` binding present; zap with 0 / 2+ projects logs and does not
  arm; arming message shape. Live two-press deferred to human, same as `W`.

## Out of scope

- The `relay join` name-hijack (register rebinds a live name with no
  liveness check, db.py register ON CONFLICT). Root cause of the incident,
  explicitly deferred by the operator; the wipe is cleanup, not prevention.
- A messages-only wipe flag (`--messages`); two half-wipes to remember.
- Multi-project zap or a TUI project picker.
- Undo (same stance as the original wipe spec: `--dry-run` and arming are
  the guardrails; file work lives in git, untouched).
