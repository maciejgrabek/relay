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
    # Verify "up" entry produces correct (key, label) pair. The label is the
    # SHORT `bar` field ("move"), not the full `help` sentence ("move up") -
    # a full sentence on the bar is what let it overflow an 80-column
    # terminal (review round 1, finding 1).
    ok &= check('hot_pairs includes ("up/k", "move")',
                ("up/k", "move") in hot_pairs)

    # Check help_rows on actual content, not just length
    help_rows = commands.help_rows(table)
    ok &= check("help_rows returns EVERY entry",
                len(help_rows) == len(table))
    # Verify "up" entry with key includes key, help, and :name form
    ok &= check('help_rows includes ("up k", "move up   (:up)")',
                ("up k", "move up   (:up)") in help_rows)
    # "answer" binds no real Textual key (ENTER is consumed by the DataTable
    # - binding it too would double-fire), but it still needs a legend: an
    # unbound key that is invisible everywhere is the exact bug class this
    # table exists to prevent. `bar_key` supplies that legend without
    # claiming a key token (see key_tokens check below).
    answer_cmd = next(c for c in commands.CMD if c.name == "answer")
    ok &= check("answer claims no real key token",
                commands.key_tokens(answer_cmd) == [])
    ok &= check("answer has a display-only bar_key",
                answer_cmd.bar_key == "⏎")
    ok &= check('help_rows includes ("⏎", "send Enter to the selected '
                'session   (:answer)")',
                ("⏎", "send Enter to the selected session   (:answer)")
                in help_rows)
    ok &= check("answer is on the hot bar (ENTER's legend belongs there too)",
                answer_cmd.hot is True)

    # Task 3 review finding 1: `colon` shipped with no _KEY_DISPLAY entry, so
    # the ?-overlay rendered the literal Textual token ("colon") instead of
    # the glyph (":") - the same bug class Task 2 fixed as its finding F3,
    # recurring because a NEW key was added and the map was not extended.
    # Guard the whole class, not just this one instance: no raw multi-
    # character Textual key name may survive into the KEY COLUMN help_rows()
    # hands to the `?` overlay (checking the column, not the whole row text,
    # is deliberate - "space" or "escape" could otherwise false-positive
    # against ordinary help prose).
    _RAW_KEY_NAMES = ("colon", "comma", "exclamation_mark", "question_mark",
                      "space", "escape")
    _leaked = [tok for key, _what in help_rows for tok in key.split()
               if tok in _RAW_KEY_NAMES]
    ok &= check(f"no raw Textual key name leaks into the ?-overlay key "
                f"column (found {_leaked})", _leaked == [])

    ok &= check("exact name completes to itself",
                commands.complete("audit", table) == ["audit"])
    ok &= check("a unique prefix completes",
                commands.complete("aud", table) == ["audit"])
    ok &= check("an unknown prefix completes to nothing",
                commands.complete("zzz", table) == [])
    ok &= check("an empty prefix offers every TYPEABLE command",
                len(commands.complete("", table))
                == len([c for c in table if c.palette]))
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

    # Fix 6: NEVER_EXPOSE is a blacklist; EXPOSE is the allowlist a `cli=`
    # entry must additionally satisfy. A verb not on either list (not
    # dangerous, just not routed) must still fail - it is not vetted.
    ok &= check("validate catches a cli verb not in EXPOSE",
                commands.validate([
                    commands.Cmd(name="x", help="h",
                                 cli="frobnicate")]) != [])
    ok &= check("every table entry's cli verb (if any) is in EXPOSE",
                all(c.cli in commands.EXPOSE for c in table if c.cli))
    ok &= check("EXPOSE and NEVER_EXPOSE do not overlap",
                commands.EXPOSE & commands.NEVER_EXPOSE == set())

    ok = _palette_checks(ok)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def _palette_checks(ok):
    """The palette: filtering, ordering, and the two-column help layout."""
    table = commands.CMD

    # A non-verb is EXCLUDED, not demoted. Demotion only ever applied to the
    # empty query, which is why typing "settings" answered with
    # `settings settingsleft settingsright` - two modal sub-keys that do
    # nothing at all unless the settings editor is already open. They keep
    # their keys and stay documented in `?`; they are simply not commands.
    everything = commands.filter_cmds("", table)
    typeable = [c for c in table if c.palette]
    ok &= check("an empty query offers every TYPEABLE command",
                len(everything) == len(typeable))
    ok &= check("...and no non-verb among them",
                all(c.palette for c in everything))
    ok &= check("some entries really are excluded (else this proves nothing)",
                len(typeable) < len(table))

    for junk in ("settingsleft", "settingsright", "up", "down", "back"):
        ok &= check(f"{junk} is not typeable at any query",
                    not any(c.name == junk
                            for c in commands.filter_cmds(junk, table)))
    ok &= check("typing 'settings' offers ONLY settings",
                [c.name for c in commands.filter_cmds("settings", table)]
                == ["settings"])
    # The palette reopening itself deadlocked the pump: _cmdline_close()'s
    # remove() is async, so dispatch mounted a second #cmdline on top of a
    # widget that had not been pruned yet, and the whole panel froze until a
    # timeout let go. app.py guards the mount as well; this keeps the path
    # from being reachable by typing in the first place.
    ok &= check("the palette's own opener is not a typeable command",
                not any(c.name == "commands"
                        for c in commands.filter_cmds("comm", table)))

    # Excluded from the PALETTE is not excluded from the DOCS: `?` is where
    # a key that is not a verb still has to be discoverable.
    rows = commands.help_rows(table)
    ok &= check("`?` still documents every entry, typeable or not",
                len(rows) == len(table))
    for junk in ("settingsleft", "settingsright", "back", "commands"):
        ok &= check(f"`?` still documents {junk}",
                    any(f":{junk}" in r or junk in r for r in
                        [f"{k} {h}" for k, h in rows]))

    ok &= check("TAB completion offers no non-verb either",
                "settingsleft" not in commands.complete("settings", table))

    # A genuine INTERIOR match - "work" would not do, since it is a prefix of
    # "workspaces" and a prefix-only implementation would pass.
    ok &= check("an INTERIOR substring matches (prefix-only would not)",
                any(c.name == "workspaces"
                    for c in commands.filter_cmds("space", table)))

    au = commands.filter_cmds("au", table)
    ok &= check("a prefix match ranks above a substring match",
                au and au[0].name == "audit")

    ok &= check("an unknown query matches nothing",
                commands.filter_cmds("zzzzz", table) == [])
    ok &= check("filtering is case-insensitive",
                [c.name for c in commands.filter_cmds("WORK", table)]
                == [c.name for c in commands.filter_cmds("work", table)])
    ok &= check("a leading slash or colon is ignored",
                [c.name for c in commands.filter_cmds("/au", table)]
                == [c.name for c in commands.filter_cmds("au", table)])

    lines = commands.palette_lines("", table, 0, 70, limit=5)
    ok &= check("palette renders at most `limit` rows plus a footer",
                len(lines) <= 6)
    ok &= check("the cursor row is marked", any("\u25b8" in l for l in lines))
    ok &= check("a name and its help both appear",
                any("arm" in l for l in lines))
    ok &= check("the overflow footer counts what is not shown",
                any(str(len([c for c in table if c.palette])) in l
                    for l in lines))
    ok &= check("every rendered line fits the width",
                all(len(l) <= 70 for l in commands.palette_lines(
                    "", table, 0, 70, limit=5)))
    ok &= check("an empty result set says so",
                commands.palette_lines("zzzzz", table, 0, 70)
                and "no match" in commands.palette_lines(
                    "zzzzz", table, 0, 70)[0])

    ok &= check("the cursor cannot render out of range",
                commands.palette_lines("", table, 999, 70, limit=3) is not None)

    wide = commands.help_columns(commands.help_rows(table), 180)
    ok &= check("at a wide terminal the help is TWO columns, halving the rows",
                len(wide) <= (len(table) // 2) + 2)
    ok &= check("two-column rows fit the width",
                all(len(r) <= 180 for r in wide))
    ok &= check("two clean columns truncate nothing",
                not any("\u2026" in r for r in wide))
    narrow = commands.help_columns(commands.help_rows(table), 100)
    ok &= check("at a narrow terminal it falls back to ONE column, because two "
                "thin columns would truncate what one wide one shows",
                len(narrow) == len(table))
    ok &= check("the single column fits too",
                all(len(r) <= 100 for r in narrow))
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
