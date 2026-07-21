"""Tests for the reactor pressure model + temperature bands.

Run: python3 iterm/test_reactor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import app  # noqa: E402
from watcher import SessionInfo  # noqa: E402


def S(**k):
    return SessionInfo("x", **k)


def run():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    P = app.reactor_pressure

    # Idle + manual -> zero pressure -> STABLE.
    chk("idle/manual = 0 pressure", P([S(mode="off", state="idle")]) == 0.0)
    chk("0 -> STABLE", app.reactor_band(0.0)[0] == "STABLE")

    # Hotter modes contribute more.
    chk("insane > wild > safe",
        P([S(mode="insane", state="idle")])
        > P([S(mode="wild", state="idle")])
        > P([S(mode="safe", state="idle")]) > 0)

    # An unhandled blocked session is the single biggest contributor.
    chk("blocked dominates",
        P([S(mode="safe", state="blocked")]) > P([S(mode="insane", state="working")]))

    # A held prompt is the same "human is the bottleneck" state as blocked.
    chk("prompting weighs like blocked",
        P([S(mode="safe", state="prompting")])
        == P([S(mode="safe", state="blocked")]))

    # A stale armed session is unaccounted risk, not calm.
    st = S(mode="wild", state="working")
    st.stale = True
    chk("stale adds heat",
        P([st]) > P([S(mode="wild", state="working")]))

    # Pressure scales with fleet size.
    one = P([S(mode="insane", state="working")])
    three = P([S(mode="insane", state="working") for _ in range(3)])
    chk("more armed sessions = more pressure", three > one)

    # Bands are monotonic and hit each label.
    labels = [app.reactor_band(t)[0] for t in (0.0, 2.0, 5.0, 9.0)]
    chk("bands ascend STABLE/WARM/ELEVATED/CRITICAL",
        labels == ["STABLE", "◷ WARM", "⚠ ELEVATED", "☢ CRITICAL"])

    # CRITICAL pulses; lower bands don't.
    chk("CRITICAL pulses", app.reactor_band(9.0)[2] is True)
    chk("WARM does not pulse", app.reactor_band(2.0)[2] is False)

    # --- mascot: alarmed > critical > working > idle -------------------------
    M = app.mascot_frame
    chk("alarmed beats everything",
        "⊙" in M(0, "☢ CRITICAL", alarmed=True, working=True))
    chk("critical face", M(0, "☢ CRITICAL", alarmed=False, working=True)
        == "(x_x)")
    chk("working flickers",
        M(0, "◷ WARM", alarmed=False, working=True)
        != M(1, "◷ WARM", alarmed=False, working=True)
        and "◕" in M(0, "◷ WARM", alarmed=False, working=True))
    chk("idle mostly steady with a periodic blink",
        M(1, "STABLE", alarmed=False, working=False) == "(－‿－)"
        and M(8, "STABLE", alarmed=False, working=False) == "(￣‿￣)")

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
