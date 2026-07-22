"""Tests for pure recap aggregation. No iTerm2, no file I/O.

Run: python3 iterm/test_recap.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import recap  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    entries = [
        {"ts": 100.0, "verdict": "auto-approved"},
        {"ts": 150.0, "verdict": "auto-approved"},
        {"ts": 150.0, "verdict": "escalated"},
        {"ts": 200.0, "verdict": "delivered"},
        {"ts": 50.0,  "verdict": "auto-approved"},   # before window
        {"ts": 160.0, "verdict": "would-approve"},   # dry-run, not counted
        {"bogus": 1},                                # garbled, skipped
    ]
    s = recap.summarize(entries, since=100.0)
    ok &= check("cleared counts auto-approved in window", s["cleared"] == 2)
    ok &= check("woke counts escalated", s["woke"] == 1)
    ok &= check("delivered counts delivered", s["delivered"] == 1)

    empty = recap.summarize([], since=0.0)
    ok &= check("empty log -> zeros",
                empty == {"cleared": 0, "woke": 0, "delivered": 0})
    ok &= check("start_of_today is a float epoch",
                isinstance(recap.start_of_today(), float)
                and recap.start_of_today() > 0)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
