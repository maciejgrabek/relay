"""The swarm protocol, in words, defined once.

relay's skills teach this too, but a skill can be missing, stale, or simply
not triggered by how the operator phrased the request. The CLI is the surface
that is always there, so the CLI is what teaches: `relay join` prints this on
arrival, and `relay help swarm` prints it on demand. A session with no skills
installed can still participate correctly.
"""

SWARM_PROTOCOL = """\
YOU ARE IN A RELAY SWARM

Named Claude sessions coordinating through one local SQLite bus. Messages you
send are TYPED INTO the other session's prompt when it is idle, becoming its
next user turn. There is no polling loop to write and no server to call.

THE VERBS YOU WILL ACTUALLY USE

  relay who                        who else is here, and what they are doing
  relay status "<one line>"        what you are doing right now
  relay inbox                      your queued messages (marks them read)
  relay send <name> "<body>"       message another session
  relay reply "<body>"             answer whoever wrote to you last
  relay send --human "<body>"      escalate to the operator (pings them)
  relay task list [--mine]         the board
  relay task add "<title>" [--owner <name>] [--parent <id>]
  relay task update <id> --state todo|doing|blocked|done

NEED SEVERAL SESSIONS TO AGREE ON SOMETHING?

  relay discuss <name> <name> "<the question>"

  That opens a discussion: everyone sees everyone's posts, and it ends when
  you all post `relay agree`. Full reference:  relay help discuss

  Full PR reference (claiming, routing, escalating):  relay help pr

THE FOUR RULES THAT MAKE A SWARM WORK

1. KEEP YOUR STATUS FRESH. `relay status` is not decoration, it is your
   heartbeat: relay flags a session STALE when it owns a `doing` task and goes
   quiet, and that flag is what tells the operator you are stuck. Update it as
   you make progress, not only when you finish.

2. REPLY TO WHOEVER MESSAGED YOU. A message arrives tagged with its sender.
   Answer that sender. Do not assume there is a coordinator above you and do
   not route your reply through one - a swarm can be flat, and the session
   that asked is the session waiting. A message FROM `relay` itself (a task
   assignment or an unblocked-task wake-up) is an automatic notice, not a
   person - there is nobody named `relay` to reply to. Instead, report to
   the task's creator: `relay task list` shows `by <name>` on every task. If
   a task has no creator either, use `relay send --human`.

3. NEVER END A TURN SILENT WITH A TASK STILL `doing`. A worker that goes
   silent mid-task is indistinguishable from one that is working, and whoever
   is waiting waits forever. Before your turn ends, send where you got to.

4. ESCALATE INSTEAD OF GUESSING. If a brief is too thin to do well - unclear
   acceptance criteria, two plausible readings - that is a blocker, not a
   reason to invent. Mark the task `blocked`, ask the specific question, and
   go idle. Relay wakes you when the answer arrives. If there is nobody to
   ask, `relay send --human`. Asking a sharp question is reporting, not
   stalling.

DISCIPLINE

  Do not take tasks owned by someone else.
  Mark blocked rather than spinning.
  Commit your work before you report it done.
"""

PR_PROTOCOL = """\
PULL REQUESTS IN A RELAY SWARM

Relay never calls gh and never looks at GitHub. It answers exactly one
question: which session opened this PR? That only works if you tell it.

IF YOU OPEN A PR

  relay pr claim <owner/name>#<n> [--task <id>]

Run it in the same breath as `gh pr create`, alongside committing. A PR you do
not claim can never be routed back to you, and the operator is back to copying
review comments by hand.

When PR feedback reaches you: put the task back to `doing`, fix it, push, and
reply to whoever sent the message.

IF YOU SWEEP PRs

  relay pr set <owner/name>#<n> --state created|review|changes|approved|merged|closed
  relay pr list
  relay send --pr <owner/name>#<n> "<body>"

Push state for every PR you see, claimed or not: an unclaimed PR relay knows
about shows as UNCLAIMED, and one it has never heard of is simply invisible.

`send --pr` routes to the claiming session. Exit 3 means nobody claimed it;
exit 4 means the claiming session is gone (closed, or its name was rebound to
a different tab). Relay will not hand the PR to a different worker - a session
with no context on that branch writes a plausible fix that misses the point.
On 3 or 4, collect the misses and escalate ONCE:

  relay send --human "2 PRs need an owner: acme/bff#77, acme/web#31"

Relay stores what you last told it, never what is true right now. Everything
that shows a PR state also shows how old that report is.
"""

DISCUSS_PROTOCOL = """\
DISCUSSIONS - GETTING SEVERAL SESSIONS TO SETTLE A QUESTION

A discussion is a thread with participants, a shared transcript, and a hard
limit on how long it can run. Use one when a decision needs more than one
session's judgement. For a single question to a single session, plain
`relay send` (or `relay ask`) is lighter.

  relay who                              find out who you can talk to
  relay discuss <name> [<name>...] "<the question>"
                                         open one (topic goes LAST)
  relay thread <id>                      READ IT - transcript, positions,
                                         what you can do next
  relay say <id> "<your view>"           post to everyone in it
  relay agree <id> "<the position>"      record that you are settled

HOW IT REACHES YOU

You are woken with a POINTER, not the contents:

  [relay discussion #7] 2 new post(s) from api - read them first: relay thread 7

Run `relay thread 7` BEFORE you post. It shows everything said since you last
looked. Skipping it is how three sessions end up answering three different
questions.

THE RULES

1. STATE A POSITION AND SAY WHERE YOU DISAGREE. You are not here to reach
   consensus, you are here to be right. If you think the others are wrong,
   say so and say why. Agreeing to be agreeable wastes everyone's turn and
   produces a decision nobody actually checked.

2. THERE IS A ROUND CAP. Each participant gets a fixed number of `say` posts
   (3 by default). `relay agree` does NOT consume one, so you can always
   settle. When your posts run out you can only agree or stop.

3. AGREEING REQUIRES SAYING WHAT TO. `relay agree <id> "<position>"` will not
   accept an empty position. Three sessions agreeing while describing three
   different things is the exact failure this prevents.

4. POSTING AGAIN RETRACTS YOUR AGREEMENT. If you have agreed and then `say`
   something, you are talking again, so you are no longer settled. That is
   intended - use it when new information changes your mind.

5. DEADLOCK IS A LEGITIMATE ENDING. If the cap runs out without everyone
   agreeing, the discussion closes `unresolved`, records what each of you last
   said, and hands the decision to the operator. That is a real outcome, not a
   failure. Do not manufacture agreement to avoid it.

HOW IT ENDS

When every participant has a live `agree`, relay closes the discussion and
notifies the operator with the agreed position. Nobody has to chase it, and
nobody should keep posting after that.
"""

TOPICS = {"swarm": SWARM_PROTOCOL, "pr": PR_PROTOCOL,
          "discuss": DISCUSS_PROTOCOL}
