"""Tests for the burn detector's decision (pure logic).

Run: python3 iterm/test_burn.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import burn  # noqa: E402
from burn import Track, sample  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    W = 15.0            # window, minutes
    MIN = 60.0
    FP = "treehash-1"

    def go(track, now, **kw):
        """sample() with the healthy-burning defaults, overridable per case."""
        args = dict(fp=FP, turns=0, out=0, working=True, shared=False,
                    attended=False, window_min=W)
        args.update(kw)
        return sample(track, now, args["fp"], args["turns"], args["out"],
                      args["working"], args["shared"], args["attended"],
                      args["window_min"])

    # --- a fresh Track cannot burn before a full window ----------------------
    t, v = go(Track(), 0.0, turns=0)
    ok &= check("first sight anchors, claims nothing", not v.burning)
    ok &= check("anchor records the fingerprint", t.fp == FP and t.since == 0.0)
    t2, v = go(t, 14 * MIN, turns=30)
    ok &= check("one minute short does not burn", not v.burning)

    # --- the main case -------------------------------------------------------
    t3, v = go(t, 15 * MIN, turns=30, out=40000)
    ok &= check("unchanged tree past the window burns", v.burning)
    ok &= check("evidence counts from the anchor, not lifetime",
                v.turns == 30 and v.spent == 40000)
    ok &= check("quiet_for is the elapsed time", v.quiet_for == 15 * MIN)
    ok &= check("no reason when it is burning", v.reason == "")

    # --- the turn floor: one long thinking turn is not a loop ----------------
    _, v = go(t, 60 * MIN, turns=burn.MIN_TURNS - 1, out=90000)
    ok &= check("under the turn floor does not burn", not v.burning)
    _, v = go(t, 60 * MIN, turns=burn.MIN_TURNS, out=90000)
    ok &= check("exactly the turn floor burns", v.burning)

    # --- a moving tree re-arms BOTH counters ---------------------------------
    moved, v = go(t, 10 * MIN, fp="treehash-2", turns=20, out=50000)
    ok &= check("a changed fingerprint claims nothing", not v.burning)
    ok &= check("...and re-anchors the clock", moved.since == 10 * MIN)
    ok &= check("...and re-anchors the turn count", moved.turns_at == 20)
    ok &= check("...and re-anchors the token count", moved.out_at == 50000)
    _, v = go(moved, 24 * MIN, fp="treehash-2", turns=40, out=90000)
    ok &= check("the clock restarted from the change", not v.burning)
    _, v = go(moved, 25 * MIN, fp="treehash-2", turns=40, out=90000)
    ok &= check("and fires a full window after it", v.burning)

    # --- idle: judged at the moment, never accumulated -----------------------
    _, v = go(t, 30 * MIN, turns=30, working=False)
    ok &= check("an idle session never burns", not v.burning)
    ok &= check("...and says why", v.reason == "idle")
    # The load-bearing one: a healthy session flips working/idle every turn.
    # If idle re-armed the anchor, this could never accumulate.
    tick = t
    for i in range(1, 31):
        tick, v = go(tick, i * MIN, turns=i * 2, working=(i % 2 == 0))
    ok &= check("alternating working/idle still accumulates", v.burning)

    # --- the two silences ----------------------------------------------------
    _, v = go(t, 30 * MIN, turns=30, shared=True)
    ok &= check("a shared workdir never burns", not v.burning)
    ok &= check("...and says shared", v.reason == "shared")

    att, v = go(t, 30 * MIN, turns=30, attended=True)
    ok &= check("a selected tab never burns", not v.burning)
    ok &= check("...and says attended", v.reason == "attended")
    ok &= check("...and holds the clock at zero", att.since == 30 * MIN)
    ok &= check("...and re-anchors its counters too", att.turns_at == 30)
    _, v = go(att, 44 * MIN, turns=60)
    ok &= check("the countdown starts when you leave", not v.burning)
    _, v = go(att, 45 * MIN, turns=60)
    ok &= check("...and fires one window later", v.burning)

    # --- missing inputs ------------------------------------------------------
    _, v = go(Track(), 30 * MIN, fp="", turns=30)
    ok &= check("no fingerprint never burns", not v.burning)
    ok &= check("...and says no-git", v.reason == "no-git")
    _, v = go(t, 30 * MIN, turns=None)
    ok &= check("no usage never burns", not v.burning)
    ok &= check("...and says no-usage", v.reason == "no-usage")

    # --- off -----------------------------------------------------------------
    _, v = go(t, 99 * MIN, turns=99, window_min=0)
    ok &= check("window 0 never burns", not v.burning)
    ok &= check("...and says off", v.reason == "off")

    # --- the evidence line ---------------------------------------------------
    _, v = go(t, 22 * MIN, turns=18, out=85200)
    ok &= check("evidence names all three numbers",
                burn.evidence(v) == "22m unchanged, 18 turns, 85.2k out")

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
