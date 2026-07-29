# Swarm Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session told "work with the other sessions via relay" can onboard itself with one command, correctly, even when no relay skill is installed or triggered.

**Architecture:** The swarm protocol text moves out of the skills and into a new `iterm/protocol.py` module, so it is defined once and printed by three paths: `relay join` (register plus teach), `relay help swarm` (teach only), and `relay help pr`. The skills stay, get broadened triggers, and lose their assumption that every swarm has a coordinator above the workers.

**Tech Stack:** Python 3 stdlib only. No pytest: `__main__` runners wired into `test/run.sh`.

**Depends on:** `docs/plans/2026-07-29-pr-routing.md`. Task 1 references the PR verbs and `db.RESERVED_NAMES`, both of which that plan introduces. Land PR routing first.

## Global Constraints

- **No em-dash characters (U+2014) anywhere** - source, comments, docs, commit messages. Use a plain ASCII hyphen.
- **Commit messages carry no `Co-Authored-By` trailer.**
- The protocol text is defined exactly once, in `iterm/protocol.py`. No path may re-word it inline.
- Relay stays gh-less: nothing here shells out to `gh`.
- Registration must remain an explicit act. Nothing in this plan may auto-register a session relay merely watches.

---

### Task 1: Protocol text module and `relay help`

**Files:**
- Create: `iterm/protocol.py`
- Create: `iterm/test_protocol.py`
- Modify: `iterm/cli.py` (handler after `cmd_msgs`, parser entry after the `msgs` subparser)
- Modify: `iterm/test_cli.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `protocol.SWARM_PROTOCOL` (str), `protocol.PR_PROTOCOL` (str), `protocol.TOPICS` (dict mapping `"swarm"`/`"pr"` to those strings), and `cli.cmd_help`.

- [ ] **Step 1: Write the failing tests**

Create `iterm/test_protocol.py`:

```python
"""Tests for the swarm protocol text - the thing relay prints to teach a
session how to participate.

Run: python3 iterm/test_protocol.py    (no deps - has a __main__ runner)
 or: ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import protocol  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    p = protocol.SWARM_PROTOCOL

    # The four discipline rules are the whole reason this text exists. A
    # session that reads it and still goes silent has been failed by it.
    ok &= check("teaches that status is the heartbeat",
                "relay status" in p and "heartbeat" in p)
    ok &= check("teaches replying to the sender, not to an assumed coordinator",
                "sender" in p)
    ok &= check("teaches never ending a turn silent on a doing task",
                "silent" in p or "silence" in p)
    ok &= check("teaches escalating instead of guessing",
                "--human" in p or "escalation" in p)

    ok &= check("names the verbs a session actually needs",
                all(v in p for v in ("relay inbox", "relay send",
                                     "relay task update", "relay status")))
    ok &= check("does not assume a coordinator exists",
                "the coordinator" not in p.lower())

    ok &= check("PR topic covers claim and routing",
                "relay pr claim" in protocol.PR_PROTOCOL
                and "--pr" in protocol.PR_PROTOCOL)

    ok &= check("TOPICS exposes both topics",
                set(protocol.TOPICS) == {"swarm", "pr"})
    ok &= check("no em-dash anywhere in the protocol text",
                all("\u2014" not in t for t in protocol.TOPICS.values()))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
```

Append to `iterm/test_cli.py`:

```python
    rc, out, err = run_cli("help", "swarm")
    ok &= check("relay help swarm exits 0", rc == 0)
    ok &= check("relay help swarm prints the protocol",
                "relay inbox" in out and "heartbeat" in out)

    rc, out, err = run_cli("help", "pr")
    ok &= check("relay help pr prints the PR protocol",
                "relay pr claim" in out)

    rc, out, err = run_cli("help")
    ok &= check("bare relay help lists the topics", rc == 0
                and "swarm" in out and "pr" in out)

    rc, out, err = run_cli("help", "nonsense")
    ok &= check("an unknown topic is a usage error", rc == 2)

    _before = _session_count()
    run_cli("help", "swarm")
    ok &= check("relay help NEVER registers anything",
                _session_count() == _before)
```

Add the helper it needs, next to the file's other helpers:

```python
def _session_count():
    c = db.connect()
    n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    c.close()
    return n
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_protocol.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'protocol'`

- [ ] **Step 3: Write the protocol module**

Create `iterm/protocol.py`:

```python
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

  Full reference, including PR routing and spawning:  relay help pr

THE FOUR RULES THAT MAKE A SWARM WORK

1. KEEP YOUR STATUS FRESH. `relay status` is not decoration, it is your
   heartbeat: relay flags a session STALE when it owns a `doing` task and goes
   quiet, and that flag is what tells the operator you are stuck. Update it as
   you make progress, not only when you finish.

2. REPLY TO WHOEVER MESSAGED YOU. A message arrives tagged with its sender.
   Answer that sender. Do not assume there is a coordinator above you and do
   not route your reply through one - a swarm can be flat, and the session
   that asked is the session waiting.

3. NEVER END A TURN SILENT WITH A TASK STILL `doing`. A worker that stops
   without a word is indistinguishable from one that is working, and whoever
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
```

- [ ] **Step 4: Add the `help` verb**

In `iterm/cli.py`, add the import next to `import timers`:

```python
import protocol  # noqa: E402
```

Add the handler after `cmd_msgs`:

```python
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
```

Add the parser entry after the `msgs` subparser:

```python
    hp = sub.add_parser("help", help="print the swarm protocol (registers "
                                     "nothing)")
    hp.add_argument("topic", nargs="?", default=None,
                    choices=sorted(protocol.TOPICS))
    hp.set_defaults(fn=cmd_help)
```

`choices` gives the unknown-topic case argparse's exit 2 for free.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 iterm/test_protocol.py && python3 iterm/test_cli.py`
Expected: both PASS

- [ ] **Step 6: Commit**

```bash
git add iterm/protocol.py iterm/test_protocol.py iterm/cli.py iterm/test_cli.py
git commit -m "feat(cli): relay help swarm|pr - the protocol lives in the CLI, not only in skills"
```

---

### Task 2: `relay join`

**Files:**
- Modify: `iterm/cli.py` (handler after `cmd_register`, parser entry after the `register` subparser, module docstring verb list)
- Modify: `skills/relay-cli-reference.md`
- Test: `iterm/test_cli.py`

**Interfaces:**
- Consumes: `db.register`, `db.list_sessions`, `db.undelivered`, `db.mark_delivered`, `db.RESERVED_NAMES`, `db.ROLES`, `protocol.SWARM_PROTOCOL`, `cli.my_iterm_id`.
- Produces: `cli.cmd_join`, `cli._default_project(conn) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `iterm/test_cli.py`:

```python
    # --- relay join ---------------------------------------------------------
    rc, out, err = run_cli("join", "w1")
    ok &= check("join exits 0", rc == 0)
    ok &= check("join registers the session", _sess("w1") is not None)
    ok &= check("join defaults the role to worker",
                _sess("w1")["role"] == "worker")
    ok &= check("join prints the protocol", "relay inbox" in out
                and "heartbeat" in out)
    ok &= check("join prints the roster so it knows who to talk to",
                "SWARM" in out or "roster" in out.lower())

    # A second session, in a different tab, joining sees the first in the
    # roster. run_cli's iterm_id kwarg is how this file already switches tabs,
    # and it PERSISTS - every later call runs as that tab until changed, so
    # each step below states the tab it means.
    rc, out, err = run_cli("join", "w2", iterm_id="w0t2p0:BBBB-2222")
    ok &= check("join lists the sessions already present", "w1" in out)
    ok &= check("join defaults project to the single active project",
                _sess("w2")["project"] == _sess("w1")["project"])

    # w1 messages w2, then w2 re-joins and finds it waiting.
    run_cli("send", "w2", "welcome aboard", iterm_id="w0t1p0:AAAA-1111")
    rc, out, err = run_cli("join", "w2", iterm_id="w0t2p0:BBBB-2222")
    ok &= check("re-joining shows queued messages", "welcome aboard" in out)
    ok &= check("re-joining is safe and still exits 0", rc == 0)

    rc, out, err = run_cli("join", "human")
    ok &= check("join rejects the reserved name 'human'", rc == 1)
    rc, out, err = run_cli("join", "relay")
    ok &= check("join rejects the reserved name 'relay'", rc == 1)
    ok &= check("a rejected join registers nothing", _sess("human") is None)

    rc, out, err = run_cli("join", "w3", "--role", "coordinator",
                           iterm_id="w0t3p0:CCCC-3333")
    ok &= check("join takes an explicit role",
                _sess("w3")["role"] == "coordinator")

    # Leave the environment as the rest of the suite expects it.
    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"
```

Add the helper it needs, next to `_session_count` from Task 1:

```python
def _sess(name):
    c = db.connect()
    row = c.execute("SELECT * FROM sessions WHERE name = ?",
                    (name,)).fetchone()
    c.close()
    return row
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 iterm/test_cli.py`
Expected: FAIL - argparse exits 2 with "invalid choice: 'join'"

- [ ] **Step 3: Implement `join`**

In `iterm/cli.py`, after `cmd_register`:

```python
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
    name = args.name.strip()
    if not name:
        return _err("name cannot be empty")
    if name in db.RESERVED_NAMES:
        return _err(f"'{name}' is reserved - 'relay' is the sender of system "
                    f"wake-ups and 'human' is the operator's escalation "
                    f"mailbox; pick another name")
    conn = db.connect()
    project = args.project if args.project is not None else \
        _default_project(conn)
    db.register(conn, name, sid, args.role, project)
    db.set_session_context(conn, name, os.getcwd(),
                           db.get_session(conn, name)["spawn_prompt"])

    print(f"joined as '{name}' ({args.role}) on project '{project}'")
    print()
    others = [s for s in db.list_sessions(conn, project)
              if s["name"] != name and not s["closed_at"]]
    print("SWARM ROSTER")
    if others:
        for s in others:
            print(f"  {s['name']:<16} {s['role']:<12} "
                  f"{s['status_text'] or '-'}")
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
```

Add the parser entry after the `register` subparser:

```python
    j = sub.add_parser("join", help="register AND print the swarm protocol "
                                    "(start here)")
    j.add_argument("name")
    j.add_argument("--role", default="worker", choices=db.ROLES)
    j.add_argument("--project", default=None)
    j.set_defaults(fn=cmd_join)
```

Add `relay join <name>` to the module docstring's verb list, above `relay register`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 iterm/test_cli.py`
Expected: PASS

- [ ] **Step 5: Document it**

At the very top of `skills/relay-cli-reference.md`, before the `relay register` block:

```
    relay join <name> [--role worker|coordinator] [--project <p>]
        START HERE. Registers this session AND prints, in one go: who else is
        in the swarm, anything already queued for you, and the protocol you
        are expected to follow. Safe to re-run - it rebinds the name to this
        tab and re-reads your inbox. `relay register` is the same binding
        without the teaching.

    relay help swarm | relay help pr
        The protocol text alone, registering nothing.
```

- [ ] **Step 6: Commit**

```bash
git add iterm/cli.py iterm/test_cli.py skills/relay-cli-reference.md
git commit -m "feat(cli): relay join - one command to register, see the swarm, and learn the rules"
```

---

### Task 3: Skills and README

**Files:**
- Modify: `skills/relay-worker/SKILL.md`
- Modify: `skills/relay-coordinator/SKILL.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the verbs from Tasks 1 and 2 and from the PR routing plan.
- Produces: nothing code-facing.

- [ ] **Step 1: Broaden both skill descriptions**

In `skills/relay-worker/SKILL.md` frontmatter:

```yaml
description: Use when told you are a relay swarm worker, or asked to work with / coordinate with / report to other Claude sessions through relay, join a relay swarm, or pick up work from another session - registers the session and follows the relay inbox/task/status protocol
```

In `skills/relay-coordinator/SKILL.md` frontmatter:

```yaml
description: Use when told you are a relay swarm coordinator, or asked to split work across several Claude sessions through relay, delegate to other sessions, or drive a relay swarm - registers the session, writes specs, creates and assigns epics, spawns workers, and routes progress
```

- [ ] **Step 2: Fix the coordinator assumption in relay-worker**

In `skills/relay-worker/SKILL.md`, replace the "On start" step 1 with:

```markdown
1. Join (your name and project come from the prompt that invoked you):
   `relay join <your-name>` - this registers you, shows who else is here, and
   prints the protocol.
```

Then replace every instruction to report to `<coordinator>` with reporting to
the sender. Specifically, in "Working an assigned task" step 5 and in the
"Never go silent" section:

```markdown
   Reply to WHOEVER SENT YOU THE WORK - a message arrives tagged with its
   sender, and that sender is the one waiting. A swarm can be flat: do not
   assume a coordinator exists above you. If the work came from no one (you
   found it on the board yourself) and you need a decision, escalate with
   `relay send --human "<the question>"`.
```

- [ ] **Step 3: Add the PR claim to the worker's done-checklist**

In `skills/relay-worker/SKILL.md`, in "Working an assigned task" step 5, extend the commit rule:

```markdown
5. When the work is done: commit it first - on a worktree you are on branch
   relay/<your-name>; commit everything there (an uncommitted worktree blocks
   cleanup and can be lost). If you opened a pull request, claim it in the
   same breath:

       relay pr claim <owner/name>#<n> --task <id>

   A PR you do not claim cannot be routed back to you when a reviewer asks for
   changes, and a human ends up hunting for which session wrote it. Then
   `relay task update <epic-id> --state done` and reply to whoever sent you
   the work.

   If PR feedback later arrives: put the task back to `doing`, fix it, push,
   and reply to the sender.
```

- [ ] **Step 4: Add the README section**

Add to `README.md`, near the swarm material:

```markdown
### Telling sessions to work together

Point each session at relay by name and it self-onboards - no skill required,
because the CLI teaches the protocol itself:

    you are api-worker. run: relay join api-worker
    then work with the other sessions through relay.

`relay join` registers the session, shows it who else is in the swarm, hands it
anything already queued, and prints the rules it is expected to follow: keep
your status fresh (it is your heartbeat), reply to whoever messaged you, never
end a turn silent with a task still `doing`, and escalate rather than guess.

`relay help swarm` prints the same protocol without registering anything, for
reading first. Joining stays an explicit act: relay will not enrol a session it
merely watches, because an enrolled session is one any local process can send
text to.
```

- [ ] **Step 5: Verify the skills still install**

Run: `./install.sh --dry-run 2>/dev/null || grep -n "skills" install.sh`
Expected: the symlink block covers `skills/*/`, so `relay-worker` and `relay-coordinator` continue to link. No new skill directory is added by this plan, so nothing else changes.

- [ ] **Step 6: Run the whole suite**

Run: `./test/run.sh`
Expected: `ALL SUITES PASSED`

- [ ] **Step 7: Commit**

```bash
git add skills/relay-worker/SKILL.md skills/relay-coordinator/SKILL.md README.md
git commit -m "docs(skill): flat swarms, reply to the sender, claim your PRs"
```

---

## Verification

Before calling this plan done:

- [ ] `./test/run.sh` prints `ALL SUITES PASSED`
- [ ] `relay help swarm` in a real tab prints the protocol and registers nothing
- [ ] `relay join tmp1` in a real tab prints roster, inbox, and protocol; `relay doctor` then shows `tmp1`
- [ ] `grep -rnP '\x{2014}' iterm/ README.md skills/ docs/plans/` returns nothing
- [ ] `grep -rn 'the coordinator' skills/relay-worker/SKILL.md` returns nothing
