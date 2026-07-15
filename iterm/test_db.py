"""Tests for the swarm SQLite layer. Temp DB file per run, no iTerm2 imports.

Run: python3 iterm/test_db.py    (no deps - has a __main__ runner)
 or: ./test/run.sh
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import db  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)          # let connect() create it fresh
    return path


def run():
    ok = True
    path = _tmpdb()
    conn = db.connect(path)

    # --- sessions -----------------------------------------------------------
    db.register(conn, "bff-worker", "UUID-1", "worker", "webshop", now=100.0)
    row = db.get_session(conn, "bff-worker")
    ok &= check("register + get_session", row is not None
                and row["role"] == "worker" and row["project"] == "webshop"
                and row["iterm_session_id"] == "UUID-1"
                and row["registered_at"] == 100.0)

    ok &= check("get_by_iterm_id", db.get_by_iterm_id(conn, "UUID-1")["name"] == "bff-worker")
    ok &= check("get_session miss -> None", db.get_session(conn, "nope") is None)

    # re-register same name rebinds (respawned worker reclaims identity)
    db.register(conn, "bff-worker", "UUID-2", "worker", "webshop", now=200.0)
    row = db.get_session(conn, "bff-worker")
    ok &= check("re-register rebinds iterm id", row["iterm_session_id"] == "UUID-2")
    ok &= check("re-register keeps registered_at", row["registered_at"] == 100.0)

    # bad role rejected
    try:
        db.register(conn, "x", "U", "boss")
        ok &= check("bad role raises", False)
    except ValueError:
        ok &= check("bad role raises", True)

    # status
    ok &= check("set_status on registered -> True",
                db.set_status(conn, "bff-worker", "working on #14", now=300.0))
    ok &= check("status persisted",
                db.get_session(conn, "bff-worker")["status_text"] == "working on #14")
    ok &= check("set_status keeps last_seen fresh",
                db.get_session(conn, "bff-worker")["last_seen"] == 300.0)
    ok &= check("set_status unknown -> False", not db.set_status(conn, "ghost", "x"))

    # list
    db.register(conn, "coord", "UUID-3", "coordinator", "webshop", now=110.0)
    db.register(conn, "other", "UUID-4", "worker", "blog", now=120.0)
    ok &= check("list all -> 3", len(db.list_sessions(conn)) == 3)
    ok &= check("list by project -> 2",
                len(db.list_sessions(conn, project="webshop")) == 2)

    # connect() default path honors RELAY_DB at CALL time
    path2 = _tmpdb()
    os.environ["RELAY_DB"] = path2
    try:
        c2 = db.connect()
        db.register(c2, "envtest", "U9", "worker")
        ok &= check("RELAY_DB env honored", os.path.exists(path2))
        c2.close()
    finally:
        os.environ.pop("RELAY_DB", None)

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
