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

    # --- every registered style keeps the same contract --------------------
    # Written as a loop over the registry, not a list of style names: a style
    # added later is covered the day it is added, which is the only way a plug
    # point stays honest. What is asserted here is the reason the boot screen
    # is allowed to exist - it names what it is waiting on and it never claims
    # to be finished early. Decoration is each style's own business.
    for name in boot.BOOT_STYLES:
        def frame(st, tick=9, cols=100, rows=40):
            return boot.render(st, tick=tick, cols=cols, rows=rows, pal=PAL,
                               style=name)

        mid = plain(frame(steps(2)))
        check(f"{name}: names the subsystem it is waiting on",
              "Subsystem 2" in mid)
        check(f"{name}: shows the latest value a step reported",
              "value 1" in mid)
        check(f"{name}: never shows a value a step has not reported",
              "value 2" not in mid)
        check(f"{name}: no sign-off while a step is pending",
              boot.WELCOME not in mid)
        check(f"{name}: signs off once every step reports",
              boot.WELCOME in plain(frame(steps(3), tick=20)))
        check(f"{name}: renders with no steps at all",
              isinstance(frame([]), str))
        for cols, rows in ((20, 10), (60, 24), (80, 24), (200, 60), (5, 3)):
            fr = frame(steps(3), cols=cols, rows=rows)
            check(f"{name}: renders a string at {cols}x{rows}",
                  isinstance(fr, str) and fr)
            check(f"{name}: no line overruns {cols} columns at {rows} rows",
                  all(len(l) <= max(20, cols)
                      for l in plain(fr).splitlines()))

        # Markup discipline, asserted structurally rather than by example: the
        # ONLY tags a frame may contain are the palette colours _tag opens and
        # the closer it emits. Anything else - a literal '[  OK  ]' mark, a
        # path with a bracket in it - is text that must have been escaped, and
        # would otherwise vanish from the screen or raise on an unknown style.
        raw = frame([boot.step("Command", "rm [dangerous] /tmp", "warn"),
                     boot.step("Pending")])
        stray = [t for t in TAGS.findall(raw.replace(r"\[", "\x00"))
                 if t.strip("[]") not in set(PAL.values()) | {"/"}]
        check(f"{name}: emits no tag that is not a palette colour or a closer",
              not stray)
        check(f"{name}: a '[' in a value is escaped, not opened as markup",
              r"\[dangerous]" in raw and "dangerous" in plain(raw))

    # The styles that promise the WHOLE report, not just the latest line.
    # `minimal` is the deliberate exception and says so in its docstring: it
    # trades the full block for two lines, which is the reason to pick it. A
    # style added later belongs here unless it makes the same trade.
    for name in ("bios", "console", "crt"):
        f_all = plain(boot.render(steps(3), tick=20, cols=100, rows=40,
                                  pal=PAL, style=name))
        check(f"{name}: renders every label", 
              all(f"Subsystem {i}" in f_all for i in range(3)))
        check(f"{name}: renders every reported value",
              all(f"value {i}" in f_all for i in range(3)))

    # Style-specific promises, one each - the thing you would pick that style
    # FOR, so a refactor that quietly turns them all back into bios fails here.
    warned = [boot.step("Event Seam", "no post_url", "warn"),
              boot.step("Audit Log", "corrupt", "danger"),
              boot.step("Sessions", "4 found")]
    con = plain(boot.render(warned, tick=20, cols=100, rows=40, pal=PAL,
                            style="console"))
    check("console: a warn step is marked WARN, a danger step FAIL",
          "[ WARN ] Event Seam" in con and "[ FAIL ] Audit Log" in con)
    check("console: a healthy step is marked OK", "[  OK  ] Sessions" in con)
    check("console: anchors at the top, not the middle",
          con.splitlines()[0].strip().startswith("relay"))
    mini = plain(boot.render(steps(2), tick=9, cols=100, rows=40, pal=PAL,
                             style="minimal"))
    check("minimal: draws a progress bar with the work still to do",
          boot.BAR_ON in mini and boot.BAR_OFF in mini)
    check("minimal: stays small - no block logo",
          "██████╗" not in mini and boot.WORDMARK in mini)
    crt_mid = plain(boot.render(steps(2), tick=9, cols=100, rows=40, pal=PAL,
                                style="crt"))
    check("crt: uses dot leaders", "·····" in crt_mid)
    check("crt: keeps the logo", "██████╗" in crt_mid)
    # The scanline is a COLOUR sweep: the text of the frame must not move, or
    # the boot screen would be unreadable while it played.
    a = boot.render(steps(2), tick=9, cols=100, rows=40, pal=PAL, style="crt")
    b = boot.render(steps(2), tick=10, cols=100, rows=40, pal=PAL, style="crt")
    check("crt: the scanline changes colour without moving any text",
          a != b and plain(a).replace(boot.SPIN[9 % len(boot.SPIN)], "")
          == plain(b).replace(boot.SPIN[10 % len(boot.SPIN)], ""))

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
