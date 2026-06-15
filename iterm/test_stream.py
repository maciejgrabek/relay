"""Frame-SEQUENCE stress harness - the test type that catches the bug class
that single-snapshot tests (test_gates) miss: behavior of the watcher over a
stream of evolving screens.

Every live bug in this project was a "screen changes over time" bug:
  - stale prompt scrolled into history read as live
  - alert spam while a question's text churns
  - inject spam while a prompt redraws
  - back-to-back distinct prompts: second one stuck

Those are invisible to a test that feeds one frame. Here we replay a SEQUENCE of
frames through the real Watcher._handle (with a fake iTerm2 session) and assert
on CUMULATIVE effects: how many Enters were sent, how many alerts fired.

Adding a scenario is a few lines - that's the point. As new churn/timing bugs
turn up in real use, encode them here so they can't come back.

Run: python3 iterm/test_stream.py        (or -v for per-frame trace)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import watcher as W  # noqa: E402

VERBOSE = "-v" in sys.argv


# ---- frame builders: cheap, readable screens -------------------------------

def prompt(cmd="echo hi", cursor=1, churn=0, yes="Yes", no="No"):
    """A 'Do you want to proceed?' permission prompt. `churn` appends drifting
    trailing dots to the cursor line to simulate a redraw between polls. `cursor`
    is which option carries the ❯ (1 or 2)."""
    o1 = f"{'❯' if cursor == 1 else ' '} 1. {yes}" + ("." * (churn % 3) if cursor == 1 else "")
    o2 = f"{'❯' if cursor == 2 else ' '} 2. {no}" + ("." * (churn % 3) if cursor == 2 else "")
    return [" Bash command", "", f"   {cmd}", "   summary line", "",
            "Do you want to proceed?", o1, o2]


def question(churn=0):
    """A real multi-choice question (no proceed-marker). Must NEVER auto-answer."""
    return ["Which approach should we take?",
            f"❯ 1. Rewrite the parser{'.' * (churn % 3)}",
            "  2. Patch the existing one", "  3. Leave it"]


def working():
    return ["⏺ doing the thing", "· Manifesting… (1m 2s · ↓ 2.8k tokens)", "❯",
            "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt"]


def idle():
    return ["~/Work/relay", "❯", "  ⏵⏵ accept edits on (shift+tab to cycle)"]


def stale_prompt_while_working():
    """An already-answered prompt still in scrollback while a spinner runs."""
    return ["Do you want to proceed?", "❯ 1. Yes", "  2. No",
            "⏺ Yes - proceeding, working now...",
            "· Crunching… (2m · ↓ 5k tokens)", "❯", "  esc to interrupt"]


# ---- a frame is (screen, repeat_count): poll this screen N times in a row ---
def rep(screen, n):
    return [(screen, n)]


# ---- the replay engine -----------------------------------------------------

class _FakeSession:
    def __init__(self):
        self.sent = []

    async def async_send_text(self, t):
        self.sent.append(t)


async def run_scenario(name, mode, frames, expect_injects, expect_alerts):
    """frames: list of (screen, repeat) pairs, replayed in order. Returns
    (ok, injects, alerts)."""
    # Stub side effects; count alerts.
    alerts = {"n": 0}
    W.notify_mac = lambda *a, **k: alerts.__setitem__("n", alerts["n"] + 1)
    W.audit.record = lambda *a, **k: True
    from watcher import Watcher, SessionInfo

    w = Watcher(connection=None, dry_run=False)
    w.notify_cooldown = 0  # exercise the prompt_id logic, not the time backstop
    fs = _FakeSession()
    info = SessionInfo("s", title=name, _iterm_session=fs, mode=mode)
    w.sessions["s"] = info

    poll = 0
    for screen, count in frames:
        for _ in range(count):
            poll += 1
            await w._handle(info, list(screen), [True] * len(screen))
            if VERBOSE:
                print(f"    poll {poll:2} state={info.state:9} "
                      f"injects={len(fs.sent)} alerts={alerts['n']}")

    injects, got_alerts = len(fs.sent), alerts["n"]
    ok = injects == expect_injects and got_alerts == expect_alerts
    flag = "PASS" if ok else "FAIL"
    print(f"{flag} [{mode:6}] {name}: injects={injects} (exp {expect_injects}), "
          f"alerts={got_alerts} (exp {expect_alerts})")
    return ok


# ---- scenarios: (name, mode, frames, expect_injects, expect_alerts) --------
# This is the catalogue. Each line is a real over-time behavior we care about.
SCENARIOS = [
    # A single prompt redrawing 8x must approve exactly once (churn).
    ("churn one prompt", "insane",
     [(prompt(churn=i), 1) for i in range(8)], 1, 0),

    # No-command prompt churning (the p:-id case that used to spam).
    ("churn no-command prompt", "insane",
     [(["Do you want to proceed?", f"❯ 1. Yes{'.'*(i%3)}", "  2. No"], 1)
      for i in range(8)], 1, 0),

    # Back-to-back DISTINCT prompts, no working frame between -> each approves.
    ("back-to-back A,B,C", "insane",
     rep(prompt("echo A"), 1) + rep(prompt("echo B"), 1) + rep(prompt("echo C"), 1),
     3, 0),

    # HALF-RENDERED prompt: marker appears, then menu draws over 2 polls, then
    # complete. Must NOT flash a 'cursor not on option 1' escalation - it waits,
    # then approves exactly once when the menu is complete. (The transient-block
    # bug: a flash of 'blocked' during quick prompts.)
    ("prompt rendering then approve", "insane",
     rep([" Bash command", "", "   echo A", "", "Do you want to proceed?"], 1)
     + rep([" Bash command", "", "   echo A", "", "Do you want to proceed?", "❯ 1. Yes"], 1)
     + rep(prompt("echo A"), 2), 1, 0),

    # Realistic: prompt -> work -> prompt -> work, each prompt distinct.
    ("prompt/work cycling", "insane",
     rep(prompt("echo A"), 2) + rep(working(), 2)
     + rep(prompt("echo B"), 2) + rep(working(), 2), 2, 0),

    # Stale prompt visible while working -> NOT acted on, no inject, no alert.
    ("stale prompt while working", "insane",
     rep(stale_prompt_while_working(), 5), 0, 0),

    # A real QUESTION churning -> never auto-answered in ANY mode; with cooldown
    # 0 each distinct churn frame alerts (escalation), but ZERO injects.
    ("question churn never injects", "insane",
     [(question(churn=i), 1) for i in range(6)], 0, None),  # alerts: don't care

    # SAFE mode: a dangerous prompt repeated -> never injects, alerts (once;
    # stable id debounces the repeats).
    ("safe: dangerous prompt", "safe",
     rep(prompt("git push --force"), 5), 0, 1),

    # SAFE mode: a safe prompt repeated -> approve once.
    ("safe: safe prompt", "safe",
     rep(prompt("grep foo src"), 5), 1, 0),

    # WILD: dangerous prompt -> approves (ignores danger), once despite repeats.
    ("wild: dangerous prompt once", "wild",
     rep(prompt("git push --force"), 5), 1, 0),

    # OFF (manual): nothing ever injected or alerted, however it churns.
    ("off: never acts", "off",
     rep(prompt("echo A"), 3) + rep(question(), 3), 0, 0),

    # Idle the whole time -> nothing.
    ("idle only", "insane", rep(idle(), 5), 0, 0),
]


async def main():
    ok = True
    for name, mode, frames, ei, ea in SCENARIOS:
        if ea is None:
            # "don't care" on alerts: only assert injects.
            alerts = {"n": 0}
            W.notify_mac = lambda *a, **k: alerts.__setitem__("n", alerts["n"] + 1)
            W.audit.record = lambda *a, **k: True
            from watcher import Watcher, SessionInfo
            w = Watcher(connection=None, dry_run=False)
            w.notify_cooldown = 0
            fs = _FakeSession()
            info = SessionInfo("s", title=name, _iterm_session=fs, mode=mode)
            w.sessions["s"] = info
            for screen, count in frames:
                for _ in range(count):
                    await w._handle(info, list(screen), [True] * len(screen))
            res = len(fs.sent) == ei
            print(f"{'PASS' if res else 'FAIL'} [{mode:6}] {name}: "
                  f"injects={len(fs.sent)} (exp {ei}), alerts={alerts['n']} (any)")
            ok = ok and res
        else:
            ok = await run_scenario(name, mode, frames, ei, ea) and ok
    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
