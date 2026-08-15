"""Tests for the caffeinate hold/release state machine (pure logic).

Run: python3 iterm/test_power.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from power import Power  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    MIN = 60.0

    # --- holds while anything is working, however long -----------------------
    p = Power(release_after=30.0)
    ok &= check("starts held", p.held and not p.manual)
    ok &= check("working keeps the assertion",
                p.tick(0.0, True) and p.tick(10 * 3600.0, True))
    ok &= check("working clears the idle anchor", p.idle_since is None)

    # --- fires at the boundary, not before -----------------------------------
    p = Power(release_after=30.0)
    p.tick(1000.0, False)
    ok &= check("idle anchors on first idle tick", p.idle_since == 1000.0)
    ok &= check("still held one second early",
                p.tick(1000.0 + 30 * MIN - 1, False) is True)
    ok &= check("released exactly at the boundary",
                p.tick(1000.0 + 30 * MIN, False) is False)
    ok &= check("an auto release is not manual", not p.manual)

    # --- auto release re-acquires the moment work resumes --------------------
    ok &= check("work re-acquires after an auto release",
                p.tick(2000.0 + 30 * MIN, True) is True)
    ok &= check("re-acquired state is held, unmanual", p.held and not p.manual)

    # --- manual release is sticky --------------------------------------------
    p = Power(release_after=30.0)
    p.tick(0.0, False)
    p.toggle(10.0)
    ok &= check("c releases", p.held is False)
    ok &= check("c marks the release manual", p.manual is True)
    ok &= check("work does NOT re-acquire after a manual release",
                p.tick(20.0, True) is False)
    ok &= check("still manual after work", p.manual is True)

    # --- c from either released state holds and restarts the clock -----------
    p.toggle(100.0)
    ok &= check("c takes the assertion back", p.held is True)
    ok &= check("taking it back clears the manual flag", p.manual is False)
    ok &= check("taking it back restarts the idle clock", p.idle_since == 100.0)
    ok &= check("so the timer can fire again later",
                p.tick(100.0 + 30 * MIN, False) is False)

    # --- 0 = never, but the key still works ----------------------------------
    p = Power(release_after=0.0)
    p.tick(0.0, False)
    ok &= check("release_after 0 never fires",
                p.tick(10 * 3600.0, False) is True)
    p.toggle(10 * 3600.0)
    ok &= check("release_after 0 still releases by hand", p.held is False)

    # --- an empty fleet counts as idle ---------------------------------------
    p = Power(release_after=5.0)
    p.tick(0.0, False)          # no sessions at all -> any_working is False
    ok &= check("empty fleet releases", p.tick(5 * MIN, False) is False)

    # --- status strings ------------------------------------------------------
    p = Power(release_after=30.0)
    p.tick(0.0, True)
    ok &= check("nothing to say while working", p.status(0.0) == "")
    p = Power(release_after=0.0)
    p.tick(0.0, False)
    ok &= check("nothing to say when the timer is off", p.status(0.0) == "")
    p = Power(release_after=30.0)
    p.tick(0.0, False)
    ok &= check("countdown rounds up to whole minutes",
                p.status(0.0) == "☕ releases in 30m")
    ok &= check("countdown counts down", p.status(29 * MIN) == "☕ releases in 1m")
    ok &= check("under a minute counts seconds",
                p.status(30 * MIN - 30) == "☕ releases in 30s")
    p.tick(30 * MIN, False)
    ok &= check("auto-released says the Mac may sleep",
                p.status(30 * MIN) == "☕ released · Mac may sleep")
    p2 = Power(release_after=30.0)
    p2.toggle(0.0)
    ok &= check("manually released names the key",
                p2.status(0.0) == "☕ released (c)")

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
