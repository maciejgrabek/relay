# Relay PR Routing and Swarm Onboarding - Design Spec

**Date:** 2026-07-29
**Status:** Designed

## Summary

Relay learns to answer one question: **who owns this pull request?** A worker
claims the PR it opened; a PR-sweep session pushes what it sees on GitHub; and
`relay send --pr owner/name#482 "changes requested: ..."` routes the feedback
back to the session that actually holds the context, or refuses and escalates
to the human.

Alongside it, relay becomes self-teaching: `relay join <name>` registers a
session and prints the swarm protocol, so a session told "work with the others
via relay" onboards itself without depending on a skill having triggered.

The messaging substrate this rides on already exists (`relay send`, `relay
inbox`, watcher injection into idle sessions, the swarm view). Nothing about
delivery, arming, or approval changes.

## Motivating scenario

The user runs several worker sessions across separate repos and one long-lived
PR session. The PR session has its own sweep skill (living outside this repo)
that walks a list of repos with `gh` and reports PR state.

Today, when that sweep finds "changes requested" on PR 482, the user copies the
feedback into whichever session they believe wrote it - after working out which
one that was. That last part is the actual pain: nothing records it.

After this change:

1. `api-worker` opens PR 482 and runs `relay pr claim acme/api#482 --task 14`.
2. The sweep session pushes what it sees: `relay pr set acme/api#482 --state
   changes`.
3. It routes: `relay send --pr acme/api#482 "changes requested: <summary>"`.
4. Relay resolves the owner, confirms it is still the same session, and queues
   the message. The watcher types it into `api-worker` when idle.
5. `api-worker` flips task 14 back to `doing`, fixes, pushes, and replies to
   the sender.
6. If the owner is gone, relay refuses (non-zero exit) and the sweep batches
   every miss into one `relay send --human` ping. The human decides.

## 1. Architecture

No new moving parts. The DB stays the bus; the watcher stays the delivery leg.
This adds one table and one resolution rule: `(repo, number)` resolves to an
owning session, checked for identity before anything is delivered.

**Relay stays gh-less.** It never shells out to `gh`, never polls GitHub, never
holds a repo list. Every PR fact in the DB was pushed in by a session. This is
the boundary that keeps relay a local bus rather than a GitHub integration.

The consequence must be stated plainly because the TUI depends on it: **stored
PR state is last-known-as-reported, not truth.** The UI always renders the age
of that report next to the state, so a stale `approved` reads as stale rather
than as fact.

## 2. Data model

New table, appended to `_SCHEMA`. Per the idiom documented at `db.py:91`, a new
`CREATE TABLE IF NOT EXISTS` needs no migration.

```sql
CREATE TABLE IF NOT EXISTS prs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL,                    -- owner/name
  number INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'created',
  task_id INTEGER,                       -- nullable; PRs route without a task
  owner TEXT NOT NULL DEFAULT '',        -- session name at claim time
  owner_session_id TEXT NOT NULL DEFAULT '',  -- $ITERM_SESSION_ID at claim
  claimed_at REAL NOT NULL DEFAULT 0,
  state_changed_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,              -- last time any session touched it
  last_routed_at REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_prs_ref ON prs(repo, number);
```

Indexes live outside `_SCHEMA` and run after `_migrate`, matching the existing
convention at `db.py:85`.

### owner_session_id is load-bearing

Session names in relay are reclaimable: re-registering an existing name rebinds
it to a new tab, and `relay restore` depends on that. So routing by name alone
can deliver "your PR has changes requested" to a session that has never seen
that branch, six hours after the original author's tab closed.

Storing `$ITERM_SESSION_ID` at claim time turns "is this still the session that
opened the PR?" into a real check. It is the difference between a record and a
hope, and it is why branch-name derivation (`relay/<name>` implies owner) was
rejected as the primary mechanism.

### PR state vocabulary

A flat enum, chosen to match what one `gh pr list --json
number,state,reviewDecision,headRefName,title` call can compute:

| state | meaning |
|---|---|
| `created` | open, no review activity yet |
| `review` | review requested or pending |
| `changes` | changes requested |
| `approved` | approved, not yet merged |
| `merged` | merged |
| `closed` | closed without merging |

Relay validates the token and stores it. It never derives or infers state.

### PRs are not tasks

`todo/doing/blocked/done` is relay's own state machine, and `clean`, `wipe` and
`restore` all reason about it. PR state is external truth relay cannot verify.
Modelling PRs as tasks would make the kanban assert things relay does not know,
and would put the recovery verbs in the business of deleting pull requests.

They are linked instead: `prs.task_id` lets a kanban card render `#14 ▸ PR 482
⊘changes` while the PR itself lives in its own pane.

## 3. Routing rule

`relay send --pr <ref> "<body>"` resolves in three steps:

1. **No `prs` row, or a row with no `owner`** -> exit 3, stderr `unclaimed:
   acme/api#482`. Nobody claimed it (a human PR, or a worker that skipped the
   claim step).
2. **Row has an owner, but the session is gone** -> exit 4, stderr `owner gone:
   api-worker (<reason>)`. Three distinguished reasons: the session row is
   missing, `closed_at != 0`, or `iterm_session_id` no longer matches
   `owner_session_id` (the name was rebound to a different tab).
3. **Otherwise** -> queue the message to the owner, stamp `last_routed_at`, and
   print `routed to api-worker (task #14)` on stdout.

Relay never auto-restores the author, never reassigns to a different worker,
and never rewrites task state as a side effect of routing. All three were
considered and rejected: a worker with no context on a branch re-deriving
intent from review comments produces plausible-looking fixes that miss the
point, and the miss is not caught until the next review cycle. Both failure
modes are the human's call.

The message carries `--kind` like any other, defaulting to `info`.

## 4. CLI verbs

One PR reference format everywhere: `owner/name#number`, as a positional
argument. A single format is one less thing for a session to get wrong.

```
relay pr set <ref> --state created|review|changes|approved|merged|closed
                   [--title <t>] [--branch <b>] [--project <p>]
relay pr claim <ref> [--task <id>] [--branch <b>] [--project <p>]
relay pr list [--project <p>] [--mine] [--days <n>]
relay send --pr <ref> "<body>" [--kind <k>]
relay send --human "<body>"
```

**`relay pr set`** upserts the PR row. Run by the sweep session on every sweep.
It works on unclaimed PRs: pushing state is independent of ownership, which is
what makes an unclaimed PR visible rather than silently absent. Updating
`state` to a different value stamps `state_changed_at`; every call stamps
`updated_at`.

**`relay pr claim`** attaches ownership: sets `owner` to the calling session's
name, `owner_session_id` to its `$ITERM_SESSION_ID`, and optionally `task_id`
and `branch`. Creates the row if the sweep has not seen the PR yet. Re-claiming
overwrites ownership, which is the correct behaviour when a worker is restored
and resumes a PR.

**`relay pr list`** prints PRs in stable order (repo, then number) with state,
age since the state changed, owner, task, and an `unclaimed` or `owner gone`
marker. `--mine` filters to the calling session; `--days` bounds the window
(default: the retention window).

**`relay send --human`** is a reserved recipient. The message is stored with
`to_name = 'human'` and `kind = 'escalation'`, so the existing
`swarm.escalation_pings` path (`swarm.py:81`) plays the sound and posts the
notification, and it appears in the swarm feed. It is **never injected into any
session**: `human` is not a registered name and the delivery loop skips it.

This exists because a PR-sweep session sits at the top of its swarm with no
coordinator above it to escalate to, and every escalation today must be
addressed to a session.

`human` therefore joins `relay` as a **reserved session name**: `relay
register` and `relay join` reject it, so no tab can ever become the recipient
of escalations meant for the operator. `relay send --all` is unaffected, since
it targets live registered sessions and `human` is never one.

The three target forms of `relay send` are mutually exclusive: a positional
name, `--all`, `--pr <ref>`, or `--human`. Supplying more than one is an
argument error, not a precedence rule to memorise.

**`relay doctor`** gains a PR block: counts by state, plus any PR that is
unclaimed or whose owner is gone.

## 5. Retention

PRs mirror the message-retention design (`db.prune_messages`, called once at
launch from `app.py`):

- `RELAY_PR_RETENTION_DAYS`, default 7.
- Prunes rows in `merged` or `closed` state whose `updated_at` is older than
  the window.
- **Open PRs are never pruned**, at any age. An open PR is live state, not
  history. This mirrors the existing rule that queued (undelivered) messages
  survive pruning regardless of age.

Seven days covers the stated working rhythm (PR opened one day, reviewed the
next) with slack for a weekend.

## 6. TUI

### Where

A new pane in the **swarm view** (`TAB`), not a third top-level view. The swarm
view is already the "what is the fleet doing" screen, and PRs are the tail end
of that work. A third view is a third key that gets forgotten.

The fleet line gains a segment: `PRs 5 · 2 need work`.

### Layout

An **attention strip on top**, then the full list below in stable order:

```
 PULL REQUESTS
 ‼ acme/api#482   ⊘changes  4h   api-worker  #14
 ‼ acme/bff#77    ⊘changes  1d   UNCLAIMED
 ──────────────────────────────────────────────────
   acme/api#480   ✓merged   2d   api-worker  #11
   acme/api#482   ⊘changes  4h   api-worker  #14
   acme/bff#77    ⊘changes  1d   UNCLAIMED
   acme/web#31    ◷review   6h   fe-worker   #21
```

The strip duplicates the rows needing action (`changes`, unclaimed, owner
gone). The main list never reorders as states change, so a row stays where the
eye last found it. This follows the same principle as the existing message
feed and roster.

Age is always rendered next to state, per section 1: the pane shows when relay
was last told something, never implying it knows more.

The kanban card for a task with a PR gains a suffix: `#14 ▸ PR 482 ⊘changes`.

The control view is untouched. It is already at its column budget, and PR state
is not a per-tab property.

## 7. Testing

Same style as the existing suites (no pytest, `__main__` runners, wired into
`test/run.sh`):

**Pure logic** (`swarm.py`, no sqlite or iTerm2 imports), mirroring `gates.py`:

- `resolve_pr_route` across all four outcomes: routable, unclaimed, owner
  closed, owner name rebound to a different session id.
- The rebound case specifically: a claim written by session A under name
  `api-worker`, then `api-worker` re-registered from a different tab, must
  resolve to `owner gone` and not to the new tab. This is the bug the whole
  identity column exists to prevent.
- PR pane row ordering: attention strip contents, stable main-list order.
- Retention predicate: merged/closed prune, open never prunes.

**CLI verbs** against a temp DB (`test_cli.py` style): ref parsing including
malformed refs, `pr set` upsert and `state_changed_at` stamping, `pr claim`
create-and-update paths, exit codes 3 and 4 with their stderr text, `send
--human` storing an undeliverable escalation, `register`/`join` rejecting the
reserved names `human` and `relay`, and `send` rejecting two target forms at
once.

**Render** (`test_swarm.py` style): the PR pane headless, including the empty
case and a long list.

## 8. Swarm onboarding

The protocol already lives in `skills/relay-worker` and
`skills/relay-coordinator`, symlinked into `~/.claude/skills` by `install.sh`.
Three gaps make that insufficient for "you two work together via relay":

1. **Trigger too narrow.** `relay-worker`'s description reads "Use when told
   you are a relay swarm worker", which may not match how the instruction is
   actually phrased.
2. **No peer mode.** Both skills assume a coordinator-above-workers hierarchy.
   `relay-worker` hardcodes reporting to the coordinator. A PR-sweep session is
   a peer that dispatches, and two sessions told to collaborate have no
   hierarchy at all.
3. **Skills can be absent.** If the symlink is missing or the skill does not
   trigger, the session has nothing.

### relay join

```
relay join <name> [--role worker|coordinator] [--project <p>]
relay help swarm
relay help pr
```

`relay join <name>` is one command that does everything a session needs to
start:

1. **Registers.** `--role` defaults to `worker`. `--project` defaults to the
   single active project if exactly one exists, else the workdir basename.
2. **Prints the roster:** every live session, its role, and its current status
   line, so the joining session knows who it can talk to.
3. **Prints its inbox:** anything already queued for that name.
4. **Prints the protocol:** the verbs it will need, plus four discipline rules
   - keep `relay status` fresh because it is the heartbeat relay's staleness
   detector reads; reply to whoever messaged you rather than to an assumed
   coordinator; never end a turn silent with a task still `doing`; escalate to
   the human rather than guessing.

`relay help swarm` prints the same protocol text without registering, for a
session (or a human) that wants to read first. `relay help pr` covers the PR
verbs. The protocol text is defined once and shared by all three paths.

The point is that **a session with no skills installed can still participate
correctly**, because the CLI teaches it. Skills become the richer version
rather than the only version.

### Skill changes

- `relay-worker`: reply to the message sender, not to a hardcoded coordinator;
  escalate to the human when there is nobody to reply to. Add `relay pr claim`
  to the done-checklist, next to the existing commit-before-reporting rule -
  the same moment, so it inherits a habit workers already follow. On receiving
  PR feedback: flip the task back to `doing`, fix, push, reply to the sender.
- Both skills: descriptions broadened to match natural phrasings ("work with
  the other sessions via relay", "coordinate through relay", "join the swarm").
- `relay-cli-reference.md`: the five new verbs plus a "routing PR feedback"
  section the sweep skill can follow.
- README: a short section showing the one-liner that starts a collaboration.

### Deliberately not doing: auto-registration

Relay could register every session it watches, removing setup entirely. It
would also make every unregistered tab addressable and injectable by any local
process, widening the prompt-injection surface the swarm spec calls out in its
section 11. Joining a swarm stays an explicit act.

## 9. Scope boundaries

**In:** everything above.

**Out, deliberately:**

- Any `gh` call, GitHub polling, webhook, or repo list inside relay.
- Auto-restoring a dead PR owner, or reassigning the PR to another worker.
- Relay mutating task state as a side effect of routing (the worker does it).
- Review-comment storage or threading. Relay routes a message; the body is
  whatever the sweep session wrote.
- A PR view separate from the swarm view.

## 10. Residual risks

**A worker that never claims is invisible to routing.** `relay pr set` from the
sweep makes the PR *visible* (it shows as `UNCLAIMED` in the pane and in
`doctor`), so the gap surfaces instead of silently swallowing a PR, but routing
still cannot happen automatically. Accepted: an unclaimed PR escalates to the
human, which is the same policy chosen for a dead owner.

**Stored PR state can be stale.** Relay only knows what the last sweep told it.
Mitigated by always rendering the age of the report, never the bare state.

**`relay pr set` grows relay a mirror of PR state**, a step beyond a pure
message bus. Accepted because operator transparency was an explicit goal:
without it, "which session did which PR" is answerable only by a Claude session
and never by a human looking at a screen.
