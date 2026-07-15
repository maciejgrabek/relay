"""Relay swarm CLI - the verbs Claude sessions shell out to.

    relay register --name X --role worker|coordinator [--project P]
    relay status "text"
    relay send <name> "body"
    relay inbox
    relay msgs [--with N] [--project P]
    relay task add|update|list ...        (task verbs)
    relay spawn --name X [--project P] [--dir D] "prompt"

Every verb resolves "me" from $ITERM_SESSION_ID (set by iTerm2 in every
session). Writes go straight to the SQLite bus (db.py); the relay TUI's
watcher performs deliveries. Exit codes: 0 ok, 1 user/state error (printed to
stderr so the calling Claude session sees why), 2 argparse usage error.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db      # noqa: E402
import swarm   # noqa: E402


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


def _require_me(conn):
    me = whoami(conn)
    if me is None:
        return None, _err("this session is not registered - run: "
                          "relay register --name <name> --role worker|coordinator")
    return me, 0


def _ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    return f"{d // 3600}h ago"


# --- verb handlers (each returns an exit code) --------------------------------

def cmd_register(args) -> int:
    sid = my_iterm_id()
    if not sid:
        return _err("$ITERM_SESSION_ID not set - are you inside iTerm2?")
    conn = db.connect()
    db.register(conn, args.name, sid, args.role, args.project or "")
    print(f"registered '{args.name}' as {args.role}"
          + (f" on project '{args.project}'" if args.project else ""))
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
    if db.get_session(conn, args.to) is None:
        return _err(f"unknown recipient '{args.to}' - relay msgs shows known "
                    f"names; sessions register themselves first")
    db.queue_message(conn, me["name"], args.to, args.body, me["project"])
    print(f"queued for {args.to} (delivered when their session is idle "
          f"and the relay TUI is running)")
    return 0


def cmd_inbox(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    msgs = db.undelivered(conn, me["name"])
    if not msgs:
        print("no new messages")
        return 0
    for m in msgs:
        print(f"#{m['id']} from {m['from_name']} ({_ago(m['created_at'])}): "
              f"{m['body']}")
        db.mark_delivered(conn, m["id"])
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
        print(f"{time.strftime('%m-%d %H:%M', time.localtime(m['created_at']))} "
              f"{m['from_name']} -> {m['to_name']}: {m['body']}{tick}")
    return 0


def cmd_task_add(args) -> int:
    conn = db.connect()
    me, rc = _require_me(conn)
    if me is None:
        return rc
    blockers = [int(x) for x in args.blocked_by.split(",") if x.strip()] \
        if args.blocked_by else []
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
                         swarm.wakeup_assignment_body(task), project)
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
                                 swarm.wakeup_unblocked_body(t), t["project"])
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


def cmd_spawn(args) -> int:
    import asyncio
    import spawn as spawnmod
    workdir = os.path.abspath(args.dir or os.getcwd())
    if not os.path.isdir(workdir):
        return _err(f"workdir not found: {workdir}")
    sid = asyncio.run(spawnmod.spawn_worker(
        args.name, args.project or "", args.prompt, workdir, args.role))
    print(f"spawned '{args.name}' ({args.role}) in {workdir} "
          f"[session {sid[:8]}]")
    return 0


# --- parser --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="relay", description=__doc__)
    sub = p.add_subparsers(dest="verb", required=True)

    r = sub.add_parser("register", help="bind this session to a swarm name")
    r.add_argument("--name", required=True)
    r.add_argument("--role", required=True, choices=db.ROLES)
    r.add_argument("--project", default="")
    r.set_defaults(fn=cmd_register)

    s = sub.add_parser("status", help="update my one-line status")
    s.add_argument("text")
    s.set_defaults(fn=cmd_status)

    sd = sub.add_parser("send", help="queue a message to a named session")
    sd.add_argument("to")
    sd.add_argument("body")
    sd.set_defaults(fn=cmd_send)

    ib = sub.add_parser("inbox", help="print + mark delivered my queued messages")
    ib.set_defaults(fn=cmd_inbox)

    ms = sub.add_parser("msgs", help="message history")
    ms.add_argument("--with", dest="with_name", default=None)
    ms.add_argument("--project", default=None)
    ms.set_defaults(fn=cmd_msgs)

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

    sp = sub.add_parser("spawn", help="open an iTerm2 tab running claude, "
                                      "pre-registered under --name")
    sp.add_argument("prompt")
    sp.add_argument("--name", required=True)
    sp.add_argument("--project", default=None)
    sp.add_argument("--dir", default=None)
    sp.add_argument("--role", default="worker", choices=db.ROLES)
    sp.set_defaults(fn=cmd_spawn)

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
