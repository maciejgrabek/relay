# Session Conversations - Design Spec

**Date:** 2026-08-03
**Status:** Designed

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
   thread and pings the operator with the agreed position. If the round cap is
   spent first, relay closes it `unresolved` and pings with each session's
   last stated position.

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
  state TEXT NOT NULL DEFAULT 'open',   -- open | agreed | unresolved
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

1. The iTerm2 tab title, run through `titles.strip` so relay's own status
   prefixes never leak into a name.
2. The basename of the working directory.
3. `session` as a last resort.

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

### The round cap

A participant's round count is its number of `say` posts in the thread.
`agree` does not consume a round, so settling is never rationed.

`relay say` from a participant at its cap is refused, non-zero, with the
remaining options spelled out: post `relay agree <id> "<position>"`, or stop
and let the thread close.

### Closing

The watcher evaluates open threads once per tick, in the same sweep as
`_check_escalations`:

- **Unanimous** - state `agreed`, `outcome` set to the agreed position, a
  message queued to `human` with kind `escalation` so the existing operator
  ping fires.
- **Every participant at cap without unanimity** - state `unresolved`,
  `outcome` set to each participant's last stated position, same ping.

`unresolved` is a normal outcome. Three sessions that cannot converge is
information the operator wants, not a failure to retry.

Closing is idempotent and guarded by `state='open'`, so a tick that races
another cannot double-ping.

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

### Catch-up

The pointer names how many posts that session has not been delivered, and
`relay thread` renders the whole transcript in order. A session therefore
always reads what its peers said before it posts again, which is what stops
three participants from talking past each other.

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
discussion object would hand it closing semantics it cannot satisfy - nobody
posts `agree` to a question, so the watcher would eventually close every ask as
`unresolved` and ping the operator about a conversation that worked perfectly.

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

## 7. Anti-sycophancy

The plumbing is the easy half. Two Claude sessions asked to converge will
either agree instantly with whatever was said first, or ping-pong politely
until the cap.

The opening envelope and `relay thread` output therefore instruct participants
to **state a position and name where they disagree**, never to "reach
consensus". Consensus as an instruction is how sycophancy is manufactured.

`agree` requiring non-empty position text is the structural half of the same
guard: "I agree" with no content is not expressible, so three participants who
agree while describing three different things is visible in the outcome rather
than hidden behind a unanimous close.

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

**Sycophantic unanimity.** The guards in section 7 are framing and structure,
not enforcement. If sessions still agree in one round every time, the feature
is decorative. This is the risk most worth watching after the first real use,
and the cheapest signal is whether `unresolved` ever occurs.

**Cost is invisible until it is not.** N participants times R rounds is N times
R full Claude turns on real sessions, spent while the operator is away. The
default cap of 3 is deliberately low, and the cap is per participant so the
worst case is bounded before a thread opens.

**A session can ignore the pointer** and post without running `relay thread`,
which reintroduces exactly the talking-past-each-other the design exists to
prevent. Mitigated only by envelope wording. If it proves common, the fallback
is refusing a `say` from a participant with undelivered posts.

**Auto-registration loosens the injection boundary.** Accepted deliberately,
bounded as described in section 2. The operator-visible consequence is that
`relay who` will list sessions nobody explicitly named.

**Derived names can be unhelpful.** A tab titled `zsh` in a directory called
`src` produces a poor name. Mitigated by rename being free and explicit naming
being unchanged.
