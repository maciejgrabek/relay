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

    # --- schema versioning --------------------------------------------------
    ok &= check("connect stamps user_version = 1",
                conn.execute("PRAGMA user_version").fetchone()[0] == 1)

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

    # re-register WITHOUT a project must not wipe the existing binding (a
    # spawned worker re-registering per the skill omits --project; live bug
    # found 2026-07-15: scribe's project became '' and its messages vanished
    # from --project filters).
    db.register(conn, "bff-worker", "UUID-2", "worker", "", now=300.0)
    ok &= check("re-register with empty project preserves it",
                db.get_session(conn, "bff-worker")["project"] == "webshop")
    db.register(conn, "bff-worker", "UUID-2", "worker", "otherproj", now=310.0)
    ok &= check("re-register with explicit project updates it",
                db.get_session(conn, "bff-worker")["project"] == "otherproj")
    db.register(conn, "bff-worker", "UUID-2", "worker", "webshop", now=320.0)

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

    # --- messages -------------------------------------------------------------
    m1 = db.queue_message(conn, "coord", "bff-worker", "spec ready", "webshop", now=400.0)
    m2 = db.queue_message(conn, "coord", "bff-worker", "and hurry", "webshop", now=401.0)
    m3 = db.queue_message(conn, "bff-worker", "coord", "ack", "webshop", now=402.0)
    ok &= check("queue_message returns ids", m1 > 0 and m2 == m1 + 1)

    und = db.undelivered(conn, "bff-worker")
    ok &= check("undelivered for name, oldest first",
                [m["id"] for m in und] == [m1, m2])
    ok &= check("undelivered all -> 3", len(db.undelivered(conn)) == 3)

    db.mark_delivered(conn, m1, now=410.0)
    und = db.undelivered(conn, "bff-worker")
    ok &= check("mark_delivered removes from queue",
                [m["id"] for m in und] == [m2])

    hist = db.message_history(conn, with_name="coord")
    ok &= check("history with_name matches both directions", len(hist) == 3)
    hist = db.message_history(conn, with_name="bff-worker")
    ok &= check("history newest last", hist[-1]["id"] == m3)

    # --- tasks ------------------------------------------------------------------
    epic = db.add_task(conn, "BFF changes", project="webshop", owner="bff-worker",
                       spec_path="/w/specs/bff.md", created_by="coord", now=500.0)
    t_a = db.add_task(conn, "wire endpoint", project="webshop", parent_id=epic,
                      owner="bff-worker", created_by="bff-worker", now=501.0)
    t_b = db.add_task(conn, "fe form", project="webshop", owner="fe-ui",
                      blocked_by=(t_a,), created_by="coord", now=502.0)
    row = db.get_task(conn, t_b)
    ok &= check("add_task blocked_by stored", row["blocked_by"] == str(t_a))
    ok &= check("epic has no parent", db.get_task(conn, epic)["parent_id"] is None)
    ok &= check("subtask parent set", db.get_task(conn, t_a)["parent_id"] == epic)

    ok &= check("set_task_state", db.set_task_state(conn, t_a, "doing", now=510.0)
                and db.get_task(conn, t_a)["state"] == "doing")
    ok &= check("set_task_state bumps updated_at",
                db.get_task(conn, t_a)["updated_at"] == 510.0)
    ok &= check("set_task_state unknown id -> False",
                not db.set_task_state(conn, 9999, "done"))
    try:
        db.set_task_state(conn, t_a, "paused")
        ok &= check("bad state raises", False)
    except ValueError:
        ok &= check("bad state raises", True)

    ok &= check("list_tasks by project",
                len(db.list_tasks(conn, project="webshop")) == 3)
    ok &= check("list_tasks by owner",
                {t["id"] for t in db.list_tasks(conn, owner="bff-worker")} == {epic, t_a})

    # current_task_for: doing beats blocked beats todo
    ok &= check("current_task_for prefers doing",
                db.current_task_for(conn, "bff-worker")["id"] == t_a)
    db.set_task_state(conn, t_a, "done", now=520.0)
    ok &= check("current_task_for falls back (epic todo)",
                db.current_task_for(conn, "bff-worker")["id"] == epic)
    ok &= check("current_task_for none -> None",
                db.current_task_for(conn, "ghost") is None)

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
