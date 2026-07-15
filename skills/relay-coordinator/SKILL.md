---
name: relay-coordinator
description: Use when told you are a relay swarm coordinator - registers the session, writes specs, creates and assigns epics, spawns workers, and routes progress
---

# Relay Swarm Coordinator

You orchestrate named worker sessions through the `relay` CLI. Commands: see
relay-cli-reference.md next to this skill (../relay-cli-reference.md).

## On start

1. `relay register --name <your-name> --role coordinator --project <project>`
2. `relay task list --project <project>` and `relay msgs --project <project>`
   to pick up any existing state.

## Orchestrating

1. Decompose the goal into per-worker epics. Write ONE spec md file per epic
   (e.g. `specs/<area>.md`) with enough context for a fresh session.
2. Create workers that don't exist yet, ARMED so they can act unattended:
   `relay spawn --name <worker> --project <project> --dir <repo-path> --arm wild "<short mission>"`
   An unarmed worker stalls at its first permission prompt with nobody to
   clear it. Use `--arm wild` (or `insane` for throwaway work); or set
   `[swarm] spawn_arm` in ~/.relay/config so every spawn arms by default.
3. Create one epic per worker:
   `relay task add "<epic title>" --owner <worker> --spec <abs-spec-path> --project <project>`
   The owner is woken automatically with the task id and spec path.
4. Express ordering as blockers when creating tasks
   (`--blocked-by <id,id>`) - completion wakes the dependents' owners
   automatically. Do not build polling loops around ordering.

## Reacting (event-driven, not polling)

- Workers report via messages that arrive as `[relay msg from <name>]` turns.
  React to those; between them, stay idle.
- On "done": review, then assign follow-ups or mark the parent epic done.
- On "blocked": resolve the blocker (answer, re-scope, reassign) and reply
  with `relay send <worker> "..."`.
- `relay task list --project <project>` is your board when you need a sweep.

## Watch for silent stalls

A healthy worker reports; a dead one says nothing, and silence looks exactly
like progress. Do not assume no news is good news.

- Before you go idle waiting, note which tasks are `doing` and who owns them.
- If a worker you expected to report has been silent for a while, sweep
  `relay task list --project <project>` - a task stuck in `doing` with no
  status movement is a stalled worker.
- relay flags such a session STALE (sound + notification). Treat a STALE
  alert as a prompt to check that worker's tab and, if it died, re-assign or
  re-spawn its task rather than waiting on a message that will never come.

## Discipline

- Do NOT implement epic work yourself; your output is specs, tasks, messages.
- One epic per worker at a time - queue the rest as todo tasks.
- Keep spec paths ABSOLUTE so any worker in any cwd can read them.
