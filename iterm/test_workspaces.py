"""Tests for workspace grouping (pure logic).

Run: python3 iterm/test_workspaces.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from workspaces import freeze, group, short_path, summary  # noqa: E402


class S:
    def __init__(self, name, wd, armed=False, attn=False, burn=False):
        self.name, self.wd = name, wd
        self.armed, self.attn, self.burn = armed, attn, burn

    def __repr__(self):
        return self.name


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def names(groups):
    return [(k, [m.name for m in ms]) for k, ms in groups]


def run():
    ok = True
    K = lambda s: s.wd  # noqa: E731

    # --- the core rule: two or more, or no group at all ----------------------
    a, b, c = S("a", "/w/relay"), S("b", "/w/relay"), S("c", "/w/cloud")
    ok &= check("two in one directory make a group",
                names(group([a, b], K)) == [("/w/relay", ["a", "b"])])
    ok &= check("a directory with one session gets NO group",
                names(group([a, c], K)) == [(None, ["a"]), (None, ["c"])])
    ok &= check("groups and solos coexist",
                names(group([a, b, c], K))
                == [("/w/relay", ["a", "b"]), (None, ["c"])])

    # --- order: gather, never sort -------------------------------------------
    x = [S("t1", "/w/relay"), S("t2", "/w/cloud"), S("t3", "/w/relay"),
         S("t4", "/w/cloud")]
    ok &= check("a group sits where its FIRST member sat",
                [k for k, _ in group(x, K)] == ["/w/relay", "/w/cloud"])
    ok &= check("members keep their relative order",
                names(group(x, K)) == [("/w/relay", ["t1", "t3"]),
                                       ("/w/cloud", ["t2", "t4"])])
    ok &= check("every input appears exactly once",
                sorted(m.name for _, ms in group(x, K) for m in ms)
                == ["t1", "t2", "t3", "t4"])

    # --- the stability guarantee ---------------------------------------------
    before = names(group(x, K))
    x[0].attn = True                     # a session changes STATE
    x[2].armed = True
    ok &= check("state changes cannot reorder anything",
                names(group(x, K)) == before)

    # --- an unreadable directory is not a workspace --------------------------
    u1, u2 = S("u1", ""), S("u2", "")
    ok &= check("empty keys never group together",
                names(group([u1, u2], K)) == [(None, ["u1"]), (None, ["u2"])])
    ok &= check("an ungroupable session keeps its own slot in the order",
                names(group([a, u1, b], K))
                == [("/w/relay", ["a", "b"]), (None, ["u1"])])

    # --- min_size is a knob, not a constant ----------------------------------
    ok &= check("min_size=1 groups a lone session",
                names(group([c], K, min_size=1)) == [("/w/cloud", ["c"])])
    ok &= check("min_size=3 leaves a pair ungrouped",
                names(group([a, b], K, min_size=3))
                == [(None, ["a"]), (None, ["b"])])

    # --- empty input ---------------------------------------------------------
    ok &= check("no sessions, no groups", group([], K) == [])

    # --- the freeze: what gets stored as the key -----------------------------
    ok &= check("nothing frozen yet, no persisted value -> the live path",
                freeze("", "", "/w/live") == "/w/live")
    ok &= check("the persisted column beats the live path",
                freeze("", "/w/persisted", "/w/live") == "/w/persisted")
    ok &= check("an already-frozen key is NEVER re-frozen",
                freeze("/w/first", "/w/persisted", "/w/live") == "/w/first")
    ok &= check("a session that cd'd keeps the directory it started in",
                freeze("/w/relay", "", "/w/relay/iterm") == "/w/relay")
    ok &= check("nothing readable yet stays empty, so it groups with nobody",
                freeze("", "", "") == "")

    # --- display paths -------------------------------------------------------
    home = "/Users/me"
    ok &= check("home collapses to ~",
                short_path("/Users/me/Work/relay", home) == "~/Work/relay")
    ok &= check("home itself is ~", short_path("/Users/me", home) == "~")
    ok &= check("a path outside home is left alone",
                short_path("/opt/src", home) == "/opt/src")
    ok &= check("a near-miss prefix is NOT collapsed",
                short_path("/Users/median/x", home) == "/Users/median/x")
    ok &= check("empty stays empty", short_path("", home) == "")

    # --- the summary that rides the rule -------------------------------------
    m = [S("a", "/w", armed=True, attn=True), S("b", "/w"), S("c", "/w")]
    s = summary(m, lambda i: i.armed, lambda i: i.attn, lambda i: i.burn)
    ok &= check("summary counts sessions first",
                s[0] == ("3 sessions", "plain"))
    ok &= check("summary names what a count MEANS, not its colour",
                ("1 armed", "armed") in s and ("1 needs you", "attention") in s)
    ok &= check("a zero count is omitted, not printed as 0",
                all("0 " not in t for t, _ in s))
    ok &= check("compact form uses the glyphs the rows already use",
                [t for t, _ in summary(m, lambda i: i.armed, lambda i: i.attn,
                                       lambda i: i.burn, compact=True)]
                == ["3", "◉1", "‼1"])
    ok &= check("compact form still omits zero counts",
                all("0" not in t for t, _ in
                    summary([m[1]], lambda i: i.armed, lambda i: i.attn,
                            compact=True)))
    ok &= check("one session is singular",
                summary([m[0]], lambda i: False, lambda i: False)[0]
                == ("1 session", "plain"))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
