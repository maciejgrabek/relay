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
2. Create workers that don't exist yet:
   `relay spawn --name <worker> --project <project> --dir <repo-path> "<short mission>"`
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

## Discipline

- Do NOT implement epic work yourself; your output is specs, tasks, messages.
- One epic per worker at a time - queue the rest as todo tasks.
- Keep spec paths ABSOLUTE so any worker in any cwd can read them.
