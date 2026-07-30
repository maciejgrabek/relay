"""Tests for the relay CLI verbs, run in-process against a temp RELAY_DB.

Run: python3 iterm/test_cli.py    or    ./test/run.sh
"""
import io
import os
import re
import sys
import tempfile
import time
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(__file__))

# Point the CLI at a scratch DB and fake an iTerm identity BEFORE importing.
_TMP = tempfile.mkdtemp()
os.environ["RELAY_DB"] = os.path.join(_TMP, "relay.db")
os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"

import cli     # noqa: E402
import db      # noqa: E402
import timers  # noqa: E402


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


def _rebind(name, sid):
    """Simulate the name being reclaimed by a different tab."""
    c = db.connect()
    c.execute("UPDATE sessions SET iterm_session_id = ? WHERE name = ?",
              (sid, name))
    c.commit()
    c.close()


def _one_message(to_name):
    c = db.connect()
    row = c.execute("SELECT * FROM messages WHERE to_name = ? "
                    "ORDER BY id DESC LIMIT 1", (to_name,)).fetchone()
    c.close()
    return row


def _session_count():
    c = db.connect()
    n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    c.close()
    return n


def _sess(name):
    c = db.connect()
    row = c.execute("SELECT * FROM sessions WHERE name = ?",
                    (name,)).fetchone()
    c.close()
    return row


def run():
    ok = True

    # spawn stub: cmd_spawn does `import spawn as spawnmod` at call time, so
    # patching the module attribute here sticks for every run_cli("spawn", ...)
    # below. Registers the (fake) worker so later checks can inspect it.
    import spawn as spawnmod
    spawn_calls = []

    async def _fake_spawn(name, project, prompt, workdir, role="worker",
                          arm="off"):
        spawn_calls.append({"name": name, "workdir": workdir, "arm": arm})
        c = db.connect()
        db.register(c, name, f"FAKE-{name}", role, project)
        db.set_session_context(c, name, workdir, prompt)
        return f"FAKE-{name}"

    spawnmod.spawn_worker = _fake_spawn

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

    # task list shows who created each task, so a worker whose wake-up came
    # from 'relay' can find a person to reply to
    code, out, _ = run_cli("task", "list", "--project", "webshop",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("task list shows the creator",
                f"#{epic_id}" in out and "by coord" in out)

    # --- self-scheduling: relay timer add -------------------------------
    code, out, err = run_cli("timer", "add", "--key", "pr-duty",
                             "--every", "20", "--times", "10",
                             "--say", "Read .relay/prompts/pr-duty.md and "
                                      "do what it says.",
                             iterm_id="w0t1p0:TIMER-SID")
    trows = db.list_timers(db.connect(), "TIMER-SID")
    ok &= check("timer add creates one row", code == 0 and len(trows) == 1)
    ok &= check("timer add forces idle mode", trows[0]["mode"] == "idle")
    ok &= check("timer add stores interval + cap",
                trows[0]["interval_min"] == 20 and trows[0]["max_fires"] == 10)
    ok &= check("timer add labels the row self:<key>",
                trows[0]["label"] == "self:pr-duty")
    ok &= check("timer add is live immediately",
                trows[0]["enabled"] == 1 and trows[0]["active"] == 1)

    # Upsert: same key updates in place, never stacks.
    code, _, _ = run_cli("timer", "add", "--key", "pr-duty",
                         "--every", "30", "--times", "5", "--say", "new text",
                         iterm_id="w0t1p0:TIMER-SID")
    trows = db.list_timers(db.connect(), "TIMER-SID")
    ok &= check("timer add upserts by key (still one row)",
                code == 0 and len(trows) == 1)
    ok &= check("timer add upsert applies new values",
                trows[0]["interval_min"] == 30 and trows[0]["payload"] == "new text"
                and trows[0]["max_fires"] == 5)

    # Re-registering an EXHAUSTED timer must not revive it - and the only
    # re-registration that can revive one is a LARGER --times, which would
    # raise max_fires above fire_count and make capped() false again. Same
    # --times keeps the row capped no matter what the code does, so it proves
    # nothing; this re-registers 1-fire-exhausted with --times 10.
    run_cli("timer", "add", "--key", "x", "--every", "20", "--times", "1",
            "--say", "x", iterm_id="w0t1p0:EXHAUST-SID")
    exhaust_row = db.get_timer_by_key(db.connect(), "EXHAUST-SID", "x")
    db.mark_timer_fired(db.connect(), exhaust_row["id"])   # fire_count -> cap
    code, out, _ = run_cli("timer", "add", "--key", "x", "--every", "25",
                           "--times", "10", "--say", "revive me",
                           iterm_id="w0t1p0:EXHAUST-SID")
    r = db.get_timer_by_key(db.connect(), "EXHAUST-SID", "x")
    ok &= check("re-registering an exhausted timer does not reset fire_count",
                code == 0 and r["fire_count"] == 1)
    ok &= check("a larger --times does NOT raise an exhausted timer's cap",
                r["max_fires"] == 1)
    ok &= check("an exhausted timer stays exhausted after re-registration",
                timers.capped(r))
    ok &= check("re-registering an exhausted timer still updates text/interval",
                r["interval_min"] == 25 and r["payload"] == "revive me")
    ok &= check("re-registering an exhausted timer says it is exhausted",
                "exhausted" in out.lower())
    ok &= check("re-registering an exhausted timer points at the t overlay",
                "overlay" in out.lower())
    ok &= check("exhausted output reports the surviving cap, not the request",
                "10 fire" not in out)

    # Exhausted AND operator-disabled at once: the session must hear BOTH
    # facts, not just the first one an elif chain happened to match.
    run_cli("timer", "add", "--key", "both", "--every", "20", "--times", "1",
            "--say", "x", iterm_id="w0t1p0:BOTH-SID")
    both_row = db.get_timer_by_key(db.connect(), "BOTH-SID", "both")
    db.mark_timer_fired(db.connect(), both_row["id"])      # fire_count -> cap
    db.update_timer(db.connect(), both_row["id"], enabled=0)
    code, out, _ = run_cli("timer", "add", "--key", "both", "--every", "20",
                           "--times", "5", "--say", "y",
                           iterm_id="w0t1p0:BOTH-SID")
    ok &= check("an exhausted AND off timer reports both facts",
                code == 0 and "exhausted" in out.lower()
                and "off" in out.lower())

    # Upsert must never touch enabled/active - an operator-disabled or
    # pending-restore timer stays that way across re-registration, and the
    # fact is visible in the printed output.
    run_cli("timer", "add", "--key", "x", "--every", "20", "--times", "5",
            "--say", "x", iterm_id="w0t1p0:OPOFF-SID")
    opoff_row = db.get_timer_by_key(db.connect(), "OPOFF-SID", "x")
    db.update_timer(db.connect(), opoff_row["id"], enabled=0)
    code, out, _ = run_cli("timer", "add", "--key", "x", "--every", "25",
                           "--times", "5", "--say", "y",
                           iterm_id="w0t1p0:OPOFF-SID")
    r = db.get_timer_by_key(db.connect(), "OPOFF-SID", "x")
    ok &= check("upsert of an OFF timer leaves it off but updates other fields",
                code == 0 and r["enabled"] == 0 and r["interval_min"] == 25
                and r["payload"] == "y")
    ok &= check("upsert of an OFF timer says so in the output",
                "off" in out.lower())

    run_cli("timer", "add", "--key", "x", "--every", "20", "--times", "5",
            "--say", "x", iterm_id="w0t1p0:PENDING-SID")
    pending_row = db.get_timer_by_key(db.connect(), "PENDING-SID", "x")
    db.update_timer(db.connect(), pending_row["id"], active=0)
    code, out, _ = run_cli("timer", "add", "--key", "x", "--every", "20",
                           "--times", "5", "--say", "z",
                           iterm_id="w0t1p0:PENDING-SID")
    r = db.get_timer_by_key(db.connect(), "PENDING-SID", "x")
    ok &= check("upsert of a pending-restore timer leaves active untouched",
                code == 0 and r["active"] == 0)
    ok &= check("upsert of a pending-restore timer says so in the output",
                "restore" in out.lower())

    # Per-session cap of 5 timers, enforced only on a fresh INSERT.
    for i in range(5):
        code, _, _ = run_cli("timer", "add", "--key", f"cap{i}", "--every", "20",
                             "--times", "5", "--say", "x",
                             iterm_id="w0t1p0:CAP-SID")
        ok &= check(f"per-session cap: timer {i + 1} of 5 succeeds", code == 0)
    code, _, err = run_cli("timer", "add", "--key", "cap5", "--every", "20",
                           "--times", "5", "--say", "x",
                           iterm_id="w0t1p0:CAP-SID")
    ok &= check("per-session cap rejects a 6th distinct key",
                code == 1 and "5" in err and "timer list" in err
                and "timer rm" in err)
    ok &= check("per-session cap left exactly 5 timers",
                len(db.list_timers(db.connect(), "CAP-SID")) == 5)
    # An upsert of an EXISTING key is always allowed, even at the cap - a
    # session at the limit must still be able to update its own timer.
    code, _, _ = run_cli("timer", "add", "--key", "cap0", "--every", "45",
                         "--times", "5", "--say", "updated",
                         iterm_id="w0t1p0:CAP-SID")
    ok &= check("upsert of an existing key still works at the per-session cap",
                code == 0
                and db.get_timer_by_key(db.connect(), "CAP-SID", "cap0")
                    ["interval_min"] == 45)
    # The cap counts SELF-registered rows only. Operator rows added in the `t`
    # overlay carry key='' - five of them must not lock the session out of
    # self-scheduling (and must not get it told to `relay timer rm` a human's
    # timer to make room).
    _opc = db.connect()
    for i in range(5):
        db.add_timer(_opc, iterm_session_id="OPCAP-SID", label="relay",
                     interval_min=5, payload=f"operator {i}", mode="idle")
    code, _, err = run_cli("timer", "add", "--key", "mine", "--every", "20",
                           "--times", "5", "--say", "x",
                           iterm_id="w0t1p0:OPCAP-SID")
    ok &= check("5 operator rows (key='') do not block self-registration",
                code == 0
                and db.get_timer_by_key(db.connect(), "OPCAP-SID", "mine")
                    is not None)
    ok &= check("operator rows survive a self-registration next to them",
                len(db.list_timers(db.connect(), "OPCAP-SID")) == 6)

    # Interval clamps to [1, 90]; cap clamps to [1, 50]. Both bounds.
    run_cli("timer", "add", "--key", "clamped", "--every", "999",
            "--times", "999", "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "clamped")
    ok &= check("timer add clamps interval to 90 and cap to 50",
                r["interval_min"] == 90 and r["max_fires"] == 50)

    run_cli("timer", "add", "--key", "lowclamp", "--every", "0",
            "--times", "1", "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "lowclamp")
    ok &= check("timer add clamps interval up to the 1m floor",
                r["interval_min"] == 1 and r["max_fires"] == 1)

    # A junk --every (e.g. a typo'd unit like "60m") must be REJECTED, not
    # silently clamped to the 1-minute floor - a silent clamp would make a
    # plausible typo 60x more aggressive than intended.
    code, _, err = run_cli("timer", "add", "--key", "junk", "--every", "abc",
                           "--times", "5", "--say", "x",
                           iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "junk")
    ok &= check("timer add rejects a junk interval instead of clamping to 1",
                code == 1 and r is None and "--every" in err)

    code, _, err = run_cli("timer", "add", "--key", "junk2", "--every", "60m",
                           "--times", "5", "--say", "x",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects a unit-suffixed --every (e.g. '60m')",
                code == 1 and "--every" in err)

    # Guards. Missing flags must produce OUR message, not argparse's, so the
    # session learns why the flag exists.
    code, _, err = run_cli("timer", "add", "--every", "20", "--times", "5",
                           "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    ok &= check("missing --key gives the teaching error",
                code == 1 and "--key" in err and "instead of" in err)

    code, _, err = run_cli("timer", "add", "--key", "nocap", "--every", "20",
                           "--say", "x", iterm_id="w0t1p0:TIMER-SID")
    ok &= check("missing --times gives the teaching error",
                code == 1 and "--times" in err)

    code, _, err = run_cli("timer", "add", "--key", "capless", "--every", "20",
                           "--times", "0", "--say", "x",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects --times 0", code == 1 and "cap" in err.lower())

    code, _, err = run_cli("timer", "add", "--key", "BadKey!", "--every", "20",
                           "--times", "5", "--say", "x",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects a malformed key", code == 1 and "key" in err.lower())

    code, _, err = run_cli("timer", "add", "--key", "empty", "--every", "20",
                           "--times", "5", "--say", "   ",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer add rejects an empty payload", code == 1)

    # Newlines are collapsed so a payload can never submit early.
    run_cli("timer", "add", "--key", "multi", "--every", "20", "--times", "5",
            "--say", "line one\nline two", iterm_id="w0t1p0:TIMER-SID")
    r = db.get_timer_by_key(db.connect(), "TIMER-SID", "multi")
    ok &= check("timer add sanitizes newlines out of the payload",
                "\n" not in r["payload"] and r["payload"] == "line one line two")

    # A long inline payload warns but still succeeds.
    code, out, err = run_cli("timer", "add", "--key", "longish", "--every", "20",
                             "--times", "5", "--say", "y" * 250,
                             iterm_id="w0t1p0:TIMER-SID")
    ok &= check("long inline payload warns but succeeds",
                code == 0 and ".relay/prompts" in (out + err))

    # No iTerm identity at all -> clean error, not a traceback.
    code, _, err = run_cli("timer", "add", "--key", "k", "--every", "5",
                           "--times", "5", "--say", "x", iterm_id="")
    ok &= check("timer add without ITERM_SESSION_ID errors cleanly",
                code == 1 and "ITERM_SESSION_ID" in err)
    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"

    # --- self-scheduling: list + rm -------------------------------------
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer list shows this session's timers",
                code == 0 and "pr-duty" in out and "clamped" in out)

    # A different tab sees none of them.
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:STRANGER-SID")
    ok &= check("timer list is scoped to the calling session",
                code == 0 and "pr-duty" not in out)

    # _timer_line's fires-left must be clamped at 0 like timers.fires_left -
    # reachable via the overlay's '[' key lowering max_fires below fire_count.
    over_id = db.add_timer(db.connect(), iterm_session_id="OVERCAP-SID",
                           label="", interval_min=5, payload="p", mode="idle",
                           max_fires=2)
    db.update_timer(db.connect(), over_id, fire_count=5, max_fires=1)
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:OVERCAP-SID")
    ok &= check("timer list clamps fires-left at 0, never negative",
                code == 0 and "-1 left" not in out and "0 left" in out)

    # rm by key.
    code, _, _ = run_cli("timer", "rm", "--key", "clamped",
                         iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer rm --key deletes it",
                code == 0
                and db.get_timer_by_key(db.connect(), "TIMER-SID", "clamped") is None)

    # rm by id, from the wrong session, must refuse.
    victim = db.get_timer_by_key(db.connect(), "TIMER-SID", "pr-duty")
    code, _, err = run_cli("timer", "rm", "--id", str(victim["id"]),
                           iterm_id="w0t1p0:STRANGER-SID")
    ok &= check("timer rm cannot touch another session's timer",
                code == 1
                and db.get_timer_by_key(db.connect(), "TIMER-SID", "pr-duty") is not None)

    # rm of something that isn't there.
    code, _, err = run_cli("timer", "rm", "--key", "ghost",
                           iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer rm on a missing key errors cleanly", code == 1)

    # rm by id, from the owning session, works.
    code, _, _ = run_cli("timer", "rm", "--id", str(victim["id"]),
                         iterm_id="w0t1p0:TIMER-SID")
    ok &= check("timer rm --id works for the owner",
                code == 0
                and db.get_timer_by_key(db.connect(), "TIMER-SID", "pr-duty") is None)

    # Empty list is a friendly message, not a crash.
    code, out, _ = run_cli("timer", "list", iterm_id="w0t1p0:EMPTY-SID")
    ok &= check("timer list with no timers says so",
                code == 0 and "no timers" in out.lower())
    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"

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

    # recap: subcommand parses/dispatches; the command itself is read-only.
    args = cli.build_parser().parse_args(["recap", "--all"])
    ok &= check("recap subcommand dispatches to cmd_recap",
                args.fn is cli.cmd_recap and args.all is True)
    args2 = cli.build_parser().parse_args(["recap"])
    ok &= check("recap defaults to today (all=False)", args2.all is False)
    code, out, _ = run_cli("recap")
    ok &= check("recap exits 0 and reports today's tally",
                code == 0 and "relay recap (today)" in out and "tasks:" in out)
    code, out, _ = run_cli("recap", "--all")
    ok &= check("recap --all reports all time", code == 0
                and "relay recap (all time)" in out)

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

    # --- reserved names are blocked system-wide, not just at cmd_register ---
    # relay spawn never checks args.name itself; it goes through
    # spawn.spawn_worker -> db.register directly. The stubbed _fake_spawn
    # above calls the real db.register, so this exercises the db.register
    # guard (not a cli.py-level one) - without it, `relay spawn --name human`
    # would create a live session literally named 'human', and the watcher
    # would inject the operator's escalations straight into it.
    code, out, err = run_cli("spawn", "watch the PRs", "--name", "human",
                             "--project", "webshop", iterm_id="w0t0p0:CO-ID")
    ok &= check("spawn --name human fails instead of creating a session",
                code == 1 and db.get_session(conn, "human") is None)
    ok &= check("spawn --name human error explains the reservation",
                "reserved" in err)

    # --- spawn --worktree -----------------------------------------------------
    import subprocess
    repo = os.path.join(tempfile.mkdtemp(), "webshop")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "root"], check=True)

    code, _, err = run_cli("spawn", "go", "--name", "wt1", "--worktree",
                           iterm_id="w0t0p0:CO-ID")
    ok &= check("--worktree requires --dir", code == 1 and "--dir" in err)

    code, _, err = run_cli("spawn", "go", "--name", "bad/name", "--worktree",
                           "--dir", repo, iterm_id="w0t0p0:CO-ID")
    ok &= check("--worktree refuses path-y names", code == 1
                and "simple --name" in err)

    nogit = tempfile.mkdtemp()
    code, _, err = run_cli("spawn", "go", "--name", "wt1", "--worktree",
                           "--dir", nogit, iterm_id="w0t0p0:CO-ID")
    ok &= check("--worktree needs a git repo", code == 1
                and "not a git repository" in err)

    code, out, _ = run_cli("spawn", "go", "--name", "wt1", "--project",
                           "webshop", "--worktree", "--dir", repo,
                           iterm_id="w0t0p0:CO-ID")
    wt = os.path.join(os.path.dirname(repo), "webshop-wt1")
    ok &= check("worktree created + spawned there", code == 0
                and os.path.isdir(wt) and spawn_calls[-1]["workdir"] == wt)
    branches = subprocess.run(["git", "-C", repo, "branch", "--list",
                               "relay/wt1"], capture_output=True, text=True)
    ok &= check("branch relay/wt1 exists", "relay/wt1" in branches.stdout)
    ok &= check("worktree_repo recorded",
                db.get_session(conn, "wt1")["worktree_repo"] == repo)

    code, _, err = run_cli("spawn", "go", "--name", "wt1", "--worktree",
                           "--dir", repo, iterm_id="w0t0p0:CO-ID")
    ok &= check("existing worktree path refused", code == 1)

    # spawn failure AFTER worktree creation cleans the worktree back up
    real_fake = spawnmod.spawn_worker

    async def _boom(*a, **k):
        raise RuntimeError("iterm2 exploded")

    spawnmod.spawn_worker = _boom
    code, out, err = run_cli("spawn", "go", "--name", "wtfail", "--project",
                             "webshop", "--worktree", "--dir", repo,
                             iterm_id="w0t0p0:CO-ID")
    wtf = os.path.join(os.path.dirname(repo), "webshop-wtfail")
    ok &= check("failed spawn -> error surfaced", code == 1
                and "spawn failed" in err)
    ok &= check("failed spawn -> worktree cleaned up",
                not os.path.exists(wtf))
    spawnmod.spawn_worker = real_fake

    # --- wipe removes clean worktrees, keeps dirty ones -----------------------
    # second worktree worker, made dirty
    run_cli("spawn", "go", "--name", "wt2", "--project", "webshop",
            "--worktree", "--dir", repo, iterm_id="w0t0p0:CO-ID")
    wt2 = os.path.join(os.path.dirname(repo), "webshop-wt2")
    with open(os.path.join(wt2, "uncommitted.txt"), "w") as f:
        f.write("wip")
    # both must be CLOSED to be wipe candidates
    import time as _t
    db.mark_closed(conn, "wt1", _t.time())
    db.mark_closed(conn, "wt2", _t.time())

    code, out, _ = run_cli("wipe", "wt1", "wt2", "--project", "webshop",
                           "--dry-run")
    ok &= check("dry-run plans removal + keep", code == 0
                and "remove worktree" in out and "uncommitted" in out)
    ok &= check("dry-run removed nothing",
                os.path.isdir(wt) and os.path.isdir(wt2))

    code, out, _ = run_cli("wipe", "wt1", "wt2", "--project", "webshop",
                           "--yes")
    ok &= check("wipe removed clean worktree", code == 0
                and not os.path.exists(wt))
    ok &= check("wipe kept dirty worktree", os.path.isdir(wt2))
    branches = subprocess.run(["git", "-C", repo, "branch", "--list",
                               "relay/wt1"], capture_output=True, text=True)
    ok &= check("branch relay/wt1 deleted", "relay/wt1" not in branches.stdout)

    # --- relay demo (guided 60s tour of the whole loop) -----------------------
    # Hermetic lock: never read the developer's real ~/.relay/relay.lock.
    os.environ["RELAY_LOCK"] = os.path.join(tempfile.mkdtemp(), "relay.lock")
    code, out, _ = run_cli("demo", iterm_id="w0t7p0:DEMO-ID")
    ok &= check("demo registers coordinator + spawns worker", code == 0
                and db.get_session(conn, "demo-coord") is not None
                and db.get_session(conn, "demo-w1") is not None
                and spawn_calls[-1]["name"] == "demo-w1"
                and spawn_calls[-1]["arm"] == "wild")
    demo_tasks = db.list_tasks(conn, project="demo")
    ok &= check("demo assigns the haiku task to the worker",
                len(demo_tasks) == 1 and demo_tasks[0]["owner"] == "demo-w1"
                and "haiku" in demo_tasks[0]["title"])
    ok &= check("demo queues the worker's wake-up",
                any(m["kind"] == "wake"
                    for m in db.undelivered(conn, "demo-w1")))
    ok &= check("demo warns when the panel is not running",
                "panel is not running" in out)
    ok &= check("demo prints the cleanup line",
                "relay wipe --project demo --all --yes" in out)

    # --- update --auto (quiet start-up self-update) ---------------------------
    origin = os.path.join(tempfile.mkdtemp(), "origin")
    subprocess.run(["git", "init", "-q", origin], check=True)
    subprocess.run(["git", "-C", origin, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "v1"], check=True)
    clone = os.path.join(tempfile.mkdtemp(), "clone")
    subprocess.run(["git", "clone", "-q", origin, clone], check=True)
    subprocess.run(["git", "-C", origin, "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-q", "--allow-empty",
                    "-m", "v2"], check=True)
    stamp = os.path.join(tempfile.mkdtemp(), "update-check")
    os.environ["RELAY_UPDATE_STAMP"] = stamp
    real_root = cli._repo_root
    cli._repo_root = lambda: clone
    try:
        code, out, _ = run_cli("update", "--auto")
        ok &= check("auto update fast-forwards + says so",
                    code == 0 and "updated" in out)
        heads = [subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout
                 for d in (origin, clone)]
        ok &= check("clone is at origin HEAD", heads[0] == heads[1])
        ok &= check("auto check wrote the throttle stamp",
                    os.path.exists(stamp))

        code, out, _ = run_cli("update", "--auto")
        ok &= check("second auto check throttled, silent",
                    code == 0 and out == "")

        os.remove(stamp)
        code, out, _ = run_cli("update", "--auto")
        ok &= check("up to date -> silent", code == 0 and out == "")

        os.remove(stamp)
        os.environ["RELAY_NO_AUTOUPDATE"] = "1"
        code, out, _ = run_cli("update", "--auto")
        ok &= check("RELAY_NO_AUTOUPDATE=1 skips entirely",
                    code == 0 and out == "" and not os.path.exists(stamp))
        del os.environ["RELAY_NO_AUTOUPDATE"]

        # dirty checkout: auto stays silent and touches nothing
        subprocess.run(["git", "-C", origin, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-q", "--allow-empty",
                        "-m", "v3"], check=True)
        with open(os.path.join(clone, "wip.txt"), "w") as f:
            f.write("wip")
        code, out, _ = run_cli("update", "--auto")
        ok &= check("dirty checkout -> auto silent, no update",
                    code == 0 and out == "")
        os.remove(os.path.join(clone, "wip.txt"))
    finally:
        cli._repo_root = real_root
        os.environ.pop("RELAY_UPDATE_STAMP", None)
        os.environ.pop("RELAY_NO_AUTOUPDATE", None)

    # --- relay pr set / claim / list ----------------------------------------
    run_cli("register", "--name", "api-worker", "--role", "worker",
            "--project", "webshop", iterm_id="w0t1p0:PR-ID")

    rc, out, err = run_cli("pr", "set", "acme/api#482", "--state", "changes",
                         "--title", "Add rate limiting")
    ok &= check("pr set exits 0", rc == 0)
    ok &= check("pr set confirms the ref and state",
                "acme/api#482" in out and "changes" in out)

    rc, out, err = run_cli("pr", "set", "acme/api#482", "--state", "sideways")
    ok &= check("pr set rejects an unknown state with usage exit 2", rc == 2)

    rc, out, err = run_cli("pr", "set", "acme/api", "--state", "changes")
    ok &= check("pr set rejects a malformed ref", rc == 1)
    ok &= check("the malformed-ref error teaches the format",
                "owner/name#number" in err)

    rc, out, err = run_cli("pr", "claim", "acme/api#482", "--task", "14")
    ok &= check("pr claim exits 0", rc == 0)
    ok &= check("pr claim names the owner it recorded", "api-worker" in out)

    rc, out, err = run_cli("pr", "list")
    ok &= check("pr list shows the PR, its state and its owner",
                "acme/api#482" in out and "changes" in out
                and "api-worker" in out)
    ok &= check("pr list shows the age of the report next to the state",
                "ago" in out or "s " in out)

    run_cli("pr", "set", "acme/bff#77", "--state", "review")
    rc, out, err = run_cli("pr", "list")
    ok &= check("an unclaimed PR is listed and marked UNCLAIMED",
                "acme/bff#77" in out and "UNCLAIMED" in out)

    rc, out, err = run_cli("pr", "list", "--mine")
    ok &= check("--mine hides PRs this session did not claim",
                "acme/api#482" in out and "acme/bff#77" not in out)

    # --- relay send --pr ----------------------------------------------------
    # pr-sweep gets its OWN iterm id, distinct from api-worker's (PR-ID): two
    # session rows must never share one id, or whoami()'s
    # get_by_iterm_id (no ORDER BY) resolves "me" non-deterministically.
    run_cli("register", "--name", "pr-sweep", "--role", "coordinator",
            "--project", "webshop", iterm_id="w0t9p0:SWEEP-ID")

    # Every send below runs AS pr-sweep. iterm_id is set explicitly on this
    # first call only; it stays ambient (ITERM_SESSION_ID) for the rest of
    # the block, since nothing else in the block changes it.
    rc, out, err = run_cli("send", "--pr", "acme/api#482",
                         "changes requested: tighten the rate limit test",
                         iterm_id="w0t9p0:SWEEP-ID")
    ok &= check("routing to a live claiming session exits 0", rc == 0)
    ok &= check("success names the resolved owner", "api-worker" in out)

    rc, out, err = run_cli("send", "--pr", "acme/bff#77", "please fix")
    ok &= check("an unclaimed PR exits 3", rc == 3)
    ok &= check("the unclaimed error names the ref", "acme/bff#77" in err)

    rc, out, err = run_cli("send", "--pr", "acme/nope#1", "please fix")
    ok &= check("a PR relay never heard of also exits 3", rc == 3)

    # Rebind api-worker's NAME to a different tab (not pr-sweep's id, which
    # is untouched), then route again.
    _rebind("api-worker", "SID-OTHER")
    rc, out, err = run_cli("send", "--pr", "acme/api#482", "please fix")
    ok &= check("a rebound owner exits 4, not 0", rc == 4)
    ok &= check("the owner-gone error explains why", "rebound" in err)

    rc, out, err = run_cli("send", "--pr", "acme/api", "please fix")
    ok &= check("a malformed ref is a plain user error (exit 1)", rc == 1)

    # --- relay send --human (still as pr-sweep) ------------------------------
    rc, out, err = run_cli("send", "--human", "PR 77 is unclaimed - who owns it?")
    ok &= check("send --human exits 0", rc == 0)
    ok &= check("the human escalation is stored undelivered as an escalation",
                _one_message(to_name="human")["kind"] == "escalation"
                and _one_message(to_name="human")["delivered_at"] is None)

    rc, out, err = run_cli("register", "--name", "human", "--role", "worker")
    ok &= check("'human' cannot be registered as a session name", rc == 1)
    ok &= check("the reserved-name error explains what human is",
                "reserved" in err)

    rc, out, err = run_cli("send", "--human", "--pr", "acme/api#482", "both")
    ok &= check("two target forms at once is an error", rc != 0)

    # --- doctor reports PR health ---
    rc, out, err = run_cli("doctor")
    ok &= check("doctor exits 0", rc == 0)
    ok &= check("doctor reports PR counts", "PULL REQUESTS" in out)
    ok &= check("doctor surfaces PRs that cannot be routed",
                "unclaimed" in out.lower() or "UNCLAIMED" in out)
    ok &= check("doctor flagged rows show age of state report",
                " ago " in out)

    # --- relay help ------------------------------------------------------
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

    # Restore the file's ambient identity so tests defined after this block
    # (bin/relay verb-routing check) are unaffected.
    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"

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

    # --- relay join: re-joining must never move a live session off its
    # project (regression) ---------------------------------------------------
    # A restored worker reclaims its identity by re-running `relay join
    # <name>` with no --project. That must keep the project it was already
    # on, not whatever _default_project happens to resolve to right now -
    # which can change the moment a second project shows up in the active
    # set. Prove it by manufacturing exactly that: one session on a known
    # project, a second session on a DIFFERENT project (so the active-project
    # count is no longer 1), then re-join the first with no --project.
    rc, out, err = run_cli("join", "proj-keeper", "--project", "webshop",
                           iterm_id="w0t4p0:DDDD-4444")
    ok &= check("proj-keeper joins webshop explicitly", rc == 0
                and _sess("proj-keeper")["project"] == "webshop")

    rc, out, err = run_cli("join", "other-proj-worker", "--project",
                           "otherproj", iterm_id="w0t5p0:EEEE-5555")
    ok &= check("other-proj-worker joins a different project", rc == 0)

    rc, out, err = run_cli("join", "proj-keeper", iterm_id="w0t6p0:FFFF-6666")
    ok &= check("re-joining without --project keeps the existing project "
                "instead of falling back to the cwd basename",
                rc == 0 and _sess("proj-keeper")["project"] == "webshop")

    os.environ["ITERM_SESSION_ID"] = "w0t1p0:AAAA-1111"

    # --- cli._default_project (unit-level, isolated DB) ---------------------
    # The end-to-end join tests above run against a shared temp DB that
    # already holds several distinct active projects by this point, so they
    # only ever exercise the "several projects -> cwd fallback" branch - they
    # would still pass even if the "exactly one active project" branch were
    # deleted outright. Test _default_project directly against a DB whose
    # session set is fully controlled.
    _dp_path = os.path.join(tempfile.mkdtemp(), "default-project.db")
    dpconn = db.connect(_dp_path)
    cwd_base = os.path.basename(os.getcwd())

    ok &= check("_default_project falls back to cwd basename with zero "
                "active projects", cli._default_project(dpconn) == cwd_base)

    db.register(dpconn, "dp-a", "sid-dp-a", "worker", "solo-project")
    ok &= check("_default_project returns the project when exactly one is "
                "active", cli._default_project(dpconn) == "solo-project")

    db.register(dpconn, "dp-b", "sid-dp-b", "worker", "second-project")
    ok &= check("_default_project falls back to cwd basename with two or "
                "more active projects",
                cli._default_project(dpconn) == cwd_base)

    db.mark_closed(dpconn, "dp-b", time.time())
    ok &= check("_default_project excludes closed sessions from the active "
                "count", cli._default_project(dpconn) == "solo-project")

    dpconn.close()

    # --- bin/relay routes every CLI verb ---------------------------------
    # bin/relay dispatches on a hardcoded case list; a verb missing from it does
    # NOT error, it falls through and launches a second TUI. That is how `relay
    # timer add` shipped broken once - every other test calls cli.main() in
    # process and never touches the launcher. This compares the two lists.
    launcher = os.path.join(os.path.dirname(__file__), "..", "bin", "relay")
    with open(launcher) as f:
        launcher_src = f.read()
    m = re.search(r"^\s*((?:[a-z]+\|)+[a-z]+)\)\s*$", launcher_src, re.M)
    routed = set(m.group(1).split("|")) if m else set()
    verbs = set()
    for act in cli.build_parser()._subparsers._group_actions:
        verbs |= set(act.choices)
    missing = sorted(verbs - routed)
    ok &= check(f"every cli.py verb is routed by bin/relay (missing: {missing})",
                bool(routed) and not missing)

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
