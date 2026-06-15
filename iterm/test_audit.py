"""Tests for the audit log: append + 7-day retention prune.

Run: python3 iterm/test_audit.py
Uses a temp file via RELAY_AUDIT_LOG so it never touches the real ~/.relay log.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))


def run():
    tmp = tempfile.mkdtemp()
    os.environ["RELAY_AUDIT_LOG"] = os.path.join(tmp, "audit.jsonl")
    os.environ["RELAY_AUDIT_RETENTION_DAYS"] = "7"
    # import AFTER env is set so module-level paths pick it up
    import audit
    import importlib
    importlib.reload(audit)

    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    NOW = 1_000_000_000.0  # fixed clock (no Date.now in this env anyway)

    # record() now RETURNS True on a durable write
    r = audit.record("auto-approved", "tabA", "grep foo", "safe", now=NOW)
    check("record returns True on success", r is True)
    audit.record("escalated", "tabB", "git push --force", "dangerous", now=NOW)
    # an OLD one, 8 days before NOW -> should be pruned
    audit.record("auto-approved", "tabC", "old cmd", "safe", now=NOW - 8 * 86400)

    lines = open(audit.AUDIT_PATH).read().strip().splitlines()
    check("3 entries written", len(lines) == 3)
    first = json.loads(lines[0])
    check("entry has verdict/session/command/reason",
          {"verdict", "session", "command", "reason", "ts"} <= set(first))

    # prune relative to NOW: the 8-day-old one drops
    dropped = audit.prune_old(now=NOW)
    check("prune dropped exactly 1", dropped == 1)
    lines2 = open(audit.AUDIT_PATH).read().strip().splitlines()
    check("2 entries remain", len(lines2) == 2)
    sessions = {json.loads(l)["session"] for l in lines2}
    check("old tabC pruned, tabA/tabB kept", sessions == {"tabA", "tabB"})

    # prune again is a no-op
    check("second prune drops 0", audit.prune_old(now=NOW) == 0)

    # PRESERVE corruption: a malformed line must survive prune, not be erased.
    with open(audit.AUDIT_PATH, "a") as f:
        f.write("this is not json{{{\n")
    before = open(audit.AUDIT_PATH).read().splitlines()
    audit.prune_old(now=NOW)
    after = open(audit.AUDIT_PATH).read().splitlines()
    check("malformed line preserved by prune",
          "this is not json{{{" in after and len(after) == len(before))

    # missing-ts entry is KEPT (can't prove it's old)
    with open(audit.AUDIT_PATH, "a") as f:
        f.write(json.dumps({"verdict": "escalated", "session": "noTs"}) + "\n")
    audit.prune_old(now=NOW + 999 * 86400)   # far future: would prune anything datable
    kept_sessions = [json.loads(l).get("session")
                     for l in open(audit.AUDIT_PATH).read().splitlines()
                     if l.strip().startswith("{")]
    check("entry without ts is kept", "noTs" in kept_sessions)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
