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

  relay status "<one line>"        what you are doing right now
  relay inbox                      your queued messages (marks them read)
  relay send <name> "<body>"       message another session
  relay send --human "<body>"      escalate to the operator (pings them)
  relay task list [--mine]         the board
  relay task add "<title>" [--owner <name>] [--parent <id>]
  relay task update <id> --state todo|doing|blocked|done

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

TOPICS = {"swarm": SWARM_PROTOCOL, "pr": PR_PROTOCOL}
