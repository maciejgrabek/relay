"""Tests for merging relay's hook entries into Claude Code's settings.
Pure dicts, no files.

Run: python3 iterm/test_hooks.py    or    ./test/run.sh
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import hooks  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    ok &= check("relay ships PostToolUse, UserPromptSubmit and SessionStart "
                "entries",
                set(hooks.ENTRIES) == {"PostToolUse", "UserPromptSubmit",
                                       "SessionStart"})
    ok &= check("the PostToolUse group matches SendMessage only",
                hooks.ENTRIES["PostToolUse"][0]["matcher"] == "SendMessage")
    ok &= check("every relay hook except SessionStart is async and runs "
                "relay hook <event>",
                all(h["async"] is True and h["command"].startswith("relay hook ")
                    for event, g in hooks.ENTRIES.items() if event != "SessionStart"
                    for grp in g
                    for h in grp["hooks"]))
    ok &= check("the SessionStart hook is sync with a 5s timeout",
                "async" not in hooks.ENTRIES["SessionStart"][0]["hooks"][0]
                and hooks.ENTRIES["SessionStart"][0]["hooks"][0]["timeout"] == 5)

    empty = {}
    merged = hooks.merge(empty)
    ok &= check("merge into empty settings adds exactly relay's entries",
                merged == {"hooks": hooks.ENTRIES})
    ok &= check("merge does not mutate its input", empty == {})
    ok &= check("status of merged is ok everywhere",
                hooks.status(merged) == {"PostToolUse": "ok",
                                         "UserPromptSubmit": "ok",
                                         "SessionStart": "ok"})
    ok &= check("status of empty is missing everywhere",
                hooks.status({}) == {"PostToolUse": "missing",
                                     "UserPromptSubmit": "missing",
                                     "SessionStart": "missing"})

    two_event = {"hooks": {k: v for k, v in hooks.ENTRIES.items()
                           if k != "SessionStart"}}
    ok &= check("an installed two-event settings dict reports "
                "SessionStart: missing",
                hooks.status(two_event)["SessionStart"] == "missing")
    m_two = hooks.merge(two_event)
    ok &= check("merge of it adds SessionStart without touching the other two",
                m_two["hooks"]["PostToolUse"] == two_event["hooks"]["PostToolUse"]
                and m_two["hooks"]["UserPromptSubmit"]
                    == two_event["hooks"]["UserPromptSubmit"]
                and m_two["hooks"]["SessionStart"]
                    == hooks.ENTRIES["SessionStart"])

    # foreign hooks survive, relay's are appended after them
    foreign = {"permissions": {"allow": ["Bash(ls:*)"]},
               "hooks": {"PostToolUse": [{"matcher": "Bash",
                                          "hooks": [{"type": "command",
                                                     "command": "notify.sh"}]}],
                         "Stop": [{"hooks": [{"type": "command",
                                              "command": "ding.sh"}]}]}}
    snapshot = copy.deepcopy(foreign)
    m2 = hooks.merge(foreign)
    ok &= check("foreign settings keys are untouched",
                m2["permissions"] == snapshot["permissions"]
                and m2["hooks"]["Stop"] == snapshot["hooks"]["Stop"])
    ok &= check("foreign PostToolUse group kept, relay's appended after it",
                m2["hooks"]["PostToolUse"][0] == snapshot["hooks"]["PostToolUse"][0]
                and m2["hooks"]["PostToolUse"][1] == hooks.ENTRIES["PostToolUse"][0])
    ok &= check("merge is idempotent", hooks.merge(m2) == m2)

    # a stale relay entry (older command string) is replaced, not doubled
    stale = copy.deepcopy(m2)
    stale["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"] = "relay hook prompt --v0"
    ok &= check("status reports a stale entry",
                hooks.status(stale)["UserPromptSubmit"] == "stale")
    m3 = hooks.merge(stale)
    ok &= check("merge replaces the stale group in place",
                m3["hooks"]["UserPromptSubmit"] == hooks.ENTRIES["UserPromptSubmit"]
                and len(m3["hooks"]["PostToolUse"]) == 2)

    # strip removes only ours
    s = hooks.strip(m3)
    ok &= check("strip leaves the foreign groups",
                s["hooks"]["PostToolUse"] == [snapshot["hooks"]["PostToolUse"][0]]
                and s["hooks"]["Stop"] == snapshot["hooks"]["Stop"])
    ok &= check("strip drops an event that held only relay's group",
                "UserPromptSubmit" not in s["hooks"])
    ok &= check("strip of empty is empty", hooks.strip({}) == {})
    ok &= check("strip drops an empty hooks dict entirely",
                "hooks" not in hooks.strip({"hooks": hooks.ENTRIES}))

    # a group mixing relay's hook with a foreign one is never dropped whole
    mixed = {"hooks": {"UserPromptSubmit": [{"hooks": [
        {"type": "command", "command": "relay hook prompt", "async": True},
        {"type": "command", "command": "other.sh"}]}]}}
    s2 = hooks.strip(mixed)
    ok &= check("strip removes only relay's hook from a mixed group",
                s2["hooks"]["UserPromptSubmit"][0]["hooks"]
                == [{"type": "command", "command": "other.sh"}])

    # a group in the middle of the list gets replaced in place, not moved
    foo = {"matcher": "Read", "hooks": [{"type": "command", "command": "foo.sh"}]}
    bar = {"hooks": [{"type": "command", "command": "bar.sh"}]}
    stale_mid = {"matcher": "SendMessage",
                 "hooks": [{"type": "command",
                           "command": "relay hook post-tool --old",
                           "async": True}]}
    mid = {"hooks": {"PostToolUse": [foo, stale_mid, bar]}}
    m4 = hooks.merge(mid)
    ok &= check("a stale group in the middle is replaced in place, not moved",
                m4["hooks"]["PostToolUse"]
                == [foo, hooks.ENTRIES["PostToolUse"][0], bar])

    # a "hooks" value that is not a dict is treated as empty, never raises
    ok &= check("merge treats a non-dict hooks value as empty",
                hooks.merge({"hooks": "x"}) == {"hooks": hooks.ENTRIES})
    ok &= check("status treats a non-dict hooks value as missing everywhere",
                hooks.status({"hooks": ["nope"]}) == {"PostToolUse": "missing",
                                                       "UserPromptSubmit": "missing",
                                                       "SessionStart": "missing"})
    ok &= check("strip leaves a hooks value it does not understand alone",
                hooks.strip({"hooks": 7}) == {"hooks": 7})

    d = hooks.diff_text({}, hooks.merge({}), "/x/settings.json")
    ok &= check("diff_text is a unified diff naming the file",
                d.startswith("--- /x/settings.json") and "+++ " in d
                and '"relay hook post-tool"' in d)
    ok &= check("diff_text of no change is empty",
                hooks.diff_text(m2, hooks.merge(m2), "/x") == "")
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
