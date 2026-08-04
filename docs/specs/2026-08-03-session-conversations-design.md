# Session Conversations - Design Spec

**Date:** 2026-08-03
**Status:** Implemented (2026-08-03), amended twice the same day

> **Amendment (2026-08-03): relay does not decide.** As first designed and
> built, relay enforced the round cap by refusing a post, and closed a
> discussion `unresolved` on a spent cap, escalating it to the operator. Both
> were relay deciding things that belong to the sessions having the
> conversation - the first silenced an agent mid-argument, the second declared
> the agents had failed and reassigned their decision to a human. Corrected:
> the cap is a reported budget that never blocks; relay closes a thread on
> exactly one condition (every participant posted `agree`, which is reading
> what they did, not judging it); every other ending is declared by an agent
> via `relay close`, including escalating to a human, which is now the agents'
> call to make. `THREAD_STATES` is `open | agreed | closed`; `unresolved` is
> gone. Sections 4, 5 and 7 below are amended in place; section 12's first
> risk is retired, because policing the quality of agents' decisions is not
> relay's job.

> **Amendment 2 (2026-08-03): enforce the physical, keep teaching the social.**
> Amendment 1 pulled relay out of the agents' decisions, which left open what
> relay may still refuse. The line: relay enforces **conditions it can read off
> its own rows**, and teaches everything else in prose. `say` / `agree` /
> `close` are now refused while the caller has undelivered posts in that
> thread - not a judgement about the conversation, just the fact that those
> rows have not reached it yet - and the refusal prints those posts and marks
> them read, so the retry costs a bash call instead of a turn. `relay thread`
> consumes the caller's queued posts for the same reason. Section 5's catch-up
> paragraph and section 12's ignore-the-pointer risk are amended below. Nothing
> about HOW to argue moved from prose into structure: section 7 stands
> unchanged and the round budget is still advisory.
>
> The same line applied to spawn - outside this spec's scope, in the same pass:
> `relay spawn` refuses an unarmed worker (unless `--arm off` is explicit) and
> refuses a workdir a live worker already occupies (unless `--worktree` or the
> new `--share`). Both were prose in `relay-coordinator`; both are facts relay
> already stores.

## Summary

Relay learns to let sessions **talk to each other and settle a question**
without the operator carrying messages between tabs.

Three capabilities, in order of leverage:

1. **Zero-setup identity.** `relay join` with no arguments registers the
   session under a derived name, shows who else is here, and prints the
   protocol. "Use relay to talk to the other sessions" becomes a one-command
   instruction that works in any tab.
2. **Discussions.** `relay discuss <peer...> "<topic>"` opens a thread with a
   shared transcript. Every participant sees every post. The thread ends when
   all participants agree, or when a round cap is spent, and either way the
   operator gets one notification carrying the outcome - not the transcript.
3. **Synchronous ask.** `relay ask <peer> "<question>" --wait <s>` blocks the
   calling CLI and returns the peer's answer inside the same turn, so a
   question does not cost the asker a turn boundary.

The substrate does not change. The DB is still the bus, the watcher is still
the only thing that types into a tab, and delivery still waits for an idle
Claude prompt.

## Motivating scenario

The operator has three Claude sessions open on the same codebase and wants
them to settle a design question while walking away. Today that means reading
session A's opinion, pasting it into B, pasting B's rebuttal into C, and
adjudicating by hand. Relay's existing `relay send` is point to point: if A
messages B and C separately, B and C never see each other, and the operator is
still the router.

After this change:

1. The operator types one instruction into session A: *"use relay to discuss
   the DB-per-service question with the other two sessions and agree on an
   approach."*
2. A runs `relay join` (auto-named), `relay who` to find the peers, then
   `relay discuss bff-worker api-worker "one shared DB or one per service?"`.
3. B and C are woken by the watcher with a pointer. Each runs `relay thread 7`,
   reads the topic and transcript, and posts with `relay say 7 "..."`.
4. Each catches up on what the others posted before it posts again, because
   every delivery names the posts that session has not read.
5. When all three have posted `relay agree 7 "<position>"`, relay closes the
   thread and notifies the operator with the agreed position. If they cannot
   converge, one of them ends it with `relay close 7 "<how it ended>"`, or
   asks the operator with `relay send --human`. That call is theirs, not
   relay's.

The operator reads one notification and makes zero copy-paste round trips.

## 1. Architecture

No new processes and no daemon. This adds one table, two message columns, and
one evaluation step in the watcher's existing per-tick sweep.

- **The DB stays the bus.** CLI verbs run by Claude sessions write rows and
  exit.
- **The watcher stays the delivery leg** and gains one responsibility:
  deciding when a thread has closed.
- **Threads are stored, agreement is derived.** The `threads` table holds the
  topic, participants, cap and outcome. Who has agreed is computed from the
  messages, not tracked separately.

### Schema

```sql
CREATE TABLE IF NOT EXISTS threads(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT NOT NULL DEFAULT '',
  topic TEXT NOT NULL,
  opener TEXT NOT NULL,
  participants TEXT NOT NULL,           -- comma separated, includes the opener
  rounds_cap INTEGER NOT NULL DEFAULT 3,
  state TEXT NOT NULL DEFAULT 'open',   -- open | agreed | closed
  outcome TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  closed_at REAL NOT NULL DEFAULT 0
);
```

Migration 10 adds two columns to `messages`:

```sql
ALTER TABLE messages ADD COLUMN thread_id INTEGER;
ALTER TABLE messages ADD COLUMN reply_to  INTEGER;
```

`thread_id` is NULL for ordinary `relay send` traffic, so every existing
message path is unaffected. Per the established idiom, the new table goes in
`_SCHEMA` (a `CREATE TABLE IF NOT EXISTS`, a no-op on existing DBs) while the
two `ALTER`s go in `_MIGRATIONS`.

Three message kinds join `MESSAGE_KINDS`: `say` and `agree` (meaningful only
with a `thread_id`) and `ask` (meaningful only with a `reply_to` correlation).

## 2. Identity without setup

`_require_me` becomes `_ensure_me`. A verb that needs an identity registers the
calling session instead of failing with instructions.

**Derived name**, in order:

1. The basename of the working directory.
2. `session` as a last resort.

(An earlier draft derived from the iTerm2 tab title first. That is not
available: iTerm2 exports only `$ITERM_SESSION_ID`, and reading a title needs
the iTerm2 API, i.e. the TUI process. The CLI must work without it.)

The result is slugified to the same character class names already use, and
deduped with a `-2` / `-3` suffix against live sessions. `RESERVED_NAMES` are
never produced: a derived name that collides with one is treated as taken and
gets a suffix.

**Role** defaults to `worker`. **Project** comes from the existing
`_default_project`.

**Explicit naming still wins.** `relay join <name>` and `relay register --name`
behave exactly as they do today; called from a session that was previously
auto-registered, they rename it in place, preserving its messages and tasks.

**Read-only verbs never auto-register**: `msgs`, `who`, `help`, `version`,
`doctor`, `recap`. Reading the board is not joining it.

### Security posture

Auto-registration widens the set of tabs the watcher may type into, which is a
deliberate loosening of today's rule that registration is an explicit act. Two
properties bound it:

- A session only becomes addressable by **running a relay verb itself**, which
  is an act by that session, at the operator's prompting. A tab that has never
  heard of relay stays untouchable.
- The reserved-name guards at both `db.register` and the watcher's injection
  point are unchanged, so `relay` and `human` remain unaddressable.

## 3. Discovery

`relay who` prints live sessions: name, role, project, workdir, current status
line, and last-seen age. Read-only, no registration side effect.

`relay join` with no arguments is the intended entry point: register, print the
same table `who` prints, then print the protocol. One command takes a session
from knowing nothing to being able to talk.

## 4. Discussions

### Verbs

- `relay discuss <peer> [<peer>...] "<topic>" [--rounds N]` - open a thread.
  Every peer must resolve to a live session; an unknown name is an error that
  names `relay who`. The opener is a participant. Default cap 3.
- `relay say <id> "<body>"` - post to the thread. Delivered to every other
  participant.
- `relay agree <id> "<position>"` - record that you are settled, and on what.
  The position text is required and non-empty.
- `relay thread <id>` - print the topic, participants, the full transcript in
  order, who has agreed and on what, rounds remaining per participant, and the
  thread's state. This is the read path, and it is plain stdout.

### Agreement is derived, and retractable

A participant's position is its most recent `agree` message in the thread. A
`say` posted **after** an `agree` retracts that agreement: a session that is
still talking is not settled. This falls out of ordering the messages and needs
no extra state.

Unanimity means every participant in `participants` has a live (non-retracted)
`agree`.

### The round budget (advisory)

A participant's round count is its number of `say` posts in the thread.
`agree` does not consume one, so settling is never rationed.

`relay say` past the budget is **allowed**, and reports what the post costs.
Refusing it would silence an agent that still has something to say, and how
long a conversation needs to be is part of the conversation - which belongs to
the participants. Relay reports the cost; they decide whether to spend it.

### Closing - and who does it

**Relay closes a thread on exactly one condition:** every participant has a
live `agree`. The watcher checks this once per tick, in the same sweep as
`_check_escalations`, sets state `agreed` with the agreed positions as
`outcome`, and queues an FYI to `human`. That is relay reading what the agents
did, not judging it.

Relay never closes a discussion for any other reason - not for running long,
not for failing to converge - and never escalates one on the participants'
behalf. Every other ending is declared by an agent:

- `relay close <id> "<how it ended>"` sets state `closed` with the declaring
  agent's summary. Use for converged-offline, agreed-to-disagree, or
  stopped-being-useful.
- If a human is genuinely needed, the agents decide that and say so with
  `relay send --human`.

Closing is idempotent and guarded by `state='open'`, so a tick that races
another cannot double-notify. `THREAD_STATES` is `open | agreed | closed`.

## 5. Delivery: a pointer, not the payload

`delivery_text` flattens newlines and strips non-printable characters on
purpose: injected text is one paste plus one discrete Enter. A multi-participant
transcript flattened onto a single line is unreadable, and widening the
injection path to multi-line paste would enlarge the one surface where
attacker-influenceable text becomes keystrokes.

So a thread message injects a **pointer**:

```
[relay discuss #7 from api-worker] 2 new posts on "one shared DB or one per
service?" - read them first: relay thread 7
```

The pointer deliberately does not say "reply". A thread is posted to with
`relay say`, and `relay reply` is a different verb aimed at point-to-point
messages; naming the wrong one in the one line a woken session is guaranteed
to read would be the single most expensive wording mistake available. The
affordance for what to do next comes from `relay thread` itself, which knows
the session's state.

The recipient runs `relay thread 7` and receives the topic, transcript,
agreement state and rounds remaining as ordinary bash output: no flattening, no
paste limits, no control-character hazard. The cost is one extra tool call.

Plain point-to-point `relay send` keeps its inline body. A one-line message
should not become two round trips.

### Catch-up (amended: enforced, not only encouraged)

The pointer names how many posts that session has not been delivered, and
`relay thread` renders the whole transcript in order.

Wording alone could not guarantee this, so the gate is mechanical: `say`,
`agree` and `close` are **refused while the caller has undelivered rows in
that thread**. Undelivered is exactly the right predicate - it is relay's own
record that those posts have not reached that session - so this is enforcing a
fact, not adjudicating a conversation.

The refusal is designed to cost nothing: it **prints the unread posts and
marks them delivered**, so re-running the command succeeds immediately. A
session cannot post over posts it has not seen, but what it does having seen
them - including re-posting the same text verbatim - stays its call. Blocking
until the watcher delivered would have cost a full turn instead; the CLI
consuming its own messages is the pattern `relay ask` already uses.

`relay thread` consumes the caller's queued rows for the same thread for the
same reason: reading the transcript IS reading them, and leaving them queued
would wake the session later with a pointer to posts already in its context.

### Batching

`_deliver` drains **all** queued messages for a session into one injected turn
rather than one per tick, bounded by a character budget with an overflow line
(`+N more, run relay inbox`). Today three queued messages wake a session three
separate times and cost three full Claude turns for the same information.

The audit contract is unchanged: log before act, one audit record per delivered
message, mark delivered only after the send returns.

## 6. Synchronous ask

`relay ask <peer> "<question>" [--wait <s>]` queues a message with kind `ask`,
then polls the DB for a reply correlated by `reply_to`, printing the answer and
exiting 0.

**An ask is not a thread.** It creates no `threads` row, has no rounds and no
agreement: it is one message plus its correlated answer. Routing it through the
discussion object would hand it closing semantics it cannot satisfy: nobody
posts `agree` to a question, so the thread would sit open forever and clutter
the DISCUSSIONS pane with conversations that worked perfectly.

- **Correlation.** The question row's id is the correlation key. The peer's
  envelope carries it, and `relay reply <id> "<body>"` sets `reply_to`, so a
  message arriving mid-wait cannot be mistaken for the answer.
- **Forgiving fallback.** If no correlated reply appears but the peer sent any
  message to the asker after the question was queued, that is accepted as the
  answer. The feature must not hang because the peer replied sloppily.
- **Consumption.** The asking CLI marks the answer delivered itself, so the
  watcher never also injects it as a stray turn. The asker is blocked and
  therefore not idle, so the watcher would not deliver to it anyway; this makes
  that guarantee explicit rather than incidental.
- **Timeout.** Default 120s, capped at 540s, both under Claude Code's bash
  timeout ceiling. On timeout the CLI exits non-zero with "queued - they will
  reply async". The question is not withdrawn: it degrades exactly into normal
  asynchronous delivery, and the asker can end its turn and be woken by the
  answer.
- **Two blocked askers** both time out. There is no deadlock and no daemon.
- **Interrupted asker.** If the operator interrupts the turn, the CLI dies
  without marking anything. The peer still answers and the answer arrives later
  as an ordinary injected turn. Nothing is lost.

`relay reply [<id>] "<body>"` also stands alone as the ceremony-killer for
ordinary messages: with no id it targets the newest message the session
received, resolving the recipient from that message's sender. If the last
delivery was a **batch of more than one**, bare `reply` refuses and lists the
ids. A silently mis-threaded reply is worse than one more argument.

## 7. Anti-sycophancy, within limits

Relay may teach sessions **how to talk**; it may not touch **what they
decide**. Everything here sits on the first side of that line.

`relay thread` and the protocol instruct participants to **state a position
and name where they disagree**, and say plainly that agreement is not
required. Consensus as an instruction is how sycophancy is manufactured, and
"you must reach agreement" would also be relay dictating an outcome.

`agree` requiring non-empty position text is the structural half: "I agree"
with no content is not expressible, so three participants agreeing while
describing three different things is visible in the outcome rather than hidden
behind a unanimous close. That is a format rule, not a judgement.

Relay does **not** go further. It does not score, gate, or retry a decision it
thinks was reached too easily. If sessions converge badly, that is theirs to
own - building machinery to police it would make relay the arbiter of
decisions that are not its to make.

## 8. Surfacing

- A **DISCUSSIONS pane** in the swarm view, built on the pattern the PR pane
  established: an attention strip on top for threads needing the operator,
  over a stable list that never reorders underneath.
- `relay doctor` reports open threads with their age and how many participants
  have agreed.

## 9. Learnability: a session must never need a skill

A session learns to talk to its peers from relay itself. Skills are an
optimization, not a prerequisite: they may not have triggered, they may have
been compacted away, and a session woken by an injected message may have no
idea relay exists.

**The design principle: every output a session reads ends with the moves
available to it right now.** Not static help text - affordances computed from
that session's actual state. A participant at its round cap is not told to
post; a session with nobody to reply to is not told to reply.

Five surfaces carry it, and each must stand alone:

1. **`relay -h`** leads with talking, not with the verb inventory. A session
   told "use relay to talk to the other sessions" and nothing else runs this
   first, and the first thing it must see is `relay join`.
2. **`relay join`** (no arguments) is the one command that takes a session from
   knowing nothing to being able to talk: it registers, lists the peers, and
   prints the protocol including `discuss` / `say` / `agree`.
3. **The delivery envelope** carries the next move inline, because a woken
   session may have read nothing else. A plain message ends with how to reply;
   a thread pointer ends with `run relay thread <id> before you reply`.
4. **`relay thread <id>`** ends with what this session can do in this thread
   right now: post (with rounds remaining), agree, or - at cap - only agree or
   let it close. This is the surface a participant reads most often, so it
   carries the most weight.
5. **Errors teach the fix**, extending the idiom `cmd_send` already uses
   (`unknown recipient ... relay msgs shows known names`). An unknown peer
   names `relay who`; a `say` to a closed thread says so and prints the
   outcome; a bare `reply` after a batch lists the ids.

`relay help discuss` joins `swarm` and `pr` in `protocol.TOPICS`, so the full
protocol is readable without registering - reading the rules must not require
committing to them.

### Skills

`relay-worker` shrinks. The section teaching "reply to whoever sent you" and
the `relay`-is-not-a-person fallback largely collapses, because the delivery
envelope carries the reply affordance and `relay reply` resolves the recipient
itself. The skill gains a short discussions section pointing at `relay help
discuss` rather than restating it, so the protocol has one home.

## 10. Delivery order

This is more than one plan's worth of work, and the pieces are independently
useful. Three phases, each shippable on its own:

**Phase 1 - talk at all, with no setup.** Auto-identity, `relay join` with no
arguments, `relay who`, `relay reply`, the `reply_to` column, the reply
affordance in the delivery envelope, batched delivery, and the `relay -h`
rewrite. This alone removes most of the copy-paste and all of the registration
ceremony, and everything after it depends on the identity and envelope work.

**Phase 2 - discussions.** The `threads` table, `discuss` / `say` / `agree` /
`thread`, the pointer envelope, watcher-side closing, the DISCUSSIONS pane,
`relay help discuss`, and the skill edits. This is the phase that delivers the
motivating scenario.

**Phase 3 - synchronous ask.** `relay ask --wait`, entirely additive and the
easiest to defer if phases 1 and 2 turn out to be enough.

## 11. Non-goals

- Channels, presence, typing indicators, or a persistent chat UI. This is a
  control panel, not a chat product.
- Threading for ordinary `relay send` traffic beyond the single `reply_to`
  link. There is no thread tree.
- Relay adjudicating a discussion, summarizing it, or judging the quality of an
  agreement. Relay records positions and reports them.
- Cross-project discussions. Participants come from one project, matching how
  `--all` already scopes.
- Discussions surviving `relay wipe`. They are swarm state and are wiped with
  it.

## 12. Residual risks

**Cost is not bounded by relay.** With the budget advisory, N participants
times R rounds is a floor, not a ceiling: a pair that keeps posting keeps
spending turns while the operator is away. This is the deliberate price of
relay not overriding agents. Mitigations are visibility (the DISCUSSIONS pane,
`relay doctor`) and the operator's own controls (pause, wipe) - the human has
the brakes; relay does not apply them. Revisit only if a discussion actually
runs away in practice.

~~**A session can ignore the pointer**~~ - retired by amendment 2. The
fallback named here (refusing a write from a participant with undelivered
posts) is now the shipped behaviour, extended to `agree` and `close` and made
free to recover from. What remains is narrower: a session can still post
without having *understood* what it read, which is not something relay can or
should check.

**Auto-registration loosens the injection boundary.** Accepted deliberately,
bounded as described in section 2. The operator-visible consequence is that
`relay who` will list sessions nobody explicitly named.

**Derived names can be unhelpful.** A tab titled `zsh` in a directory called
`src` produces a poor name. Mitigated by rename being free and explicit naming
being unchanged.
