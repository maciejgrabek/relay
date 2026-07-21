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
    MS = app.mascot_state
    chk("alarmed beats everything",
        MS("☢ CRITICAL", alarmed=True, working=True) == "alarmed")
    chk("critical beats working",
        MS("☢ CRITICAL", alarmed=False, working=True) == "critical")
    chk("working beats idle", MS("◷ WARM", alarmed=False, working=True)
        == "working")
    chk("idle otherwise", MS("STABLE", alarmed=False, working=False) == "idle")

    F = app.mascot_face_big
    f_alarm = F(0, "☢ CRITICAL", alarmed=True, working=True)
    chk("alarmed face has wide eyes + alert",
        any("⊙" in l for l in f_alarm) and any("‼" in l for l in f_alarm))
    chk("critical face has x eyes",
        any("x  x" in l for l in F(0, "☢ CRITICAL", alarmed=False,
                                   working=False)))
    chk("working face flickers between ticks",
        F(0, "◷ WARM", alarmed=False, working=True)
        != F(1, "◷ WARM", alarmed=False, working=True))
    chk("idle blinks periodically",
        F(8, "STABLE", alarmed=False, working=False)
        != F(1, "STABLE", alarmed=False, working=False))
    chk("face is banner-height", len(f_alarm) == 6)

    comp = app.banner_with_face(0, "STABLE", alarmed=False, working=False)
    chk("banner keeps the logo and gains the face",
        "██████╗" in comp and "╭" in comp
        and len(comp.splitlines()) == len(app.BANNER.splitlines()))

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
