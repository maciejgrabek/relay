"""Tests for the swarm protocol text - the thing relay prints to teach a
session how to participate.

Run: python3 iterm/test_protocol.py    (no deps - has a __main__ runner)
 or: ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import protocol  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    p = protocol.SWARM_PROTOCOL

    # The four discipline rules are the whole reason this text exists. A
    # session that reads it and still goes silent has been failed by it.
    ok &= check("teaches that status is the heartbeat",
                "relay status" in p and "heartbeat" in p)
    ok &= check("teaches replying to the sender, not to an assumed coordinator",
                "sender" in p)
    ok &= check("teaches never ending a turn silent on a doing task",
                "silent" in p or "silence" in p)
    ok &= check("teaches escalating instead of guessing",
                "--human" in p or "escalation" in p)

    ok &= check("names the verbs a session actually needs",
                all(v in p for v in ("relay inbox", "relay send",
                                     "relay task update", "relay status")))
    ok &= check("does not assume a coordinator exists",
                "the coordinator" not in p.lower())

    ok &= check("PR topic covers claim and routing",
                "relay pr claim" in protocol.PR_PROTOCOL
                and "--pr" in protocol.PR_PROTOCOL)

    ok &= check("TOPICS exposes both topics",
                set(protocol.TOPICS) == {"swarm", "pr"})
    ok &= check("no em-dash anywhere in the protocol text",
                all("\u2014" not in t for t in protocol.TOPICS.values()))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
