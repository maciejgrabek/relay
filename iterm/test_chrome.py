"""Tests for panel/rail chrome (pure rendering).

Run: python3 iterm/test_chrome.py    or    ./test/run.sh
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import chrome  # noqa: E402
from chrome import (cells, panel, panel_bottom, panel_top, rail_bottom,  # noqa: E402
                    rail_group, rail_row, rail_top, rule, side)

TAG = re.compile(r"\[/?[^\[\]]*\]")


def plain(s: str) -> str:
    """Markup stripped, for asserting on what the eye sees."""
    return TAG.sub("", s)


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    W = 60

    # --- widths: the whole point of a box is that it lines up ----------------
    ok &= check("panel_top is exactly W cells",
                cells(panel_top("sessions", "6 units", W)) == W)
    ok &= check("panel_top with no right label is exactly W cells",
                cells(panel_top("sessions", "", W)) == W)
    ok &= check("panel_bottom is exactly W cells", cells(panel_bottom(W)) == W)
    ok &= check("side is exactly W cells", cells(side(" hello", W)) == W)
    ok &= check("side pads short content to W",
                cells(side("", W)) == W)

    # Colour must not change any width - this is the bug that eats box edges.
    ok &= check("colour does not change panel_top width",
                cells(panel_top("sessions", "6 units", W, "green", "bold green",
                                "dim")) == W)
    ok &= check("markup content does not change side width",
                cells(side(" [bold red]held[/] npm test", W, "green")) == W)
    ok &= check("wide glyphs are counted as the cells they take",
                cells(side(" 日本語のタイトル", W)) == W)

    # --- the four rules ------------------------------------------------------
    top = plain(panel_top("sessions", "6 units · 3 armed", W))
    ok &= check("rule 1: the title sits IN the top edge",
                top.startswith("┌─sessions "))
    ok &= check("rule 3: the summary sits on the right of the same edge",
                top.endswith("── 6 units · 3 armed ┐"))
    ok &= check("the edge between them is unbroken rule",
                set(top[len("┌─sessions "):top.index("── 6 units")]) == {"─"})

    # --- the open shape ------------------------------------------------------
    rt = plain(rail_top("~/Work/relay", "3 sessions", W))
    ok &= check("rail_top opens with the heavy-down joint", rt.startswith("┎─"))
    ok &= check("rail_top has NO right corner", not rt.endswith("┐"))
    ok &= check("rail_top carries its title", rt.startswith("┎─~/Work/relay "))
    ok &= check("rail_top carries its summary", rt.endswith("── 3 sessions "))
    ok &= check("rail_bottom uses the heavy-up joint",
                plain(rail_bottom(W)).startswith("┖─"))
    ok &= check("rail_row is one glyph and the row, nothing else",
                plain(rail_row(" api-server")) == "┃ api-server")
    ok &= check("rail_row does not pad, so no column has to agree with it",
                cells(rail_row(" x")) == 3)

    # --- section rules -------------------------------------------------------
    r = plain(rule("interactions", "last 7 days", W))
    ok &= check("a section rule opens with light rule, not a corner",
                r.startswith("── interactions "))
    ok &= check("a section rule is open on the right too",
                r.endswith("── last 7 days "))
    ok &= check("a section rule stays within its width", cells(rule("x", "", W)) <= W)

    # --- truncation: a title must never push the edge out --------------------
    long_title = "a-very-long-workspace-name-that-cannot-possibly-fit-here"
    narrow = panel_top(long_title, "9 units", 30)
    ok &= check("an over-long title is clipped, not overflowed",
                cells(narrow) == 30)
    ok &= check("a clipped title says it was clipped", "…" in plain(narrow))
    tight = plain(panel_top("sessions", "6 units · 3 armed", 22))
    ok &= check("the summary is dropped before the title is",
                "sessions" in tight and "6 units" not in tight)
    ok &= check("a panel too narrow for chrome renders bare lines",
                panel(["x"], "t", "", chrome.MIN_WIDTH - 1) == ["x"])

    # --- assembly ------------------------------------------------------------
    p = panel(["one", "two"], "feed", "5 of 200", W, "dim")
    ok &= check("panel is top + rows + bottom", len(p) == 4)
    ok &= check("every panel line is exactly W cells",
                all(cells(ln) == W for ln in p))
    ok &= check("panel content is indented off its own border",
                plain(p[1]).startswith("│ one"))

    g = rail_group(["a", "b"], "~/Work/relay", "2 sessions", W)
    ok &= check("rail_group is rule + rows + rule", len(g) == 4)
    ok &= check("no rail line carries a right-hand edge",
                not any(plain(ln).rstrip().endswith(("│", "┐", "┘"))
                        for ln in g))
    ok &= check("a group nested in a panel still fits its panel",
                all(cells(side(" " + ln, W + 4)) == W + 4 for ln in g))

    # --- degenerate inputs ---------------------------------------------------
    ok &= check("zero width does not raise", isinstance(panel_top("x", "", 0), str))
    ok &= check("empty title does not raise",
                cells(panel_top("", "", W)) == W)
    ok &= check("over-long content pushes the bar out by its overflow only",
                cells(side("x" * (W + 5), W)) == W + 5 + 2)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
