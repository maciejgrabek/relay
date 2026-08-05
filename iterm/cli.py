"""Relay swarm CLI - the verbs Claude sessions shell out to.

    relay join <name> [--role worker|coordinator] [--project P]
    relay register --name X --role worker|coordinator [--project P]
    relay status "text"
    relay send <name> "body"
    relay inbox
    relay msgs [--with N] [--project P]
    relay task add|update|list ...        (task verbs)
    relay pr set|claim|list ...           (pull request verbs)
    relay spawn --name X [--project P] [--dir D] "prompt"

Every verb resolves "me" from $ITERM_SESSION_ID (set by iTerm2 in every
session). Writes go straight to the SQLite bus (db.py); the relay TUI's
watcher performs deliveries. Exit codes: 0 ok, 1 user/state error (printed to
stderr so the calling Claude session sees why), 2 argparse usage error, 3 =
`send --pr` to an unclaimed PR, 4 = `send --pr` whose owner is gone.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db        # noqa: E402
import protocol  # noqa: E402
import swarm     # noqa: E402
import timers    # noqa: E402


def my_iterm_id():
    """Bare session UUID. $ITERM_SESSION_ID looks like 'w0t2p0:UUID'; the
    iTerm2 Python API (and therefore the sessions table) uses just the UUID."""
    sid = os.environ.get("ITERM_SESSION_ID", "")
    if not sid:
        return None
    return sid.split(":", 1)[-1] or None


def whoami(conn):
    sid = my_iterm_id()
    return db.get_by_iterm_id(conn, sid) if sid else None


def _err(msg: str) -> int:
    print(f"relay: {msg}", file=sys.stderr)
    return 1


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _ensure_me(conn):
    """This session's row, auto-registering it if it has none.

    Registration is still an explicit act, in the sense that a session only
    becomes addressable by RUNNING a relay verb itself - a tab that never
    touches relay stays untouchable by the watcher's delivery leg. What changes
    is that the act no longer has to be a separate command carrying a name the
    operator invented, which was the whole barrier to "just talk to the other
    session". Rename later with `relay join <name>`; nothing is lost.
    """
    me = whoami(conn)
    if me is not None:
        return me, 0
    sid = my_iterm_id()
    if not sid:
        return None, _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    name = swarm.derive_name(os.getcwd(), db.registered_names(conn))
    db.register(conn, name, sid, "worker", _default_project(conn))
    db.set_session_context(conn, name, os.getcwd(), "")
    print(f"relay: registered this session as '{name}' "
          f"(rename with: relay join <name>)")
    return db.get_session(conn, name), 0


# Historical name. Auto-registration made "require" the wrong verb, but the
# call sites read fine either way.
_require_me = _ensure_me


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    return f"{d // 3600}h ago"


# Custom message kinds are allowed but kept machine-friendly: one short
# lowercase token. Known kinds (db.MESSAGE_KINDS) get dedicated rendering.
_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")

# Self-scheduling timer key: one short lowercase slug, stable across
# re-registrations so a session upserts its timer instead of stacking copies.
_TIMER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")

# Inline payloads longer than this get a nudge toward a prompt file. Not an
# error: a long one-liner still works, it just ages badly across compaction.
_PAYLOAD_WARN_LEN = 200

# Per-session cap on CLI-created timers, checked only on a fresh INSERT (an
# upsert of an existing key is always allowed - a session at the limit must
# still be able to update its own timer). Bounds the aggregate damage of a
# session that invents a new --key every turn instead of upserting.
_MAX_TIMERS_PER_SESSION = 5

# Exit codes beyond the usual 0/1/2, so a sweep session can branch on WHY a
# route failed instead of parsing stderr.
EXIT_UNCLAIMED = 3      # relay has no owner recorded for that PR
EXIT_OWNER_GONE = 4     # it had one, and that session is no longer there

# The operator's mailbox. Never a registered session, so nothing is ever
# injected into it; the watcher pings and marks it read.
HUMAN = "human"


def _pr_retention_days() -> float:
    """Shared by `pr list` and the TUI's launch-time prune so the CLI window
    and the pane window cannot drift apart."""
    try:
        return float(os.environ.get("RELAY_PR_RETENTION_DAYS", "7"))
    except ValueError:
        return 7.0


# --- verb handlers (each returns an exit code) --------------------------------

def cmd_register(args) -> int:
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    name = args.name.strip()
    if not name:
        return _err("name cannot be empty")
    if name in db.RESERVED_NAMES:
        return _err(f"'{name}' is reserved - 'relay' is the sender of system "
                    f"wake-ups and 'human' is the operator's escalation "
                    f"mailbox; pick another name")
    conn = db.connect()
    db.register(conn, name, sid, args.role, args.project or "")
    if args.dir:
        db.set_session_context(conn, name, os.path.abspath(args.dir),
                               db.get_session(conn, name)["spawn_prompt"])
    print(f"registered '{name}' as {args.role}"
          + (f" on project '{args.project}'" if args.project else ""))
    return 0


def _default_project(conn) -> str:
    """One active project means joining it is unambiguous, so do not make the
    session guess a flag. Zero or several means fall back to the workdir
    basename, which at least groups sessions in the same repo."""
    projects = {s["project"] for s in db.list_sessions(conn)
                if s["project"] and not s["closed_at"]}
    if len(projects) == 1:
        return next(iter(projects))
    return os.path.basename(os.getcwd())


def cmd_join(args) -> int:
    """Register and teach in one command. This is the entry point an operator
    can paste into any session: 'you are api-worker, run relay join
    api-worker'. Everything the session needs to behave correctly is in the
    output - no skill required."""
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    conn = db.connect()
    me_now = whoami(conn)
    if args.name is None:
        # Bare `relay join` is the zero-setup entry point. On a session that
        # already has an identity it is a RE-ORIENTATION (show me the roster
        # and the rules again), never a rename - silently renaming a session
        # mid-swarm would break every peer that already knows it.
        name = (me_now["name"] if me_now is not None
                else swarm.derive_name(os.getcwd(),
                                       db.registered_names(conn)))
    else:
        name = args.name.strip()
        if not name:
            return _err("name cannot be empty")
        if name in db.RESERVED_NAMES:
            return _err(f"'{name}' is reserved - 'relay' is the sender of "
                        f"system wake-ups and 'human' is the operator's "
                        f"escalation mailbox; pick another name")
        if me_now is not None and me_now["name"] != name:
            db.rename_session(conn, me_now["name"], name)
    existing = db.get_session(conn, name)
    if args.project is not None:
        project = args.project
    elif existing is not None and existing["project"]:
        # Reclaiming an identity (a restored worker re-running `relay join`
        # with no --project) must keep the project it was already on - not
        # whatever _default_project resolves to right now, which can differ
        # once other sessions have joined other projects in the meantime.
        project = existing["project"]
    else:
        project = _default_project(conn)
    db.register(conn, name, sid, args.role, project)
    db.set_session_context(conn, name, os.getcwd(),
                           db.get_session(conn, name)["spawn_prompt"])

    print(f"joined as '{name}' ({args.role}) on project '{project}'")
    print()
    others = [s for s in db.list_sessions(conn, project)
              if s["name"] != name and not s["closed_at"]]
    print("SWARM ROSTER")
    if others:
        here = os.getcwd()
        for s in others:
            shared = ("  (your working copy - do not both edit it)"
                      if swarm.same_checkout(s["workdir"], here) else "")
            print(f"  {s['name']:<16} {s['role']:<12} "
                  f"{s['status_text'] or '-'}{shared}")
    else:
        print("  (nobody else yet - you are first)")
    print()

    msgs = db.undelivered(conn, name)
    print("YOUR INBOX")
    if msgs:
        for m in msgs:
            print(f"  from {m['from_name']}: {m['body']}")
            db.mark_delivered(conn, m["id"])
    else:
        print("  (empty)")
    print()
    print(protocol.SWARM_PROTOCOL, end="")
    return 0


def cmd_status(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    db.set_status(conn, me["name"], args.text)
    print(f"status set: {args.text}")
    return 0


def cmd_send(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    kind = args.kind or "info"
    if kind == "wake":
        return _err("kind 'wake' is reserved for relay's automatic wake-ups")
    if not _KIND_RE.match(kind):
        return _err(f"--kind must be one short lowercase token "
                    f"(a-z, 0-9, -, _), got {kind!r}")
    # Flag targets (--all/--pr/--human) take no separate recipient name - the
    # positional `to` slot holds the message BODY instead, since there is
    # nothing else for it to hold. `--pr` is checked by presence, not
    # truthiness: `--pr ""` is an explicitly-passed (if malformed) ref, and
    # treating it as "no --pr" would silently fall through to the plain
    # <name> path below instead of hitting the malformed-ref error.
    picked = [name for name, on in
              (("--all", args.all), ("--pr", args.pr is not None),
               ("--human", args.human)) if on]
    if len(picked) > 1:
        return _err("pick one target: a name, --all, --pr, or --human")

    if picked:
        # A flag target takes at most ONE positional (the body). Two
        # positionals means a recipient name was given alongside the flag -
        # e.g. `relay send api-worker --human "..."` or
        # `relay send --pr <ref> "body" "extra"` - which is a conflicting
        # instruction, not something to resolve by silently keeping one
        # positional and discarding the other.
        if args.to is not None and args.body is not None:
            return _err(f"{picked[0]} takes only the message body - got both "
                        f"'{args.to}' and a separate body; drop one")
        body = args.to if args.body is None else args.body
    else:
        body = args.body

    if args.human:
        if not body:
            return _err('usage: relay send --human "<body>"')
        db.queue_message(conn, me["name"], HUMAN, body, me["project"],
                         kind="escalation")
        print("escalated to the human (pings the operator when the relay "
              "TUI is running; never injected into any session)")
        return 0

    if args.pr is not None:
        ref, rc = _pr_ref_or_err(args.pr)
        if ref is None:
            return rc
        repo, number = ref
        if not body:
            return _err('usage: relay send --pr <owner/name>#<n> "<body>"')
        row = db.get_pr(conn, repo, number)
        owner_session = (db.get_session(conn, row["owner"])
                         if row is not None and row["owner"] else None)
        status, detail = swarm.resolve_pr_route(row, owner_session)
        if status == "unclaimed":
            print(f"relay: unclaimed: {repo}#{number} has no owner recorded "
                  f"- nobody ran `relay pr claim`. Escalate to the human.",
                  file=sys.stderr)
            return EXIT_UNCLAIMED
        if status == "gone":
            print(f"relay: owner gone: {detail}. Escalate to the human.",
                  file=sys.stderr)
            return EXIT_OWNER_GONE
        db.queue_message(conn, me["name"], detail, body, me["project"],
                         kind=kind)
        db.touch_pr_routed(conn, repo, number)
        task = f" (task #{row['task_id']})" if row["task_id"] else ""
        print(f"routed to {detail}{task}")
        return 0

    if args.all:
        if body is None:
            return _err("message body required")
        if not args.project:
            return _err("--all requires --project")
        targets = [s for s in db.list_sessions(conn, args.project)
                   if s["name"] != me["name"] and not s["closed_at"]]
        if not targets:
            return _err(f"no live sessions in project '{args.project}'")
        for s in targets:
            db.queue_message(conn, me["name"], s["name"], body,
                             args.project, kind=kind)
        print(f"queued for {len(targets)} session(s): "
              + ", ".join(s["name"] for s in targets))
        return 0
    if args.to is None or args.body is None:
        return _err('usage: relay send <name> "<body>"  or  '
                    'relay send --all --project <p> "<body>"')
    if db.get_session(conn, args.to) is None:
        return _err(f"unknown recipient '{args.to}' - relay msgs shows known "
                    f"names; sessions register themselves first")
    db.queue_message(conn, me["name"], args.to, args.body, me["project"],
                     kind=kind)
    print(f"queued for {args.to} (delivered when their session is idle "
          f"and the relay TUI is running)")
    return 0


def _thread_or_err(conn, tid, me, *, must_participate=True):
    """(thread_row, 0) or (None, exit_code). Every thread verb needs the same
    checks, and each failure teaches the way out rather than just saying no."""
    th = db.get_thread(conn, tid)
    if th is None:
        return None, _err(f"no discussion #{tid} - relay who lists sessions, "
                          f'relay discuss <name> "<topic>" opens one')
    if th["state"] != "open":
        return None, _err(f"discussion #{tid} is closed ({th['state']}): "
                          f"{th['outcome'] or '(no outcome recorded)'}")
    if must_participate and me["name"] not in db.participants_of(th):
        return None, _err(f"you are not in discussion #{tid} - its "
                          f"participants are "
                          f"{', '.join(db.participants_of(th))}")
    return th, 0


def _thread_msgs_for(conn, tid: int, name: str):
    """This session's undelivered rows for one thread - the posts it provably
    has not seen, since a delivered row is one the watcher already typed into
    its prompt (or it read via `relay inbox` / `relay thread`)."""
    return [m for m in db.undelivered(conn, name)
            if (m["thread_id"] if "thread_id" in m.keys() else None) == tid]


def _catch_up_or_err(conn, th, me, verb: str) -> int:
    """Refuse a write to a discussion whose newest posts this session has not
    read - and hand it those posts on the way out.

    The design's one enforceable half. Everything about HOW to argue is prose
    the session may or may not follow, and rightly so; but "you are about to
    reply to something you have not read" is not a judgement about the
    conversation, it is a fact relay holds - the rows are still undelivered.
    This is what stops three participants answering three different questions,
    which the pointer envelope could only ask them not to do.

    The refusal prints the unread posts and marks them delivered, so the retry
    costs one bash call rather than a whole turn waiting to be woken - and the
    session cannot post again without those posts having entered its context.
    What it does with them (including re-posting the same thing verbatim) stays
    its call."""
    unread = _thread_msgs_for(conn, th["id"], me["name"])
    if not unread:
        return 0
    print(f"{len(unread)} post(s) in discussion #{th['id']} you have not read:")
    for m in unread:
        mark = "AGREES:" if swarm.kind_of(m) == "agree" else "       "
        print(f"  {m['from_name']:<14} {mark} {m['body']}")
        db.mark_delivered(conn, m["id"])
    return _err(f"read before you {verb} - the posts above arrived after you "
                f"last looked. Full transcript: relay thread {th['id']}. "
                f"Re-run your command if it still stands.")


def _broadcast(conn, th, sender: str, body: str, kind: str) -> None:
    """Post to every participant except the sender - one message row each.

    Fan-out rather than a shared mailbox, so delivery, batching, the inbox and
    the audit trail all keep working exactly as they already do. The transcript
    de-dupes these back into one post (db.thread_messages).

    ONE timestamp for the whole fan-out, computed here rather than per row:
    letting each INSERT call time.time() separately makes the rows differ by
    microseconds, and the transcript's de-dupe key - which includes the
    timestamp, so that two genuinely separate posts of the same text stay two
    posts - then never matches. The symptom is every post rendering once per
    recipient."""
    stamp = time.time()
    for p in db.participants_of(th):
        if p == sender:
            continue
        db.queue_message(conn, sender, p, body, th["project"], kind=kind,
                         thread_id=th["id"], now=stamp)


def cmd_discuss(args) -> int:
    """Open a discussion: a thread with participants, a shared transcript and
    a round cap."""
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    words = list(args.peers or ())
    if len(words) < 2:
        return _err('usage: relay discuss <name> [<name>...] "<topic>"  '
                    "(relay who lists names)")
    topic = words.pop().strip()      # the LAST positional is the topic
    if not topic:
        return _err("the topic cannot be empty")
    if args.rounds < 1 or args.rounds > 10:
        return _err("--rounds must be between 1 and 10 - every round costs "
                    "each participant a full Claude turn")
    seen, names = set(), []
    for p in words:
        if p == me["name"] or p in seen:
            continue
        seen.add(p)
        if p in db.RESERVED_NAMES:
            return _err(f"'{p}' is relay itself, not a session you can talk "
                        f"to - relay who lists who is here")
        s = db.get_session(conn, p)
        if s is None or s["closed_at"]:
            return _err(f"unknown session '{p}' - relay who lists who is here")
        names.append(p)
    if not names:
        return _err("name at least one OTHER session to discuss this with - "
                    "relay who lists them")
    tid = db.create_thread(conn, topic, me["name"], names,
                           project=me["project"], rounds_cap=args.rounds)
    th = db.get_thread(conn, tid)
    # The opening post IS the topic, and it counts as the opener's first round
    # - stating the question is taking a turn.
    _broadcast(conn, th, me["name"], topic, "say")
    print(f"opened discussion #{tid} with {', '.join(names)}")
    print(f"topic: {topic}")
    print(f"each participant may post {args.rounds} time(s) "
          f"(you have used 1). Settle it with: "
          f'relay agree {tid} "<the position>"')
    print(f"read it any time: relay thread {tid}")
    return 0


def cmd_say(args) -> int:
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    th, rc = _thread_or_err(conn, args.id, me)
    if th is None:
        return rc
    body = (args.body or "").strip()
    if not body:
        return _err("say something - an empty post is not a position")
    rc = _catch_up_or_err(conn, th, me, "post")
    if rc:
        return rc
    msgs = db.thread_messages(conn, th["id"])
    used = swarm.round_counts(msgs).get(me["name"], 0)
    _broadcast(conn, th, me["name"], body, "say")
    # The budget is INFORMATION, not a gate. Refusing the post would be relay
    # overriding an agent that still has something to say - and how long a
    # conversation needs to be is part of the conversation, which is theirs.
    # Relay reports the cost; the agents decide whether to keep spending it.
    left = th["rounds_cap"] - used - 1
    if left > 0:
        print(f"posted to #{th['id']} ({left} of your suggested "
              f"{th['rounds_cap']} posts left)")
    else:
        over = used + 1 - th["rounds_cap"]
        print(f"posted to #{th['id']} - that is {over} past the suggested "
              f"budget of {th['rounds_cap']}. Every post costs each "
              f"participant a turn, so settle it if you can: "
              f'relay agree {th["id"]} "<the position>", or end it: '
              f'relay close {th["id"]} "<how it ended>"')
    return 0


def cmd_close(args) -> int:
    """End a discussion the participants have finished with.

    Relay closes a thread on its own ONLY when everyone has agreed, because
    that is reading what the agents did rather than judging it. Every other
    ending is theirs to declare - including "we disagree and we are done",
    which is a real outcome and not a failure."""
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    th, rc = _thread_or_err(conn, args.id, me)
    if th is None:
        return rc
    summary = (args.summary or "").strip()
    if not summary:
        return _err(f'say how it ended: relay close {th["id"]} '
                    f'"<what you concluded, or what you could not settle>"')
    # Ending a conversation you have not finished reading is the same mistake
    # as replying to one, with a worse blast radius: it takes the decision away
    # from every other participant.
    rc = _catch_up_or_err(conn, th, me, "close it")
    if rc:
        return rc
    db.close_thread(conn, th["id"], "closed", f"{me['name']}: {summary}")
    for p in db.participants_of(th):
        if p == me["name"]:
            continue
        db.queue_message(conn, me["name"], p,
                         f"closed discussion #{th['id']}: {summary}",
                         th["project"], kind="info")
    print(f"closed discussion #{th['id']}: {summary}")
    return 0


def cmd_agree(args) -> int:
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    th, rc = _thread_or_err(conn, args.id, me)
    if th is None:
        return rc
    position = (args.position or "").strip()
    if not position:
        # Requiring the text is structural anti-sycophancy: "I agree" with no
        # content is not expressible, so three sessions agreeing while
        # describing three different things shows up in the outcome instead of
        # hiding behind a unanimous close.
        return _err(f"state WHAT you are agreeing to: relay agree "
                    f'{th["id"]} "<the position>"')
    rc = _catch_up_or_err(conn, th, me, "agree")
    if rc:
        return rc
    _broadcast(conn, th, me["name"], position, "agree")
    print(f"recorded your position on #{th['id']}: {position}")
    print("(posting again with relay say retracts it)")
    return 0


def cmd_thread(args) -> int:
    """The read path, and the surface a participant reads most often - so it
    carries the most teaching weight. Ends with what THIS session can do right
    now, computed from its actual state, not generic help."""
    conn = db.connect()
    me = whoami(conn)
    th = db.get_thread(conn, args.id)
    if th is None:
        return _err(f"no discussion #{args.id}")
    parts = db.participants_of(th)
    msgs = db.thread_messages(conn, th["id"])
    print(f"DISCUSSION #{th['id']}  [{th['state']}]")
    print(f"topic: {th['topic']}")
    print(f"with:  {', '.join(parts)}")
    print()
    print("TRANSCRIPT")
    if not msgs:
        print("  (nothing posted yet)")
    for m in msgs:
        mark = "AGREES:" if swarm.kind_of(m) == "agree" else "       "
        print(f"  {m['from_name']:<14} {mark} {m['body']}")
    # Reading the transcript IS reading the posts in it: consume this session's
    # queued rows for the thread. Otherwise a session that dutifully followed
    # the pointer is still woken later with a pointer to posts already in its
    # context (a wasted turn), and is still refused a post by the catch-up gate
    # for messages it demonstrably read. Same self-consumption the `ask` path
    # already does when it collects its own answer.
    if me is not None and me["name"] in parts:
        for m in _thread_msgs_for(conn, th["id"], me["name"]):
            db.mark_delivered(conn, m["id"])
    print()
    pos = swarm.positions(msgs)
    counts = swarm.round_counts(msgs)
    print("POSITIONS")
    for p in parts:
        if p in pos:
            print(f"  {p:<14} settled: {pos[p]}")
        else:
            left = th["rounds_cap"] - counts.get(p, 0)
            print(f"  {p:<14} not settled ({left} post(s) left)")
    if th["state"] != "open":
        print()
        print(f"OUTCOME ({th['state']}): {th['outcome']}")
        return 0
    print()
    print("STATE YOUR POSITION AND SAY WHERE YOU DISAGREE. You are not")
    print("required to reach agreement, and agreeing to be agreeable produces")
    print("a decision nobody checked. The call is yours, not relay's.")
    if me is not None and me["name"] in parts:
        left = th["rounds_cap"] - counts.get(me["name"], 0)
        budget = (f"({left} of your suggested {th['rounds_cap']} posts left)"
                  if left > 0 else
                  f"(past the suggested budget of {th['rounds_cap']} - still "
                  f"allowed, but each post costs everyone a turn)")
        print()
        print("YOU CAN:")
        print(f'  relay say {th["id"]} "<your view>"        {budget}')
        print(f'  relay agree {th["id"]} "<the position>"   settle it')
        print(f'  relay close {th["id"]} "<how it ended>"   end it, agreed '
              f'or not')
    return 0


def cmd_reply(args) -> int:
    """Answer whoever wrote to you, without having to know their name.

    This is the ceremony `relay send <name>` used to demand: a session had to
    parse the sender out of its injected turn and retype it. Getting that wrong
    is SILENT - the message goes to a real session that never asked - which is
    exactly the class of mistake a CLI should make impossible rather than
    document."""
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    if args.target is None:
        return _err('usage: relay reply "<body>"  or  '
                    'relay reply <msg-id> "<body>"')
    if args.body is None:
        mid, body = None, args.target
    else:
        if not str(args.target).isdigit():
            return _err(f"expected a message id, got {args.target!r} - "
                        f'usage: relay reply <msg-id> "<body>"')
        mid, body = int(args.target), args.body
    if not str(body).strip():
        return _err("the reply body cannot be empty")
    if mid is None:
        batch = db.last_batch(conn, me["name"])
        if not batch:
            return _err("nothing to reply to - you have received no messages. "
                        'Use: relay send <name> "<body>" '
                        "(relay who lists names)")
        if len(batch) > 1:
            ids = ", ".join(f"#{m['id']} from {m['from_name']}"
                            for m in batch)
            return _err(f"your last delivery held {len(batch)} messages "
                        f"({ids}) - say which one you are answering: "
                        f'relay reply <msg-id> "<body>"')
        mid = batch[0]["id"]
    src = db.get_message(conn, mid)
    if src is None:
        return _err(f"no message #{mid} - relay msgs shows your history")
    if src["to_name"] != me["name"]:
        return _err(f"message #{mid} was not addressed to you "
                    f"(it went to '{src['to_name']}')")
    target = src["from_name"]
    if target in db.RESERVED_NAMES:
        return _err(f"#{mid} came from '{target}', which is relay itself and "
                    f"not a session that can receive a reply. If you need a "
                    f'decision, use: relay send --human "<the question>"')
    if db.get_session(conn, target) is None:
        return _err(f"'{target}' is no longer registered - "
                    f"relay who lists who is here")
    db.queue_message(conn, me["name"], target, body, me["project"],
                     kind=args.kind or "info", reply_to=mid)
    print(f"replied to {target} (re: #{mid})")
    return 0


_ASK_WAIT_MAX = 540      # under Claude Code's bash timeout ceiling (600s)
_ASK_POLL_S = 0.5


def cmd_ask(args) -> int:
    """Ask a peer and block until it answers, so a question does not cost the
    asker a turn boundary.

    NOT a discussion: no rounds, no agreement, no threads row. Routing it
    through the discussion object would hand it closing semantics it cannot
    satisfy - nobody posts `agree` to a question - so every successful ask
    would eventually close as `unresolved` and ping the operator about a
    conversation that worked perfectly."""
    conn = db.connect()
    me, rc = _ensure_me(conn)
    if me is None:
        return rc
    if args.to == me["name"]:
        return _err("you cannot ask yourself")
    if args.to in db.RESERVED_NAMES:
        return _err(f"'{args.to}' is relay itself, not a session that can "
                    f'answer. For a human decision: relay send --human "..."')
    peer = db.get_session(conn, args.to)
    if peer is None or peer["closed_at"]:
        return _err(f"unknown session '{args.to}' - relay who lists who "
                    f"is here")
    if not str(args.body).strip():
        return _err('usage: relay ask <name> "<question>"')
    wait = max(0, min(int(args.wait), _ASK_WAIT_MAX))
    started = time.time()
    aid = db.queue_message(conn, me["name"], args.to, args.body,
                           me["project"], kind="ask", now=started)
    deadline = started + wait
    while True:
        row = db.find_reply(conn, me["name"], aid, args.to, started)
        if row is not None:
            # Consume it here: the asker is blocked and therefore not idle, so
            # the watcher would not deliver it anyway - marking makes that
            # guarantee explicit instead of incidental, and stops the answer
            # arriving again later as a stray injected turn.
            db.mark_delivered(conn, row["id"])
            print(f"{args.to}: {row['body']}")
            return 0
        if time.time() >= deadline:
            break
        time.sleep(_ASK_POLL_S)
    return _err(f"no answer from {args.to} within {wait}s - your question is "
                f"queued and will be delivered when they are idle. End your "
                f"turn; relay wakes you with the reply.")


def cmd_inbox(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    msgs = db.undelivered(conn, me["name"])
    if not msgs:
        print("no new messages")
        return 0
    threaded = set()
    for m in msgs:
        k = swarm.kind_of(m)
        tag = f" [{k}]" if k != "info" else ""
        # A discussion post read here instead of via `relay thread` must still
        # say which discussion it belongs to - a bare body with no thread is
        # unanswerable, and `relay reply` is the wrong verb for it.
        tid = m["thread_id"] if "thread_id" in m.keys() else None
        where = f" (discussion #{tid})" if tid else ""
        if tid:
            threaded.add(tid)
        print(f"#{m['id']} from {m['from_name']}{tag}{where} "
              f"({_ago(m['created_at'])}): {m['body']}")
        db.mark_delivered(conn, m["id"])
    for tid in sorted(threaded):
        print(f"  -> discussion #{tid}: read it with `relay thread {tid}`, "
              f'post with `relay say {tid} "<your view>"`')
    return 0


def cmd_who(args) -> int:
    """Who can I talk to? Read-only on purpose: reading the roster is not
    joining it, and a session that only wants to look should not become
    addressable as a side effect."""
    conn = db.connect()
    me = whoami(conn)
    rows = [s for s in db.list_sessions(conn, args.project)
            if not s["closed_at"]]
    if not rows:
        print("nobody is registered yet - a session joins with: relay join")
        return 0
    print(f"{'NAME':<18} {'ROLE':<12} {'SEEN':<10} STATUS")
    for s in rows:
        mine = "  (you)" if me is not None and s["name"] == me["name"] else ""
        # Naming a shared working copy here is the reporting half of what
        # `relay spawn` refuses outright: a session that joined on its own
        # cannot be un-spawned, so the peers who would be clobbered by it are
        # told instead.
        if (not mine and me is not None
                and swarm.same_checkout(s["workdir"], me["workdir"])):
            mine = "  (your working copy - do not both edit it)"
        print(f"{s['name']:<18} {s['role']:<12} "
              f"{_ago(s['last_seen']):<10} {s['status_text'] or '-'}{mine}")
    print()
    print('talk to one:                    relay send <name> "<body>"')
    print('settle something with several:  '
          'relay discuss <name> <name> "<topic>"')
    return 0


def cmd_msgs(args) -> int:
    conn = db.connect()
    rows = db.message_history(conn, with_name=args.with_name,
                              project=args.project)
    if not rows:
        print("no messages")
        return 0
    for m in rows:
        tick = "" if m["delivered_at"] else "  [queued]"
        k = swarm.kind_of(m)
        tag = f" [{k}]" if k != "info" else ""
        tid = m["thread_id"] if "thread_id" in m.keys() else None
        where = f" (#{tid})" if tid else ""
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))} "
              f"{m['from_name']} -> {m['to_name']}{tag}{where}: "
              f"{m['body']}{tick}")
    return 0


def cmd_help(args) -> int:
    """Teach without touching state. Registering is an explicit act, and a
    session reading the rules must be able to do so before committing to
    them."""
    if not args.topic:
        print("relay help <topic>\n")
        for name in protocol.TOPICS:
            print(f"  {name}")
        return 0
    print(protocol.TOPICS[args.topic], end="")
    return 0


def _pr_ref_or_err(ref: str):
    """(repo, number) or (None, exit_code). One format, taught on failure."""
    parsed = swarm.parse_pr_ref(ref)
    if parsed is None:
        return None, _err(f"'{ref}' is not a PR reference - use "
                          f"owner/name#number, e.g. acme/api#482")
    return parsed, 0


def cmd_pr_set(args) -> int:
    """The sweep session's verb: push what GitHub currently says. Relay never
    looks - it stores what it was told, and the UI always shows how old that
    telling is."""
    ref, rc = _pr_ref_or_err(args.ref)
    if ref is None:
        return rc
    repo, number = ref
    conn = db.connect()
    me = whoami(conn)
    project = args.project if args.project is not None else (
        me["project"] if me else "")
    row = db.upsert_pr(conn, repo, number, project=project, state=args.state,
                       title=args.title, branch=args.branch)
    print(f"{repo}#{number} -> {row['state']}"
          + (f" ({row['title']})" if row["title"] else ""))
    return 0


def cmd_pr_claim(args) -> int:
    """The worker's verb, run right after `gh pr create`. This is the only
    thing that makes 'who owns this PR' answerable later."""
    ref, rc = _pr_ref_or_err(args.ref)
    if ref is None:
        return rc
    repo, number = ref
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    row = db.claim_pr(conn, repo, number, owner=me["name"],
                      owner_session_id=sid, task_id=args.task,
                      branch=args.branch,
                      project=args.project if args.project is not None
                      else me["project"])
    task = f" for task #{row['task_id']}" if row["task_id"] else ""
    print(f"{repo}#{number} claimed by {me['name']}{task}")
    return 0


def cmd_pr_list(args) -> int:
    conn = db.connect()
    me = whoami(conn)
    if args.mine and me is None:
        return _err("--mine needs a registered session - run relay register")
    days = args.days if args.days is not None else _pr_retention_days()
    since = time.time() - float(days) * 86400
    rows = db.list_prs(conn, project=args.project,
                       owner=me["name"] if args.mine else None, since=since)
    if not rows:
        print("no pull requests")
        return 0
    sessions = {s["name"]: s for s in db.list_sessions(conn)}
    for r in rows:
        status, detail = swarm.resolve_pr_route(r, sessions.get(r["owner"]))
        who = (r["owner"] if status == "ok"
               else "UNCLAIMED" if status == "unclaimed"
               else f"{r['owner']} (GONE)")
        task = f"  #{r['task_id']}" if r["task_id"] else ""
        age = swarm.fmt_age(time.time() - r["state_changed_at"])
        print(f"{r['repo']}#{r['number']:<6} {r['state']:<9} {age:>4} ago  "
              f"{who}{task}")
    return 0


def cmd_task_add(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    try:
        blockers = [int(x) for x in args.blocked_by.split(",") if x.strip()] \
            if args.blocked_by else []
    except ValueError:
        return _err(f"--blocked-by must be comma-separated task ids, got "
                    f"{args.blocked_by!r}")
    # Validate referenced ids exist: a typo'd blocker never completes, so the
    # dependent would wait forever with no signal - fail loudly at creation.
    if args.parent is not None and db.get_task(conn, args.parent) is None:
        return _err(f"--parent #{args.parent} does not exist")
    missing = [b for b in blockers if db.get_task(conn, b) is None]
    if missing:
        return _err("--blocked-by references nonexistent task(s): "
                    + ", ".join(f"#{b}" for b in missing))
    project = args.project or me["project"]
    tid = db.add_task(conn, args.title, project=project, parent_id=args.parent,
                      owner=args.owner, spec_path=args.spec,
                      blocked_by=blockers, created_by=me["name"])
    print(f"created task #{tid} [{'epic' if args.parent is None else 'subtask'}]"
          f" {args.title}")
    # Assignment wake-up - but not when assigning to yourself (a worker
    # breaking its own epic into subtasks must not spam its own inbox).
    if args.owner and args.owner != me["name"]:
        task = db.get_task(conn, tid)
        db.queue_message(conn, "relay", args.owner,
                         swarm.wakeup_assignment_body(task), project, kind="wake")
    return 0


def cmd_task_update(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    if not db.set_task_state(conn, args.id, args.state):
        return _err(f"no task #{args.id}")
    print(f"task #{args.id} -> {args.state}")
    if args.state == "done":
        # Unblock trigger: poke the owner of every task this completion fully
        # unblocked (all of its blockers are now done).
        for t in swarm.unblocked_by_completion(db.list_tasks(conn), args.id):
            if t["owner"]:
                db.queue_message(conn, "relay", t["owner"],
                                 swarm.wakeup_unblocked_body(t), t["project"],
                                 kind="wake")
    return 0


def cmd_task_list(args) -> int:
    conn = db.connect()
    owner = None
    if args.mine:
        me, rc = _require_me(conn)
        if me is None:
            return rc
        owner = me["name"]
    rows = db.list_tasks(conn, project=args.project, owner=owner)
    if not rows:
        print("no tasks")
        return 0
    # Epics first with their subtasks nested under them.
    by_parent = {}
    for t in rows:
        by_parent.setdefault(t["parent_id"], []).append(t)

    # For --mine filtering, only show blockers that are also owned by this owner
    blockers_to_show = None
    if owner is not None:
        blockers_to_show = {t["id"] for t in rows}

    def fmt(t):
        bits = [f"#{t['id']} [{t['state']}] {t['title']}"]
        if t["owner"]:
            bits.append(f"@{t['owner']}")
        bb = swarm.parse_blockers(t["blocked_by"])
        if bb:
            # When filtering by owner, only show blockers in the filtered set
            if blockers_to_show is not None:
                bb = [b for b in bb if b in blockers_to_show]
            if bb:
                bits.append("blocked-by " + ",".join(f"#{b}" for b in bb))
        if t["spec_path"]:
            bits.append(f"spec:{t['spec_path']}")
        if t["created_by"]:
            bits.append(f"by {t['created_by']}")
        return "  ".join(bits)

    listed = set()
    for t in by_parent.get(None, []):
        print(fmt(t))
        listed.add(t["id"])
        for c in by_parent.get(t["id"], []):
            print("    " + fmt(c))
            listed.add(c["id"])
    for t in rows:                      # orphans (parent outside the filter)
        if t["id"] not in listed:
            print(fmt(t))
    return 0


def cmd_timer_add(args) -> int:
    """Register (or update) a timer bound to THIS tab.

    Deliberately does NOT use _require_me: timers bind to an iTerm session id,
    not to a swarm name, so this must work in a plain unregistered Claude tab.
    Guards live here and nowhere else - the engine treats these rows as
    ordinary timers:
      - mode is always 'idle' ('now' would inject mid-turn into our own tab)
      - the fire cap is mandatory (unattended self-injection needs a ceiling)
      - --key upserts (stops the fire -> re-register -> duplicate cascade)
      - upsert never revives an exhausted timer: neither its fire_count nor
        its max_fires is written, and enabled/active are never touched - only
        a fresh registration goes live immediately; an exhausted or
        operator-disabled/pending-restore timer stays that way until an
        operator re-arms it from the `t` overlay
      - a fresh registration (not an upsert) is capped at
        _MAX_TIMERS_PER_SESSION self-registered timers per tab
    """
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")

    key = (args.key or "").strip()
    if not _TIMER_KEY_RE.match(key):
        return _err("--key must be a short slug: lowercase letter first, then "
                    "letters/digits/-/_ (max 24), e.g. --key pr-duty. It is "
                    "what makes re-registering update the timer instead of "
                    "adding another one.")

    payload = timers.sanitize_payload(args.say)
    if not payload:
        return _err("--say cannot be empty")

    try:
        times = int(args.times)
    except (TypeError, ValueError):
        times = 0
    if times < 1:
        return _err("--times must be at least 1 - a self-registered timer "
                    "always needs a fire cap. Try --times 10.")
    times = min(50, times)

    try:
        every = int(args.every)
    except (TypeError, ValueError):
        return _err(f"--every must be a whole number of minutes (1-90), got "
                    f"{args.every!r}. Try --every 20.")
    interval = timers.clamp_interval(every)

    conn = db.connect()
    existing = db.get_timer_by_key(conn, sid, key)
    now = time.time()
    notes = []
    exhausted = False
    if existing is not None:
        exhausted = timers.capped(existing)
        fields = dict(interval_min=interval, payload=payload, mode="idle")
        # An exhausted row gets its text and schedule updated and NOTHING that
        # bears on whether it fires again: not fire_count, not max_fires, not
        # the clock. Resetting fire_count would revive it outright; writing the
        # new max_fires would revive it just as effectively, since a larger
        # --times raises the cap above the count and capped() goes false again.
        # enabled/active are never touched on any upsert, so a timer an
        # operator turned off or left pending-restore stays that way.
        if not exhausted:
            fields.update(max_fires=times, fire_count=0,
                          last_fired_at=now, bound_at=now)
        db.update_timer(conn, existing["id"], **fields)
        tid = existing["id"]
        verb = "updated"
        # Independent ifs, not an elif chain: a row can be exhausted AND
        # operator-disabled at once, and the session needs to hear both facts.
        if exhausted:
            notes.append(
                f"already reached its fire cap "
                f"({existing['fire_count']}/{existing['max_fires']}) - it is "
                f"exhausted, not revived. --times {times} was NOT applied; "
                f"raising the cap would revive it. An operator must restart "
                f"it from the `t` overlay (select it, press r).")
        if not existing["enabled"]:
            notes.append(
                "currently OFF - left that way. Only an operator can turn "
                "it back on (space in the `t` overlay); re-registering "
                "never does this automatically.")
        if not existing["active"]:
            notes.append(
                "pending restore (relay restarted since it last ran) - left "
                "that way. Only an operator can restore it (r in the `t` "
                "overlay); re-registering never does this automatically.")
    else:
        # Count only CLI-created rows (non-empty key). Operator rows added in
        # the `t` overlay carry key='' and must not consume this budget: the
        # guard exists to bound a session that invents a new key every turn
        # (design §9), and counting the human's timers would both lock a busy
        # tab out of self-scheduling entirely and point the session at `relay
        # timer rm`, which would happily delete the operator's row.
        existing_count = len([t for t in db.list_timers(conn, sid) if t["key"]])
        if existing_count >= _MAX_TIMERS_PER_SESSION:
            return _err(
                f"this session already has {existing_count} self-registered "
                f"timer(s) - the per-session limit is "
                f"{_MAX_TIMERS_PER_SESSION}. See `relay timer list` and "
                f"remove one with `relay timer rm` first.")
        tid = db.add_timer(conn, iterm_session_id=sid, label=f"self:{key}",
                           interval_min=interval, payload=payload,
                           mode="idle", max_fires=times, key=key, now=now)
        verb = "registered"

    if exhausted:
        # The cap is deliberately NOT raised to `times`, so report the cap the
        # row actually still carries - anything else would promise more fires
        # than this row will ever produce.
        print(f"timer {tid} {verb}: '{key}' every {interval}m, cap left at "
              f"{existing['max_fires']} fire(s) - exhausted, not firing again")
    else:
        print(f"timer {tid} {verb}: '{key}' every {interval}m, {times} "
              f"fire(s), first in {interval}m")
    for n in notes:
        print(f"note: timer {tid} {n}")
    if len(payload) > _PAYLOAD_WARN_LEN:
        print("note: that payload is long. Prefer writing the instructions to "
              ".relay/prompts/<key>.md and using a short pointer payload - it "
              "survives context compaction and stays editable.")
    return 0


def _timer_line(t, now: float) -> str:
    """One rendered row for `relay timer list`."""
    left = timers.fires_left(t)
    due = (t["last_fired_at"] or 0) + t["interval_min"] * 60 - now
    when = "due now" if due <= 0 else f"in {int(due // 60)}m{int(due % 60):02d}s"
    state = "on" if (t["enabled"] and t["active"]) else "off"
    fires = "unlimited" if left is None else f"{left} left"
    key = t["key"] or "-"
    return (f"  {t['id']:>3}  {key:<24}  every {t['interval_min']:>2}m  "
            f"{state:<3}  {fires:<11}  next {when:<10}  {t['payload'][:60]}")


def cmd_timer_list(args) -> int:
    """List only THIS session's timers - a tab can never see another's."""
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    rows = db.list_timers(db.connect(), sid)
    if not rows:
        print("no timers on this session")
        return 0
    now = time.time()
    print(f"  {'id':>3}  {'key':<24}  {'interval':<9}  {'st':<3}  "
          f"{'fires':<11}  {'next':<15}  payload")
    for t in rows:
        print(_timer_line(t, now))
    return 0


def cmd_timer_rm(args) -> int:
    """Delete one of THIS session's timers, by key or by id.

    The id path re-checks ownership: db.delete_timer takes a raw id, so without
    the check a guessed integer would delete another tab's timer.
    """
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    conn = db.connect()
    if args.key:
        row = db.get_timer_by_key(conn, sid, args.key.strip())
        if row is None:
            return _err(f"no timer with key '{args.key}' on this session")
    elif args.id:
        row = next((t for t in db.list_timers(conn, sid)
                    if str(t["id"]) == str(args.id)), None)
        if row is None:
            return _err(f"no timer {args.id} on this session")
    else:
        return _err("give --key <slug> or --id <n> (see: relay timer list)")
    db.delete_timer(conn, row["id"])
    print(f"timer {row['id']} removed")
    return 0


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*a, timeout=8):
    """Run a git command in the relay repo; return (rc, stdout) or (None, '')
    if git/repo is unavailable. Never raises."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", _repo_root(), *a],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip()
    except Exception:
        return None, ""


def local_version() -> str:
    rc, out = _git("log", "-1", "--format=%h %cd", "--date=format:%Y-%m-%d")
    return out if rc == 0 and out else "unknown (not a git checkout)"


def cmd_version(args) -> int:
    print(f"relay {local_version()}")
    return 0


def _update_stamp_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_UPDATE_STAMP", "~/.relay/update-check"))


def cmd_update(args) -> int:
    """Fetch and fast-forward the relay checkout to the latest version. Safe:
    ff-only never rewrites local history, and a dirty tree or missing remote
    stops with a clear message instead of clobbering anything.

    --auto is the quiet start-up flavor bin/relay runs before the TUI boots:
    throttled to one check per day (stamp file), short fetch timeout, and
    SILENT on every skip (offline, dirty, diverged, no remote, up to date) -
    a version check must never delay or noise up a launch. It only speaks
    when it actually updated. RELAY_NO_AUTOUPDATE=1 disables it."""
    auto = getattr(args, "auto", False)
    if auto:
        if os.environ.get("RELAY_NO_AUTOUPDATE"):
            return 0
        stamp = _update_stamp_path()
        try:
            if time.time() - os.path.getmtime(stamp) < 86400:
                return 0
        except OSError:
            pass
        # Stamp the ATTEMPT, not the success - an offline day must not retry
        # the network hit on every single launch.
        try:
            d = os.path.dirname(stamp)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(stamp, "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
    rc, _ = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return 0 if auto else _err(
            "not a git checkout - update by re-pulling however you "
            "installed relay")
    rc, dirty = _git("status", "--porcelain")
    if dirty:
        return 0 if auto else _err(
            "working tree has local changes - commit or stash them "
            "first, then rerun 'relay update'")
    rc, remote = _git("remote")
    if rc != 0 or not remote:
        return 0 if auto else _err("no git remote configured - nothing to "
                                   "update from")
    if not auto:
        print(f"current: {local_version()}")
        print("fetching...")
    rc, _ = _git("fetch", "--quiet", timeout=10 if auto else 30)
    if rc != 0:
        return 0 if auto else _err("git fetch failed (offline?) - try again "
                                   "when connected")
    rc, counts = _git("rev-list", "--count", "--left-right", "HEAD...@{u}")
    behind = counts.split("\t")[-1] if counts and "\t" in counts else "0"
    if behind == "0":
        if not auto:
            print("already up to date.")
        return 0
    if not auto:
        print(f"{behind} new commit(s) available, fast-forwarding...")
    rc, out = _git("merge", "--ff-only", "@{u}", timeout=30)
    if rc != 0:
        return 0 if auto else _err(
            "fast-forward failed (branch diverged) - resolve manually "
            "with git in the relay repo")
    if auto:
        print(f"relay updated: {behind} new commit(s) -> {local_version()}")
    else:
        print(f"updated: {local_version()}")
        print("restart relay (q, then run it again) to load the new version.")
    return 0


def cmd_doctor(args) -> int:
    """Print swarm health from OUTSIDE the TUI - a lifeline for 'I launched it
    and I'm stuck'. Reads the DB only; never mutates. Flags the two things that
    silently trap a user: undelivered messages piling up (relay TUI not running,
    or the target never idle) and tasks stuck in 'doing' with no movement."""
    import config as relay_config
    conn = db.connect()
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    cfg = relay_config.load()[0]
    print(f"relay doctor")
    print(f"  version: {local_version()}")
    print(f"  DB: {db.default_path()} (schema v{v})")
    print(f"  config: title_style={cfg.title_style} spawn_arm={cfg.spawn_arm} "
          f"stale_minutes={cfg.stale_minutes:g}")

    # The widget is the one part of relay that needs compiling, and its target/
    # dir is gitignored - so a fresh clone has the source but no binary, and 'm'
    # in the panel can only report that from a feed line you may not be looking
    # at. Doctor is where you look when something is silently not working.
    _wroot = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "widget", "src-tauri", "target")
    _wbin = next((p for p in (os.path.join(_wroot, "release", "relay-widget"),
                              os.path.join(_wroot, "debug", "relay-widget"))
                  if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if _wbin:
        print(f"  widget: built ({os.path.basename(os.path.dirname(_wbin))}), "
              f"[widget] enabled={'yes' if cfg.widget_enabled else 'no'} - "
              f"press m in the panel")
    else:
        print("  widget: NOT BUILT - 'm' in the panel will do nothing")
        print("    -> cd widget/src-tauri && cargo build --release")
        if not shutil.which("cargo"):
            print("    -> needs Rust first: "
                  "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh")

    sessions = db.list_sessions(conn)
    if not sessions:
        print("  sessions: none registered")
        print("    -> nothing is in a swarm yet. Register a session, or spawn "
              "one:\n       relay spawn --name w1 --arm wild \"your task\"")
    else:
        print(f"  sessions: {len(sessions)} registered")
        for s in sessions:
            cur = db.current_task_for(conn, s["name"])
            task = f"  {cur['state']} #{cur['id']}" if cur else ""
            mode = s["mode"] or "off"
            arm = f"  arm_request={s['arm_request']}" if s["arm_request"] else ""
            print(f"    {s['name']:<14} {s['role']:<12} "
                  f"{(s['project'] or '-'):<12} mode={mode}{task}{arm}")
        # Sessions relay spawned cannot land in one checkout (spawn refuses it),
        # but sessions that joined on their own can, and the symptom - edits
        # quietly reverting - never points here on its own.
        shared = {}
        for s in sessions:
            if s["closed_at"] or s["role"] != "worker":
                continue
            d = swarm.real_workdir(s["workdir"])
            if d:
                shared.setdefault(d, []).append(s["name"])
        for d, names in sorted(shared.items()):
            if len(names) > 1:
                print(f"    !! {', '.join(names)} share one working copy ({d})"
                      f" - parallel edits overwrite each other; give all but "
                      f"one its own git worktree")

    queued = db.undelivered(conn)
    now = time.time()
    if queued:
        oldest_min = int((now - min(m["created_at"] for m in queued)) / 60)
        print(f"  messages: {len(queued)} queued (undelivered)")
        if oldest_min >= 2:
            print(f"    !! oldest has waited {oldest_min}m - is the relay TUI "
                  f"running? It delivers messages; if it's closed they just "
                  f"sit here.")
    else:
        print("  messages: none queued")

    tasks = db.list_tasks(conn)
    if tasks:
        from collections import Counter
        by = Counter(t["state"] for t in tasks)
        print("  tasks: " + ", ".join(f"{by[s]} {s}"
              for s in ("todo", "doing", "blocked", "done") if by[s]))
        stale_cut = cfg.stale_minutes * 60
        for t in tasks:
            if t["state"] == "doing" and now - t["updated_at"] > stale_cut:
                mins = int((now - t["updated_at"]) / 60)
                print(f"    !! possible stall: #{t['id']} '{t['title'][:40]}' "
                      f"doing, owner {t['owner'] or '?'}, no update in {mins}m")
    else:
        print("  tasks: none")

    closed = db.closed_sessions(conn)
    owners = {t["owner"] for t in tasks if t["state"] != "done" and t["owner"]}
    orphans = [s for s in closed if s["name"] in owners]
    if orphans:
        print(f"  orphans: {len(orphans)} closed session(s) still own work "
              f"- 'relay restore' to revive, 'relay clean' to reset")
        for s in orphans:
            print(f"    {s['name']} (workdir: {s['workdir'] or 'unknown'})")

    prs = [dict(r) for r in db.list_prs(conn)]
    if prs:
        rows = swarm.pr_rows(prs, [dict(s) for s in db.list_sessions(conn)],
                             time.time())
        by_state = {}
        for r in rows:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        print()
        print("PULL REQUESTS  " + " · ".join(
            f"{k} {v}" for k, v in sorted(by_state.items())))
        for r in rows:
            if r["flag"]:
                age = swarm.fmt_age(r["age_s"])
                print(f"  ‼ {r['ref']:<22} {r['state']:<9} {age:>4} ago  "
                      f"{r['owner_label']}")

    threads = db.list_threads(conn, state="open")
    print()
    print(f"DISCUSSIONS  {len(threads)} open")
    for th in threads:
        parts = db.participants_of(th)
        msgs = db.thread_messages(conn, th["id"])
        settled = len([p for p in parts if p in swarm.positions(msgs)])
        print(f"  #{th['id']} {th['topic'][:44]:<44} "
              f"{settled}/{len(parts)} settled  "
              f"{swarm.fmt_age(time.time() - th['created_at']):>5} old")

    _doctor_notify()
    _doctor_statusbar(cfg)
    return 0


def _doctor_notify() -> None:
    """Report whether terminal-notifier is installed. Without it macOS
    notifications fall back to osascript: they show 'Script Editor' and clicking
    one opens Script Editor instead of jumping to the iTerm session. With it,
    notifications show as iTerm and click focuses the exact tab."""
    import shutil
    ok = "\033[32m✓\033[0m"
    no = "\033[31m✗\033[0m"
    have = shutil.which("terminal-notifier")
    print("  notifications:")
    if have:
        print(f"    {ok} terminal-notifier ({have}) - shown as iTerm, "
              f"click jumps to the session")
    else:
        print(f"    {no} terminal-notifier not installed - notifications show "
              f"'Script Editor' and click won't focus the tab")
        print("        -> brew install terminal-notifier")


def cmd_recap(args) -> int:
    """Summarize what relay did (reads the audit log + task board; never
    mutates). Default window: today; --all for all time."""
    import audit
    import recap
    since = 0.0 if getattr(args, "all", False) else recap.start_of_today()
    s = recap.summarize(audit.read_tail(limit=100000), since)
    conn = db.connect()
    from collections import Counter
    by = Counter(t["state"] for t in db.list_tasks(conn))
    window = "all time" if since == 0.0 else "today"
    print(f"relay recap ({window})")
    print(f"  cleared {s['cleared']} · woke you {s['woke']}x · "
          f"delivered {s['delivered']}")
    print(f"  tasks: {by['done']} done · {by['doing']} doing · "
          f"{by['blocked']} blocked · {by['todo']} todo")
    return 0


def _doctor_statusbar(cfg) -> None:
    """Report the three independent steps the status-bar badge needs, so a
    fresh laptop can see exactly which one is missing. iTerm2's Python API
    cannot add the component to the bar for you (step 3 is a manual drag), and
    the badge silently no-ops if any step is skipped - hence this checklist."""
    import statusbar as sb
    enabled = getattr(cfg, "statusbar_enabled", False)
    print("  statusbar:")
    if not enabled:
        print("    disabled in config. To use the badge, set [statusbar] "
              "enabled = true in ~/.relay/config")
        return
    installed = sb.provider_installed()
    running = sb.provider_alive()
    ok = "\033[32m✓\033[0m"
    no = "\033[31m✗\033[0m"
    print(f"    {ok if enabled else no} enabled in config")
    print(f"    {ok if installed else no} AutoLaunch provider installed "
          f"({sb.autolaunch_link_path()})")
    if not installed:
        print("        -> run ./install.sh (answer yes to the iTerm2 provider "
              "step); without it the badge slot ERRORS while relay is off")
    # Self-heal: installed but not running is the common rot (symlink relinked
    # after iTerm2 last launched). Start it now - same path relay + install use
    # - then re-read liveness so the line below reflects the healed state.
    heal_msg = None
    if installed and not running:
        try:
            import statusbar_ensure
            action = statusbar_ensure.ensure()
            heal_msg = statusbar_ensure._MESSAGES.get(action, action)
            running = sb.provider_alive()
        except Exception as e:
            heal_msg = f"auto-start failed: {e}"
    print(f"    {ok if running else no} provider running")
    if heal_msg:
        print(f"        {ok if running else '->'} {heal_msg}")
    # Apple Silicon: the AutoLaunch provider runs under iTerm2's bundled
    # x86_64 Python runtime, so it needs Rosetta 2 - without it the provider
    # (and thus the badge) silently never starts. This is the usual cause of a
    # provider that is installed but will not run on an M-series laptop.
    import platform
    if platform.machine() == "arm64":
        have_rosetta = False
        try:
            import subprocess
            have_rosetta = subprocess.run(
                ["/usr/sbin/pkgutil", "--pkg-info",
                 "com.apple.pkg.RosettaUpdateAuto"],
                capture_output=True).returncode == 0
        except Exception:
            pass
        print(f"    {ok if have_rosetta else no} Rosetta 2 (Apple Silicon: "
              f"iTerm2's Python runtime is x86_64)")
        if not have_rosetta:
            print("        -> install: softwareupdate --install-rosetta "
                  "--agree-to-license")
    # Step 3 can't be detected via the API - always remind. 'Relay' only
    # appears in the Configure Status Bar picker while a provider is REGISTERED
    # (provider running, or - with no provider installed - relay running). An
    # empty picker means nothing is registered right now, not that it's broken.
    print("    ? 'Relay' added to your bar (can't be auto-detected): iTerm2 "
          "Settings > Profiles >")
    print("        <profile> > Session > Configure Status Bar > drag 'Relay' "
          "in (tick 'Status bar enabled')")
    if not running:
        print("        note: 'Relay' only shows in that picker while a "
              "provider is running - start it first (above)")


def _run_git(cwd: str, *a, timeout=8):
    """Run git in an ARBITRARY repo (unlike _git, which is pinned to relay's
    own checkout): returns (rc, stdout, stderr); (None, '', msg) on hang or
    missing git. Never raises - same hardening contract as _git."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", cwd, *a],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as e:
        return None, "", str(e)


def _worktree_add(repo: str, name: str):
    """Create branch relay/<name> and a sibling worktree <repo>-<name> from
    the repo's current HEAD. Returns (worktree_path, None) on success or
    (None, error). The worktree lives NEXT TO the repo, never under ~/.relay -
    relay is a tech the session uses, not a place that owns the work."""
    rc, _, _ = _run_git(repo, "rev-parse", "--git-dir")
    if rc != 0:
        return None, f"not a git repository: {repo}"
    path = os.path.join(os.path.dirname(repo),
                        f"{os.path.basename(repo)}-{name}")
    if os.path.exists(path):
        return None, (f"worktree path already exists: {path} - pick another "
                      f"--name, or remove it (git -C {repo} worktree remove)")
    rc, _, err = _run_git(repo, "worktree", "add", path,
                          "-b", f"relay/{name}", timeout=30)
    if rc != 0:
        return None, (err or "git worktree add failed (git hung or missing?)")
    return path, None


def _worktree_dirty(workdir: str) -> bool:
    """True when the worktree has uncommitted/untracked changes - or can't be
    read at all (unreadable, hung, or missing git counts as dirty: never
    delete blind)."""
    rc, out, _ = _run_git(workdir, "status", "--porcelain")
    if rc != 0:
        return True
    return bool(out)


def _worktree_remove(repo: str, workdir: str, name: str):
    """Remove a relay-created worktree + its relay/<name> branch. Branch
    deletion is best-effort (already merged-and-deleted is not an error)."""
    rc, _, err = _run_git(repo, "worktree", "remove", workdir, timeout=30)
    if rc != 0:
        return False, (err or "git worktree remove failed")
    _run_git(repo, "branch", "-D", f"relay/{name}")
    return True, ""


def cmd_spawn(args) -> int:
    import asyncio
    import config as relay_config
    import spawn as spawnmod
    if args.worktree:
        if not args.dir:
            return _err("--worktree requires --dir <repo>")
        # The name becomes a branch (relay/<name>) and a sibling directory
        # (<repo>-<name>) - keep it a simple token so it can't redirect either.
        # Checked here, with the other argument-shape errors, so a malformed
        # command is never reported as a policy refusal.
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", args.name):
            return _err("--worktree requires a simple --name (letters, "
                        "digits, - or _): it becomes a branch and dir name")
    workdir = os.path.abspath(args.dir or os.getcwd())
    if not os.path.isdir(workdir):
        return _err(f"workdir not found: {workdir}")

    # --arm beats config [swarm] spawn_arm beats "off".
    arm = args.arm if args.arm is not None else relay_config.load()[0].spawn_arm

    # Both refusals below are PHYSICAL facts relay already holds, checked
    # before any side effect (no worktree is created only to be abandoned).
    # Relay stays out of what a session decides; where a session runs and
    # whether anyone can clear its prompts are not decisions, they are
    # conditions under which unattended work either happens or silently
    # doesn't. Teaching these in a skill only works if the skill was loaded.
    if arm == "off" and args.arm is None:
        return _err(
            f"refusing to spawn '{args.name}' unarmed: it stops at its first "
            f"permission prompt with nobody at that tab to clear it, and looks "
            f"identical to a worker that is thinking. Arm it: --arm wild (or "
            f"safe / insane), or set [swarm] spawn_arm in ~/.relay/config. If "
            f"you will sit at that tab yourself, say so: --arm off")
    if not args.worktree and not args.share:
        held = swarm.checkout_occupants(db.list_sessions(db.connect()), workdir,
                                        exclude=(args.name,))
        if held:
            return _err(
                f"{', '.join(held)} already working in {workdir} - two sessions "
                f"editing one working copy overwrite each other's edits, and "
                f"neither notices. Give this one its own: add --worktree "
                f"(branch relay/{args.name}, dir "
                f"{os.path.basename(workdir)}-{args.name}). If it will only "
                f"read there, say so: --share")
    repo = None
    if args.worktree:
        repo = workdir
        workdir, wt_err = _worktree_add(repo, args.name)
        if wt_err:
            return _err(wt_err)
        print(f"worktree {workdir} (branch relay/{args.name})")
    try:
        sid = asyncio.run(spawnmod.spawn_worker(
            args.name, args.project or "", args.prompt, workdir, args.role,
            arm=arm))
    except Exception as e:
        if repo:
            # Undo the worktree we just created: with no session row, no
            # relay verb could ever find or clean it (untracked git state).
            ok_rm, _ = _worktree_remove(repo, workdir, args.name)
            if ok_rm:
                print(f"cleaned up worktree {workdir} after failed spawn")
        return _err(f"spawn failed: {e}")
    if repo:
        db.set_worktree_repo(db.connect(), args.name, repo)
    armed = f", arm={arm}" if arm != "off" else ""
    print(f"spawned '{args.name}' ({args.role}{armed}) in {workdir} "
          f"[session {sid[:8]}]")
    return 0


def _panel_running() -> bool:
    """True when a relay panel holds the singleton lock (kernel flock, so a
    dead panel never reads as running). Never raises."""
    import fcntl
    p = os.path.expanduser(os.environ.get("RELAY_LOCK", "~/.relay/relay.lock"))
    try:
        with open(p, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                return False           # we could take it -> nobody holds it
            except OSError:
                return True
    except OSError:
        return False


def cmd_demo(args) -> int:
    """A guided 60-second tour of the whole loop, from YOUR current session:
    registers you as the demo coordinator, spawns one armed worker in a temp
    dir, assigns it a haiku task, and tells you what to watch. Everything is
    ordinary relay machinery on a throwaway 'demo' project."""
    import asyncio
    import tempfile
    import spawn as spawnmod
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - run this inside iTerm2")
    conn = db.connect()
    if not _panel_running():
        print("!! the relay panel is not running - open another tab and run\n"
              "   'relay' first: the panel is what delivers messages and\n"
              "   auto-clears the worker's prompts. The demo will only queue\n"
              "   things until it is up.")
    db.register(conn, "demo-coord", sid, "coordinator", "demo")
    workdir = tempfile.mkdtemp(prefix="relay-demo-")
    try:
        asyncio.run(spawnmod.spawn_worker(
            "demo-w1", "demo", "await your task via relay inbox", workdir,
            "worker", arm="wild"))
    except Exception as e:
        return _err(f"demo spawn failed: {e}")
    tid = db.add_task(
        conn,
        'write a haiku about terminals, then: relay send demo-coord '
        '"<the haiku>" --kind done',
        project="demo", owner="demo-w1", created_by="demo-coord")
    task = db.get_task(conn, tid)
    db.queue_message(conn, "relay", "demo-w1",
                     swarm.wakeup_assignment_body(task), "demo", kind="wake")
    print(f"""
demo is live. You are 'demo-coord'; worker 'demo-w1' spawned in {workdir}.

WATCH, in order (~60s):
  1. panel: demo-w1 flips to ▲ WILD within a few seconds (spawn pre-arm)
  2. relay types task #{tid} into demo-w1's prompt; it writes the haiku
  3. the haiku arrives HERE as a [relay done from demo-w1] turn
  4. TAB in the panel: the task moves todo -> doing -> done

then clean up with:  relay wipe --project demo --all --yes""")
    return 0


def cmd_clean(args) -> int:
    import swarm
    conn = db.connect()
    sessions = [dict(r) for r in db.closed_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    cands = swarm.clean_candidates(sessions, tasks)
    print(swarm.clean_plan_text(cands))
    if not cands or args.dry_run:
        return 0
    total_msgs = sum(db.count_messages_for(conn, c["name"]) for c in cands)
    if not args.yes and not _confirm(
            f"clean {len(cands)} session(s) + DELETE their {total_msgs} "
            f"message(s)?"):
        print("aborted.")
        return 0
    for c in cands:
        db.reset_owner_tasks(conn, c["name"])
        db.delete_messages_for(conn, c["name"])
        db.delete_session(conn, c["name"])
    print(f"cleaned {len(cands)} session(s).")
    return 0


def cmd_wipe(args) -> int:
    import swarm
    conn = db.connect()
    if args.all and args.names:
        return _err("--all takes no session names (it wipes the whole project)")
    if args.all:
        if not args.project:
            return _err("--all requires --project (refusing to wipe every "
                        "project at once)")
        nt = len(db.list_tasks(conn, project=args.project))
        ns = len(db.list_sessions(conn, project=args.project))
        nm = len(db.message_history(conn, project=args.project, limit=10**9))
        # Relay-created worktrees the DB wipe would otherwise orphan on disk.
        # Computed BEFORE the wipe (it deletes the session rows), then removed
        # after - clean ones only; dirty ones are kept (uncommitted work).
        wt_sessions = [dict(r) for r in db.list_sessions(conn,
                                                         project=args.project)]
        removals = swarm.worktree_removals(wt_sessions, os.path.isdir,
                                           _worktree_dirty)
        print(swarm.wipe_plan_text([], project_all=(nt, ns, nm)))
        n_rm = sum(1 for r in removals if r["action"] == "remove")
        for r in removals:
            if r["action"] == "remove":
                print(f"    remove worktree {r['workdir']} "
                      f"(branch relay/{r['name']})")
            else:
                print(f"    KEEP worktree {r['workdir']} - uncommitted "
                      f"changes, left on disk")
        if args.dry_run:
            return 0
        if not (nt or ns or nm):
            return 0
        wt_note = f" + {n_rm} worktree(s)" if n_rm else ""
        if not args.yes and not _confirm(
                f"permanently DELETE all of project '{args.project}' "
                f"({nt} tasks + {ns} sessions + {nm} messages{wt_note})?"):
            print("aborted.")
            return 0
        nt2, ns2, nm2 = db.wipe_project(conn, args.project)
        print(f"wiped project '{args.project}': {nt2} tasks, {ns2} sessions, "
              f"{nm2} messages.")
        for r in removals:
            if r["action"] != "remove":
                continue
            ok_rm, rm_err = _worktree_remove(r["repo"], r["workdir"], r["name"])
            print(f"  removed worktree {r['workdir']}" if ok_rm
                  else f"  worktree removal failed: {rm_err}")
        return 0

    sessions = [dict(r) for r in db.closed_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    names = args.names or None
    cands = swarm.wipe_candidates(sessions, tasks, names=names)
    for c in cands:
        if (c.get("worktree_repo") and c.get("workdir")
                and os.path.isdir(c["workdir"])):
            c["worktree_action"] = ("keep-dirty"
                                    if _worktree_dirty(c["workdir"])
                                    else "remove")
    for c in cands:
        c["msg_count"] = db.count_messages_for(conn, c["name"])
    print(swarm.wipe_plan_text(cands))
    for w in swarm.wipe_blocker_warnings(cands, tasks):
        print("  " + w)
    if not cands or args.dry_run:
        return 0
    total_tasks = sum(len(c["task_ids"]) for c in cands)
    total_msgs = sum(c["msg_count"] for c in cands)
    if not args.yes and not _confirm(
            f"permanently DELETE {total_tasks} task(s) + {len(cands)} "
            f"session(s) + {total_msgs} message(s)?"):
        print("aborted.")
        return 0
    still_closed = {r["name"] for r in db.closed_sessions(conn, args.project)}
    acted = 0
    for c in cands:
        if c["name"] not in still_closed:
            print(f"  skipped {c['name']} - revived since the plan")
            continue
        db.delete_tasks_by_ids(conn, c["task_ids"])
        db.delete_messages_for(conn, c["name"])
        db.delete_session(conn, c["name"])
        if c.get("worktree_action") == "remove":
            ok_rm, rm_err = _worktree_remove(c["worktree_repo"], c["workdir"],
                                             c["name"])
            print(f"  removed worktree {c['workdir']}" if ok_rm
                  else f"  worktree removal failed: {rm_err}")
        acted += 1
    print(f"wiped {acted} session(s).")
    return 0


def cmd_restore(args) -> int:
    import config as relay_config
    import swarm
    conn = db.connect()
    sessions = [dict(r) for r in db.list_sessions(conn, args.project)]
    tasks = [dict(r) for r in db.list_tasks(conn, project=args.project)]
    names = args.names or None
    cands = swarm.restore_candidates(sessions, tasks, names=names)
    spawn_arm = relay_config.load()[0].spawn_arm
    missing = {c["name"] for c in cands
               if c["workdir"] and not os.path.isdir(c["workdir"])}
    print(swarm.restore_plan_text(cands, spawn_arm, missing_workdirs=missing))
    # only candidates we can actually revive (workdir set AND still exists)
    doable = [c for c in cands if c["workdir"] and os.path.isdir(c["workdir"])]
    if not doable or args.dry_run:
        return 0
    if not args.yes and not _confirm(f"restore {len(doable)} session(s)?"):
        print("aborted.")
        return 0
    import asyncio
    import spawn as spawnmod
    for c in doable:
        # A restore is a fresh spawn: its arm level must follow spawn_arm, not
        # the dead worker's stale persisted mode. Clear it first so an off
        # spawn_arm comes back off (matching the plan's warning); wild/insane
        # re-arm + re-persist via spawn_worker's arm_request.
        db.set_session_mode(conn, c["name"], "")
        prompt = swarm.resume_prompt(c["name"], c["project"], c["role"],
                                     c["spawn_prompt"])
        asyncio.run(spawnmod.spawn_worker(
            c["name"], c["project"], prompt, c["workdir"], c["role"],
            arm=spawn_arm))
        print(f"restored {c['name']} in {c['workdir']}")
    return 0


# --- parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description=__doc__)
    sub = p.add_subparsers(dest="verb", required=True)

    r = sub.add_parser("register", help="bind this session to a swarm name")
    r.add_argument("--name", required=True)
    r.add_argument("--role", required=True, choices=db.ROLES)
    r.add_argument("--project", default="")
    r.add_argument("--dir", default=None,
                   help="record this session's working directory (for restore)")
    r.set_defaults(fn=cmd_register)

    j = sub.add_parser("join", help="register AND print the swarm protocol "
                                    "(start here)")
    j.add_argument("name", nargs="?", default=None,
                   help="your swarm name (omit to derive one from the "
                        "working directory)")
    j.add_argument("--role", default="worker", choices=db.ROLES)
    j.add_argument("--project", default=None)
    j.set_defaults(fn=cmd_join)

    s = sub.add_parser("status", help="update my one-line status")
    s.add_argument("text")
    s.set_defaults(fn=cmd_status)

    sd = sub.add_parser("send", help="queue a message to a named session")
    sd.add_argument("to", nargs="?", default=None,
                    help="recipient name (omit with --all)")
    sd.add_argument("body", nargs="?", default=None)
    sd.add_argument("--kind", default="info",
                    help="info|done|blocked|escalation or a custom lowercase "
                         "token ('wake' is reserved)")
    sd.add_argument("--all", action="store_true",
                    help="broadcast to every live session in --project "
                         "(except me)")
    sd.add_argument("--project", default=None)
    sd.add_argument("--pr", default=None, metavar="OWNER/NAME#N",
                    help="route to whichever session claimed this PR")
    sd.add_argument("--human", action="store_true",
                    help="escalate to the operator (pings; never injected)")
    sd.set_defaults(fn=cmd_send)

    ak = sub.add_parser("ask", help="ask a session and wait for the answer")
    ak.add_argument("to")
    ak.add_argument("body")
    ak.add_argument("--wait", type=int, default=120,
                    help="seconds to block waiting (default 120, max 540)")
    ak.set_defaults(fn=cmd_ask)

    dc = sub.add_parser("discuss", help="open a discussion with other "
                                        "sessions and settle a question")
    dc.add_argument("peers", nargs="*",
                    help="peer names, then the topic as the LAST argument")
    dc.add_argument("--rounds", type=int, default=3,
                    help="posts allowed per participant (default 3)")
    dc.set_defaults(fn=cmd_discuss)

    sy = sub.add_parser("say", help="post to a discussion")
    sy.add_argument("id", type=int)
    sy.add_argument("body")
    sy.set_defaults(fn=cmd_say)

    ag = sub.add_parser("agree", help="record the position you are settled on")
    ag.add_argument("id", type=int)
    ag.add_argument("position")
    ag.set_defaults(fn=cmd_agree)

    cs = sub.add_parser("close", help="end a discussion you are in")
    cs.add_argument("id", type=int)
    cs.add_argument("summary")
    cs.set_defaults(fn=cmd_close)

    tr = sub.add_parser("thread", help="read a discussion")
    tr.add_argument("id", type=int)
    tr.set_defaults(fn=cmd_thread)

    rp_ = sub.add_parser("reply", help="answer the last message you received")
    rp_.add_argument("target", nargs="?", default=None,
                     help="message id, or the body when replying to the last")
    rp_.add_argument("body", nargs="?", default=None)
    rp_.add_argument("--kind", default="info")
    rp_.set_defaults(fn=cmd_reply)

    ib = sub.add_parser("inbox", help="print + mark delivered my queued messages")
    ib.set_defaults(fn=cmd_inbox)

    wh = sub.add_parser("who", help="who else is here (read-only)")
    wh.add_argument("--project", default=None)
    wh.set_defaults(fn=cmd_who)

    ms = sub.add_parser("msgs", help="message history")
    ms.add_argument("--with", dest="with_name", default=None)
    ms.add_argument("--project", default=None)
    ms.set_defaults(fn=cmd_msgs)

    hp = sub.add_parser("help", help="print the swarm protocol (registers "
                                     "nothing)")
    hp.add_argument("topic", nargs="?", default=None,
                    choices=sorted(protocol.TOPICS))
    hp.set_defaults(fn=cmd_help)

    t = sub.add_parser("task", help="task board verbs")
    tsub = t.add_subparsers(dest="task_verb", required=True)

    ta = tsub.add_parser("add", help="create a task (no --parent = epic)")
    ta.add_argument("title")
    ta.add_argument("--parent", type=int, default=None)
    ta.add_argument("--owner", default=None)
    ta.add_argument("--spec", default=None)
    ta.add_argument("--blocked-by", dest="blocked_by", default=None)
    ta.add_argument("--project", default=None)
    ta.set_defaults(fn=cmd_task_add)

    tu = tsub.add_parser("update", help="change a task's state")
    tu.add_argument("id", type=int)
    tu.add_argument("--state", required=True, choices=db.TASK_STATES)
    tu.set_defaults(fn=cmd_task_update)

    tl = tsub.add_parser("list", help="list tasks (epics with nested subtasks)")
    tl.add_argument("--project", default=None)
    tl.add_argument("--mine", action="store_true")
    tl.set_defaults(fn=cmd_task_list)

    pr = sub.add_parser("pr", help="pull requests: who owns which PR")
    prsub = pr.add_subparsers(dest="pr_verb", required=True)

    prs_ = prsub.add_parser("set", help="push a PR's current state "
                                        "(the sweep session's verb)")
    prs_.add_argument("ref", help="owner/name#number, e.g. acme/api#482")
    prs_.add_argument("--state", required=True, choices=db.PR_STATES)
    prs_.add_argument("--title", default=None)
    prs_.add_argument("--branch", default=None)
    prs_.add_argument("--project", default=None)
    prs_.set_defaults(fn=cmd_pr_set)

    prc = prsub.add_parser("claim", help="record that THIS session opened "
                                         "this PR")
    prc.add_argument("ref", help="owner/name#number, e.g. acme/api#482")
    prc.add_argument("--task", type=int, default=None)
    prc.add_argument("--branch", default=None)
    prc.add_argument("--project", default=None)
    prc.set_defaults(fn=cmd_pr_claim)

    prl = prsub.add_parser("list", help="PRs with state, age, owner")
    prl.add_argument("--project", default=None)
    prl.add_argument("--mine", action="store_true")
    prl.add_argument("--days", type=float, default=None,
                     help="window in days (default: RELAY_PR_RETENTION_DAYS)")
    prl.set_defaults(fn=cmd_pr_list)

    tm = sub.add_parser("timer", help="timers bound to THIS session")
    tmsub = tm.add_subparsers(dest="timer_cmd", required=True)

    tma = tmsub.add_parser("add", help="register/update a timer on this tab")
    # --key/--times/--say are deliberately NOT argparse-required: the handler
    # rejects them with a message that explains WHY they exist (the duplicate
    # cascade, the mandatory cap), which argparse's terse "the following
    # arguments are required" cannot. --every has no such lesson, and a missing
    # --every would silently clamp to 1 minute, so it stays required here.
    tma.add_argument("--key",
                     help="stable slug, e.g. pr-duty; re-running with the same "
                          "key updates that timer instead of adding another")
    tma.add_argument("--every", required=True,
                     help="interval in minutes (clamped to 1-90)")
    tma.add_argument("--times",
                     help="fire cap, 1-50 - mandatory; there is no unlimited")
    tma.add_argument("--say",
                     help="the single-line text to inject when it fires")
    tma.set_defaults(fn=cmd_timer_add)

    tml = tmsub.add_parser("list", help="list this session's timers")
    tml.set_defaults(fn=cmd_timer_list)

    tmr = tmsub.add_parser("rm", help="remove one of this session's timers")
    tmr.add_argument("--key", help="the timer's key")
    tmr.add_argument("--id", help="the timer's numeric id (see: timer list)")
    tmr.set_defaults(fn=cmd_timer_rm)

    sp = sub.add_parser("spawn", help="open an iTerm2 tab running claude, "
                                      "pre-registered under --name")
    sp.add_argument("prompt")
    sp.add_argument("--name", required=True)
    sp.add_argument("--project", default=None)
    sp.add_argument("--dir", default=None)
    sp.add_argument("--role", default="worker", choices=db.ROLES)
    sp.add_argument("--arm", default=None,
                    choices=("off",) + db.ARM_REQUEST_MODES,
                    help="arm level the watcher applies to the new worker "
                         "(default: config [swarm] spawn_arm)")
    sp.add_argument("--worktree", action="store_true",
                    help="create a git worktree of --dir (branch relay/<name>, "
                         "sibling dir <repo>-<name>) and spawn the worker there")
    sp.add_argument("--share", action="store_true",
                    help="allow spawning into a working copy another worker "
                         "already occupies (they will overwrite each other's "
                         "edits if both write)")
    sp.set_defaults(fn=cmd_spawn)

    dr = sub.add_parser("doctor", help="print swarm health from outside the TUI")
    dr.set_defaults(fn=cmd_doctor)

    rp = sub.add_parser("recap",
                        help="summarize what relay did today (reads audit log)")
    rp.add_argument("--all", action="store_true",
                    help="all time, not just today")
    rp.set_defaults(fn=cmd_recap)

    dm = sub.add_parser("demo", help="guided 60s tour: spawn a demo worker "
                                     "and watch the whole loop run")
    dm.set_defaults(fn=cmd_demo)

    vr = sub.add_parser("version", help="print the installed relay version")
    vr.set_defaults(fn=cmd_version)

    up = sub.add_parser("update", help="fetch + fast-forward to the latest relay")
    up.add_argument("--auto", action="store_true",
                    help="quiet start-up check: throttled daily, silent when "
                         "offline/dirty/current (used by bin/relay)")
    up.set_defaults(fn=cmd_update)

    cl = sub.add_parser("clean", help="reset abandoned tasks + remove dead "
                                      "sessions")
    cl.add_argument("--project", default=None)
    cl.add_argument("--yes", action="store_true")
    cl.add_argument("--dry-run", dest="dry_run", action="store_true")
    cl.set_defaults(fn=cmd_clean)

    rs = sub.add_parser("restore", help="respawn dead workers in their workdir "
                                        "to finish their tasks")
    rs.add_argument("names", nargs="*", help="specific sessions to restore "
                    "(default: all closed sessions owning work)")
    rs.add_argument("--project", default=None)
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--dry-run", dest="dry_run", action="store_true")
    rs.set_defaults(fn=cmd_restore)

    wp = sub.add_parser("wipe", help="DELETE dead sessions' tasks (or a whole "
                                     "project with --all)")
    wp.add_argument("names", nargs="*", help="specific closed sessions to wipe "
                    "(default: all closed sessions)")
    wp.add_argument("--project", default=None)
    wp.add_argument("--all", action="store_true",
                    help="with --project: delete the ENTIRE project "
                         "(all tasks/sessions/messages, even live)")
    wp.add_argument("--yes", action="store_true")
    wp.add_argument("--dry-run", dest="dry_run", action="store_true")
    wp.set_defaults(fn=cmd_wipe)

    return p


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:          # argparse exits itself; surface its code
        return int(e.code or 0)
    try:
        return args.fn(args)
    except Exception as e:
        return _err(str(e))


if __name__ == "__main__":
    sys.exit(main())
