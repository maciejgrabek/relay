"""Tests for the relay CLI verbs, run in-process against a temp RELAY_DB.

Run: python3 iterm/test_cli.py    or    ./test/run.sh
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(__file__))

# Point the CLI at a scratch DB and fake an iTerm identity BEFORE importing.
_TMP = tempfile.mkdtemp()
os.environ["RELAY_DB"] = os.path.join(_TMP, "relay.db")
os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"

import cli  # noqa: E402
import db   # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run_cli(*argv, iterm_id=None):
    """Invoke cli.main capturing (exit_code, stdout, stderr)."""
    if iterm_id is not None:
        os.environ["ITERM_SESSION_ID"] = iterm_id
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run():
    ok = True

    ok &= check("my_iterm_id strips prefix", cli.my_iterm_id() == "AAAA-1111")

    # register self as coordinator
    code, out, _ = run_cli("register", "--name", "coord",
                           "--role", "coordinator", "--project", "webshop",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("register ok", code == 0 and "coord" in out)
    conn = db.connect()
    ok &= check("register wrote bare uuid",
                db.get_session(conn, "coord")["iterm_session_id"] == "CO-ID")

    # register a worker with a different identity
    code, out, _ = run_cli("register", "--name", "bff-worker", "--role", "worker",
                           "--project", "webshop", iterm_id="w0t1p0:BFF-ID")
    ok &= check("worker registered", code == 0)

    code, _, err = run_cli("register", "--name", "x", "--role", "boss",
                           iterm_id="w0t1p0:BFF-ID")
    ok &= check("bad role -> exit 2 (argparse)", code == 2)

    # reserved + empty names rejected
    code, _, err = run_cli("register", "--name", "relay", "--role", "worker",
                           iterm_id="w0t1p0:RES-ID")
    ok &= check("name 'relay' reserved -> exit 1", code == 1 and "reserved" in err)
    code, _, err = run_cli("register", "--name", "   ", "--role", "worker",
                           iterm_id="w0t1p0:EMPTY-ID")
    ok &= check("empty name -> exit 1", code == 1 and "empty" in err)

    # status requires registration
    code, _, err = run_cli("status", "working on #1", iterm_id="w9t9p9:GHOST")
    ok &= check("status unregistered -> error", code == 1 and "register" in err)
    code, out, _ = run_cli("status", "working on #1", iterm_id="w0t1p0:BFF-ID")
    ok &= check("status ok", code == 0
                and db.get_session(conn, "bff-worker")["status_text"] == "working on #1")

    # send: recipient must exist; sender must be registered
    code, _, err = run_cli("send", "ghost", "hello", iterm_id="w0t0p0:CO-ID")
    ok &= check("send to unknown -> exit 1", code == 1 and "ghost" in err)
    code, out, _ = run_cli("send", "bff-worker", "spec ready", iterm_id="w0t0p0:CO-ID")
    ok &= check("send queues", code == 0
                and len(db.undelivered(conn, "bff-worker")) == 1)
    row = db.undelivered(conn, "bff-worker")[0]
    ok &= check("send stamps sender+project",
                row["from_name"] == "coord" and row["project"] == "webshop")

    # inbox prints and marks delivered
    code, out, _ = run_cli("inbox", iterm_id="w0t1p0:BFF-ID")
    ok &= check("inbox shows message", code == 0 and "spec ready" in out
                and "coord" in out)
    ok &= check("inbox marks delivered", db.undelivered(conn, "bff-worker") == [])
    code, out, _ = run_cli("inbox", iterm_id="w0t1p0:BFF-ID")
    ok &= check("inbox empty afterwards", code == 0 and "no new messages" in out)

    # msgs shows history even after delivery
    code, out, _ = run_cli("msgs", "--with", "coord", iterm_id="w0t1p0:BFF-ID")
    ok &= check("msgs history", code == 0 and "spec ready" in out)

    # --- typed messages -------------------------------------------------------
    code, _, _ = run_cli("send", "bff-worker", "branch ready", "--kind", "done",
                         iterm_id="w0t0p0:CO-ID")
    row = db.undelivered(conn, "bff-worker")[0]
    ok &= check("send --kind stored", code == 0 and row["kind"] == "done")
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain

    code, _, err = run_cli("send", "bff-worker", "x", "--kind", "wake",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("kind wake reserved", code == 1 and "reserved" in err)
    code, _, err = run_cli("send", "bff-worker", "x", "--kind", "Not Valid",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("bad kind rejected", code == 1 and "lowercase" in err)
    code, _, _ = run_cli("send", "bff-worker", "y", "--kind", "review-me",
                         iterm_id="w0t0p0:CO-ID")
    ok &= check("custom kind allowed", code == 0
                and db.undelivered(conn, "bff-worker")[0]["kind"] == "review-me")
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain

    # --- broadcast ------------------------------------------------------------
    code, _, err = run_cli("send", "--all", "hello", iterm_id="w0t0p0:CO-ID")
    ok &= check("--all needs --project", code == 1 and "--project" in err)
    code, _, err = run_cli("send", "--all", "--project", "webshop", "a", "b",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("--all with two positionals rejected",
                code == 1 and "only the message body" in err)
    code, out, _ = run_cli("send", "--all", "--project", "webshop",
                           "freeze: rebasing", iterm_id="w0t0p0:CO-ID")
    ok &= check("broadcast queues to others, not sender", code == 0
                and len(db.undelivered(conn, "bff-worker")) == 1
                and db.undelivered(conn, "coord") == [])
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain

    # --- task verbs ---------------------------------------------------------
    # coordinator creates an epic for the worker -> assignment wake-up queued
    code, out, _ = run_cli("task", "add", "--owner", "bff-worker",
                           "--spec", "/w/specs/bff.md", "--project", "webshop",
                           "BFF checkout changes", iterm_id="w0t0p0:CO-ID")
    ok &= check("task add prints id", code == 0 and "#" in out)
    epic_id = int(out.split("#")[1].split()[0])
    wake = db.undelivered(conn, "bff-worker")
    ok &= check("assignment wake-up queued from relay",
                len(wake) == 1 and wake[0]["from_name"] == "relay"
                and f"#{epic_id}" in wake[0]["body"]
                and "/w/specs/bff.md" in wake[0]["body"])

    # self-owned subtask -> NO wake-up spam
    code, out, _ = run_cli("task", "add", "--parent", str(epic_id),
                           "--owner", "bff-worker", "--project", "webshop",
                           "wire endpoint", iterm_id="w0t1p0:BFF-ID")
    sub_id = int(out.split("#")[1].split()[0])
    ok &= check("self-assigned task queues no wake-up",
                len(db.undelivered(conn, "bff-worker")) == 1)

    # a dependent task, blocked by the subtask
    code, out, _ = run_cli("task", "add", "--owner", "coord",
                           "--blocked-by", str(sub_id), "--project", "webshop",
                           "review BFF work", iterm_id="w0t0p0:CO-ID")
    dep_id = int(out.split("#")[1].split()[0])

    # nonexistent blocker / parent are rejected at creation (never silently wait)
    code, _, err = run_cli("task", "add", "--blocked-by", "9999",
                           "review", iterm_id="w0t0p0:CO-ID")
    ok &= check("blocked-by nonexistent id -> exit 1",
                code == 1 and "#9999" in err)
    code, _, err = run_cli("task", "add", "--parent", "9999", "sub",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("parent nonexistent id -> exit 1",
                code == 1 and "#9999" in err)

    # task update to done -> unblock wake-up for the dependent's owner
    code, out, _ = run_cli("task", "update", str(sub_id), "--state", "done",
                           iterm_id="w0t1p0:BFF-ID")
    ok &= check("task update ok", code == 0)
    coord_wake = db.undelivered(conn, "coord")
    ok &= check("unblock wake-up queued",
                len(coord_wake) == 1 and f"#{dep_id}" in coord_wake[0]["body"]
                and "unblocked" in coord_wake[0]["body"])

    code, _, err = run_cli("task", "update", "9999", "--state", "done",
                           iterm_id="w0t1p0:BFF-ID")
    ok &= check("task update unknown id -> error", code == 1)

    # wake messages are kind='wake'; msgs shows kinds
    code, _, _ = run_cli("task", "add", "wired task", "--owner", "bff-worker",
                         iterm_id="w0t0p0:CO-ID")
    wake = db.undelivered(conn, "bff-worker")[0]
    ok &= check("assignment wake has kind=wake", wake["kind"] == "wake")
    code, out, _ = run_cli("msgs", "--project", "webshop")
    ok &= check("msgs shows kind tag", "[wake]" in out)
    run_cli("inbox", iterm_id="w0t1p0:BFF-ID")   # drain

    # task list
    code, out, _ = run_cli("task", "list", "--project", "webshop",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("task list shows epic and states", f"#{epic_id}" in out
                and "[done]" in out and "[todo]" in out)
    code, out, _ = run_cli("task", "list", "--mine", iterm_id="w0t0p0:CO-ID")
    ok &= check("task list --mine filters", f"#{dep_id}" in out
                and f"#{sub_id}" not in out)

    # spawn: first_prompt content (the iTerm2 side is smoke-tested live)
    import spawn as spawnmod
    fp = spawnmod.first_prompt("be-worker", "webshop", "implement API")
    ok &= check("spawn prompt invokes skill + identity",
                "relay-worker" in fp and "be-worker" in fp
                and "webshop" in fp and "implement API" in fp)
    fp2 = spawnmod.first_prompt("boss", "", "", role="coordinator")
    ok &= check("spawn coordinator prompt", "relay-coordinator" in fp2)

    # doctor: runs read-only against the temp DB, exits 0, reports state.
    code, out, _ = run_cli("doctor")
    ok &= check("doctor exits 0 and reports sessions/tasks",
                code == 0 and "relay doctor" in out
                and ("sessions:" in out or "registered" in out))
    # doctor on an empty DB still works and guides the user.
    import tempfile as _tf
    empty = os.path.join(_tf.mkdtemp(), "empty.db")
    old_db = os.environ["RELAY_DB"]
    os.environ["RELAY_DB"] = empty
    try:
        code, out, _ = run_cli("doctor")
        ok &= check("doctor on empty DB guides to spawn",
                    code == 0 and "none registered" in out and "relay spawn" in out)
    finally:
        os.environ["RELAY_DB"] = old_db
    # version: prints something, exits 0 (git or 'unknown').
    code, out, _ = run_cli("version")
    ok &= check("version exits 0", code == 0 and "relay" in out)

    # register --dir records workdir
    code, _, _ = run_cli("register", "--name", "ctxw", "--role", "worker",
                         "--project", "p", "--dir", "/work/ctx",
                         iterm_id="w0t9p0:CTX-ID")
    ok &= check("register --dir stores workdir",
                code == 0 and db.get_session(conn, "ctxw")["workdir"] == "/work/ctx")

    # --- clean: reset + remove closed sessions, plan/dry-run/confirm ---------
    import db as _db
    cc = db.connect()
    # a closed session owning a doing task
    _db.register(cc, "deadw", "DW", "worker", "webshop", now=1.0)
    ct = _db.add_task(cc, "half done", project="webshop", owner="deadw", now=2.0)
    _db.set_task_state(cc, ct, "doing", now=3.0)
    _db.mark_closed(cc, "deadw", 400.0)

    code, out, _ = run_cli("clean", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("clean --dry-run shows plan, changes nothing",
                code == 0 and "deadw" in out
                and db.get_session(cc, "deadw") is not None
                and db.get_task(cc, ct)["state"] == "doing")
    code, out, _ = run_cli("clean", "--project", "webshop", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("clean --yes resets task to unowned todo",
                code == 0 and db.get_task(cc, ct)["state"] == "todo"
                and db.get_task(cc, ct)["owner"] is None)
    ok &= check("clean --yes removes the closed session row",
                db.get_session(cc, "deadw") is None)
    cc.close()

    # --- restore: plan + dry-run (spawn side is live-only) ------------------
    rc = db.connect()
    import db as _db2
    _db2.register(rc, "rw", "RW", "worker", "webshop", now=1.0)
    # Use a real, existing dir: restore now SKIPs a candidate whose recorded
    # workdir no longer exists on disk.
    _rw_dir = os.path.join(_TMP, "rw")
    os.makedirs(_rw_dir, exist_ok=True)
    _db2.set_session_context(rc, "rw", _rw_dir, "do the thing")
    rt = _db2.add_task(rc, "unfinished", project="webshop", owner="rw", now=2.0)
    _db2.set_task_state(rc, rt, "doing", now=3.0)
    _db2.mark_closed(rc, "rw", 500.0)

    code, out, _ = run_cli("restore", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("restore --dry-run plans, spawns nothing",
                code == 0 and "restore rw" in out and _rw_dir in out
                and "#" + str(rt) in out
                and db.get_session(rc, "rw")["closed_at"] == 500.0)

    # a recorded workdir that no longer exists on disk is SKIPped
    _db2.register(rc, "gonedir", "GD", "worker", "webshop", now=7.0)
    _db2.set_session_context(rc, "gonedir", "/nonexistent/relay-x", "m")
    gt = _db2.add_task(rc, "y", project="webshop", owner="gonedir", now=8.0)
    _db2.set_task_state(rc, gt, "doing", now=9.0)
    _db2.mark_closed(rc, "gonedir", 500.0)
    code, out, _ = run_cli("restore", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("restore skips a workdir that no longer exists",
                "workdir no longer exists" in out and "SKIP gonedir" in out)

    # no-workdir closed session is SKIPped
    _db2.register(rc, "nowd", "NW", "worker", "webshop", now=4.0)
    nt = _db2.add_task(rc, "x", project="webshop", owner="nowd", now=5.0)
    _db2.set_task_state(rc, nt, "doing", now=6.0)
    _db2.mark_closed(rc, "nowd", 500.0)
    code, out, _ = run_cli("restore", "--project", "webshop", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("restore skips no-workdir session", "SKIP nowd" in out)
    rc.close()

    # --- doctor reports orphans: closed session owning non-done work ---------
    dc = db.connect()
    import db as _db3
    _db3.register(dc, "orph", "OR", "worker", "webshop", now=1.0)
    ot = _db3.add_task(dc, "stuck", project="webshop", owner="orph", now=2.0)
    _db3.set_task_state(dc, ot, "doing", now=3.0)
    _db3.mark_closed(dc, "orph", 400.0)
    code, out, _ = run_cli("doctor")
    ok &= check("doctor reports orphaned work",
                code == 0 and "orphan" in out.lower() and "orph" in out)
    dc.close()

    # --- wipe: orphaned + --all + guards ------------------------------------
    import db as _wdb
    wc = db.connect()
    _wdb.register(wc, "deadw", "DWX", "worker", "wp", now=1.0)
    wt = _wdb.add_task(wc, "gone", project="wp", owner="deadw", now=2.0)
    _wdb.set_task_state(wc, wt, "doing", now=3.0)
    _wdb.mark_closed(wc, "deadw", 400.0)

    code, out, _ = run_cli("wipe", "--project", "wp", "--dry-run",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --dry-run plans, deletes nothing",
                code == 0 and "deadw" in out
                and db.get_task(wc, wt) is not None)
    code, out, _ = run_cli("wipe", "--project", "wp", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --yes deletes task + session",
                code == 0 and db.get_task(wc, wt) is None
                and db.get_session(wc, "deadw") is None)

    # --all requires --project
    code, _, err = run_cli("wipe", "--all", iterm_id="w0t0p0:CO-ID")
    ok &= check("--all without --project -> error", code == 1 and "project" in err)

    # --all nukes a project, leaves another intact
    _wdb.register(wc, "a1", "A1", "worker", "PA", now=1.0)
    _wdb.register(wc, "b1", "B1", "worker", "PB", now=1.0)
    _wdb.add_task(wc, "pa", project="PA", owner="a1", now=2.0)
    _wdb.add_task(wc, "pb", project="PB", owner="b1", now=2.0)
    code, out, _ = run_cli("wipe", "--project", "PA", "--all", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --all empties the project",
                code == 0 and db.list_tasks(wc, project="PA") == []
                and db.get_session(wc, "a1") is None)
    ok &= check("wipe --all leaves other project",
                len(db.list_tasks(wc, project="PB")) == 1
                and db.get_session(wc, "b1") is not None)

    # (a) wipe --project scopes the delete to that project's tasks only (fix 1):
    # a dead session owning a task in MP AND one in MQ loses only MP's task.
    _wdb.register(wc, "multi", "MU", "worker", "MP", now=20.0)
    mp_t = _wdb.add_task(wc, "in-mp", project="MP", owner="multi", now=21.0)
    mq_t = _wdb.add_task(wc, "in-mq", project="MQ", owner="multi", now=22.0)
    _wdb.mark_closed(wc, "multi", 400.0)
    code, out, _ = run_cli("wipe", "--project", "MP", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --project deletes only that project's task (fix 1)",
                code == 0 and db.get_task(wc, mp_t) is None
                and db.get_task(wc, mq_t) is not None)

    # (b) a LIVE session in the wipe project is never touched by the orphaned form
    _wdb.register(wc, "livew", "LW", "worker", "LP", now=30.0)
    lp_t = _wdb.add_task(wc, "live-task", project="LP", owner="livew", now=31.0)
    code, out, _ = run_cli("wipe", "--project", "LP", "--yes",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe leaves a LIVE session + its task untouched",
                code == 0 and db.get_session(wc, "livew") is not None
                and db.get_task(wc, lp_t) is not None)

    # (c) --all --dry-run makes zero deletes
    _wdb.register(wc, "drw", "DRW", "worker", "DRP", now=40.0)
    dr_t = _wdb.add_task(wc, "dr-task", project="DRP", owner="drw", now=41.0)
    code, out, _ = run_cli("wipe", "--all", "--dry-run", "--project", "DRP",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe --all --dry-run deletes nothing",
                code == 0 and db.get_task(wc, dr_t) is not None
                and db.get_session(wc, "drw") is not None)

    # (d) confirm-abort (no --yes, _confirm returns False): nothing deleted
    _wdb.register(wc, "abw", "ABW", "worker", "ABP", now=50.0)
    ab_t = _wdb.add_task(wc, "ab-task", project="ABP", owner="abw", now=51.0)
    _wdb.mark_closed(wc, "abw", 400.0)
    orig = cli._confirm
    cli._confirm = lambda q: False
    try:
        code, out, _ = run_cli("wipe", "--project", "ABP",
                               iterm_id="w0t0p0:CO-ID")
    finally:
        cli._confirm = orig
    ok &= check("wipe confirm-abort deletes nothing + prints aborted",
                code == 0 and "aborted." in out
                and db.get_task(wc, ab_t) is not None
                and db.get_session(wc, "abw") is not None)

    # (e) names + --all is refused outright (looks like a single-session wipe)
    code, _, err = run_cli("wipe", "deadw", "--all", iterm_id="w0t0p0:CO-ID")
    ok &= check("wipe with names + --all -> exit 1",
                code == 1 and ("names" in err or "takes no" in err))
    wc.close()

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
