"""Tests for tab-title prefix rendering/stripping (pure logic).

Run: python3 iterm/test_titles.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import titles  # noqa: E402
from titles import render, strip_prefix  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    N = "api-server"

    # --- the spec's render table, verbatim -----------------------------------
    ok &= check("glyphs: safe working", render("glyphs", "safe", "working", False, N) == f"◉ {N}")
    ok &= check("words: safe working", render("words", "safe", "working", False, N) == f"[SAFE] {N}")
    ok &= check("hybrid: safe working", render("hybrid", "safe", "working", False, N) == f"◉ {N}")

    ok &= check("glyphs: insane blocked", render("glyphs", "insane", "blocked", False, N) == f"✦⊘ {N}")
    ok &= check("words: insane blocked", render("words", "insane", "blocked", False, N) == f"[INSANE][BLOCKED] {N}")
    ok &= check("hybrid: insane blocked", render("hybrid", "insane", "blocked", False, N) == f"✦[BLOCKED] {N}")

    ok &= check("glyphs: safe prompting", render("glyphs", "safe", "prompting", False, N) == f"◉‼ {N}")
    ok &= check("words: safe prompting", render("words", "safe", "prompting", False, N) == f"[SAFE][AWAITING] {N}")
    ok &= check("hybrid: safe prompting", render("hybrid", "safe", "prompting", False, N) == f"◉[AWAITING] {N}")

    ok &= check("glyphs: safe stale", render("glyphs", "safe", "idle", True, N) == f"◉⧗ {N}")
    ok &= check("words: safe stale", render("words", "safe", "idle", True, N) == f"[SAFE][STALE] {N}")
    ok &= check("hybrid: safe stale", render("hybrid", "safe", "idle", True, N) == f"◉[STALE] {N}")

    ok &= check("glyphs: manual blocked", render("glyphs", "off", "blocked", False, N) == f"⊘ {N}")
    ok &= check("words: manual blocked", render("words", "off", "blocked", False, N) == f"[BLOCKED] {N}")
    ok &= check("hybrid: manual blocked", render("hybrid", "off", "blocked", False, N) == f"[BLOCKED] {N}")

    for style in ("glyphs", "words", "hybrid"):
        ok &= check(f"{style}: manual idle untouched",
                    render(style, "off", "idle", False, N) == N)
    ok &= check("off style: always bare",
                render("off", "insane", "blocked", True, N) == N)

    # State priority: blocked > prompting > stale (stale + blocked -> blocked).
    ok &= check("priority: blocked beats stale",
                render("hybrid", "safe", "blocked", True, N) == f"◉[BLOCKED] {N}")

    # wild mode glyph
    ok &= check("wild glyph", render("glyphs", "wild", "working", False, N) == f"▲ {N}")

    # --- strip_prefix ---------------------------------------------------------
    ok &= check("strip glyph cluster", strip_prefix(f"✦⊘ {N}") == N)
    ok &= check("strip word pair", strip_prefix(f"[INSANE][BLOCKED] {N}") == N)
    ok &= check("strip hybrid", strip_prefix(f"◉[AWAITING] {N}") == N)
    ok &= check("strip mode-only", strip_prefix(f"▲ {N}") == N)
    ok &= check("bare name untouched", strip_prefix(N) == N)
    ok &= check("user [WIP] title preserved", strip_prefix("[WIP] foo") == "[WIP] foo")
    ok &= check("empty title", strip_prefix("") == "")
    ok &= check("prefix-like glyph inside name kept",
                strip_prefix("api ◉ server") == "api ◉ server")
    ok &= check("user '? help' title preserved", strip_prefix("? help") == "? help")
    ok &= check("stale glyph round-trip",
                strip_prefix(render("glyphs", "off", "idle", True, "api")) == "api")

    # --- shadow mode ------------------------------------------------------
    ok &= check("shadow renders its glyph prefix",
                titles.MODE_GLYPH.get("shadow") == "◌"
                and "◌" in titles.render("glyphs", "shadow", "idle", False, "api"))
    ok &= check("shadow prefix is strippable (crash-safety)",
                titles.strip_prefix("◌ api") == "api")

    # --- round-trip property over the full input space ------------------------
    rt = True
    for style in ("glyphs", "words", "hybrid"):
        for mode in ("off", "safe", "wild", "insane"):
            for state in ("idle", "working", "prompting", "blocked", "cleared"):
                for stale in (False, True):
                    t = render(style, mode, state, stale, N)
                    if strip_prefix(t) != N:
                        print(f"  round-trip FAIL: {style}/{mode}/{state}/{stale} -> {t!r}")
                        rt = False
    ok &= check("round-trip: strip(render(...)) == bare for all combos", rt)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
