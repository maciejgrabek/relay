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
