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
    ok &= check("fresh connect stamps user_version = 4",
                conn.execute("PRAGMA user_version").fetchone()[0] == 4)

    # v1 -> v4 migration: old sessions table gains arm_request, mode, and the
    # context/closed_at columns, one step at a time, ending at the current
    # version.
    import sqlite3 as _sq
    mpath = _tmpdb()
    mconn = _sq.connect(mpath)
    mconn.execute("""CREATE TABLE sessions(
        name TEXT PRIMARY KEY, iterm_session_id TEXT NOT NULL,
        role TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
        status_text TEXT NOT NULL DEFAULT '',
        registered_at REAL NOT NULL, last_seen REAL NOT NULL)""")
    mconn.execute("PRAGMA user_version = 1")
    mconn.commit()
    mconn.close()
    mig = db.connect(mpath)
    db.register(mig, "migrated", "M-1", "worker", "p")
    row = mig.execute("SELECT arm_request, mode FROM sessions "
                      "WHERE name='migrated'").fetchone()
    ok &= check("v1 db migrates to v4 with arm_request + mode columns",
                mig.execute("PRAGMA user_version").fetchone()[0] == 4
                and row["arm_request"] == "" and row["mode"] == "")
    mrow = mig.execute("SELECT workdir, spawn_prompt, closed_at FROM sessions "
                       "WHERE name='migrated'").fetchone()
    ok &= check("v1 db migrates to v4 with context + closed_at columns",
                mig.execute("PRAGMA user_version").fetchone()[0] == 4
                and mrow["workdir"] == "" and mrow["spawn_prompt"] == ""
                and mrow["closed_at"] == 0)

    # --- persisted mode (restart survival): its own DB so the session-count
    # assertions later in run() aren't perturbed by an extra registration.
    ppath = _tmpdb()
    pconn = db.connect(ppath)
    db.register(pconn, "persistw", "PW-1", "worker", "proj", now=50.0)
    ok &= check("mode default empty on fresh register",
                db.get_session(pconn, "persistw")["mode"] == "")
    ok &= check("set_session_mode on registered -> True + stored",
                db.set_session_mode(pconn, "persistw", "insane")
                and db.get_session(pconn, "persistw")["mode"] == "insane")
    ok &= check("set_session_mode unknown name -> False",
                not db.set_session_mode(pconn, "ghostw", "wild"))
    pconn.close()
    mig.close()

    # --- session context + closed_at (restore/clean foundation) -------------
    cpath = _tmpdb()
    cconn = db.connect(cpath)
    db.register(cconn, "w", "SID-W", "worker", "proj", now=10.0)
    ok &= check("context defaults empty",
                db.get_session(cconn, "w")["workdir"] == ""
                and db.get_session(cconn, "w")["spawn_prompt"] == ""
                and db.get_session(cconn, "w")["closed_at"] == 0)
    ok &= check("set_session_context stores both",
                db.set_session_context(cconn, "w", "/work/api", "build the API")
                and db.get_session(cconn, "w")["workdir"] == "/work/api"
                and db.get_session(cconn, "w")["spawn_prompt"] == "build the API")
    ok &= check("set_session_context unknown -> False",
                not db.set_session_context(cconn, "ghost", "/x", "y"))
    ok &= check("mark_closed stamps ts",
                db.mark_closed(cconn, "w", 500.0)
                and db.get_session(cconn, "w")["closed_at"] == 500.0)
    ok &= check("closed_sessions lists it",
                [r["name"] for r in db.closed_sessions(cconn)] == ["w"])
    # re-register revives (clears closed_at); keeps workdir/spawn_prompt.
    db.register(cconn, "w", "SID-W2", "worker", "proj", now=600.0)
    ok &= check("re-register clears closed_at",
                db.get_session(cconn, "w")["closed_at"] == 0
                and db.closed_sessions(cconn) == [])
    ok &= check("re-register keeps workdir",
                db.get_session(cconn, "w")["workdir"] == "/work/api")
    db.mark_closed(cconn, "w", 700.0)
    db.clear_closed(cconn, "w")
    ok &= check("clear_closed resets to 0",
                db.get_session(cconn, "w")["closed_at"] == 0)
    cconn.close()

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

    # --- arm requests (spawn pre-arming) --------------------------------------
    ok &= check("set_arm_request on registered -> True",
                db.set_arm_request(conn, "bff-worker", "wild")
                and db.get_session(conn, "bff-worker")["arm_request"] == "wild")
    db.clear_arm_request(conn, "bff-worker")
    ok &= check("clear_arm_request",
                db.get_session(conn, "bff-worker")["arm_request"] == "")
    ok &= check("set_arm_request unknown name -> False",
                not db.set_arm_request(conn, "ghost", "wild"))
    try:
        db.set_arm_request(conn, "bff-worker", "ludicrous")
        ok &= check("bad arm mode raises", False)
    except ValueError:
        ok &= check("bad arm mode raises", True)

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

    # --- clean helpers ------------------------------------------------------
    kpath = _tmpdb()
    k = db.connect(kpath)
    db.register(k, "dead", "SID-D", "worker", "p", now=1.0)
    e = db.add_task(k, "epic", project="p", owner="dead", now=2.0)
    s = db.add_task(k, "sub", project="p", owner="dead", parent_id=e, now=3.0)
    db.set_task_state(k, s, "doing", now=4.0)
    done = db.add_task(k, "finished", project="p", owner="dead", now=5.0)
    db.set_task_state(k, done, "done", now=6.0)
    n = db.reset_owner_tasks(k, "dead")
    ok &= check("reset_owner_tasks resets non-done owned tasks", n == 2)
    ok &= check("reset -> todo + unowned",
                db.get_task(k, e)["state"] == "todo"
                and db.get_task(k, e)["owner"] is None
                and db.get_task(k, s)["state"] == "todo")
    ok &= check("reset leaves done tasks alone",
                db.get_task(k, done)["state"] == "done"
                and db.get_task(k, done)["owner"] == "dead")

    db.queue_message(k, "coord", "dead", "you there?", "p", now=999_000.0)
    db.queue_message(k, "coord", "dead", "delivered one", "p", now=999_100.0)
    # mark one delivered so only the queued one is dropped
    mid = db.undelivered(k, "dead")[1]["id"]
    db.mark_delivered(k, mid, now=999_110.0)
    dn = db.delete_undelivered_to(k, "dead")
    ok &= check("delete_undelivered_to drops only queued", dn == 1)

    db.delete_session(k, "dead")
    ok &= check("delete_session removes the row",
                db.get_session(k, "dead") is None)

    # prune_messages: delivered + old only
    db.register(k, "x", "SID-X", "worker", "p", now=10.0)
    old = db.queue_message(k, "x", "coord", "old", "p", now=100.0)
    db.mark_delivered(k, old, now=101.0)
    new = db.queue_message(k, "x", "coord", "new", "p", now=1_000_000.0)
    db.mark_delivered(k, new, now=1_000_001.0)
    qd = db.queue_message(k, "x", "coord", "still queued", "p", now=100.0)
    pn = db.prune_messages(k, older_than_days=7, now=1_000_100.0)
    ok &= check("prune_messages drops old delivered only", pn == 1)
    ok &= check("prune keeps queued + recent",
                any(m["id"] == qd for m in db.undelivered(k)))

    # delete_session must NOT wipe message history (only the sessions row)
    db.register(k, "hist", "SID-H", "worker", "p", now=1_000_200.0)
    hm = db.queue_message(k, "hist", "coord", "shipped it", "p", now=1_000_210.0)
    db.mark_delivered(k, hm, now=1_000_220.0)
    db.delete_session(k, "hist")
    ok &= check("delete_session keeps delivered message history",
                any(m["id"] == hm for m in db.message_history(k)))
    k.close()

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
