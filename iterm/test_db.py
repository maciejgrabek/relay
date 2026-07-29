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


def _raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


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
    ok &= check("fresh connect stamps user_version = 8",
                conn.execute("PRAGMA user_version").fetchone()[0] == 8)

    # v1 -> v6 migration: old sessions table gains arm_request, mode, and the
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
    ok &= check("v1 db migrates to current with arm_request + mode columns",
                mig.execute("PRAGMA user_version").fetchone()[0] == 8
                and row["arm_request"] == "" and row["mode"] == "")
    mrow = mig.execute("SELECT workdir, spawn_prompt, closed_at FROM sessions "
                       "WHERE name='migrated'").fetchone()
    ok &= check("v1 db migrates to current with context + closed_at columns",
                mig.execute("PRAGMA user_version").fetchone()[0] == 8
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

    # Reserved names rejected system-wide, not just at the cmd_register
    # call site - spawn.spawn_worker and any other caller goes through
    # db.register too, so the guard belongs here.
    for reserved in db.RESERVED_NAMES:
        try:
            db.register(conn, reserved, "U-RES", "worker", "p")
            ok &= check(f"db.register raises for reserved name {reserved!r}",
                        False)
        except ValueError as e:
            ok &= check(f"db.register raises for reserved name {reserved!r}",
                        "reserved" in str(e))
    ok &= check("db.register still accepts an ordinary name",
                db.register(conn, "not-reserved", "U-OK", "worker", "p")
                is None
                and db.get_session(conn, "not-reserved") is not None)
    db.delete_session(conn, "not-reserved")   # keep later session-count checks intact

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

    # --- wipe helpers -------------------------------------------------------
    wpath = _tmpdb()
    wdb = db.connect(wpath)
    db.register(wdb, "dead", "SID-WD", "worker", "proj", now=1.0)
    t1 = db.add_task(wdb, "a", project="proj", owner="dead", now=2.0)
    t2 = db.add_task(wdb, "b", project="proj", owner="dead", now=3.0)
    db.set_task_state(wdb, t2, "done", now=4.0)
    keep = db.add_task(wdb, "other", project="proj", owner="live", now=5.0)
    n = db.delete_tasks_for_owner(wdb, "dead")
    ok &= check("delete_tasks_for_owner deletes all owner's tasks (incl done)",
                n == 2 and db.get_task(wdb, t1) is None and db.get_task(wdb, t2) is None)
    ok &= check("delete_tasks_for_owner leaves other owners",
                db.get_task(wdb, keep) is not None)

    # delete_tasks_by_ids: only the given ids, empty -> 0
    i1 = db.add_task(wdb, "i1", project="proj", owner="dead", now=6.0)
    i2 = db.add_task(wdb, "i2", project="proj", owner="dead", now=7.0)
    i3 = db.add_task(wdb, "i3", project="proj", owner="dead", now=8.0)
    n = db.delete_tasks_by_ids(wdb, [i1, i3])
    ok &= check("delete_tasks_by_ids deletes only the given ids",
                n == 2 and db.get_task(wdb, i1) is None
                and db.get_task(wdb, i3) is None
                and db.get_task(wdb, i2) is not None)
    ok &= check("delete_tasks_by_ids empty -> 0",
                db.delete_tasks_by_ids(wdb, []) == 0)

    # wipe_project: everything for a project, other projects intact
    db.register(wdb, "s1", "S1", "worker", "P1", now=10.0)
    db.register(wdb, "s2", "S2", "worker", "P2", now=11.0)
    db.add_task(wdb, "p1t", project="P1", owner="s1", now=12.0)
    db.add_task(wdb, "p2t", project="P2", owner="s2", now=13.0)
    db.queue_message(wdb, "s1", "s2", "hi", "P1", now=14.0)
    db.queue_message(wdb, "s2", "s1", "yo", "P2", now=15.0)
    nt, ns, nm = db.wipe_project(wdb, "P1")
    ok &= check("wipe_project returns counts", nt == 1 and ns == 1 and nm == 1)
    ok &= check("wipe_project clears P1",
                db.list_tasks(wdb, project="P1") == []
                and db.get_session(wdb, "s1") is None)
    ok &= check("wipe_project leaves P2 intact",
                len(db.list_tasks(wdb, project="P2")) == 1
                and db.get_session(wdb, "s2") is not None
                and len(db.message_history(wdb, project="P2")) == 1)
    wdb.close()

    # --- v5: message kind + worktree_repo ------------------------------------
    p5 = os.path.join(tempfile.mkdtemp(), "v5.db")
    conn5 = db.connect(p5)
    ok &= check("fresh DB is schema v8",
                conn5.execute("PRAGMA user_version").fetchone()[0] == 8)
    mid = db.queue_message(conn5, "a", "b", "hello")
    row = conn5.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    ok &= check("queue_message defaults kind=info", row["kind"] == "info")
    mid2 = db.queue_message(conn5, "a", "b", "done!", kind="done")
    row2 = conn5.execute("SELECT * FROM messages WHERE id=?", (mid2,)).fetchone()
    ok &= check("queue_message stores explicit kind", row2["kind"] == "done")

    db.register(conn5, "w1", "SID-W1", "worker", "proj")
    ok &= check("worktree_repo defaults empty",
                db.get_session(conn5, "w1")["worktree_repo"] == "")
    ok &= check("set_worktree_repo writes",
                db.set_worktree_repo(conn5, "w1", "/tmp/repo")
                and db.get_session(conn5, "w1")["worktree_repo"] == "/tmp/repo")
    ok &= check("set_worktree_repo unknown name -> False",
                not db.set_worktree_repo(conn5, "ghost", "/tmp/x"))

    # upgrade path: hand-build a v4 DB (no kind / worktree_repo), then connect
    p4 = os.path.join(tempfile.mkdtemp(), "v4.db")
    import sqlite3 as _sq
    old = _sq.connect(p4)
    old.executescript("""
      CREATE TABLE sessions(name TEXT PRIMARY KEY, iterm_session_id TEXT NOT NULL,
        role TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
        status_text TEXT NOT NULL DEFAULT '', registered_at REAL NOT NULL,
        last_seen REAL NOT NULL, arm_request TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT '', workdir TEXT NOT NULL DEFAULT '',
        spawn_prompt TEXT NOT NULL DEFAULT '', closed_at REAL NOT NULL DEFAULT 0);
      CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL DEFAULT '', from_name TEXT NOT NULL,
        to_name TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
        delivered_at REAL);
      CREATE TABLE tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL DEFAULT '', parent_id INTEGER, title TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'todo', owner TEXT, spec_path TEXT,
        blocked_by TEXT NOT NULL DEFAULT '', created_by TEXT,
        updated_at REAL NOT NULL);
      PRAGMA user_version = 4;
    """)
    old.commit(); old.close()
    up = db.connect(p4)
    ok &= check("v4 -> current migration runs",
                up.execute("PRAGMA user_version").fetchone()[0] == 8)
    cols_m = {r[1] for r in up.execute("PRAGMA table_info(messages)")}
    cols_s = {r[1] for r in up.execute("PRAGMA table_info(sessions)")}
    ok &= check("migration adds kind + worktree_repo",
                "kind" in cols_m and "worktree_repo" in cols_s)
    conn5.close()
    up.close()

    # --- session timers -------------------------------------------------------
    tid = db.add_timer(conn, iterm_session_id="SID1", label="api",
                       interval_min=5, payload="check PRs", mode="idle",
                       now=1000.0)
    rows = db.list_timers(conn, "SID1")
    ok &= check("add_timer + list_timers", len(rows) == 1
                and rows[0]["payload"] == "check PRs"
                and rows[0]["active"] == 1 and rows[0]["enabled"] == 1
                and rows[0]["bound_at"] == 1000.0)
    ok &= check("add_timer default fire cap 10, count 0",
                rows[0]["max_fires"] == 10 and rows[0]["fire_count"] == 0)
    db.set_timer_enabled(conn, tid, False)
    ok &= check("set_timer_enabled off",
                db.list_timers(conn, "SID1")[0]["enabled"] == 0)
    # turning a timer back ON resets its clock to a full interval (off/on
    # restarts the cycle, doesn't resume from where it paused)
    db.set_timer_enabled(conn, tid, True, now=7777.0)
    ren = db.list_timers(conn, "SID1")[0]
    ok &= check("set_timer_enabled on resets last_fired_at (full interval)",
                ren["enabled"] == 1 and ren["last_fired_at"] == 7777.0)
    db.mark_timer_fired(conn, tid, now=2000.0)
    r0 = db.list_timers(conn, "SID1")[0]
    ok &= check("mark_timer_fired sets last_fired_at AND increments fire_count",
                r0["last_fired_at"] == 2000.0 and r0["fire_count"] == 1)
    # a custom cap is honored, and update_timer can adjust it
    ct = db.add_timer(conn, iterm_session_id="SIDC", label="c", interval_min=1,
                      payload="p", mode="now", max_fires=3, now=1000.0)
    ok &= check("add_timer custom max_fires",
                db.list_timers(conn, "SIDC")[0]["max_fires"] == 3)
    db.update_timer(conn, ct, max_fires=0)
    ok &= check("update_timer can set unlimited (0)",
                db.list_timers(conn, "SIDC")[0]["max_fires"] == 0)
    db.update_timer(conn, tid, interval_min=15, mode="now")
    r = db.list_timers(conn, "SID1")[0]
    ok &= check("update_timer", r["interval_min"] == 15 and r["mode"] == "now")

    db.deactivate_all_timers(conn)
    ok &= check("deactivate_all_timers",
                db.list_timers(conn, "SID1")[0]["active"] == 0)
    n = db.restore_session_timers(conn, "SID1", now=3000.0)
    rr = db.list_timers(conn, "SID1")[0]
    ok &= check("restore_session_timers activates + resets clock + rebinds",
                n == 1 and rr["active"] == 1 and rr["last_fired_at"] == 3000.0
                and rr["bound_at"] == 3000.0)

    # restore_timer: re-activate ONLY the given timer, leaving siblings alone
    tA = db.add_timer(conn, iterm_session_id="SIDR", label="r", interval_min=1,
                      payload="a", mode="now", now=1000.0)
    db.add_timer(conn, iterm_session_id="SIDR", label="r", interval_min=1,
                 payload="b", mode="now", now=1000.0)
    db.deactivate_all_timers(conn)
    got = db.restore_timer(conn, tA, now=5000.0)
    rlist = db.list_timers(conn, "SIDR")
    ok &= check("restore_timer activates only the one timer",
                got == 1
                and [t["active"] for t in rlist if t["id"] == tA][0] == 1
                and [t["active"] for t in rlist if t["id"] != tA][0] == 0)
    ok &= check("restore_timer rebinds the clock", [t["last_fired_at"]
                for t in rlist if t["id"] == tA][0] == 5000.0)

    # restart_timer: reset fire_count to 0, re-activate, rebind (for a capped
    # 'done' timer); enabled is left untouched.
    tC = db.add_timer(conn, iterm_session_id="SIDC2", label="c", interval_min=1,
                      payload="c", mode="now", max_fires=3, now=1000.0)
    db.mark_timer_fired(conn, tC); db.mark_timer_fired(conn, tC)
    db.mark_timer_fired(conn, tC)   # fire_count now 3 == cap
    ok &= check("timer reached its cap (fire_count 3)",
                db.list_timers(conn, "SIDC2")[0]["fire_count"] == 3)
    db.set_timer_enabled(conn, tC, False)
    db.restart_timer(conn, tC, now=6000.0)
    rc = db.list_timers(conn, "SIDC2")[0]
    ok &= check("restart_timer resets count + re-activates + keeps enabled flag",
                rc["fire_count"] == 0 and rc["active"] == 1
                and rc["last_fired_at"] == 6000.0 and rc["enabled"] == 0)

    db.add_timer(conn, iterm_session_id="SID2", label="b", interval_min=1,
                 payload="p", mode="now", now=1000.0)
    db.deactivate_all_timers(conn)
    db.restore_all_present_timers(conn, ["SID1", "SID2"], now=4000.0)
    ok &= check("restore_all_present_timers activates each present session",
                db.list_timers(conn, "SID1")[0]["active"] == 1
                and db.list_timers(conn, "SID2")[0]["active"] == 1)

    db.delete_timer(conn, tid)
    ok &= check("delete_timer", db.list_timers(conn, "SID1") == [])
    ok &= check("all_timers sees other sessions' timers",
                any(t["iterm_session_id"] == "SID2" for t in db.all_timers(conn)))

    # --- self-scheduling: key column + lookup ---------------------------
    t_keyed = db.add_timer(conn, iterm_session_id="KEY-SID", label="self:prs",
                           interval_min=20, payload="check PRs", mode="idle",
                           max_fires=10, key="prs")
    row = db.get_timer_by_key(conn, "KEY-SID", "prs")
    ok &= check("get_timer_by_key finds the keyed timer",
                row is not None and row["id"] == t_keyed
                and row["key"] == "prs")

    ok &= check("get_timer_by_key misses a different key",
                db.get_timer_by_key(conn, "KEY-SID", "nope") is None)

    ok &= check("get_timer_by_key is scoped to the session",
                db.get_timer_by_key(conn, "OTHER-SID", "prs") is None)

    # Overlay-authored rows carry key='' and must never be matched by lookup,
    # or every operator-created timer would collide with the next one.
    db.add_timer(conn, iterm_session_id="KEY-SID", label="operator",
                 interval_min=5, payload="ping", mode="now")
    ok &= check("empty key never matches",
                db.get_timer_by_key(conn, "KEY-SID", "") is None)

    ok &= check("add_timer defaults key to empty string",
                db.list_timers(conn, "KEY-SID")[1]["key"] == "")

    # --- v6 -> current: a REAL pre-existing `timers` table --------------------
    # Every migration test above builds a DB with NO timers table at all, so
    # connect()'s _SCHEMA creates the full current shape and the ALTERs then
    # no-op via the swallowed "duplicate column name" - migration 6 (the `key`
    # column) never actually runs against real data. This hand-builds the
    # true v6 shape (post migration 5: max_fires/fire_count present, key
    # absent) with a populated row, so migrations 6 AND 7 (Fix 6's partial
    # unique index) both run for real.
    p6 = os.path.join(tempfile.mkdtemp(), "v6.db")
    old6 = _sq.connect(p6)
    old6.executescript("""
      CREATE TABLE timers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        iterm_session_id TEXT,
        label TEXT NOT NULL DEFAULT '',
        interval_min INTEGER NOT NULL DEFAULT 5,
        payload TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT 'idle',
        enabled INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        last_fired_at REAL NOT NULL DEFAULT 0,
        bound_at REAL NOT NULL DEFAULT 0,
        max_fires INTEGER NOT NULL DEFAULT 10,
        fire_count INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL DEFAULT 0
      );
      PRAGMA user_version = 6;
    """)
    old6.execute(
        "INSERT INTO timers(iterm_session_id, label, interval_min, payload, "
        "mode, enabled, active, last_fired_at, bound_at, max_fires, "
        "fire_count, created_at) VALUES('LEGACY-SID', 'legacy', 5, 'ping', "
        "'idle', 1, 1, 100.0, 100.0, 10, 7, 100.0)")
    old6.commit()
    old6.close()
    up6 = db.connect(p6)
    ok &= check("v6 timers table migrates to the current version",
                up6.execute("PRAGMA user_version").fetchone()[0] == 8)
    cols_t = {r[1] for r in up6.execute("PRAGMA table_info(timers)")}
    ok &= check("v6 -> current adds the key column", "key" in cols_t)
    legacy = up6.execute(
        "SELECT * FROM timers WHERE iterm_session_id='LEGACY-SID'").fetchone()
    ok &= check("legacy row gets key='' and keeps its fire_count",
                legacy["key"] == "" and legacy["fire_count"] == 7)
    ok &= check("get_timer_by_key never matches the legacy empty-key row",
                db.get_timer_by_key(up6, "LEGACY-SID", "") is None)
    # Migration 7 (the partial unique index) must also apply cleanly on this
    # path: a second empty-key row on the same session is fine (the index
    # excludes key=''), but a second row with the SAME non-empty key on the
    # same session must be rejected.
    up6.execute(
        "INSERT INTO timers(iterm_session_id, label, interval_min, payload, "
        "mode, key) VALUES('LEGACY-SID', 'overlay2', 5, 'p2', 'idle', '')")
    up6.commit()
    ok &= check("a second empty-key row on the same session is allowed",
                len(db.list_timers(up6, "LEGACY-SID")) == 2)
    db.add_timer(up6, iterm_session_id="LEGACY-SID", label="self:x",
                interval_min=5, payload="p", mode="idle", key="dup")
    try:
        up6.execute(
            "INSERT INTO timers(iterm_session_id, label, interval_min, "
            "payload, mode, key) VALUES('LEGACY-SID', 'x2', 5, 'p', 'idle', "
            "'dup')")
        up6.commit()
        ok &= check("unique index rejects a duplicate (session, key)", False)
    except _sq.IntegrityError:
        ok &= check("unique index rejects a duplicate (session, key)", True)
    up6.close()

    # --- v7 -> v8 with duplicates already in the table ------------------------
    # cmd_timer_add's lookup-then-insert is not atomic, so a DB written while
    # v7 was current can hold duplicate (iterm_session_id, key) rows. Creating
    # the unique index over those raises IntegrityError, which _migrate does
    # NOT swallow - db.connect() would then raise for the TUI, the watcher and
    # every CLI verb, with no way out. Migration 7 must dedupe first.
    p7 = os.path.join(tempfile.mkdtemp(), "v7dup.db")
    old7 = _sq.connect(p7)
    old7.executescript("""
      CREATE TABLE timers(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        iterm_session_id TEXT,
        label TEXT NOT NULL DEFAULT '',
        interval_min INTEGER NOT NULL DEFAULT 5,
        payload TEXT NOT NULL DEFAULT '',
        mode TEXT NOT NULL DEFAULT 'idle',
        enabled INTEGER NOT NULL DEFAULT 1,
        active INTEGER NOT NULL DEFAULT 1,
        last_fired_at REAL NOT NULL DEFAULT 0,
        bound_at REAL NOT NULL DEFAULT 0,
        max_fires INTEGER NOT NULL DEFAULT 10,
        fire_count INTEGER NOT NULL DEFAULT 0,
        key TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0
      );
      PRAGMA user_version = 7;
    """)
    for lbl, k in (("first", "prs"), ("racy dup", "prs"), ("third", "prs"),
                   ("other key", "docs"), ("overlay a", ""), ("overlay b", "")):
        old7.execute(
            "INSERT INTO timers(iterm_session_id, label, interval_min, "
            "payload, mode, key) VALUES('DUP-SID', ?, 5, 'p', 'idle', ?)",
            (lbl, k))
    # a same-key row on ANOTHER session must survive: the group is (sid, key)
    old7.execute(
        "INSERT INTO timers(iterm_session_id, label, interval_min, payload, "
        "mode, key) VALUES('OTHER-SID', 'elsewhere', 5, 'p', 'idle', 'prs')")
    old7.commit()
    old7.close()
    up7 = db.connect(p7)                  # must NOT raise
    ok &= check("connect() survives a v7 DB holding duplicate (sid, key) rows",
                up7.execute("PRAGMA user_version").fetchone()[0] == 8)
    dup_rows = [r for r in db.list_timers(up7, "DUP-SID") if r["key"] == "prs"]
    ok &= check("dedupe keeps exactly one row per (session, key) group",
                len(dup_rows) == 1 and dup_rows[0]["label"] == "first")
    ok &= check("dedupe keeps other keys and both empty-key operator rows",
                len(db.list_timers(up7, "DUP-SID")) == 4)
    ok &= check("dedupe is scoped per session, not per key",
                len(db.list_timers(up7, "OTHER-SID")) == 1)
    ok &= check("the unique index exists after the dedupe migration",
                up7.execute("SELECT name FROM sqlite_master WHERE type='index' "
                            "AND name='timers_sid_key'").fetchone() is not None)
    up7.close()
    # ...and it really is committed, not sitting in an open transaction.
    re7 = db.connect(p7)
    ok &= check("the dedupe is committed (survives reopening the DB)",
                len([r for r in db.list_timers(re7, "DUP-SID")
                     if r["key"] == "prs"]) == 1)
    re7.close()

    # A FRESH DB gets the index too, even though _SCHEMA no longer carries it
    # (_INDEXES runs after _migrate's v == 0 early return).
    pfresh = os.path.join(tempfile.mkdtemp(), "fresh.db")
    fresh = db.connect(pfresh)
    ok &= check("a fresh DB gets timers_sid_key from _INDEXES",
                fresh.execute("SELECT name FROM sqlite_master WHERE "
                              "type='index' AND name='timers_sid_key'"
                              ).fetchone() is not None)
    fresh.close()

    # --- R1: _migrate does not swallow non-duplicate-column errors ------------
    # Only "duplicate column name" (re-running an ALTER TABLE ADD COLUMN that
    # already landed) is a legitimate swallow. Anything else - a locked DB, an
    # I/O error, a real bug in a migration statement - must propagate, AND
    # user_version must not advance past the failing step. If it did, connect()
    # would stamp a version whose columns don't actually exist, and every
    # future connect() (TUI, watcher, every CLI verb) would break with "no
    # such column" forever, with no recovery short of manual sqlite surgery.
    rpath = _tmpdb()
    rconn = _sq.connect(rpath)
    rconn.execute("PRAGMA user_version = 6")
    rconn.commit()
    saved_migrations = db._MIGRATIONS
    db._MIGRATIONS = dict(saved_migrations)
    # Not a duplicate-column error: this table does not exist at all.
    db._MIGRATIONS[6] = ("SELECT * FROM no_such_table",)
    try:
        try:
            db._migrate(rconn)
            ok &= check("_migrate re-raises a non-duplicate-column "
                        "OperationalError", False)
        except _sq.OperationalError as e:
            ok &= check("_migrate re-raises a non-duplicate-column "
                        "OperationalError",
                        "duplicate column" not in str(e).lower())
        ok &= check("user_version is NOT advanced past the failing step",
                    rconn.execute("PRAGMA user_version").fetchone()[0] == 6)
    finally:
        db._MIGRATIONS = saved_migrations
    rconn.close()

    # --- prs ----------------------------------------------------------------
    row = db.upsert_pr(conn, "acme/api", 482, project="webshop",
                       state="created", title="Add rate limiting",
                       branch="relay/api-worker", now=1000.0)
    ok &= check("upsert_pr creates a row with no owner",
                row["repo"] == "acme/api" and row["number"] == 482
                and row["owner"] == "" and row["state"] == "created")
    ok &= check("upsert_pr stamps state_changed_at and updated_at",
                row["state_changed_at"] == 1000.0
                and row["updated_at"] == 1000.0)

    same = db.upsert_pr(conn, "acme/api", 482, state="created", now=2000.0)
    ok &= check("re-upsert with the SAME state moves updated_at only",
                same["updated_at"] == 2000.0
                and same["state_changed_at"] == 1000.0)

    moved = db.upsert_pr(conn, "acme/api", 482, state="changes", now=3000.0)
    ok &= check("upsert with a NEW state re-stamps state_changed_at",
                moved["state"] == "changes"
                and moved["state_changed_at"] == 3000.0)
    ok &= check("upsert preserves fields it was not given",
                moved["title"] == "Add rate limiting"
                and moved["branch"] == "relay/api-worker")

    ok &= check("upsert_pr rejects an unknown state", _raises(
        lambda: db.upsert_pr(conn, "acme/api", 482, state="merged-ish")))

    claimed = db.claim_pr(conn, "acme/api", 482, owner="api-worker",
                          owner_session_id="SID-A", task_id=14, now=4000.0)
    ok &= check("claim_pr records owner, session id and task",
                claimed["owner"] == "api-worker"
                and claimed["owner_session_id"] == "SID-A"
                and claimed["task_id"] == 14
                and claimed["claimed_at"] == 4000.0)
    ok &= check("claim_pr does not disturb state",
                claimed["state"] == "changes")

    fresh = db.claim_pr(conn, "acme/web", 31, owner="fe-worker",
                        owner_session_id="SID-B", project="webshop",
                        now=4100.0)
    ok &= check("claim_pr creates the row when the sweep never saw the PR",
                fresh["number"] == 31 and fresh["state"] == "created")

    reclaim = db.claim_pr(conn, "acme/api", 482, owner="api-worker",
                          owner_session_id="SID-C", now=5000.0)
    ok &= check("re-claiming overwrites the session id (restore case)",
                reclaim["owner_session_id"] == "SID-C")

    ok &= check("get_pr finds by ref",
                db.get_pr(conn, "acme/api", 482)["id"] == claimed["id"])
    ok &= check("get_pr returns None for an unknown ref",
                db.get_pr(conn, "acme/api", 999) is None)

    ok &= check("list_prs is ordered by repo then number",
                [(r["repo"], r["number"]) for r in db.list_prs(conn)]
                == [("acme/api", 482), ("acme/web", 31)])
    ok &= check("list_prs --owner filters",
                [r["number"] for r in db.list_prs(conn, owner="fe-worker")]
                == [31])
    # #482 (state "changes") and #31 (state "created") are both still open.
    # The visibility window must never hide an open PR by age - only settled
    # (merged/closed) history ages out - so `since` must not filter either
    # of these out even though #31's updated_at (4100) is before the cutoff.
    ok &= check("list_prs --since never filters an open PR by age",
                sorted(r["number"] for r in db.list_prs(conn, since=4500.0))
                == [31, 482])

    db.touch_pr_routed(conn, "acme/api", 482, now=6000.0)
    ok &= check("touch_pr_routed stamps last_routed_at",
                db.get_pr(conn, "acme/api", 482)["last_routed_at"] == 6000.0)

    # retention: merged/closed prune, open never does, at any age
    db.upsert_pr(conn, "acme/api", 400, state="merged", now=1.0)
    db.upsert_pr(conn, "acme/api", 401, state="closed", now=1.0)
    db.upsert_pr(conn, "acme/api", 402, state="review", now=1.0)
    n = db.prune_prs(conn, 7, now=1.0 + 8 * 86400)
    ok &= check("prune_prs drops old merged and closed rows", n == 2)
    ok &= check("prune_prs never drops an open PR, however old",
                db.get_pr(conn, "acme/api", 402) is not None)

    # list_prs's `since` window mirrors prune_prs's own rule: a stale review
    # (open) PR stays visible, a stale merged one does not - the invariant
    # is "open PRs never age out", true of both visibility and deletion.
    db.upsert_pr(conn, "acme/api", 501, state="review", now=100.0)
    db.upsert_pr(conn, "acme/api", 502, state="merged", now=100.0)
    windowed = [r["number"] for r in db.list_prs(conn, since=9000.0)]
    ok &= check("list_prs keeps a stale OPEN pr inside the window",
                501 in windowed)
    ok &= check("list_prs windows out a stale SETTLED pr",
                502 not in windowed)

    ok &= check("RESERVED_NAMES covers relay and human",
                set(db.RESERVED_NAMES) == {"relay", "human"})

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
