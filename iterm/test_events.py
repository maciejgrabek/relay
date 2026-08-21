"""Tests for the event seam: envelope shape, retention prune, and the
never-changes-behaviour contract on emit().

Run: python3 iterm/test_events.py
Uses a temp file via RELAY_EVENTS_LOG so it never touches the real ~/.relay log.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))


def run():
    tmp = tempfile.mkdtemp()
    os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "events.jsonl")
    os.environ["RELAY_EVENTS_RETENTION_DAYS"] = "7"
    # import AFTER env is set so module-level paths pick it up
    import events
    import importlib
    importlib.reload(events)

    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    NOW = 1_000_000_000.0   # fixed clock

    # --- emit writes one parseable line, and returns None ------------------
    r = events.emit("gate.escalated", session="api-worker",
                    session_id="w0t1", title="Relay - api-worker",
                    message="DANGEROUS_COMMAND: terraform apply", now=NOW)
    check("emit returns None (no caller may branch on it)", r is None)

    lines = open(events.EVENTS_PATH).read().strip().splitlines()
    check("one line written", len(lines) == 1)
    e = json.loads(lines[0])
    check("envelope has every top-level key",
          set(e) == {"v", "ts", "kind", "session", "session_id",
                     "title", "message", "data"})
    check("envelope version is 1", e["v"] == events.ENVELOPE_VERSION == 1)
    check("kind round-trips", e["kind"] == "gate.escalated")
    check("session round-trips", e["session"] == "api-worker")
    check("data defaults to {}", e["data"] == {})

    # --- data carries kind-specific fields ---------------------------------
    events.emit("task.done", data={"count": 3}, now=NOW)
    e2 = json.loads(open(events.EVENTS_PATH).read().strip().splitlines()[-1])
    check("data carries kind-specific fields", e2["data"] == {"count": 3})

    # --- field caps ---------------------------------------------------------
    events.emit("gate.escalated", session="s" * 500, message="m" * 5000,
                title="t" * 500, now=NOW)
    e3 = json.loads(open(events.EVENTS_PATH).read().strip().splitlines()[-1])
    check("session capped at 200", len(e3["session"]) == 200)
    check("message capped at 500", len(e3["message"]) == 500)
    check("title capped at 200", len(e3["title"]) == 200)

    # --- an unrecognised kind is still WRITTEN (not a runtime gate) --------
    events.emit("something.new", now=NOW)
    e4 = json.loads(open(events.EVENTS_PATH).read().strip().splitlines()[-1])
    check("unknown kind is written, not dropped", e4["kind"] == "something.new")
    check("VALID_KINDS has the eight stage-1 kinds",
          len(events.VALID_KINDS) == 8
          and "gate.escalated" in events.VALID_KINDS
          and "extreme.exhausted" in events.VALID_KINDS)

    # --- an unwritable path must not raise and must not stop the caller ----
    # NOTE: a merely-missing directory is NOT unwritable - _Lock's _ensure_dir
    # calls makedirs(exist_ok=True) and the write would succeed. Put a regular
    # FILE where a directory needs to be, so makedirs raises NotADirectoryError.
    blocker = os.path.join(tmp, "iam-a-file")
    open(blocker, "w").write("not a directory")
    os.environ["RELAY_EVENTS_LOG"] = os.path.join(blocker, "sub", "e.jsonl")
    importlib.reload(events)
    reached = False
    events.emit("gate.escalated", now=NOW)
    reached = True
    check("emit on an unwritable path neither raises nor blocks", reached)
    check("nothing was written to the unwritable path",
          not os.path.exists(events.EVENTS_PATH))

    # --- retention prune ----------------------------------------------------
    os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "events.jsonl")
    importlib.reload(events)
    events.emit("task.done", session="old", now=NOW - 8 * 86400)
    dropped = events.prune_old(now=NOW)
    check("prune dropped exactly the 8-day-old entry", dropped == 1)
    sessions = [json.loads(l).get("session")
                for l in open(events.EVENTS_PATH).read().strip().splitlines()]
    check("old entry gone, recent kept",
          "old" not in sessions and bool(sessions))
    check("second prune drops 0", events.prune_old(now=NOW) == 0)

    # --- corruption is evidence: prune PRESERVES unparseable lines ---------
    with open(events.EVENTS_PATH, "a") as f:
        f.write("this is not json{{{\n")
    before = open(events.EVENTS_PATH).read().splitlines()
    events.prune_old(now=NOW)
    after = open(events.EVENTS_PATH).read().splitlines()
    check("malformed line preserved by prune",
          "this is not json{{{" in after and len(after) == len(before))

    # --- an entry with no usable ts is kept (can't prove it is old) --------
    with open(events.EVENTS_PATH, "a") as f:
        f.write(json.dumps({"kind": "task.done", "session": "noTs"}) + "\n")
    events.prune_old(now=NOW + 999 * 86400)
    kept = [json.loads(l).get("session")
            for l in open(events.EVENTS_PATH).read().splitlines()
            if l.strip().startswith("{")]
    check("entry without ts is kept", "noTs" in kept)

    # --- file channel can be switched off -----------------------------------
    os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "off.jsonl")
    importlib.reload(events)
    events.configure(file_enabled=False)
    events.emit("task.done", now=NOW)
    check("file=false writes nothing", not os.path.exists(events.EVENTS_PATH))
    events.configure(file_enabled=True)

    # --- configure never raises, even with bad input -------------------------
    # configure(post_url=42): non-string truthy value should not raise
    try:
        events.configure(post_url=42)
        reached_post_url_int = True
    except Exception:
        reached_post_url_int = False
    check("configure(post_url=42) does not raise", reached_post_url_int)
    check("configure(post_url=42) coerces to string", isinstance(events._post_url, str))

    # configure(post_body=None): should not raise and should default to minimal
    try:
        events.configure(post_body=None)
        reached_post_body_none = True
    except Exception:
        reached_post_body_none = False
    check("configure(post_body=None) does not raise", reached_post_body_none)
    check("configure(post_body=None) yields 'minimal'", events._post_body == "minimal")

    # configure with valid inputs should still apply all fields correctly
    events.configure(file_enabled=False, post_url="http://example.com",
                     post_body="full", retention_days=14.0)
    check("valid configure sets file_enabled", events._file_enabled is False)
    check("valid configure sets post_url", events._post_url == "http://example.com")
    check("valid configure sets post_body", events._post_body == "full")
    check("valid configure sets RETENTION_DAYS", events.RETENTION_DAYS == 14.0)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
