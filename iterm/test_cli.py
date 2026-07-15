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

    conn.close()
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
