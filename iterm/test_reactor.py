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
    f_alarm = F(0, "☢ CRITICAL", awaiting=2, working=True)
    chk("alarmed face: wide eyes, beacon, and the COUNT in its bubble",
        any("⊙" in l for l in f_alarm) and any("‼" in l for l in f_alarm)
        and any("2 need you" in l for l in f_alarm))
    chk("alarmed singular grammar",
        any("1 needs you" in l
            for l in F(0, "STABLE", awaiting=1)))
    chk("alarmed shakes on odd ticks",
        F(1, "STABLE", awaiting=1)[1].startswith(" ╭")
        and F(0, "STABLE", awaiting=1)[1].startswith("  ╭"))
    f_crit = F(0, "☢ CRITICAL")
    chk("critical face: x eyes, static screen, radioactive beacon",
        any("x  x" in l for l in f_crit) and any("░" in l for l in f_crit)
        and any("☢" in l for l in f_crit)
        and any("CRITICAL" in l for l in f_crit))
    chk("critical static rolls between ticks",
        F(0, "☢ CRITICAL") != F(1, "☢ CRITICAL"))
    f_work = F(0, "◷ WARM", working=True)
    chk("working face: focused eyes, screen dot, spark, verb bubble",
        any("◕" in l for l in f_work) and any("·" in l for l in f_work)
        and any("⌁" in l for l in f_work)
        and any(v in l for l in f_work
                for v in app.MASCOT_WORKING_PHRASES))
    chk("working screen dot marches",
        F(0, "◷ WARM", working=True) != F(1, "◷ WARM", working=True))
    chk("working verb rotates over time",
        [v for v in app.MASCOT_WORKING_PHRASES
         if any(v in l for l in F(16, "◷ WARM", working=True))]
        != [v for v in app.MASCOT_WORKING_PHRASES
            if any(v in l for l in F(0, "◷ WARM", working=True))])
    chk("idle blinks periodically and makes small talk",
        F(0, "STABLE") != F(1, "STABLE")
        and any(p in l for l in F(1, "STABLE")
                for p in app.MASCOT_IDLE_PHRASES))
    chk("idle small talk rotates",
        any("watching the fleet" in l for l in F(48, "STABLE")))
    chk("face is banner-height", len(f_alarm) == 6)

    comp = app.banner_with_face(1, "STABLE")
    chk("banner keeps the logo and gains the colored face",
        "██████╗" in comp and "╭" in comp
        and app._MASCOT_COLOR["idle"] in comp
        and len(comp.splitlines()) == len(app.BANNER.splitlines()))
    chk("alarmed banner wears the awaiting amber",
        app._MASCOT_COLOR["alarmed"]
        in app.banner_with_face(0, "STABLE", awaiting=1))

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
