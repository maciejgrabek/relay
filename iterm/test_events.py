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

    # --- configure() applies a Config's [events] fields ---------------------
    os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "configured.jsonl")
    importlib.reload(events)
    import config
    cfg = config.Config(events_file=True, events_post_url="",
                        events_post_body="full", events_retention_days=2.0)
    events.configure(file_enabled=cfg.events_file,
                     post_url=cfg.events_post_url,
                     post_body=cfg.events_post_body,
                     retention_days=cfg.events_retention_days)
    check("configure applied retention_days", events.RETENTION_DAYS == 2.0)
    events.emit("task.done", session="cfg", now=NOW)
    check("configure(file_enabled=True) still writes",
          os.path.exists(events.EVENTS_PATH))
    # 3 days old with a 2-day retention -> pruned
    events.emit("task.done", session="stale", now=NOW - 3 * 86400)
    check("prune honours the configured retention",
          events.prune_old(now=NOW) == 1)

    # --- retention_days = 0 means NEVER PRUNE, not "wipe everything" -------
    # Every sibling duration in relay reads 0 as off (power.release_after = 0
    # is "never release", burn.window = 0 is off). Without the guard in
    # prune_old(), 0 gives cutoff == now and the arithmetic drops the entire
    # log, including rows written seconds ago.
    os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "never-prune.jsonl")
    importlib.reload(events)
    events.configure(file_enabled=True, post_url="", post_body="minimal",
                     retention_days=0.0)
    events.emit("task.done", session="ancient", now=NOW - 900 * 86400)
    events.emit("task.done", session="fresh", now=NOW)
    check("retention_days = 0 prunes nothing", events.prune_old(now=NOW) == 0)
    survivors = [json.loads(l).get("session")
                 for l in open(events.EVENTS_PATH).read().strip().splitlines()]
    check("retention_days = 0 keeps the ancient entry",
          "ancient" in survivors)
    check("retention_days = 0 keeps the fresh entry", "fresh" in survivors)

    # a negative value is the same story - config.py clamps it to 0, but the
    # module must be safe on its own for a direct RELAY_EVENTS_RETENTION_DAYS.
    events.configure(retention_days=-3.0)
    check("a negative retention_days prunes nothing too",
          events.prune_old(now=NOW) == 0
          and len(open(events.EVENTS_PATH).read().strip().splitlines()) == 2)

    os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "configured.jsonl")
    importlib.reload(events)
    events.configure(file_enabled=True, post_url="", post_body="minimal",
                     retention_days=2.0)

    # a bogus retention_days must not blow up configure()
    events.configure(retention_days="not a number")
    check("bogus retention_days leaves the previous value",
          events.RETENTION_DAYS == 2.0)
    events.configure(file_enabled=True, post_url="", post_body="minimal",
                     retention_days=7.0)

    # --- the POST channel ---------------------------------------------------
    calls = []

    class _FakeSub:
        """Stands in for the `subprocess` module inside events' namespace.

        Deliberately NOT `events.subprocess.Popen = fake` - that would mutate
        the real stdlib module object, which every other import in the process
        shares. Replacing the name in events' namespace touches only events."""
        DEVNULL = -3

        @staticmethod
        def Popen(argv, **kw):
            calls.append(argv)

    real_sub = events.subprocess
    events.subprocess = _FakeSub
    try:
        events.configure(file_enabled=False, post_url="", post_body="minimal")
        events.emit("gate.escalated", session="api-worker",
                    message="terraform apply -auto-approve", now=NOW)
        check("no post_url fires no subprocess", calls == [])

        events.configure(file_enabled=False,
                         post_url="https://ntfy.test/fleet",
                         post_body="minimal")
        events.emit("gate.escalated", session="api-worker",
                    title="Relay - api-worker",
                    message="terraform apply -auto-approve",
                    data={"secret": 1}, now=NOW)
        check("post_url fires exactly one subprocess", len(calls) == 1)
        argv = calls[0]
        check("argv is a fixed curl, no shell",
              argv[0] == "curl" and "-X" in argv and "POST" in argv)
        check("url is its own argv element, never interpolated",
              argv[-1] == "https://ntfy.test/fleet")
        check("a timeout is set", "-m" in argv)
        body = json.loads(argv[argv.index("-d") + 1])
        check("minimal carries v/ts/kind/session only",
              set(body) == {"v", "ts", "kind", "session"})
        check("minimal leaks no command text",
              "terraform" not in json.dumps(body))

        calls.clear()
        events.configure(file_enabled=False,
                         post_url="https://ntfy.test/fleet",
                         post_body="full")
        events.emit("gate.escalated", session="api-worker",
                    title="Relay - api-worker",
                    message="terraform apply -auto-approve", now=NOW)
        full = json.loads(calls[0][calls[0].index("-d") + 1])
        check("full carries the whole envelope",
              set(full) == {"v", "ts", "kind", "session", "session_id",
                            "title", "message", "data"})
        check("full does carry command text",
              "terraform" in full["message"])

        # a POST that explodes must not cost the file channel
        class _BoomSub:
            DEVNULL = -3

            @staticmethod
            def Popen(argv, **kw):
                raise OSError("curl missing")

        os.environ["RELAY_EVENTS_LOG"] = os.path.join(tmp, "both.jsonl")
        importlib.reload(events)          # reload resets events.subprocess
        events.subprocess = _BoomSub
        events.configure(file_enabled=True, post_url="https://ntfy.test/x",
                         post_body="minimal")
        events.emit("task.done", session="survivor", now=NOW)
        check("a failed POST does not cost the file write",
              "survivor" in open(events.EVENTS_PATH).read())
    finally:
        events.subprocess = real_sub
        events.configure(file_enabled=True, post_url="", post_body="minimal")

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
