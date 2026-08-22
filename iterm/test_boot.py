"""Tests for the boot screen: the strike, the POST block, and the plug point.

Run: python3 iterm/test_boot.py
Pure module - no Textual, no iTerm2, no clock. Every frame is asserted at a
given tick and terminal size.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

import boot  # noqa: E402

PAL = {"bright": "#3aff7a", "accent": "#2fc866", "dim": "#2a7d4f",
       "dimmer": "#1d5c38", "warn": "#ffb000", "danger": "#ff5555",
       "cyan": "#41ffd0", "hot": "#6effa0"}

TAGS = re.compile(r"\[/?[^\]]*\]")


def plain(frame):
    """The frame with markup stripped - what the operator actually sees.

    An ESCAPED bracket (`\\[`) is not markup: Textual renders it as a literal
    '['. Protect those before stripping tags, or this helper would eat exactly
    the text the escaping exists to preserve."""
    protected = frame.replace(r"\[", "\x00")
    return TAGS.sub("", protected).replace("\x00", "[")


def run():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    def steps(n_done, total=3):
        return [boot.step(f"Subsystem {i}",
                          f"value {i}" if i < n_done else None)
                for i in range(total)]

    # --- the strike is clock-driven and monotonic --------------------------
    r0, c0 = boot.strike_state(0)
    check("strike starts unrevealed", r0 == 0.0)
    check("strike starts at the dimmest palette step", c0 == "dimmer")
    prev = -1.0
    for t in range(0, boot.STRIKE_TICKS + 1):
        rev, _ = boot.strike_state(t)
        if rev < prev:
            prev = -99.0
            break
        prev = rev
    check("strike reveal never goes backwards", prev >= 0.0)
    rN, cN = boot.strike_state(boot.STRIKE_TICKS)
    check("strike completes fully lit", rN == 1.0 and cN == "bright")
    check("strike stays complete past its window",
          boot.strike_state(boot.STRIKE_TICKS + 50) == (1.0, "bright"))

    # --- a partly-struck logo is genuinely partial -------------------------
    early = plain(boot.render(steps(0), tick=3, cols=100, rows=40, pal=PAL))
    late = plain(boot.render(steps(0), tick=boot.STRIKE_TICKS, cols=100,
                             rows=40, pal=PAL))
    check("logo is present once fully struck", "██████╗" in late)
    check("an early frame differs from the struck frame", early != late)

    # --- the POST block reports what it was given --------------------------
    f = plain(boot.render(steps(2), tick=20, cols=100, rows=40, pal=PAL))
    check("a reported step shows its value", "value 0" in f and "value 1" in f)
    check("an unreported step shows a spinner, not a value",
          "value 2" not in f and any(g in f for g in boot.SPIN))
    check("every label is rendered", all(f"Subsystem {i}" in f for i in range(3)))

    # --- the sign-off waits for every subsystem ----------------------------
    check("no welcome while a step is pending",
          boot.WELCOME not in plain(boot.render(steps(2), tick=20, cols=100,
                                                rows=40, pal=PAL)))
    check("welcome appears once all steps report",
          boot.WELCOME in plain(boot.render(steps(3), tick=20, cols=100,
                                            rows=40, pal=PAL)))
    check("finished() agrees with the rendered welcome",
          boot.finished(steps(3)) and not boot.finished(steps(2)))
    check("finished() is False with no steps at all", not boot.finished([]))

    # --- progress() shows without completing; report() completes -----------
    # The bug this pins: deriving `done` from `value` froze the memory counter
    # on its first frame, because the first digits written looked like a
    # finished result and nothing ever incremented again.
    counting = boot.step("Memory Test")
    counting.progress("26215K")
    check("progress() puts a value on screen", counting.value == "26215K")
    check("progress() does NOT complete the step", not counting.done)
    check("a counting step blocks the welcome", not boot.finished([counting]))
    f_mid = plain(boot.render([counting], tick=5, cols=100, rows=40, pal=PAL))
    check("a counting step renders its count, not a spinner",
          "26215K" in f_mid and not any(g in f_mid for g in boot.SPIN))
    counting.progress("52430K")
    check("progress() can be called again", counting.value == "52430K")
    counting.report("262144K  OK")
    check("report() completes the step",
          counting.done and counting.value == "262144K  OK")
    check("a reported step releases the welcome", boot.finished([counting]))

    # a step built with a value is already done; built without one is not
    check("step() with a value starts done", boot.step("X", "v").done)
    check("step() without a value starts pending", not boot.step("X").done)

    # --- markup in a value must not become a tag ---------------------------
    s = [boot.step("Command", "rm [dangerous] /tmp", "warn")]
    raw = boot.render(s, tick=20, cols=100, rows=40, pal=PAL)
    check("a '[' in a value is escaped, not opened as markup",
          r"\[dangerous]" in raw)
    check("the escaped value still reads correctly",
          "dangerous" in plain(raw))

    # --- geometry: it must survive any terminal --------------------------
    for cols, rows in ((20, 10), (60, 24), (200, 60), (5, 3)):
        fr = boot.render(steps(3), tick=20, cols=cols, rows=rows, pal=PAL)
        check(f"renders a string at {cols}x{rows}", isinstance(fr, str) and fr)
    wide = plain(boot.render(steps(3), tick=20, cols=200, rows=40, pal=PAL))
    narrow = plain(boot.render(steps(3), tick=20, cols=60, rows=40, pal=PAL))
    w_indent = min(len(l) - len(l.lstrip())
                   for l in wide.splitlines() if "██" in l)
    n_indent = min(len(l) - len(l.lstrip())
                   for l in narrow.splitlines() if "██" in l)
    check("the logo is centred - a wider terminal indents it more",
          w_indent > n_indent)

    # --- the plug point ----------------------------------------------------
    check("bios is a registered style", "bios" in boot.BOOT_STYLES)
    check("the default style is registered",
          boot.DEFAULT_STYLE in boot.BOOT_STYLES)
    check("an unknown style falls back instead of raising",
          boot.render(steps(3), tick=20, cols=100, rows=40, pal=PAL,
                      style="no-such-style")
          == boot.render(steps(3), tick=20, cols=100, rows=40, pal=PAL))

    # a style added to the registry is reachable without touching render()
    boot._STYLES["probe-only"] = lambda st, **kw: "PROBE STYLE"
    try:
        check("a newly registered style is dispatched to",
              boot.render(steps(3), tick=1, cols=100, rows=40, pal=PAL,
                          style="probe-only") == "PROBE STYLE")
    finally:
        del boot._STYLES["probe-only"]

    # --- palette independence: no hardcoded colours ------------------------
    amber = dict(PAL, bright="#ffb000", accent="#c87f2f", dim="#7d5a2a",
                 dimmer="#5c3f1d", cyan="#ffd041", hot="#ffa06e")
    a = boot.render(steps(3), tick=20, cols=100, rows=40, pal=amber)
    check("frames carry the caller's palette, not a baked-in one",
          "#ffb000" in a and "#3aff7a" not in a)
    check("a partial palette does not raise",
          isinstance(boot.render(steps(1), tick=2, cols=80, rows=30,
                                 pal={"dim": "#2a7d4f"}), str))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
