"""Tests for the pure swarm decision logic. No iTerm2, no sqlite.

Run: python3 iterm/test_swarm.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from swarm import (  # noqa: E402
    parse_blockers, unblocked_by_completion, wakeup_assignment_body,
    wakeup_unblocked_body, delivery_text, claude_prompt_ready, stale_reason,
    render_swarm,
)


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def T(id, state="todo", blocked_by="", owner="w", title="t", spec_path=None):
    return {"id": id, "state": state, "blocked_by": blocked_by,
            "owner": owner, "title": title, "spec_path": spec_path,
            "project": "p", "parent_id": None}


# A realistic idle Claude Code screen tail (input box + shortcuts footer).
IDLE_TAIL = [
    "╭──────────────────────────────────────────╮",
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
    "  ? for shortcuts",
]
WORKING_TAIL = [
    "  Reticulating splines…",
    "  (esc to interrupt · 42s · ↓ 1.2k tokens)",
]
SHELL_TAIL = [
    "~/work/myproject $",
]
# After the user quits claude, the input box + footer chrome lingers ABOVE a
# live shell prompt. Delivering here would type a message into the SHELL and
# press Enter = command execution. Must be treated as NOT ready.
SHELL_AFTER_CLAUDE_TAIL = [
    "╭──────────────────────────────────────────╮",
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
    "  ? for shortcuts",
    "~/work/relay $",
]
# An idle screen whose bottom line is the box's closing border is still ready.
BOX_BOTTOM_TAIL = [
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
]


def run():
    ok = True

    # parse_blockers
    ok &= check("parse empty", parse_blockers("") == [])
    ok &= check("parse one", parse_blockers("7") == [7])
    ok &= check("parse many + junk-tolerant", parse_blockers("3, 4,") == [3, 4])

    # unblocked_by_completion: fires only when ALL blockers done
    tasks = [T(1, state="done"), T(2, state="done"),
             T(3, state="blocked", blocked_by="1,2", owner="fe"),
             T(4, state="blocked", blocked_by="1,9", owner="be"),
             T(5, state="done", blocked_by="1")]
    got = unblocked_by_completion(tasks, 1)
    ok &= check("all-blockers-done fires", [t["id"] for t in got] == [3])
    ok &= check("partial blockers do not fire", all(t["id"] != 4 for t in got))
    ok &= check("already-done target skipped", all(t["id"] != 5 for t in got))
    ok &= check("unrelated completion fires nothing",
                unblocked_by_completion(tasks, 99) == [])

    # wake-up bodies
    epic = T(12, title="BFF checkout", spec_path="/w/specs/bff.md")
    b = wakeup_assignment_body(epic)
    ok &= check("assignment names task id", "#12" in b and "BFF checkout" in b)
    ok &= check("assignment includes spec instructions",
                "/w/specs/bff.md" in b and "relay task add --parent 12" in b)
    b2 = wakeup_assignment_body(T(13, title="small fix"))
    ok &= check("assignment without spec is plain",
                "#13" in b2 and "spec" not in b2.lower())
    ub = wakeup_unblocked_body(T(3, title="fe form"))
    ok &= check("unblocked body names task", "#3" in ub and "unblocked" in ub)

    # delivery text
    ok &= check("delivery text format",
                delivery_text("coord", "go") == "[relay msg from coord] go")
    ok &= check("delivery text flattens newlines",
                "\n" not in delivery_text("coord", "a\nb"))
    dt = delivery_text("x", "a\x1b[Ab\x07c")
    ok &= check("delivery text strips control bytes",
                "\x1b" not in dt and "\x07" not in dt)

    # claude_prompt_ready
    ok &= check("idle input box -> ready", claude_prompt_ready(IDLE_TAIL))
    ok &= check("working tail -> not ready", not claude_prompt_ready(WORKING_TAIL))
    ok &= check("bare shell -> not ready", not claude_prompt_ready(SHELL_TAIL))
    ok &= check("empty screen -> not ready", not claude_prompt_ready([]))
    ok &= check("shell prompt below lingering chrome -> not ready",
                not claude_prompt_ready(SHELL_AFTER_CLAUDE_TAIL))
    ok &= check("box-bottom last line -> ready",
                claude_prompt_ready(BOX_BOTTOM_TAIL))

    # stale_reason (threshold 600s)
    ok &= check("fresh -> None",
                stale_reason(1000.0, 600, oldest_undelivered_ts=900.0) is None)
    r = stale_reason(2000.0, 600, oldest_undelivered_ts=1000.0)
    ok &= check("old queued message -> stale", r is not None and "message" in r)
    r = stale_reason(2000.0, 600, doing_since=1000.0, screen_changed_ts=1100.0)
    ok &= check("doing + quiet screen -> stale", r is not None)
    r = stale_reason(2000.0, 600, doing_since=1000.0, screen_changed_ts=1900.0)
    ok &= check("doing + recent screen change -> None", r is None)
    r = stale_reason(2000.0, 600, doing_since=1000.0, screen_changed_ts=None)
    ok &= check("doing + no screen data falls back to doing_since", r is not None)
    ok &= check("no signals -> None", stale_reason(2000.0, 600) is None)

    # render_swarm: board columns, epic progress, messages
    sessions = [
        {"name": "coord", "role": "coordinator", "project": "webshop",
         "status_text": "orchestrating", "last_seen": 950.0},
        {"name": "bff-worker", "role": "worker", "project": "webshop",
         "status_text": "on #2", "last_seen": 990.0},
    ]
    tasks = [
        {"id": 1, "project": "webshop", "parent_id": None, "title": "BFF epic",
         "state": "doing", "owner": "bff-worker", "spec_path": "/s/bff.md",
         "blocked_by": ""},
        {"id": 2, "project": "webshop", "parent_id": 1, "title": "endpoint",
         "state": "done", "owner": "bff-worker", "spec_path": None,
         "blocked_by": ""},
        {"id": 3, "project": "webshop", "parent_id": 1, "title": "tests",
         "state": "todo", "owner": "bff-worker", "spec_path": None,
         "blocked_by": ""},
        {"id": 4, "project": "webshop", "parent_id": None, "title": "review",
         "state": "blocked", "owner": "coord", "spec_path": None,
         "blocked_by": "3"},
    ]
    msgs = [{"from_name": "coord", "to_name": "bff-worker", "body": "go",
             "created_at": 900.0, "delivered_at": 901.0}]
    out = render_swarm(sessions, tasks, msgs, now=1000.0, width=100)
    ok &= check("board has the four columns",
                all(h in out for h in ("TODO", "DOING", "BLOCKED", "DONE")))
    ok &= check("tasks appear in their columns",
                "#3" in out and "#2" in out and "#4" in out)
    ok &= check("epic progress rendered", "1/2" in out and "BFF epic" in out)
    ok &= check("session roster with roles",
                "coord" in out and "bff-worker" in out)
    ok &= check("message feed present", "coord -> bff-worker: go" in out)
    ok &= check("empty inputs render", render_swarm([], [], [], 0.0) != "")

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
