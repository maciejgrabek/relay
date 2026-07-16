"""Tests for the pure status-bar label. No iTerm2 (the live half is in watcher).

Run: python3 iterm/test_statusbar.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from statusbar import label, MODE_CIRCLE  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True

    # per-mode: circle + RELAY:<mode>
    ok &= check("off", label("off") == f"{MODE_CIRCLE['off']} RELAY:off")
    ok &= check("safe", label("safe") == f"{MODE_CIRCLE['safe']} RELAY:safe")
    ok &= check("wild", label("wild") == f"{MODE_CIRCLE['wild']} RELAY:wild")
    ok &= check("insane",
                label("insane") == f"{MODE_CIRCLE['insane']} RELAY:insane")
    ok &= check("unknown mode falls back to off circle",
                label("bogus").startswith(MODE_CIRCLE["off"]))

    # the four circles are distinct (so color-by-mode is real)
    ok &= check("four distinct circles", len(set(MODE_CIRCLE.values())) == 4)

    # own panel tab: neutral, non-mode badge
    ok &= check("own panel badge", label("safe", own_panel=True) == "⬛ RELAY: panel")
    ok &= check("own panel ignores swarm name",
                label("insane", own_panel=True, name="coord",
                      role="coordinator") == "⬛ RELAY: panel")

    # swarm session: name + short role appended
    ok &= check("swarm worker appends name + short role",
                label("safe", name="bff-worker", role="worker")
                == f"{MODE_CIRCLE['safe']} RELAY:safe · bff-worker (work)")
    ok &= check("swarm coordinator short role",
                label("wild", name="coord", role="coordinator")
                == f"{MODE_CIRCLE['wild']} RELAY:wild · coord (coord)")
    ok &= check("name without role -> just name",
                label("safe", name="w1") == f"{MODE_CIRCLE['safe']} RELAY:safe · w1")
    ok &= check("no name -> bare mode",
                label("off") == f"{MODE_CIRCLE['off']} RELAY:off")

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
