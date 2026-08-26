"""Tests for the TUI command table. Pure stdlib, no textual/iterm2 imports.

Run: python3 iterm/test_commands.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import commands  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    table = commands.CMD

    ok &= check("the table is not empty", len(table) > 0)
    ok &= check("validate() reports no problems", commands.validate(table) == [])

    for c in table:
        if not (bool(c.action) ^ bool(c.cli)):
            ok &= check(f"{c.name} has exactly one of action/cli", False)
            break
    else:
        ok &= check("every entry has exactly one of action/cli", True)

    ok &= check("every entry has a help line",
                all(c.help.strip() for c in table))

    seen = {}
    dupe = None
    for c in table:
        for tok in commands.key_tokens(c):
            if tok in seen:
                dupe = (tok, seen[tok], c.name)
            seen[tok] = c.name
    ok &= check(f"no key token is claimed twice (found {dupe})", dupe is None)

    ok &= check("a multi-token key splits on commas",
                commands.key_tokens(commands.Cmd(
                    name="x", help="h", action="a", key="up,k")) == ["up", "k"])
    ok &= check("an entry with no key has no tokens",
                commands.key_tokens(commands.Cmd(
                    name="x", help="h", action="a")) == [])

    exposed = {c.cli for c in table if c.cli}
    leaked = exposed & commands.NEVER_EXPOSE
    ok &= check(f"no never-expose verb is in the table (leaked {leaked})",
                leaked == set())
    ok &= check("register is on the never-expose list",
                "register" in commands.NEVER_EXPOSE)
    ok &= check("join is on the never-expose list",
                "join" in commands.NEVER_EXPOSE)

    # Check hot_pairs on actual content, not just length
    hot_pairs = commands.hot_pairs(table)
    ok &= check("hot_pairs returns only hot entries",
                len(hot_pairs) == len([c for c in table if c.hot]))
    # Verify "up" entry produces correct (key, label) pair
    ok &= check('hot_pairs includes ("up/k", "move up")',
                ("up/k", "move up") in hot_pairs)

    # Check help_rows on actual content, not just length
    help_rows = commands.help_rows(table)
    ok &= check("help_rows returns EVERY entry",
                len(help_rows) == len(table))
    # Verify "up" entry with key includes key, help, and :name form
    ok &= check('help_rows includes ("up k", "move up   (:up)")',
                ("up k", "move up   (:up)") in help_rows)
    # Verify "answer" entry with no key renders as :name form only
    ok &= check('help_rows includes (":answer", "send Enter to the selected session")',
                (":answer", "send Enter to the selected session") in help_rows)

    ok &= check("exact name completes to itself",
                commands.complete("audit", table) == ["audit"])
    ok &= check("a unique prefix completes",
                commands.complete("aud", table) == ["audit"])
    ok &= check("an unknown prefix completes to nothing",
                commands.complete("zzz", table) == [])
    ok &= check("an empty prefix offers everything",
                len(commands.complete("", table)) == len(table))
    ok &= check("completion is sorted",
                commands.complete("", table)
                == sorted(commands.complete("", table)))

    ok &= check("parse splits verb and args",
                commands.parse("arm w1 wild") == ("arm", ["w1", "wild"], False))
    ok &= check("parse handles a bare verb",
                commands.parse("audit") == ("audit", [], False))
    ok &= check("parse strips a trailing bang",
                commands.parse("wipe!") == ("wipe", [], True))
    ok &= check("parse strips a spaced bang",
                commands.parse("wipe !") == ("wipe", [], True))
    ok &= check("parse tolerates a leading colon",
                commands.parse(":audit on") == ("audit", ["on"], False))
    ok &= check("parse of an empty line yields no verb",
                commands.parse("   ") == ("", [], False))
    ok &= check("parse strips multiple trailing bangs",
                commands.parse("wipe!!") == ("wipe", [], True))
    ok &= check("parse strips three trailing bangs",
                commands.parse("wipe!!!") == ("wipe", [], True))
    ok &= check("parse of a lone bang yields no verb but confirmed",
                commands.parse("!") == ("", [], True))
    ok &= check("completion never offers a bang",
                not any(n.endswith("!") for n in commands.complete("", table)))

    ok &= check("validate catches a duplicate key token",
                commands.validate([
                    commands.Cmd(name="a", help="h", action="x", key="v"),
                    commands.Cmd(name="b", help="h", action="y", key="v")]) != [])
    ok &= check("validate catches both action and cli set",
                commands.validate([
                    commands.Cmd(name="a", help="h", action="x", cli="ws")]) != [])
    ok &= check("validate catches neither action nor cli set",
                commands.validate([commands.Cmd(name="a", help="h")]) != [])
    ok &= check("validate catches a never-expose verb",
                commands.validate([
                    commands.Cmd(name="reg", help="h", cli="register")]) != [])
    ok &= check("validate catches an empty token in key",
                commands.validate([
                    commands.Cmd(name="x", help="h", action="a", key="a,,b")]) != [])

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
