# Relay Swarm - Design Spec

**Date:** 2026-07-14
**Status:** Approved for planning

## Summary

Relay grows from a permission-approval control panel into a session control
plane: named Claude Code sessions register as coordinators or workers, exchange
messages through a local SQLite database, and track tasks (epics with subtasks,
states, and blockers). Relay's existing iTerm2 injection machinery becomes the
delivery leg: queued messages are typed into a target session's prompt when it
is idle, waking it up. The existing arm/approve behavior is untouched.

Decision context: this absorbs and retires the synapse-mini project. Synapse's
one unique piece (spawn) is ported; its log/view/task code is not. Relay wins
because delivery into a live session (screen watching, idle detection,
keystroke injection, audit) is the hard part and already exists here.

## Motivating scenario

A workspace holds 3 git clones: backend, BFF, frontend. The user drives a UI
change in one session while a coordinator session:

1. Writes three spec files (`specs/bff.md`, `specs/be.md`, `specs/fe.md`).
2. Creates three epics, each assigned to a named worker with a spec path.
3. Relay injects a wake-up into each idle worker.
4. Each worker reads its spec, splits it into subtasks, executes, updates
   states, and messages the coordinator when done or blocked.
5. A frontend task `blocked_by` the BFF and backend tasks fires its wake-up
   only when the last blocker completes.

A long-lived "PR reviewer" session is the degenerate case: a registered worker
whose inbox receives review requests. No extra features needed.

## 1. Architecture

One process, as today: the relay TUI/watcher. No daemon, no webhooks, no
event bus. **The DB is the bus.**

- Worker/coordinator sessions *push* by shelling out to relay CLI verbs from
  Claude's Bash tool. Each verb writes rows and exits.
- The watcher *pulls* the DB on the refresh tick it already runs for screen
  watching, and performs deliveries.
- TUI closed: CLI writes still land (messages queue, tasks update); delivery
  and display resume when the TUI opens. Preserves "tool on === TUI open".
- Future remote workers (AWS/ECS) would be a thin HTTP shim over the same DB.
  Out of scope; nothing in this design blocks it.

## 2. Identity and registration

iTerm2 sets `$ITERM_SESSION_ID` in every session's environment. A session
registers itself by running:

```
relay register --name bff-worker --role worker --project webshop
```

The CLI reads `$ITERM_SESSION_ID` and stores the binding. The watcher already
enumerates sessions by that id, so name -> injectable session is a lookup.
Every CLI verb resolves "me" the same way. Unregistered sessions behave
exactly as today (display-only unless armed).

Re-registration with an existing name rebinds it (a respawned worker reclaims
its identity). Names are unique per DB.

## 3. Data model

SQLite at `~/.relay/relay.db`, WAL mode. Multiple writer processes (CLI verbs
from worker sessions, the watcher) is SQLite's sweet spot. `audit.jsonl`
stays as-is: it is an audit trail, not state.

```
sessions:  name PK, iterm_session_id, role (worker|coordinator),
           project, status_text, registered_at, last_seen
messages:  id, project, from_name, to_name, body,
           created_at, delivered_at (NULL = queued)
tasks:     id, project, parent_id (NULL = epic), title,
           state (todo|doing|blocked|done), owner, spec_path,
           blocked_by (list of task ids), created_by, updated_at
```

An epic is a task with children; no separate table or concept.

## 4. CLI verbs

```
relay register --name X --role worker|coordinator --project P
relay send <name> "text"             # queue a message to a named session
relay status "working on #14"        # update own status line
relay task add [--parent N] [--owner X] [--spec path]
               [--blocked-by N[,N...]] "title"
relay task update <id> --state todo|doing|blocked|done
relay task list [--project P] [--mine]   # plain text, session-readable
relay inbox                          # my unread messages (marks delivered)
relay msgs [--with <name>] [--project P] # full message history (a SELECT)
relay spawn --name X --project P "first prompt"   # see section 7
```

`relay` with no args launches the TUI, unchanged.

## 5. Delivery mechanics

The watcher delivers a queued message **only when the target session is idle
at Claude's input prompt** (the watcher is the one component that can see
this). Delivery = typing the message + Enter, making it the session's next
user turn, prefixed for provenance:

```
[relay msg from coord] spec ready at specs/be.md
```

Busy target -> message stays queued. Two triggers auto-generate messages:

1. **Assignment:** a task gains an `--owner` -> the owner gets a wake-up
   naming the task id, title, and spec path.
2. **Unblocking:** a task completes -> every task listing it in `blocked_by`
   whose blockers are now ALL done fires a wake-up to its owner.

Every unattended delivery is appended to the audit log before injection, same
contract as auto-approvals: an unattended injection never goes unrecorded.

## 6. Staleness escalation

Walk-away autonomy requires hearing about a dead worker within minutes. The
watcher flags a session `STALE` and fires the existing notification + sound
path when either:

- it has queued (undelivered) messages older than N minutes, or
- it owns a `doing` task and its status/screen has not changed in N minutes.

N is configurable (env var, sane default ~10 min). Relay does NOT auto-reassign
tasks or re-prompt the worker: deciding what to do with a stuck worker is the
human's call; relay's job is timely escalation.

## 7. TUI

Two views, TAB toggles:

**Control view** (existing, extended): gains ROLE and TASK NOW columns.
Arm/approve behavior and keys untouched. `STALE` shows in STATUS.

```
 MODE    STATUS  UNIT        ROLE   TASK NOW
▸◉ SAFE  ACTIVE  coord       coord  specs 3/3 done
 ◉ SAFE  ACTIVE  bff-worker  work   #14 doing
 ◉ SAFE  WAIT    be-worker   work   #17 blocked by #14
 ○ MAN   ACTIVE  fe-ui       work   #21 doing
```

**Swarm view** (new): kanban board by state, epic progress, recent-messages
feed; filterable by project when more than one is active.

```
 PROJECT webshop · coord: coord · 3 workers
┌ TODO ─────┬ DOING ────┬ BLOCKED ─┬ DONE ─┐
│ #15 bff   │ #14 bff   │ #17 be   │ #13 ✓ │
│ #16 bff   │ #21 fe-ui │          │ #20 ✓ │
└───────────┴───────────┴──────────┴───────┘
 MESSAGES
 12:01 bff→coord: endpoint scaffolded
 12:03 coord→be: spec ready specs/be.md
```

## 8. Spawn

Port synapse's spawn: `relay spawn --name be-worker --project webshop "..."`
opens an iTerm2 tab, launches `claude` with the given first prompt, and
pre-registers the name so the coordinator can address it immediately. The
generated first prompt is minimal: it invokes the relay-worker skill and
states name, project, and task; the protocol lives in the skill. Synapse is
archived after the port.

## 9. Skills (the protocol layer)

Shipped in the repo under `skills/`, symlinked into `~/.claude/skills/` by
`install.sh` so they version with the repo:

- **relay-worker:** register with `--role worker`; check `relay inbox` on
  start and between tasks; keep `relay status` fresh; split an assigned epic
  into `--parent` subtasks; update states; `relay send <coordinator>` when
  done or blocked. Discipline: do not take tasks owned by others; mark
  blocked instead of spinning.
- **relay-coordinator:** register with `--role coordinator`; write specs as
  md files; create epics with `--owner` and `--spec`; spawn or address named
  workers; monitor via `relay task list`; react to done/blocked messages.
  Discipline: do not implement tasks yourself; do not poll in a busy loop
  (injected messages wake you).

Both share one CLI verb reference so the commands are documented once.

## 10. Scope boundaries

**In v1:** everything above, including staleness escalation, multi-blocker
`blocked_by`, and the `relay msgs` history verb.

**Out (deliberately):**
- Task auto-reassignment or worker re-prompting (escalate to human instead).
- Dependency engine: chains, cycle validation, auto-ordering. `blocked_by`
  fires wake-ups; ordering judgment belongs to the LLM coordinator.
- TUI message threads (use `relay msgs`; build the view in v2 if usage
  demands it).
- Remote transport / HTTP shim, web UI.

## 11. Security posture

Two accepted risks, documented in the README when this ships:

1. **Prompt-injection surface:** any local process can `relay send` text that
   becomes another session's next user turn, and an armed session will then
   auto-approve that turn's safe commands. Arm levels remain the guardrail;
   the audit log covers forensics.
2. **Input clobbering:** an injected message interrupts anything half-typed
   in the target session's input box. Rare; accepted.

## 12. Error handling

- Delivery fails audit-write -> refuse to inject (existing contract).
- `relay send` to an unknown name -> error, non-zero exit, so the sending
  session sees it and can react.
- Target session disappeared (tab closed) -> messages stay queued; session
  goes STALE; escalation fires.
- DB locked/busy -> WAL + busy_timeout; CLI verbs retry briefly then fail
  loudly rather than hang a Claude Bash call.

## 13. Testing

Same style as existing suites (no pytest; `__main__` runners; `test/run.sh`):

- Pure-logic tests for delivery decisions (idle gating, blocker resolution,
  staleness) with no iTerm2 imports, mirroring `gates.py`.
- CLI verb tests against a temp DB file.
- TUI render tests headless (both views).
- Injection reuses the already-tested keystroke path; end-to-end delivery is
  verified the same way approvals are: `--dry-run` first, which logs
  would-deliver decisions without sending keystrokes.
