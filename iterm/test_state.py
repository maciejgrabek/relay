"""Tests for detect_state - working vs idle from real Claude Code screens.

Fixtures are taken from actual iTerm2 API captures. The key trap: the
'⏵⏵ accept edits on (shift+tab to cycle)' footer is shown in EVERY session,
idle or not - only the '· esc to interrupt' suffix appears while working.

The second trap, and why detect_state is anchored rather than windowed:
Claude Code appends an UNBOUNDED number of task and agent rows below the
input box, and prints completed-tool rows carrying elapsed time and token
counts above it. A fixed tail (this used to be "the last 6 non-blank lines")
therefore slides off the footer on a busy session and sweeps up an ordinary
completed-tool row on a quiet one. Both were measured on live sessions:

    state=working  '  ⎿  Read 244 lines (2m 3s · ↓ 1.2k tokens)'
    state=working  '⏺ Task(reviewer) done (4m 11s · ↓ 12.4k tokens)'
    state=idle     '  ⎿  Wrote 40 lines'

detect_state's answer is ANDed onto all three of relay's typing paths
(_deliver, _fire_timers, _fire_extreme all require state == "idle"), so a
false "working" makes a session permanently undeliverable and also keeps
_fire_extreme zeroing its dwell anchor, and a false "idle" lets relay type
into a live turn.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from gates import detect_state  # noqa: E402

_RULE = "─" * 96
_SCREEN_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "screens")


def screen(name):
    """A captured screen as the watcher hands it to a predicate: non-blank
    lines, '#' header stripped. Same loader as test_swarm/test_extreme."""
    with open(os.path.join(_SCREEN_DIR, name + ".txt")) as f:
        return [l for l in f.read().splitlines()
                if l.strip() and not l.startswith("#")]


# Rows Claude Code appends BELOW the footer while subagents run, and the
# completed-tool row it prints ABOVE the box. None of these is a working
# signal on its own; the counted ones deliberately carry no elapsed time or
# token count, so they cannot smuggle a _WORKING_RE match into either screen.
def _appended(n):
    return [f"  ◻ Task {i}: audit the gate logic" if i % 2 else
            f"  ⎿  Wrote {40 + i} lines" for i in range(n)]


def _screen(*, working, appended=0):
    """A full screen the way the watcher captures it: scrollback (including a
    completed-tool row with a token count - the measured false-positive),
    then the rule/input-row/rule box, then the footer, then `appended` agent
    and task rows below it."""
    footer = ("  ⏵⏵ accept edits on (shift+tab to cycle) · "
              + ("esc to interrupt · " if working else "")
              + "← for agents")
    return (["⏺ Read(iterm/gates.py)",
             "  ⎿  Read 244 lines (2m 3s · ↓ 1.2k tokens)",
             _RULE, "❯", _RULE, footer]
            + _appended(appended))


# Screens with no rule line at all: a shell, and the bare tails that used to
# be this suite's whole fixture set (see TAIL_ONLY below).
_SHELL = ["~/Work/relay", "❯ ls -la", "total 48", "drwxr-xr-x  12 maciej"]

CASES = [
    # idle: bare prompt
    (["Resume this session with:", "claude --resume abc", "~/Work took 1d8h",
      "❯"], "idle"),
    (["s&code_challenge=XQ", "Successfully logged in", "✅ Done", "❯"], "idle"),
    # idle: a finished shell command sitting at the prompt
    (["set -a; source config/test.env; set +a", "❯ relay"], "idle"),
    # idle: the accept-edits footer ALONE (no interrupt hint) must NOT be
    # working
    (["some output", "❯", "  ⏵⏵ accept edits on (shift+tab to cycle)"], "idle"),
    # no rule line anywhere -> "idle", the conservative answer this function
    # has always given for a screen it cannot place (and the same answer
    # swarm.session_working gives with no rule to anchor on). Typing safety
    # does not rest on it: swarm.claude_prompt_ready independently requires
    # box chrome, which a screen with no rule cannot have.
    (_SHELL, "idle"),
]

# These three used to assert "working". They are screen TAILS with no input
# box and no rule - a shape the watcher never actually hands to detect_state,
# which always receives the whole reconstructed screen. Anchored on the first
# rule, they have nothing to anchor to and now read "idle". Recorded here as
# the accepted cost of dropping the fixed window, NOT quietly deleted: the
# real property they were protecting (a live spinner / interrupt footer means
# working) is covered by ANCHORED below and by the real captures, on full
# screens, where it is load-bearing.
TAIL_ONLY = [
    (["✢ Improvising… (2m 54s · ↓ 10.1k tokens)", "  ⎿ Build gate logic"],
     "idle"),
    (["· Marinating… (1m 2s · ↓ 2.8k tokens)"], "idle"),
    (["~/Work/relay", "❯",
      "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt · "
      "ctrl+t to hide tasks"], "idle"),
]

# The window bug, both directions, at four appended-row counts. 6 is where
# the old fixed window lost the footer entirely; 30 is an ordinary busy
# session running several subagents.
ANCHORED = []
for _n in (0, 6, 12, 30):
    ANCHORED.append((_screen(working=True, appended=_n), "working"))
    ANCHORED.append((_screen(working=False, appended=_n), "idle"))

# FIRST rule, not last: a subagent's own box border is free to render as a
# bare rule of its own BELOW the footer, and anchoring on the last rule would
# jump the scan past the footer and call a working session idle - the unsafe
# direction, and the same trap swarm.session_working documents.
ANCHORED.append((_screen(working=True, appended=3) + [_RULE, "  ◻ Task 9"],
                 "working"))

# Real captures, whole screens, through the same loader the other suites use.
FIXTURES = [
    ("working_with_agent_rows", "working"),
    ("working_accept_edits", "working"),
    ("working_manual_mode", "working"),
    ("idle_accept_edits", "idle"),
    ("idle_nbsp_row", "idle"),
]


def run():
    ok = True
    for lines, exp in CASES + TAIL_ONLY:
        got = detect_state(lines)
        flag = "PASS" if got == exp else "FAIL"
        if got != exp:
            ok = False
        print(flag, f"exp {exp:8} got {got:8} | {lines[-1][:55]!r}")

    for lines, exp in ANCHORED:
        got = detect_state(lines)
        flag = "PASS" if got == exp else "FAIL"
        if got != exp:
            ok = False
        n = len(lines) - 6
        print(flag, f"exp {exp:8} got {got:8} | {n:2d} rows appended below "
                    f"the footer")

    for name, exp in FIXTURES:
        got = detect_state(screen(name))
        flag = "PASS" if got == exp else "FAIL"
        if got != exp:
            ok = False
        print(flag, f"exp {exp:8} got {got:8} | capture {name}")

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


# --- the canary: relay must notice when it can no longer READ a screen -------
# Every "unrecognised" path in gates fails safe to idle / nothing-actionable.
# That is right per screen and wrong across a fleet: if Claude Code's chrome
# changes shape, every session reads as a calm idle tab and relay reports quiet
# while seeing nothing. This already happened once (2026-08-10), and the only
# symptom was sessions silently going undeliverable.
def _canary():
    from gates import blind, chrome_seen, ANCHORS
    ok = True

    def chk(label, cond):
        nonlocal ok
        print(("  OK   " if cond else " FAIL  ") + label)
        ok = ok and bool(cond)

    # EVERY real captured Claude frame must be readable. This is the assertion
    # that turns the fixture corpus into a regression tripwire: if a future
    # capture lands here and relay cannot see its box, the canary is doing its
    # job and the classifier needs updating - in that order.
    import glob
    claude_frames = [os.path.basename(p)[:-4]
                     for p in sorted(glob.glob(os.path.join(_SCREEN_DIR, "*.txt")))
                     if not os.path.basename(p).startswith("shell_")]
    chk("the fixture corpus is not empty (a canary with no corpus is a "
        "no-op that always passes)", len(claude_frames) >= 5)
    for name in claude_frames:
        chk(f"relay can read the captured frame '{name}'",
            not blind(screen(name)))
    # The box rule is the anchor blind() keys on, and the one every live
    # Claude tab draws. Spot-check it is actually what is being found, so this
    # cannot pass by accidentally matching a spinner in scrollback.
    chk("an idle frame is recognised by its input box, not by chance",
        "box" in chrome_seen(screen("idle_accept_edits")))

    # A screen with none of relay's chrome is the thing worth shouting about.
    chk("a screen with no Claude chrome at all is blind",
        blind(["$ ls -la", "total 48", "drwxr-xr-x  12 me  staff   384 Aug 13 09:12 ."]))
    # ...but a BLANK screen is not: a tab starting up or scrolled to empty has
    # nothing to recognise and nothing to be wrong about. A canary that cries
    # during startup is one the operator silences.
    chk("a blank screen is not reported as blind", not blind([]))
    chk("a whitespace-only screen is not reported as blind",
        not blind(["", "   ", "\x00\x00"]))

    # The anchors are diagnostic, so each must be independently detectable -
    # otherwise "which part of the chrome moved" collapses to one bit.
    chk("a permission prompt reports its own anchor",
        "prompt" in chrome_seen(["Do you want to proceed?"]))
    chk("an option menu reports its own anchor",
        "menu" in chrome_seen(["❯ 1. Yes", "  2. No"]))
    chk("every named anchor is reachable",
        set(ANCHORS) == {"box", "prompt", "menu", "working"})
    # The canary must never be able to change a decision - it is advisory, and
    # a diagnostic that alters behaviour is a second classifier nobody audits.
    import inspect
    import gates as _g
    chk("blind() is not consulted anywhere in the decision path",
        "blind(" not in inspect.getsource(_g.classify)
        and "blind(" not in inspect.getsource(_g.detect_state))
    return ok


if __name__ == "__main__":
    _a = run()
    print()
    _b = _canary()
    sys.exit(0 if (_a and _b) else 1)
