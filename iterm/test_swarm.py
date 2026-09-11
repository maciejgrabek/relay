"""Tests for the pure swarm decision logic. No iTerm2, no sqlite.

Run: python3 iterm/test_swarm.py    or    ./test/run.sh
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(__file__))
import swarm  # noqa: E402
from swarm import (  # noqa: E402
    parse_blockers, unblocked_by_completion, wakeup_assignment_body,
    wakeup_unblocked_body, delivery_text, claude_prompt_ready, stale_reason,
    render_swarm, restore_candidates, clean_candidates, restore_plan_text,
    clean_plan_text, resume_prompt, wipe_candidates, wipe_blocker_warnings,
    wipe_plan_text, parse_pr_ref, resolve_pr_route, intervene_targets,
    intervene_counts, is_shell_job, session_working, prompt_line_empty,
    selection_dialog, _INPUT_BOX_RE,
)

_SCREEN_DIR = pathlib.Path(__file__).parent / "fixtures" / "screens"


def load_screen(name):
    """A captured screen as relay's watcher would hand it to a predicate:
    non-blank lines, '#' header stripped."""
    p = _SCREEN_DIR / f"{name}.txt"
    return [l for l in p.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _task_row(id, state="todo", blocked_by="", owner="w", title="t", spec_path=None):
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
#
# Fix round 3 (defect review, CRITICAL): round 2 rewrote this fixture's
# trailing "~/work/relay $" into "~/work/relay" + "❯", on the theory that
# only a prompt reusing Claude's own glyph is dangerous. It is not: the
# danger is a LIVE SHELL under dead chrome, whatever glyph it prints. Bash's
# "$", zsh's "%" and starship's "›" execute a delivered message exactly as
# readily as "❯" does. The original "$" form is restored, and the three
# other prompt shapes are pinned alongside it - the "❯" variant is now just
# one sibling among four, not the only case the guard understands.
_BOX_CHROME = [
    "╭──────────────────────────────────────────╮",
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
    "  ? for shortcuts",
]
SHELL_AFTER_CLAUDE_TAIL = _BOX_CHROME + [
    "~/work/relay $",
]
SHELL_AFTER_CLAUDE_TAIL_ZSH_PCT = _BOX_CHROME + [
    "~/work/relay %",
]
SHELL_AFTER_CLAUDE_TAIL_STARSHIP = _BOX_CHROME + [
    "~/work/relay on main",
    "› ",
]
SHELL_AFTER_CLAUDE_TAIL_CARET = _BOX_CHROME + [
    "~/work/relay",
    "❯",
]
# An idle screen that ends right at the box's closing border, with no footer
# captured below it, is still ready - the footer is not required for
# condition 1, only a genuinely bracketed input row. The row still needs
# chrome on BOTH sides (Task 2's own bracketing rule, reused here) - every
# real capture always shows the opening border too, so that side is included
# rather than modeling a truncation that reality never produces.
BOX_BOTTOM_TAIL = [
    "╭──────────────────────────────────────────╮",
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
    tasks = [_task_row(1, state="done"), _task_row(2, state="done"),
             _task_row(3, state="blocked", blocked_by="1,2", owner="fe"),
             _task_row(4, state="blocked", blocked_by="1,9", owner="be"),
             _task_row(5, state="done", blocked_by="1")]
    got = unblocked_by_completion(tasks, 1)
    ok &= check("all-blockers-done fires", [t["id"] for t in got] == [3])
    ok &= check("partial blockers do not fire", all(t["id"] != 4 for t in got))
    ok &= check("already-done target skipped", all(t["id"] != 5 for t in got))
    ok &= check("unrelated completion fires nothing",
                unblocked_by_completion(tasks, 99) == [])

    # wake-up bodies
    epic = _task_row(12, title="BFF checkout", spec_path="/w/specs/bff.md")
    b = wakeup_assignment_body(epic)
    ok &= check("assignment names task id", "#12" in b and "BFF checkout" in b)
    ok &= check("assignment includes spec instructions",
                "/w/specs/bff.md" in b and "relay task add --parent 12" in b)
    b2 = wakeup_assignment_body(_task_row(13, title="small fix"))
    ok &= check("assignment without spec is plain",
                "#13" in b2 and "spec" not in b2.lower())
    ub = wakeup_unblocked_body(_task_row(3, title="fe form"))
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
    ok &= check("zsh '%' prompt below lingering chrome -> not ready",
                not claude_prompt_ready(SHELL_AFTER_CLAUDE_TAIL_ZSH_PCT))
    ok &= check("starship '›' prompt below lingering chrome -> not ready",
                not claude_prompt_ready(SHELL_AFTER_CLAUDE_TAIL_STARSHIP))
    ok &= check("'❯' prompt below lingering chrome -> not ready",
                not claude_prompt_ready(SHELL_AFTER_CLAUDE_TAIL_CARET))
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

    # a failed peer row (delivered_at None, via=peer) is not relay's to
    # deliver - it must not inflate the fleet line's queued count
    peer_msgs = msgs + [{"from_name": "coord", "to_name": "bff-worker",
                         "body": "lost", "created_at": 902.0,
                         "delivered_at": None, "via": "peer"}]
    peer_out = render_swarm(sessions, tasks, peer_msgs, now=1000.0, width=100)
    ok &= check("a failed peer row does not inflate the fleet line's queued count",
                "msgs" not in peer_out.splitlines()[0])
    relay_msgs = msgs + [{"from_name": "coord", "to_name": "bff-worker",
                          "body": "pending", "created_at": 902.0,
                          "delivered_at": None, "via": "relay"}]
    relay_out = render_swarm(sessions, tasks, relay_msgs, now=1000.0,
                             width=100)
    ok &= check("an undelivered relay row still shows up as queued",
                "msgs 1 queued" in relay_out)

    ok &= check("delivery_text info unchanged",
                swarm.delivery_text("coord", "hi") == "[relay msg from coord] hi")
    ok &= check("delivery_text carries kind",
                swarm.delivery_text("bff", "done", "done")
                == "[relay done from bff] done")
    ok &= check("kind_of tolerates missing key",
                swarm.kind_of({"id": 1, "body": "x"}) == "info")
    ok &= check("kind_of reads kind",
                swarm.kind_of({"id": 1, "kind": "blocked"}) == "blocked")
    one_session = [{"name": "a", "role": "worker", "project": "",
                    "status_text": ""}]
    fed = swarm.render_swarm(
        one_session, [],
        [{"from_name": "a", "to_name": "b", "body": "hi", "delivered_at": 1,
          "kind": "escalation"}], now=0.0)
    ok &= check("feed tags non-info kind", "[escalation]" in fed)
    fed2 = swarm.render_swarm(
        one_session, [],
        [{"from_name": "a", "to_name": "b", "body": "hi", "delivered_at": 1,
          "kind": "info"}], now=0.0)
    ok &= check("feed leaves info untagged", "[info]" not in fed2)

    # --- escalation pings ---------------------------------------------------
    esc = [{"id": 1, "kind": "escalation", "from_name": "w1", "to_name": "c",
            "body": "need creds"},
           {"id": 2, "kind": "info", "from_name": "w1", "to_name": "c",
            "body": "hi"},
           {"id": 3, "kind": "escalation", "from_name": "w2", "to_name": "c",
            "body": "stuck"}]
    ok &= check("escalation_pings picks unpinged escalations",
                [m["id"] for m in swarm.escalation_pings(esc, {1})] == [3])
    ok &= check("escalation_pings empty when all seen",
                swarm.escalation_pings(esc, {1, 3}) == [])

    fresh = [{"id": 1, "to_name": "human", "from_name": "pr-sweep"},
             {"id": 2, "to_name": "api-worker", "from_name": "pr-sweep"}]
    ok &= check("a human escalation is closed by the ping itself",
                swarm.escalations_to_close(fresh) == [1])
    ok &= check("a session-addressed escalation stays queued for injection",
                2 not in swarm.escalations_to_close(fresh))

    # --- restore/clean planning ---------------------------------------------
    S = [
        {"name": "bff", "role": "worker", "project": "shop", "workdir": "/w/bff",
         "spawn_prompt": "bff work", "closed_at": 500.0},
        {"name": "api", "role": "worker", "project": "shop", "workdir": "",
         "spawn_prompt": "", "closed_at": 900.0},
        {"name": "live", "role": "worker", "project": "shop", "workdir": "/w/l",
         "spawn_prompt": "", "closed_at": 0.0},
    ]
    T = [
        {"id": 1, "state": "doing", "owner": "bff"},
        {"id": 2, "state": "done", "owner": "bff"},
        {"id": 3, "state": "todo", "owner": "api"},
        {"id": 4, "state": "doing", "owner": "live"},
    ]
    auto = restore_candidates(S, T)
    ok &= check("auto restore = closed sessions owning non-done work",
                [c["name"] for c in auto] == ["api", "bff"])
    ok &= check("candidate carries task ids (non-done only)",
                next(c for c in auto if c["name"] == "bff")["task_ids"] == [1])
    named = restore_candidates(S, T, names=["live"])
    ok &= check("named restore includes a live session",
                len(named) == 1 and named[0]["name"] == "live"
                and named[0]["live"] is True)
    ok &= check("named restore of a session owning no non-done work -> empty",
                restore_candidates(S, T, names=["nobody"]) == [])

    txt = restore_plan_text(auto, spawn_arm="wild")
    ok &= check("plan shows workdir + tasks", "/w/bff" in txt and "#1" in txt)
    ok &= check("plan flags no-workdir candidate", "SKIP api" in txt)
    ok &= check("plan no arm warning when armed", "will not act" not in txt)
    ok &= check("plan warns when spawn_arm off",
                "will not act" in restore_plan_text(auto, spawn_arm="off"))
    ok &= check("named-live plan notes zombie tab",
                "zombie" in restore_plan_text(named, spawn_arm="wild"))
    miss_txt = restore_plan_text(
        [{"name": "bff", "role": "worker", "project": "shop",
          "workdir": "/nonexistent/relay-x", "spawn_prompt": "bff work",
          "task_ids": [1], "live": False}],
        spawn_arm="wild", missing_workdirs={"bff"})
    ok &= check("missing workdir renders SKIP line",
                "workdir no longer exists" in miss_txt and "SKIP bff" in miss_txt)
    ok &= check("empty restore plan notes nothing to restore",
                "(nothing to restore)" in restore_plan_text([], spawn_arm="wild"))

    cc = clean_candidates(S, T)
    ok &= check("clean candidates = all closed sessions",
                [c["name"] for c in cc] == ["api", "bff"])
    ok &= check("clean plan resets + removes",
                "reset" in clean_plan_text(cc) and "remove" in clean_plan_text(cc))

    rp = resume_prompt("bff", "shop", "worker", "bff work")
    ok &= check("resume prompt invokes skill + RESUMING + mission",
                "relay-worker" in rp and "RESUMING" in rp and "bff work" in rp
                and "relay task list --mine" in rp)

    # --- wipe planning ------------------------------------------------------
    WS = [
        {"name": "dead", "closed_at": 500.0},
        {"name": "live", "closed_at": 0.0},
    ]
    WT = [
        {"id": 1, "owner": "dead", "state": "doing", "blocked_by": ""},
        {"id": 2, "owner": "dead", "state": "done", "blocked_by": ""},
        {"id": 3, "owner": "live", "state": "todo", "blocked_by": "1"},
    ]
    wc = wipe_candidates(WS, WT)
    ok &= check("wipe candidates = closed sessions only",
                [c["name"] for c in wc] == ["dead"])
    ok &= check("wipe includes done tasks",
                sorted(wc[0]["task_ids"]) == [1, 2])
    ok &= check("wipe names filter to a closed session",
                [c["name"] for c in wipe_candidates(WS, WT, names=["dead"])] == ["dead"])
    ok &= check("wipe names filter excludes a live session",
                wipe_candidates(WS, WT, names=["live"]) == [])

    warns = wipe_blocker_warnings(wc, WT)
    ok &= check("blocker warning fires across the wipe boundary",
                any("#1 is a blocker of #3" in w for w in warns))
    # if the dependent is ALSO wiped, no warning
    WT2 = WT + [{"id": 4, "owner": "dead", "state": "todo", "blocked_by": "1"}]
    wc2 = wipe_candidates(WS, WT2)
    warns2 = wipe_blocker_warnings(wc2, WT2)
    ok &= check("no warning when dependent is also wiped",
                not any("#4" in w for w in warns2))

    txt = wipe_plan_text(wc)
    ok &= check("wipe plan lists session + task count",
                "dead" in txt and "delete" in txt.lower())
    ok &= check("empty wipe plan", "(nothing to wipe)" in wipe_plan_text([]))
    allt = wipe_plan_text([], project_all=(5, 2, 9))
    ok &= check("project --all plan shows totals",
                "5" in allt and "2" in allt and "9" in allt)

    wsess = [{"name": "w1", "closed_at": 5, "workdir": "/tmp/r-w1",
              "worktree_repo": "/tmp/r"}]
    wc = wipe_candidates(wsess, [])
    ok &= check("wipe candidate carries worktree fields",
                wc[0]["workdir"] == "/tmp/r-w1"
                and wc[0]["worktree_repo"] == "/tmp/r")
    wc[0]["worktree_action"] = "remove"
    ok &= check("wipe plan shows worktree removal",
                "remove worktree /tmp/r-w1" in wipe_plan_text(wc)
                and "relay/w1" in wipe_plan_text(wc))
    wc[0]["worktree_action"] = "keep-dirty"
    ok &= check("wipe plan keeps dirty worktree",
                "uncommitted" in wipe_plan_text(wc))

    # wipe_plan_text: msg_count renders when stamped, absent stays terse
    mcands = [{"name": "g", "task_ids": [1, 2], "workdir": "",
               "worktree_repo": "", "msg_count": 3}]
    ok &= check("wipe plan shows message count when stamped",
                "delete 2 task(s), 3 message(s), session g"
                in wipe_plan_text(mcands))
    mcands[0].pop("msg_count")
    ok &= check("wipe plan omits messages when not stamped",
                "delete 2 task(s), session g" in wipe_plan_text(mcands))

    # --- TUI visuals: ages, fleet line, interactions, bars, markup -----------
    ok &= check("fmt_age seconds/minutes/hours",
                (swarm.fmt_age(8), swarm.fmt_age(250), swarm.fmt_age(7300))
                == ("8s", "4m", "2h"))

    FS = [{"name": "coord", "role": "coordinator", "project": "p",
           "status_text": "", "mode": "safe"},
          {"name": "bff", "role": "worker", "project": "p",
           "status_text": "", "mode": "wild"},
          {"name": "api", "role": "worker", "project": "p",
           "status_text": "", "mode": ""},
          {"name": "etl", "role": "worker", "project": "p",
           "status_text": "", "mode": ""}]
    FT = [{"id": 1, "owner": "bff", "state": "doing", "project": "p",
           "parent_id": None, "title": "x", "blocked_by": ""},
          {"id": 2, "owner": "api", "state": "blocked", "project": "p",
           "parent_id": None, "title": "y", "blocked_by": ""}]
    fl = swarm.fleet_line(FS, FT, stale={"etl"}, queued=3)
    ok &= check("fleet line counts busy/blocked/idle",
                "4 units" in fl and "1 busy" in fl and "1 blocked" in fl
                and "2 idle" in fl)
    ok &= check("fleet line armed glyph counts", "◉1" in fl and "▲1" in fl)
    ok &= check("fleet line stale + queued", "1 STALE" in fl
                and "msgs 3 queued" in fl)

    # --- fleet map: one hex per unit, peripheral vision only -----------------
    import re as _re
    plain = lambda s: _re.sub(r"\[/?[^\]]*\]", "", s)  # noqa: E731

    ok &= check("no map for a small fleet (the roster already shows it)",
                swarm.fleet_map(FS, FT, stale={"etl"}) == [])

    def _unit(i, mode=""):
        return {"name": f"w{i}", "role": "worker", "project": "p",
                "status_text": "", "mode": mode}
    BIG = [_unit(i, "wild" if i % 2 else "") for i in range(14)]
    BT = [{"id": 1, "owner": "w1", "state": "doing", "project": "p",
           "parent_id": None, "title": "x", "blocked_by": ""},
          {"id": 2, "owner": "w2", "state": "blocked", "project": "p",
           "parent_id": None, "title": "y", "blocked_by": ""}]
    m = swarm.fleet_map(BIG, BT, stale={"w3"})
    cells = sum(len([c for c in plain(r) if c in "●○"]) for r in m)
    ok &= check("every unit gets exactly one cell", cells == len(BIG))
    ok &= check("the map is a honeycomb (rows offset by half a cell)",
                len(m) > 1
                and (len(plain(m[0])) - len(plain(m[0]).lstrip()))
                != (len(plain(m[1])) - len(plain(m[1]).lstrip())))
    joined = "".join(m)
    ok &= check("stale is red, blocked yellow, busy green, idle dim",
                "[red]●" in joined and "[yellow]●" in joined
                and "[green]●" in joined and "[dim]○" in joined)
    ok &= check("a unit that needs a human outranks one that is merely busy",
                swarm._unit_cell("w3", {"w3"}, set(), set(), "wild")
                == swarm._unit_cell("w3", {"w3"}, {"w3"}, {"w3"}, "wild"))
    big_view = swarm.render_swarm(BIG, BT, [], now=0.0, stale={"w3"})
    ok &= check("the swarm view shows the map under the fleet line",
                "●" in big_view
                and big_view.index("FLEET") < big_view.index("●"))
    ok &= check("a small fleet's view has no map",
                "●" not in swarm.render_swarm(FS, FT, [], now=0.0))

    IM = [{"from_name": "coord", "to_name": "bff", "created_at": 100.0,
           "kind": "info", "delivered_at": 1},
          {"from_name": "bff", "to_name": "coord", "created_at": 200.0,
           "kind": "done", "delivered_at": 1},
          {"from_name": "coord", "to_name": "api", "created_at": 300.0,
           "kind": "blocked", "delivered_at": 1},
          {"from_name": "relay", "to_name": "api", "created_at": 400.0,
           "kind": "wake", "delivered_at": 1}]
    rows = swarm.interaction_rows(IM, coordinators={"coord"}, now=400.0)
    ok &= check("interactions: relay wake-ups excluded, 2 pairs",
                len(rows) == 2)
    ok &= check("interactions: coordinator listed first",
                all(r["a"] == "coord" for r in rows))
    cb = next(r for r in rows if r["b"] == "bff")
    ok &= check("interactions: direction counts", cb["sent"] == 1
                and cb["recv"] == 1)
    ca = next(r for r in rows if r["b"] == "api")
    ok &= check("interactions: blocked pair flagged, fresh first",
                ca["flag"] and rows[0]["b"] == "api")
    many = [{"from_name": f"w{i}", "to_name": "coord", "created_at": float(i),
             "kind": "info", "delivered_at": 1} for i in range(9)]
    ok &= check("interactions capped at 6",
                len(swarm.interaction_rows(many, now=10.0)) == 6)

    ok &= check("progress bar halves", swarm.progress_bar(4, 8)
                == "▰▰▰▰▰▱▱▱▱▱")
    ok &= check("progress bar zero total", swarm.progress_bar(0, 0)
                == "▱▱▱▱▱▱▱▱▱▱")

    vs = swarm.render_swarm(
        FS, FT,
        [{"from_name": "bff", "to_name": "coord", "created_at": 390.0,
          "kind": "escalation", "delivered_at": None,
          "body": "[red]hostile[/red] help"}],
        now=400.0, stale={"etl"}, activity={"bff": 388.0})
    ok &= check("render: fleet line on top", vs.splitlines()[0]
                .startswith("FLEET"))
    # The section headings are titled rules now (chrome.rule): a screen's own
    # name stays upper case (PARKED / TIMERS / SWARM), a region inside it is
    # lower case, so the two never read as the same kind of thing.
    ok &= check("render: interactions section", "interactions" in vs)
    ok &= check("render: interactions heading is a titled rule",
                any(l.startswith("──") and "interactions" in l
                    for l in vs.splitlines()))
    ok &= check("render: heartbeat age on roster", "12s" in vs)
    ok &= check("render: stale roster row marked", "⧗" in vs)
    ok &= check("render: escalation feed line colored",
                "[red]" in vs and "escalation" in vs)
    ok &= check("render: hostile body escaped, not executed as markup",
                "\\[red]hostile" in vs)

    # --- live-scoped stakes (the cry-wolf fix) -------------------------------
    # registry: bare-sid -> session row. Only sids relay currently SEES live
    # (in live_sids) count as live names.
    reg = {"sidA": {"name": "w1"}, "sidB": {"name": "w2"},
           "sidGone": {"name": "ghost"}}
    live = swarm.live_names(reg, {"sidA", "sidB"})   # sidGone not watched
    ok &= check("live_names = watched sessions only",
                live == {"w1", "w2"})
    ok &= check("live_names excludes an unwatched (dead-tab) session",
                "ghost" not in live)

    msgs = [{"to_name": "w1", "delivered_at": None},
            {"to_name": "ghost", "delivered_at": None},   # stale target
            {"to_name": "ghost", "delivered_at": None}]
    ok &= check("live_queued_count counts only messages to live targets",
                swarm.live_queued_count(msgs, live) == 1)
    ok &= check("live_queued_count is 0 when nothing is live",
                swarm.live_queued_count(msgs, set()) == 0)

    tks = [{"state": "doing", "owner": "w1"},
           {"state": "doing", "owner": "ghost"},     # orphan, not a live stake
           {"state": "todo", "owner": "w2"},         # not doing
           {"state": "doing", "owner": None}]        # ownerless
    ok &= check("live_doing_count counts only live-owned doing tasks",
                swarm.live_doing_count(tks, live) == 1)

    # --- worktree_removals: --all must clean up worktrees too ----------------
    sess = [
        {"name": "w-clean", "worktree_repo": "/repo", "workdir": "/repo-w-clean"},
        {"name": "w-dirty", "worktree_repo": "/repo", "workdir": "/repo-w-dirty"},
        {"name": "w-gone",  "worktree_repo": "/repo", "workdir": "/repo-w-gone"},
        {"name": "no-wt",   "worktree_repo": "",      "workdir": "/somewhere"},
    ]
    exists = lambda p: p != "/repo-w-gone"          # w-gone's dir is gone
    dirty = lambda p: p == "/repo-w-dirty"          # only w-dirty has changes
    rem = swarm.worktree_removals(sess, exists, dirty)
    by = {r["name"]: r["action"] for r in rem}
    ok &= check("worktree_removals: clean worktree -> remove",
                by.get("w-clean") == "remove")
    ok &= check("worktree_removals: dirty worktree -> keep-dirty (never destroyed)",
                by.get("w-dirty") == "keep-dirty")
    ok &= check("worktree_removals: vanished workdir skipped",
                "w-gone" not in by)
    ok &= check("worktree_removals: session without a relay worktree skipped",
                "no-wt" not in by)
    ok &= check("worktree_removals: carries repo + workdir for the git call",
                any(r["name"] == "w-clean" and r["repo"] == "/repo"
                    and r["workdir"] == "/repo-w-clean" for r in rem))

    # --- PR ref parsing -----------------------------------------------------
    ok &= check("parse_pr_ref splits owner/name#number",
                swarm.parse_pr_ref("acme/api#482") == ("acme/api", 482))
    ok &= check("parse_pr_ref accepts dots and dashes in the repo",
                swarm.parse_pr_ref("my-org/api.core#7")
                == ("my-org/api.core", 7))
    for bad in ("acme/api", "482", "acme/api#", "#482", "acme#482",
                "acme/api#abc", "a/b#1#2", "", "acme/api#-1"):
        ok &= check(f"parse_pr_ref rejects {bad!r}",
                    swarm.parse_pr_ref(bad) is None)

    # GitHub repo names are case-insensitive; a mixed-case ref must resolve
    # to the SAME (repo, number) as its lowercase form, or one PR becomes two
    # rows under a case-sensitive index (Finding 3).
    ok &= check("parse_pr_ref lowercases the repo",
                swarm.parse_pr_ref("Acme/API#482") == ("acme/api", 482))
    ok &= check("mixed case and lowercase refs parse identically",
                swarm.parse_pr_ref("Acme/API#482")
                == swarm.parse_pr_ref("acme/api#482"))
    ok &= check("parse_pr_ref leaves the PR number untouched",
                swarm.parse_pr_ref("ACME/API#007") == ("acme/api", 7))

    # --- route resolution ---------------------------------------------------
    live = {"name": "api-worker", "iterm_session_id": "SID-A", "closed_at": 0}
    pr = {"owner": "api-worker", "owner_session_id": "SID-A"}
    ok &= check("routable when the owner session is the claiming session",
                swarm.resolve_pr_route(pr, live) == ("ok", "api-worker"))

    ok &= check("no row at all is unclaimed",
                swarm.resolve_pr_route(None, None)[0] == "unclaimed")
    ok &= check("a row the sweep pushed but nobody claimed is unclaimed",
                swarm.resolve_pr_route(
                    {"owner": "", "owner_session_id": ""}, None)[0]
                == "unclaimed")

    st, why = swarm.resolve_pr_route(pr, None)
    ok &= check("owner name no longer registered is gone", st == "gone")
    ok &= check("gone reason names the missing session",
                "api-worker" in why)

    st, why = swarm.resolve_pr_route(
        pr, {"name": "api-worker", "iterm_session_id": "SID-A",
             "closed_at": 123.0})
    ok &= check("closed owner session is gone", st == "gone")
    ok &= check("gone reason says closed", "closed" in why)

    # The bug owner_session_id exists to prevent: the name was reclaimed by a
    # different tab, which never saw this branch.
    st, why = swarm.resolve_pr_route(
        pr, {"name": "api-worker", "iterm_session_id": "SID-Z",
             "closed_at": 0})
    ok &= check("name rebound to a different tab is gone, NOT routable",
                st == "gone")
    ok &= check("gone reason says rebound", "rebound" in why)

    # --- PR pane ------------------------------------------------------------
    now = 10_000.0
    sess = [{"name": "api-worker", "iterm_session_id": "SID-A",
             "closed_at": 0, "role": "worker", "project": "webshop",
             "status_text": ""}]
    prs = [
        {"repo": "acme/api", "number": 482, "state": "changes",
         "state_changed_at": now - 4 * 3600, "owner": "api-worker",
         "owner_session_id": "SID-A", "task_id": 14, "project": "webshop"},
        {"repo": "acme/bff", "number": 77, "state": "changes",
         "state_changed_at": now - 86400, "owner": "",
         "owner_session_id": "", "task_id": None, "project": "webshop"},
        {"repo": "acme/api", "number": 480, "state": "merged",
         "state_changed_at": now - 2 * 86400, "owner": "api-worker",
         "owner_session_id": "SID-A", "task_id": 11, "project": "webshop"},
    ]
    rows = swarm.pr_rows(prs, sess, now)
    ok &= check("pr_rows preserves the stable repo/number order it was given",
                [r["ref"] for r in rows]
                == ["acme/api#482", "acme/bff#77", "acme/api#480"])
    ok &= check("changes-requested is flagged for attention",
                rows[0]["flag"] is True)
    ok &= check("an unclaimed PR is flagged and labelled UNCLAIMED",
                rows[1]["flag"] is True
                and rows[1]["owner_label"] == "UNCLAIMED")
    ok &= check("a merged PR is not flagged", rows[2]["flag"] is False)

    gone_rows = swarm.pr_rows(
        [dict(prs[0], owner_session_id="SID-OLD")], sess, now)
    ok &= check("an owner whose name was rebound is flagged GONE",
                gone_rows[0]["flag"] is True
                and "GONE" in gone_rows[0]["owner_label"])

    text = "\n".join(swarm.render_prs(rows, width=100))
    ok &= check("the pane shows the age beside every state, never a bare "
                "state", text.count("4h") >= 1 and text.count("1d") >= 1)
    ok &= check("flagged rows are duplicated into an attention strip above",
                text.count("acme/api#482") == 2)
    ok &= check("unflagged rows appear exactly once",
                text.count("acme/api#480") == 1)
    # The separator is the bare dash line INSIDE the pane - located by shape,
    # not by "the first ─ in the text", which is now the titled rule the
    # section opens with.
    _lines = text.splitlines()
    _sep = next(i for i, l in enumerate(_lines)
                if set(l.strip()) == {"─"})
    ok &= check("the attention strip sits above the separator",
                any("acme/api#482" in l for l in _lines[:_sep])
                and any("acme/api#482" in l for l in _lines[_sep + 1:]))

    # Empty renders like MESSAGES does - header plus a "(none)" line - so the
    # section never looks like a missing feature, and the empty state teaches
    # how it gets filled.
    empty = swarm.render_prs([], 100)
    ok &= check("render_prs still renders a header with no PRs",
                "pull requests" in empty[0] and empty[0].startswith("── "))
    ok &= check("the empty pane says none and names the verb that fills it",
                len(empty) == 2 and "(none" in empty[1]
                and "relay pr claim" in empty[1])

    ok &= check("the fleet line counts PRs and how many need work",
                "PRs 3 · 2 need work"
                in swarm.fleet_line(sess, [], prs=rows))

    full = swarm.render_swarm(sess, [], [], now, width=100, prs=prs)
    ok &= check("render_swarm includes the PR pane", "pull requests" in full)
    noprs = swarm.render_swarm(sess, [], [], now, width=100)
    ok &= check("render_swarm shows the PR pane even with no prs argument",
                "pull requests" in noprs)
    ok &= check("that empty pane carries the (none) line, not stray rows",
                "(none" in noprs.split("pull requests")[1].split("messages")[0])

    kb = swarm.render_swarm(
        sess,
        [{"id": 14, "project": "webshop", "parent_id": None, "state": "doing",
          "title": "rate limiting", "owner": "api-worker"}],
        [], now, width=120, prs=prs)
    ok &= check("a task with a PR shows it on its kanban card",
                "PR 482" in kb)

    # --- kanban PR suffix: floor-width degrade, never overflow the column --
    # Code review finding: `width=60` is the app's real floor (`w = max(60,
    # ...)`), giving colw=12 - narrower than even the shortest legible PR
    # suffix. The suffix must degrade (full -> "PRnnn" -> nothing) instead
    # of overflowing into the neighbouring kanban column.
    floor_tasks = [
        {"id": 14, "project": "webshop", "parent_id": None, "state": "doing",
         "title": "rate limiting across every gateway endpoint we own",
         "owner": "api-worker"},
        {"id": 15, "project": "webshop", "parent_id": None, "state": "doing",
         "title": "a totally unrelated task with no PR at all",
         "owner": "api-worker"},
    ]
    floor_prs = [dict(prs[0], state="approved")]
    floor_view = swarm.render_swarm(sess, floor_tasks, [], now, width=60,
                                    prs=floor_prs)
    colw = max(12, (60 - 3 * 3) // 4)
    row_width = 4 * colw + 3 * 3
    line14 = next(l for l in floor_view.splitlines() if "#14" in l)
    ok &= check("a PR suffix that cannot fit degrades instead of "
                "overflowing the kanban row",
                len(line14) == row_width)
    ok &= check("the degraded card still shows the task in its own column",
                "#14" in line14[colw + 3: 2 * colw + 3])

    no_pr_view = swarm.render_swarm(sess, [floor_tasks[1]], [], now,
                                    width=60)
    with_pr_view = swarm.render_swarm(sess, [floor_tasks[1]], [], now,
                                      width=60, prs=floor_prs)
    line15_no_pr = next(l for l in no_pr_view.splitlines() if "#15" in l)
    line15_with_pr = next(l for l in with_pr_view.splitlines() if "#15" in l)
    ok &= check("a card with no matching PR renders byte-identical whether "
                "or not prs is passed",
                line15_no_pr == line15_with_pr)

    # --- discussions pane ----------------------------------------------------
    def _th(i, topic, state="open", outcome="", cap=3, parts="a,b",
            created=0.0):
        return {"id": i, "topic": topic, "state": state, "outcome": outcome,
                "rounds_cap": cap, "participants": parts, "opener": "a",
                "created_at": created, "closed_at": 0.0, "project": "p"}

    empty = swarm.render_discussions([], 100)
    ok &= check("discussions pane renders when empty",
                any("discussions" in ln and ln.startswith("── ")
                    for ln in empty))
    ok &= check("the empty pane teaches how to open one",
                any("relay discuss" in ln for ln in empty))

    r_open = swarm.thread_row(_th(1, "one DB or many?"),
                              [{"from_name": "a", "kind": "agree",
                                "body": "X", "created_at": 1, "id": 1}],
                              now=100.0)
    ok &= check("thread_row counts settled participants",
                r_open["settled"] == 1 and r_open["total"] == 2)
    ok &= check("an open thread is not flagged", not r_open["flag"])
    r_done = swarm.thread_row(_th(2, "cache?", state="closed",
                                  outcome="a: yes | b: no"), [], now=100.0)
    ok &= check("a closed thread is flagged for the operator",
                r_done["flag"])

    pane = swarm.render_discussions([r_open, r_done], 100)
    body = "\n".join(pane)
    ok &= check("pane shows the topic", "one DB or many?" in body)
    ok &= check("pane shows settled counts", "1/2" in body)
    ok &= check("pane shows how it ended", "closed" in body)
    ok &= check("pane duplicates what needs attention on top",
                body.count("cache?") == 2)
    ids = [ln for ln in pane if "#1" in ln or "#2" in ln]
    ok &= check("the main list stays in id order",
                ids[-2].find("#1") >= 0 and ids[-1].find("#2") >= 0)

    # --- thread verdicts ----------------------------------------------------
    def _m(frm, kind, body, t):
        return {"from_name": frm, "kind": kind, "body": body,
                "created_at": float(t), "id": int(t)}

    parts = ["a", "b"]
    posts = [_m("a", "say", "I think X", 1)]
    ok &= check("one post is not agreement",
                swarm.thread_verdict(parts, posts, 3)[0] == "open")
    ok &= check("round_counts counts says",
                swarm.round_counts(posts) == {"a": 1})
    ok &= check("agree does not consume a round",
                swarm.round_counts(posts + [_m("a", "agree", "X", 2)])
                == {"a": 1})
    ok &= check("a lone agree is not unanimity",
                swarm.thread_verdict(parts, posts
                                     + [_m("a", "agree", "X", 2)], 3)[0]
                == "open")

    both = posts + [_m("a", "agree", "X", 2), _m("b", "agree", "X too", 3)]
    st, outcome = swarm.thread_verdict(parts, both, 3)
    ok &= check("unanimous agree closes agreed", st == "agreed")
    ok &= check("outcome carries both positions",
                "X" in outcome and "X too" in outcome)

    retracted = both + [_m("b", "say", "actually, wait", 4)]
    ok &= check("a say after agree retracts it",
                swarm.thread_verdict(parts, retracted, 3)[0] == "open")
    ok &= check("re-agreeing after a retraction settles again",
                swarm.thread_verdict(
                    parts, retracted + [_m("b", "agree", "ok X", 5)],
                    3)[0] == "agreed")

    # Relay never closes a discussion for running long. A spent budget is not
    # a verdict, and declaring one would be relay deciding the agents failed.
    capped = []
    for i in (1, 2, 3):
        capped.append(_m("a", "say", f"a post {i}", i * 10))
        capped.append(_m("b", "say", f"b post {i}", i * 10 + 1))
    ok &= check("a spent budget does NOT close the discussion",
                swarm.thread_verdict(parts, capped, 3)[0] == "open")
    ok &= check("relay never produces an 'unresolved' verdict itself",
                swarm.thread_verdict(parts, capped * 5, 1)[0] == "open")
    ok &= check("only the agents' own agreement closes it",
                swarm.thread_verdict(
                    parts, capped + [_m("a", "agree", "fine", 99),
                                     _m("b", "agree", "fine", 100)],
                    3)[0] == "agreed")
    ok &= check("round_counts still reports the budget for display",
                swarm.round_counts(capped) == {"a": 3, "b": 3})
    ok &= check("a participant who never posted blocks agreement",
                swarm.thread_verdict(["a", "b", "c"], both, 3)[0] == "open")

    # --- batch_delivery_text ------------------------------------------------
    one = [{"id": 5, "from_name": "a", "body": "just this", "kind": "info"}]
    bt = swarm.batch_delivery_text(one)
    ok &= check("single message keeps the inline body", "just this" in bt)
    ok &= check("single message names its id", "5" in bt)
    ok &= check("single message teaches reply", "relay reply" in bt)
    ok &= check("delivery text is one line", "\n" not in bt)
    ok &= check("empty batch is empty", swarm.batch_delivery_text([]) == "")

    many = [{"id": i, "from_name": f"s{i}", "body": f"body{i}",
             "kind": "info"} for i in range(1, 4)]
    btm = swarm.batch_delivery_text(many)
    ok &= check("batch is one line", "\n" not in btm)
    ok &= check("batch counts the messages", "3" in btm)
    ok &= check("batch points at inbox", "relay inbox" in btm)
    ok &= check("batch names its senders", "s1" in btm and "s3" in btm)

    huge = [{"id": 1, "from_name": "a", "body": "x" * 5000, "kind": "info"}]
    ok &= check("delivery text is bounded",
                len(swarm.batch_delivery_text(huge)) <= 700)
    ok &= check("control characters are stripped",
                "\x1b" not in swarm.batch_delivery_text(
                    [{"id": 1, "from_name": "a", "body": "a\x1b[Bb",
                      "kind": "info"}]))
    ok &= check("a newline in a body cannot split the injected turn",
                "\n" not in swarm.batch_delivery_text(
                    [{"id": 1, "from_name": "a", "body": "one\ntwo",
                      "kind": "info"}]))

    # --- thread pointers ----------------------------------------------------
    tp = [{"id": 9, "from_name": "api", "body": "one per service",
           "kind": "say", "thread_id": 7}]
    pt = swarm.batch_delivery_text(tp)
    ok &= check("thread delivery points at relay thread",
                "relay thread 7" in pt)
    # A thread is posted to with `relay say`; naming `relay reply` in the one
    # line a woken session is guaranteed to read would send it down the wrong
    # verb entirely.
    ok &= check("thread pointer does not say reply", "relay reply" not in pt)
    ok &= check("thread pointer names the sender", "api" in pt)
    ok &= check("thread pointer is one line", "\n" not in pt)
    ok &= check("thread pointer does not inline the payload",
                "one per service" not in pt)
    mixed = tp + [{"id": 10, "from_name": "bff", "body": "shared",
                   "kind": "say", "thread_id": 7}]
    ok &= check("several posts in one thread collapse to one pointer",
                swarm.batch_delivery_text(mixed).count("relay thread 7") == 1)
    ok &= check("the pointer counts the posts",
                "2" in swarm.batch_delivery_text(mixed))
    # Mixed traffic (a thread post AND a plain message) must NOT masquerade as
    # a pure thread pointer, or the plain message would be silently invisible.
    both_kinds = tp + [{"id": 11, "from_name": "z", "body": "unrelated",
                        "kind": "info"}]
    ok &= check("mixed thread + plain traffic falls back to the inbox pointer",
                "relay inbox" in swarm.batch_delivery_text(both_kinds))

    # --- derive_name --------------------------------------------------------
    ok &= check("derive_name uses the cwd basename",
                swarm.derive_name("/Users/x/Work/api", set()) == "api")
    ok &= check("derive_name slugifies",
                swarm.derive_name("/Users/x/My Big_Repo!", set())
                == "my-big-repo")
    ok &= check("derive_name dedupes with -2",
                swarm.derive_name("/Users/x/api", {"api"}) == "api-2")
    ok &= check("derive_name dedupes past -2",
                swarm.derive_name("/Users/x/api", {"api", "api-2"})
                == "api-3")
    ok &= check("derive_name never yields a reserved name",
                swarm.derive_name("/tmp/human", set()) == "human-2")
    # The relay repo is itself called 'relay', which is reserved - a session
    # working ON relay must not derive the one name the watcher refuses to
    # deliver to.
    ok &= check("derive_name suffixes 'relay' rather than proposing it",
                swarm.derive_name("/Users/x/Work/relay", set()) == "relay-2")
    ok &= check("derive_name falls back when the basename is empty",
                swarm.derive_name("/", set()) == "session")
    ok &= check("derive_name handles a trailing slash",
                swarm.derive_name("/Users/x/api/", set()) == "api")
    ok &= check("derive_name truncates long basenames",
                len(swarm.derive_name("/x/" + "a" * 80, set())) <= 24)
    ok &= check("derive_name tolerates an empty cwd",
                swarm.derive_name("", set()) == "session")

    # --- working copies ------------------------------------------------------
    import tempfile
    d = tempfile.mkdtemp()
    ok &= check("same_checkout ignores a trailing slash",
                swarm.same_checkout(d, d + "/"))
    link = os.path.join(tempfile.mkdtemp(), "alias")
    os.symlink(d, link)
    ok &= check("same_checkout resolves symlinks (one dir, two paths)",
                swarm.same_checkout(d, link))
    ok &= check("different directories are different checkouts",
                not swarm.same_checkout(d, tempfile.mkdtemp()))
    # Unknown must never read as a match: a spawn is REFUSED on the back of
    # this, and missing data must not manufacture a collision.
    ok &= check("an unknown workdir matches nothing",
                not swarm.same_checkout("", d)
                and not swarm.same_checkout("", ""))
    # real_workdir must agree with db._norm_workdir on the root path: stripping
    # "/" down to "" would silently reclassify a literal root workdir as
    # "unknown", which same_checkout treats as "never matches".
    ok &= check("real_workdir preserves a literal root path",
                swarm.real_workdir("/") == "/")

    def _sess_row(name, workdir, role="worker", closed_at=0):
        return {"name": name, "workdir": workdir, "role": role,
                "closed_at": closed_at}

    rows = [_sess_row("w1", d), _sess_row("w2", link),
            _sess_row("dead", d, closed_at=123.0),
            _sess_row("coord", d, role="coordinator"),
            _sess_row("elsewhere", tempfile.mkdtemp())]
    held = swarm.checkout_occupants(rows, d)
    ok &= check("occupants find every live worker in that checkout",
                held == ["w1", "w2"])
    ok &= check("a closed session holds nothing", "dead" not in held)
    ok &= check("a coordinator in the repo is not a collision",
                "coord" not in held)
    ok &= check("occupants can exclude the session being (re)spawned",
                swarm.checkout_occupants(rows, d, exclude=("w1",)) == ["w2"])
    ok &= check("occupants of an empty directory list is empty",
                swarm.checkout_occupants(rows, tempfile.mkdtemp()) == [])

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def test_session_working():
    ok = True
    expected = {
        "idle_accept_edits": False,
        "working_accept_edits": True,
        "working_manual_mode": True,
        "working_with_agent_rows": True,
        "draft_in_box": True,
        "input_placeholder": True,
        "selection_dialog": False,
        "shell_zsh": False,
        "live_draft": False,
        "idle_nbsp_row": False,      # idle, so not working
    }
    for name, want in expected.items():
        got = session_working(load_screen(name))
        ok &= check(f"session_working({name}) is {want}", got == want)
    ok &= check("no fixture is missing",
                len(list(_SCREEN_DIR.glob('*.txt'))) == len(expected))
    ok &= check("empty screen is not working", session_working([]) is False)

    # Fix round 1: Claude Code appends an unbounded number of agent/task rows
    # below the footer. A fixed-size tail window can scroll the footer out of
    # range on a busy session running several concurrent subagents - the
    # unsafe direction, since claude_prompt_ready treats
    # session_working() == False as one signal that a screen is idle and
    # safe to type into. This must stay True no matter how many rows trail
    # the footer.
    swarmed = load_screen("working_with_agent_rows") + [
        f"  ◯ worker-{i}  doing things" for i in range(12)
    ]
    ok &= check("footer stays visible behind 12+ trailing agent rows",
                session_working(swarmed) is True)

    # The anchor is _bracket_line, not a bare "^-+$" rule. Older Claude Code
    # draws the box with corners, which a bare-rule anchor never matches - so
    # every legacy screen was unanchored and could never read working, while
    # _INPUT_BOX_RE still matched its "| >" row. A legacy session mid-turn
    # therefore read as READY and relay would have typed into a live turn.
    # Keeping the old input shape is only worth doing if this keeps up with it.
    _legacy_box = ["╭" + "─" * 20 + "╮",
                   "│ >                  │",
                   "╰" + "─" * 20 + "╯"]
    _legacy_working = _legacy_box + [
        "  ⏵⏵ accept edits on · esc to interrupt · ← for agents"]
    _legacy_idle = _legacy_box + ["  ⏵⏵ accept edits on · ← for agents"]
    ok &= check("a legacy corner-drawn box mid-turn reads working",
                session_working(_legacy_working) is True)
    ok &= check("a legacy corner-drawn box at rest reads idle",
                session_working(_legacy_idle) is False)
    ok &= check("a working legacy session is never ready to type into",
                claude_prompt_ready(_legacy_working) is False)
    ok &= check("an idle legacy session is still ready",
                claude_prompt_ready(_legacy_idle) is True)

    # Fix round 2: anchoring on the LAST rule line (fix round 1's approach)
    # reopens the exact same bug class it closed - just with the trigger
    # moved from row COUNT to row CONTENT. Claude Code's trailing agent/task
    # rows are unbounded in both number and shape, and nothing stops one of
    # them from rendering as a bare run of "─" (e.g. a sub-agent's own box
    # border scrolling into view). If that happens below the real footer,
    # a last-rule anchor jumps PAST the footer and reports a working session
    # as idle - the unsafe direction. Anchoring on the FIRST rule instead
    # cannot be pushed past the footer by anything appended after it.
    base = load_screen("working_with_agent_rows")
    ok &= check(
        "a bare trailing rule line below the footer does not hide it",
        session_working(base + ["─" * 15]) is True)
    ok &= check(
        "a 200-char trailing rule line below the footer does not hide it",
        session_working(base + ["─" * 200]) is True)
    interleaved = list(base)
    for i in range(5):
        interleaved.append(f"  ◯ worker-{i}  doing things")
        interleaved.append("─" * (10 + i))
    ok &= check(
        "several trailing rules interleaved among agent rows do not hide the footer",
        session_working(interleaved) is True)

    # The accepted cost, named explicitly: first-rule anchoring can only
    # fail SAFE. If a "─" rule happens to sit up in ordinary scrollback
    # (e.g. an old box border from a prior turn, still visible above the
    # current input box) and a stale "esc to interrupt" from that same old
    # turn is still on screen between that old rule and the real footer,
    # the scan region now starts too EARLY and picks up the stale marker -
    # a false True on a session that is actually idle. This is deliberate,
    # not a bug: a delayed message is cheap and self-corrects as the screen
    # scrolls, unlike the false False that motivated this fix.
    fail_safe_not_precise = [
        "─" * 10,  # a rule up in scrollback, e.g. an old box border
        "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt · ← for agents",  # stale, from a prior turn
        "  some other old scrollback content",
        "─" * 10,  # rule directly above the real (current) input box
        "❯",
        "─" * 10,  # rule directly above the real footer
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",  # real footer: idle, no marker
    ]
    ok &= check(
        "ACCEPTED COST (fail-safe, not desired precision): a rule + stale "
        "marker in scrollback above the real idle footer reads as working",
        session_working(fail_safe_not_precise) is True)

    # Contrast case: a stale marker with NO rule line preceding it anywhere
    # is excluded, because the anchor (the first rule) sits after it.
    no_rule_above_stray_marker = [
        "  some old scrollback content",
        "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt · ← for agents",  # stale, no rule above it
        "  more old scrollback content",
        "─" * 10,  # first rule: directly above the real input box
        "❯",
        "─" * 10,  # rule directly above the real footer
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",  # real footer: idle, no marker
    ]
    ok &= check(
        "a stale marker with no rule line above it still reads as idle",
        session_working(no_rule_above_stray_marker) is False)
    return ok


def test_input_row_and_drafts():
    ok = True
    # Fix round 2: draft_in_box and input_placeholder both carry the exact
    # same live (rule-bracketed) row - "❯ Press up to edit queued messages" -
    # so both must read as empty. The half-typed sentence in draft_in_box.txt
    # is a scrollback echo of an ALREADY-SUBMITTED queued message, not text
    # sitting in the live row; see that fixture's header. live_draft.txt is
    # what now carries a real draft in its live row.
    expected = {
        "idle_accept_edits": True,
        "working_accept_edits": True,
        "working_manual_mode": True,
        "working_with_agent_rows": True,
        "draft_in_box": True,
        "input_placeholder": True,
        "selection_dialog": False,
        "shell_zsh": False,
        "live_draft": False,
        "idle_nbsp_row": True,       # the NBSP-padded row IS empty
    }
    for name, want in expected.items():
        got = prompt_line_empty(load_screen(name))
        ok &= check(f"prompt_line_empty({name}) is {want}", got == want)
    ok &= check("no fixture is missing",
                len(list(_SCREEN_DIR.glob('*.txt'))) == len(expected))

    ok &= check("finds a modern input row",
                any(_INPUT_BOX_RE.match(l) for l in load_screen("idle_accept_edits")))
    ok &= check("still finds a legacy row",
                bool(_INPUT_BOX_RE.match("  │ > ")))
    ok &= check("unknown text in the row reads as a draft, not empty",
                prompt_line_empty(["────", "❯ some unrecognised hint", "────",
                                   "  ⏵⏵ accept edits on · ← for agents"]) is False)
    ok &= check("no input row at all is not empty", prompt_line_empty([]) is False)

    # Fix round 2: a fixed window is the same bug class session_working's
    # fix round 2 already closed - Claude Code appends an unbounded number of
    # agent/task rows below the footer, and the input row is above the
    # footer, so any fixed-size tail eventually scrolls the input row out of
    # range. That's a permanent False once enough rows accumulate, which
    # means extreme mode's push (gated on this predicate) goes permanently
    # dead - the exact liveness bug this whole plan exists to fix.
    base = load_screen("working_with_agent_rows")
    swarmed = base + [f"  ◯ agent {i}" for i in range(30)]
    ok &= check(
        "an idle empty row stays found behind 30 trailing agent rows "
        "(no fixed window)",
        prompt_line_empty(swarmed) is True)

    # Fix round 2 (defect review, Important): scanning top-down and
    # returning on the FIRST rule-bracketed row answers about the row
    # furthest from the bottom, not the live one. Every bracketed row must
    # be checked; any one of them carrying a draft vetoes the whole screen.
    STALE_EMPTY_ABOVE_LIVE_DRAFT = [
        "─" * 10,
        "❯",
        "─" * 10,
        "  some old scrollback content",
        "─" * 10,
        "❯ ok - c",
        "─" * 10,
        "  ⏵⏵ accept edits on · ← for agents",
    ]
    ok &= check(
        "a stale EMPTY bracketed row above a LIVE bracketed draft vetoes "
        "(every bracketed row is checked, not just the first one found)",
        prompt_line_empty(STALE_EMPTY_ABOVE_LIVE_DRAFT) is False)

    # The inverse already worked before this fix (a top-down first-match
    # scan hits the draft first either way) - kept as a named regression
    # guard so the fix cannot flip it.
    STALE_DRAFT_ABOVE_LIVE_EMPTY = [
        "─" * 10,
        "❯ an old half-typed thing",
        "─" * 10,
        "  some old scrollback content",
        "─" * 10,
        "❯",
        "─" * 10,
        "  ⏵⏵ accept edits on · ← for agents",
    ]
    ok &= check(
        "a stale draft above a live EMPTY row still vetoes",
        prompt_line_empty(STALE_DRAFT_ABOVE_LIVE_EMPTY) is False)

    TWO_BRACKETED_ROWS_BOTH_FREE = [
        "─" * 10,
        "❯",
        "─" * 10,
        "  some old scrollback content",
        "─" * 10,
        "❯",
        "─" * 10,
        "  ⏵⏵ accept edits on · ← for agents",
    ]
    ok &= check(
        "two bracketed rows, both free, is True",
        prompt_line_empty(TWO_BRACKETED_ROWS_BOTH_FREE) is True)

    return ok


def test_bracket_line():
    ok = True
    # A lone box-drawing glyph (no rule character, or too short) must not
    # read as a bracket - closes the finding that '│', '││', '┼' all read
    # as brackets under "every character is a box-drawing glyph" alone.
    ok &= check("a lone vertical bar is not a bracket",
                swarm._bracket_line("│") is False)
    ok &= check("two vertical bars is not a bracket",
                swarm._bracket_line("││") is False)
    ok &= check("a lone junction glyph is not a bracket",
                swarm._bracket_line("┼") is False)
    # The legacy corner-drawn shapes iterm/test_extreme.py models must stay
    # accepted, or that suite's 9 assertions go red.
    ok &= check("a legacy top corner is a bracket",
                swarm._bracket_line("╭──╮") is True)
    ok &= check("a legacy bottom corner is a bracket",
                swarm._bracket_line("╰──╯") is True)
    ok &= check("a bare rule is a bracket",
                swarm._bracket_line("─────") is True)

    # _bracketed_input_rows requires chrome ABOVE and BELOW the input row, and
    # the spec calls that rule load-bearing. Either half could be deleted with
    # every suite still green, so pin both halves independently.
    #
    # ABOVE-only is the safety regression: a live shell prompt with Claude's
    # last rule still painted above it (the operator quit Claude mid-screen)
    # would become a "bracketed input row", and relay would type a message
    # body plus a bare Enter into that shell - the body executes as a command.
    above_only = [
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
        "─" * 60,
        "❯ ",
        "~/Work/relay on main",
    ]
    ok &= check(
        "a rule above but ordinary text below is NOT a bracketed input row",
        swarm._bracketed_input_rows(above_only) == [])
    ok &= check(
        "...so a live shell prompt under a lingering rule is never ready",
        claude_prompt_ready(above_only) is False)

    # BELOW-only is the other half: an _INPUT_BOX_RE match with ordinary text
    # above it is a scrollback echo of an already-submitted message or a shell
    # prompt, never the live row (see prompt_line_empty's docstring).
    below_only = [
        "~/Work/relay on main",
        "❯ ",
        "─" * 60,
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
    ]
    ok &= check(
        "ordinary text above but a rule below is NOT a bracketed input row",
        swarm._bracketed_input_rows(below_only) == [])
    ok &= check(
        "...so that screen is never ready either",
        claude_prompt_ready(below_only) is False)

    # Control: the same row with chrome on BOTH sides is the live row. Without
    # this, both assertions above would also pass if the function returned []
    # for everything.
    both_sides = [
        "~/Work/relay on main",
        "─" * 60,
        "❯ ",
        "─" * 60,
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
    ]
    ok &= check("chrome on both sides IS a bracketed input row",
                swarm._bracketed_input_rows(both_sides) == [2])
    ok &= check("...and that screen is ready",
                claude_prompt_ready(both_sides) is True)
    return ok


def test_selection_dialog_and_readiness():
    ok = True
    dialogs = {
        "selection_dialog": True,
        "idle_accept_edits": False,
        "working_accept_edits": False,
        "working_manual_mode": False,
        "working_with_agent_rows": False,
        "draft_in_box": False,
        "input_placeholder": False,
        "shell_zsh": False,
        "live_draft": False,
        "idle_nbsp_row": False,      # not a dialog
    }
    for name, want in dialogs.items():
        ok &= check(f"selection_dialog({name}) is {want}",
                    selection_dialog(load_screen(name)) == want)
    ok &= check("no fixture is missing from the dialog table",
                len(list(_SCREEN_DIR.glob('*.txt'))) == len(dialogs))

    # live_draft is READY: readiness answers "can relay type into this session
    # at all", not "is it safe to overwrite what is already typed". The draft
    # is a separate gate (prompt_line_empty), which _fire_extreme applies on
    # its own; a queued message delivery deliberately appends to a draft
    # rather than being blocked by one.
    ready = {
        "idle_accept_edits": True,
        "working_accept_edits": False,
        "working_manual_mode": False,
        "working_with_agent_rows": False,
        "draft_in_box": False,
        "input_placeholder": False,
        "selection_dialog": False,
        "shell_zsh": False,
        "live_draft": True,
        "idle_nbsp_row": True,       # idle with a free row -> ready
    }
    for name, want in ready.items():
        ok &= check(f"claude_prompt_ready({name}) is {want}",
                    claude_prompt_ready(load_screen(name)) == want)
    ok &= check("no fixture is missing from the readiness table",
                len(list(_SCREEN_DIR.glob('*.txt'))) == len(ready))

    ok &= check("a shell prompt alone is never ready",
                claude_prompt_ready(["~/Work/relay", "❯ "]) is False
                or not session_working(["~/Work/relay", "❯ "]))
    ok &= check("empty screen is not ready", claude_prompt_ready([]) is False)

    # Fix round 2 (defect review, Important): round 1's bottom-up chrome walk
    # required EVERY trailing line, all the way to the bottom of the screen,
    # to be recognized chrome - so a task list or agent rows below the
    # footer (the NORMAL idle state after any turn that used subagents) made
    # the whole screen read not-ready. That reintroduced spec Finding 4, the
    # exact bug this plan exists to close. Condition 1 no longer cares what
    # trails the footer at all: it only asks whether a genuinely bracketed
    # input row exists ANYWHERE on screen (_bracketed_input_rows, shared
    # with Task 2's prompt_line_empty) - a separate, narrower structural
    # guard (condition 2, exercised further below) closes the shell defect
    # without that side effect.
    idle_plus_agent_rows = load_screen("idle_accept_edits") + [
        "  ⏺ main",
        "  ◯ general-purpose  Task 4 implementer",
        "  ◯ general-purpose  Task 5 implementer",
    ]
    ok &= check(
        "idle screen plus a task-list/agent-row tail is ready - Finding 4 "
        "must not reopen",
        claude_prompt_ready(idle_plus_agent_rows) is True)

    idle_plus_many_agent_rows = load_screen("idle_accept_edits") + [
        f"  ◯ general-purpose  Task {i} implementer" for i in range(20)
    ]
    ok &= check(
        "idle screen plus 20 trailing agent rows is still ready - no fixed "
        "window, no requirement that every trailing row be recognized chrome",
        claude_prompt_ready(idle_plus_many_agent_rows) is True)

    # Fix round 3: the widened shell-prompt guard (condition 2 now also vetoes
    # a trailing line whose last glyph is a shell prompt sigil) must not start
    # eating the ordinary idle tail. Task lists and agent rows end in ordinary
    # words, never in a prompt sigil, so pin the exact shapes.
    idle_plus_todo_list = load_screen("idle_accept_edits") + [
        "  ⏺ main",
        "  ⎿ ☒ Restore the shell fixtures",
        "  ⎿ ☒ Widen the trailing-prompt guard",
        "  ⎿ ☐ Anchor selection_dialog structurally",
        "  ◯ general-purpose  Task 4 implementer",
    ]
    ok &= check(
        "a todo list and agent rows below the footer stay ready under the "
        "widened shell-prompt guard",
        claude_prompt_ready(idle_plus_todo_list) is True)

    # Carried-forward defect (Task 2 review), closed structurally instead of
    # by walking every trailing line. shell_zsh is already pinned False above
    # via the fixture dict - it has no bracketed row at all (a bare zsh "❯"
    # has no chrome on either side). The harder half of the same defect is
    # Claude's own box/footer chrome LINGERING on screen for a line or two
    # after quitting, sitting ABOVE a now-live shell prompt that reuses the
    # same "❯" glyph: condition 1 alone would still find the (dead) bracketed
    # row above and read ready. Condition 2 closes it - any unbracketed
    # _INPUT_BOX_RE match below the last bracketed row, which is exactly the
    # shape of a live shell prompt sitting under a dead Claude box, vetoes.
    lingering_box_above_live_shell = [
        "─" * 10,
        "❯",
        "─" * 10,
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
        "~/Work/relay",
        "❯",
    ]
    ok &= check(
        "a lingering Claude box above a live shell prompt is never ready",
        claude_prompt_ready(lingering_box_above_live_shell) is False)

    # None of the fixtures pair a real bracketed input row with dialog
    # markers (selection_dialog.txt has no input row at all, so condition 1
    # alone already fails it) - condition 4 is otherwise never exercised.
    # selection_dialog's own docstring says a dialog MAY render an input row,
    # so build a screen that does, to prove the dialog check is load-bearing
    # on its own rather than redundant with condition 1.
    dialog_with_bracketed_row = [
        "─" * 10,
        "❯",
        "─" * 10,
        "Enter to select · ↑/↓ to navigate · Esc to cancel",
    ]
    ok &= check(
        "a selection dialog is never ready even when it renders a bracketed "
        "input row",
        claude_prompt_ready(dialog_with_bracketed_row) is False)

    # Fix round 3 (defect review, Important): selection_dialog scanned a fixed
    # `tail[-6:]` window. A dialog's navigation footer is CONTENT, not chrome,
    # and Claude Code is free to render rows below it (or wrap the dialog body
    # past six non-blank lines) - six trailing rows were enough to disarm this
    # plan's only safety fix entirely and let relay type into a live menu. The
    # markers are now looked for across the whole screen, so no row count can
    # push them out of range.
    dialog_rows = [
        "  1. [ ] Option one",
        "  2. [ ] Option two",
        "Enter to select · ↑/↓ to navigate · Esc to cancel",
    ]
    trailing = [f"  ◯ general-purpose  Task {i}" for i in range(6)]
    ok &= check("a dialog with 5 rows below its footer is still a dialog",
                selection_dialog(dialog_rows + trailing[:5]) is True)
    ok &= check(
        "a dialog with 6 rows below its footer is STILL a dialog (no fixed "
        "window - the 6th row used to push the markers out of range)",
        selection_dialog(dialog_rows + trailing) is True)
    ok &= check(
        "a dialog with 30 rows below its footer is still a dialog",
        selection_dialog(dialog_rows
                         + [f"  ◯ agent {i}" for i in range(30)]) is True)
    ok &= check(
        "readiness follows: a bracketed row under a dialog with 6 trailing "
        "rows is never ready",
        claude_prompt_ready(dialog_with_bracketed_row + trailing) is False)

    # Fix round 4 (defect review, promoted Minor -> silent permanent stall):
    # round 3's sigil test looked at the last glyph of EVERY line below the
    # last bracketed row, and Claude's OWN todo/task/tool rows are exactly
    # what renders there. A todo item that happens to end in "%", "#" or ">"
    # was read as a live shell prompt, so the session stopped receiving
    # messages, timers and pushes - with no message, no timer, no push and
    # nothing at all to tell the operator. A line that STARTS with a Claude
    # Code row glyph is Claude's rendering and can never be a shell prompt,
    # so it is not eligible for the sigil test at all.
    idle = load_screen("idle_accept_edits")
    for row, why in (
        ("  ⎿  ☐ Raise coverage to 90%", "a todo row ending in '%'"),
        ("  ⎿  ☐ Fix issue #", "a todo row ending in '#'"),
        ("  ⏺ Wrote <html>", "a tool row ending in '>'"),
        ("  ◯ general-purpose  merge main >", "an agent row ending in '>'"),
        ("  ⏺ cost: $", "a tool row ending in '$'"),
    ):
        ok &= check(f"{why} below the footer stays ready",
                    claude_prompt_ready(idle + [row]) is True)

    # The sigil rule itself is NOT narrowed: only which lines are eligible
    # changed. A shell prompt line never starts with a Claude row glyph, so
    # all four prompt shapes below still veto (also pinned as full screens
    # further down in this suite).
    for prompt, why in (
        ("~/work/relay $", "bash '$'"),
        ("~/work/relay %", "zsh '%'"),
        ("› ", "starship '›'"),
        ("❯", "pure '❯'"),
        ("~/work/relay #", "root '#'"),
        ("~/work/relay ➜", "oh-my-zsh '➜'"),
    ):
        ok &= check(f"{why} below the footer still vetoes readiness",
                    claude_prompt_ready(idle + [prompt]) is False)

    # Fix round 4 (defect review, promoted Minor -> silent permanent stall):
    # round 3's whole-screen marker scan false-positived on ordinary content.
    # info.last_screen carries ~40 non-blank lines of scrollback, so a session
    # merely DISPLAYING relay's own source - the line below is copied verbatim
    # out of swarm.py - had two markers on screen and vetoed its own
    # readiness forever. The scan is now anchored at the last bracketed input
    # row, the same structural anchor session_working uses: a real dialog has
    # no bracketed input row at all, so when there are none the whole screen
    # is scanned exactly as before.
    src = ['_DIALOG_MARKERS = ("Enter to select", "to navigate", '
           '"Esc to cancel")']
    ok &= check(
        "relay's own source in scrollback above a live input box is not a "
        "dialog",
        selection_dialog(src + idle) is False)
    ok &= check(
        "...and that session is still ready - scrollback above the input box "
        "can never veto readiness",
        claude_prompt_ready(src + idle) is True)

    # Fix round 5 (final branch review, Important): round 4's glyph exclusion
    # was aimed at the wrong thing, and the proof was checked in with it.
    # _CLAUDE_ROW_GLYPHS shipped WITHOUT "◻" and "…" while both sat on lines
    # 5-6 of this branch's own fixture, working_with_agent_rows.txt. Pin the
    # two rows verbatim from that file, so the fixture and the glyph list can
    # never drift apart again silently.
    agent_rows = load_screen("working_with_agent_rows")
    fixture_rows = [l for l in agent_rows
                    if l.strip()[:1] in ("◻", "…")]
    ok &= check("the fixture really does carry a ◻ row and a … row",
                len(fixture_rows) == 2)
    for row in fixture_rows:
        # " #" appended on purpose: the two rows as captured end in ")" and
        # "d", so they could never trip the suffix test and asserting on them
        # verbatim would be vacuous. With a sigil on the end, the ONLY thing
        # that can stop them reading as a live shell prompt is their leading
        # glyph being in _CLAUDE_ROW_GLYPHS.
        ok &= check(
            f"a row copied out of working_with_agent_rows.txt "
            f"({row.strip()[:1]!r}) is not a shell prompt even ending in '#'",
            swarm._shell_prompt_tail(row + " #") is False)
    ok &= check("◻ is in the Claude row-glyph list",
                "◻" in swarm._CLAUDE_ROW_GLYPHS)
    ok &= check("… is in the Claude row-glyph list",
                "…" in swarm._CLAUDE_ROW_GLYPHS)

    # ...and the shapes the reviewer measured False on the shipped code.
    for row, why in (
        ("     ◻ Task 9: Raise coverage to 90%", "a ◻ task row ending in '%'"),
        ("      … +5 pending, 3 completed #", "a … elision row ending in '#'"),
    ):
        ok &= check(f"{why} below the footer stays ready",
                    claude_prompt_ready(idle + [row]) is True)

    # The case NO glyph list can ever reach: genuine Claude Code chrome that
    # starts with an ordinary letter and ends in a sigil. This is why the
    # suffix test is confined to the bottom-most line and why a sigil glued
    # to a digit is a unit rather than a prompt.
    ok &= check(
        "the auto-compact footer ending in '%' does not stall a session - no "
        "glyph list could ever have excluded it",
        claude_prompt_ready(idle + ["Context left until auto-compact: 23%"])
        is True)
    ok &= check("a '%' glued to a digit is a unit, not a prompt",
                swarm._shell_prompt_tail("Context left: 23%") is False)
    ok &= check("a sigil after a space is still a prompt",
                swarm._shell_prompt_tail("~/work/relay %") is True)

    # The exclusion covers "%" ONLY. Exempting every sigil glued to a digit
    # was tried and reverted: bash's default prompt glues "$" straight to the
    # path, so it let through macOS's own "bash-3.2$" and every host or
    # directory whose name ends in a digit. Those are ordinary prompts. Each
    # of these is a real shell prompt shape that must still be refused.
    for prompt in ("bash-3.2$", "sh-5.2$", "bash-5.1#",
                   "maciej@mbp:~/work/relay2$", "user@web01$",
                   "~/work/py3$", "(venv) ~/work/proj2$"):
        ok &= check(
            "a digit-glued '$' or '#' is still a shell prompt: %r" % prompt,
            swarm._shell_prompt_tail(prompt) is True)
        ok &= check(
            "a dead Claude box above %r is never ready" % prompt,
            claude_prompt_ready(_BOX_CHROME + [prompt]) is False)

    # The narrowing itself: the suffix test applies to the BOTTOM-MOST line
    # only. Claude Code wraps long tool output, and a wrapped continuation row
    # carries no leading glyph at all - it is plain indented text that can end
    # in any character the file it printed happens to end in. Applying the
    # suffix test to every row below the box made each of those a candidate
    # for a permanent silent stall, and no glyph list can reach them because
    # they have no glyph. A live shell prompt is never in this position: it is
    # always the bottom-most line.
    wrapped_tool_output = idle + [
        "  ⎿  Read src/app.tsx (214 lines)",
        '     export default function App() { return <div className="root">',
        "  ⏺ Done",
    ]
    ok &= check(
        "a wrapped tool-output row ending in '>' ABOVE another row does not "
        "stall the session - only the bottom-most line is eligible",
        claude_prompt_ready(wrapped_tool_output) is True)
    cost_line_above_footer = idle + [
        "  Total cost: $",
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
    ]
    ok &= check(
        "a /cost line ending in '$' ABOVE the footer does not stall the "
        "session either",
        claude_prompt_ready(cost_line_above_footer) is True)

    # The bottom-most-line rule must not weaken the shape it exists to catch:
    # a dead Claude frame with a LIVE shell prompt drawn under it. The shell
    # prompt is always the bottom-most line in that shape, so every prompt
    # theme below still vetoes - including two-line prompts, where Claude's
    # rows sit above the sigil line rather than below it.
    for extra, why in (
        (["~/work/relay $"], "bash '$'"),
        (["~/work/relay %"], "zsh '%'"),
        (["~/work/relay", "› "], "a starship two-line '›' prompt"),
        (["~/work/relay", "❯"], "a two-line pure '❯' prompt"),
        (["~/work/relay #"], "root '#'"),
        (["~/work/relay ➜"], "oh-my-zsh '➜'"),
    ):
        ok &= check(f"{why} as the bottom-most line still vetoes readiness",
                    claude_prompt_ready(idle + extra) is False)

    # A Claude row ABOVE a live shell prompt must not rescue the shell: the
    # rule reads the LAST line, and that is still the prompt.
    ok &= check(
        "a Claude task row above a live shell prompt does not rescue it",
        claude_prompt_ready(
            idle + ["  ⏺ Wrote <html>", "~/work/relay $"]) is False)

    # Fix round 4 (defect review, Minor): selection_dialog requires TWO of the
    # three markers. One is deliberately not enough - "to navigate" alone shows
    # up in ordinary output, and a single-marker rule would veto that session's
    # messages, timers and pushes forever. Pin the threshold from BOTH sides.
    ok &= check(
        "the marker list is exactly the three the spec names",
        swarm._DIALOG_MARKERS == ("Enter to select", "to navigate",
                                  "Esc to cancel"))
    for one, why in (
        (["  Use ↑/↓ to navigate the diff hunks"], "'to navigate' alone"),
        (["  Press Enter to select a file from the list above"],
         "'Enter to select' alone"),
        (["  Esc to cancel is printed by the wizard, not by a menu"],
         "'Esc to cancel' alone"),
    ):
        ok &= check(f"{why} on screen is NOT a dialog",
                    selection_dialog(one) is False)
        ok &= check(f"...and a session showing {why} is still ready",
                    claude_prompt_ready(idle + one) is True)
    for a, b in (("Enter to select", "to navigate"),
                 ("to navigate", "Esc to cancel"),
                 ("Enter to select", "Esc to cancel")):
        ok &= check(
            f"any two markers ({a!r} + {b!r}) IS a dialog - every entry in "
            "the list counts",
            selection_dialog([f"  {a} · {b}"]) is True)

    # The anchor must not weaken dialog detection: selection_dialog.txt has
    # ZERO bracketed input rows, so it keeps the whole-screen scan and stays
    # True no matter how many rows Claude renders below its footer.
    real_dialog = load_screen("selection_dialog")
    for n in (0, 5, 6, 50):
        ok &= check(
            f"the real selection_dialog fixture with {n} trailing rows is "
            "still a dialog",
            selection_dialog(
                real_dialog + [f"  ◯ agent {i}" for i in range(n)]) is True)
        ok &= check(
            f"the real selection_dialog fixture with {n} trailing rows is "
            "never ready",
            claude_prompt_ready(
                real_dialog + [f"  ◯ agent {i}" for i in range(n)]) is False)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def _irow(sid, name, project, is_shell=False, working=True):
    return {"sid": sid, "name": name, "project": project,
            "is_shell": is_shell, "working": working}


def test_intervene_targets():
    ok = True
    rows = [
        _irow("s1", "bff", "relay", working=True),
        _irow("s2", "api", "relay", working=False),
        _irow("s3", "web", "other", working=True),
        _irow("s4", "", "", working=True),          # unregistered: no project
        _irow("s5", "shellish", "relay", is_shell=True),
        _irow("own", "panel", "relay"),
    ]
    ids = lambda ts: [t["sid"] for t in ts]

    all_t = intervene_targets(rows, "all", "s1", "own")
    ok &= check("all excludes relay's own panel", "own" not in ids(all_t))
    ok &= check("all excludes shell jobs", "s5" not in ids(all_t))
    ok &= check("all includes the unregistered tab", "s4" in ids(all_t))
    ok &= check("all includes every other project", "s3" in ids(all_t))

    proj = intervene_targets(rows, "project", "s1", "own")
    ok &= check("project scopes to the selected row's project",
                ids(proj) == ["s1", "s2"])
    ok &= check("project MISSES the unregistered tab", "s4" not in ids(proj))

    sel = intervene_targets(rows, "selected", "s1", "own")
    ok &= check("selected is exactly one row", ids(sel) == ["s1"])
    ok &= check("selected refuses relay's own panel",
                intervene_targets(rows, "selected", "own", "own") == [])
    ok &= check("selected refuses a shell job",
                intervene_targets(rows, "selected", "s5", "own") == [])

    ok &= check("project of an unregistered selection is empty",
                intervene_targets(rows, "project", "s4", "own") == [])

    n, w, i = intervene_counts(all_t)
    ok &= check("counts total", n == 4)
    ok &= check("counts working", w == 3)
    ok &= check("counts idle", i == 1)
    ok &= check("counts of nothing are zeros", intervene_counts([]) == (0, 0, 0))

    # relocated from watcher.py, which never had coverage for it
    ok &= check("a plain shell is a shell job", is_shell_job("zsh"))
    ok &= check("a login shell strips its dash", is_shell_job("-zsh"))
    ok &= check("claude is not a shell job", not is_shell_job("claude"))
    ok &= check("unknown job is not a shell job", not is_shell_job("node"))
    ok &= check("empty job is not a shell job", not is_shell_job(""))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def test_resolve_scope():
    print()
    print("--- resolve_scope ---")
    ok = True

    def row(sid, name="", ws="", shell=False, working=False):
        return {"sid": sid, "name": name, "project": "", "workspace": ws,
                "is_shell": shell, "working": working}

    rows = [
        row("own", ws="/Users/x/Work/relay"),
        row("s1", name="w1", ws="/Users/x/Work/relay", working=True),
        row("s2", name="w2", ws="/Users/x/Work/relay"),
        row("s3", name="w3", ws="/Users/x/Work/other"),
        row("sh", ws="/Users/x/Work/relay", shell=True),
    ]

    def R(tok, sel="s1", **k):
        return swarm.resolve_scope(tok, rows, sel, "own", **k)

    kind, label, t = R("")
    ok &= check("an empty token is the selected row",
                kind == "selected" and [r["sid"] for r in t] == ["s1"])

    kind, label, t = R("all")
    ok &= check("`all` reaches every controllable session",
                kind == "all" and {r["sid"] for r in t} == {"s1", "s2", "s3"})
    ok &= check("`all` never includes relay's own tab",
                all(r["sid"] != "own" for r in t))
    ok &= check("`all` excludes a shell tab by default",
                all(r["sid"] != "sh" for r in t))

    kind, label, t = R("all", allow_shell=True)
    ok &= check("allow_shell lets arm reach a shell tab",
                {r["sid"] for r in t} == {"s1", "s2", "s3", "sh"})

    kind, label, t = R("here")
    ok &= check("`here` is the selected row's workspace",
                kind == "workspace" and {r["sid"] for r in t} == {"s1", "s2"})
    ok &= check("`here` names the directory it resolved to", "relay" in label)

    kind, label, t = R("w3")
    ok &= check("a registry name resolves to that one session",
                kind == "session" and [r["sid"] for r in t] == ["s3"])
    ok &= check("a session resolution says so in the label",
                label == "session w3")

    kind, label, t = R("other")
    ok &= check("a workspace basename resolves to its whole directory",
                kind == "workspace" and [r["sid"] for r in t] == ["s3"])

    # A name that is BOTH a session name and a workspace basename takes the
    # session: a session name was assigned on purpose, a basename is
    # inferred from a path.
    both = rows + [row("s9", name="other", ws="/Users/x/Work/nine")]
    kind, label, t = swarm.resolve_scope("other", both, "s1", "own")
    ok &= check("session beats workspace when both match",
                kind == "session" and [r["sid"] for r in t] == ["s9"])

    kind, label, t = R("nosuchthing")
    ok &= check("an unknown token resolves to nothing",
                kind == "none" and t == [])
    ok &= check("...and the label carries the token back for the report",
                "nosuchthing" in label)

    kind, label, t = swarm.resolve_scope("here", [row("own", ws="")], "own",
                                         "own")
    ok &= check("a zero-target resolution is not a silent success", t == [])

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def _plain(line: str) -> str:
    """Rich markup stripped, for width checks on rendered rows."""
    import re
    keep = line.replace("\\[", "\x00")
    return re.sub(r"\[/?[a-z ]+\]", "", keep).replace("\x00", "[")


def test_conversations_and_chat():
    print("\n== conversations() / render_chat() ==")
    ok = True
    now = 1_000_000.0

    def m(id, f, t, body, kind="info", ago=0, thread=None, delivered=True):
        return {"id": id, "from_name": f, "to_name": t, "body": body,
                "kind": kind, "created_at": now - ago, "thread_id": thread,
                "project": "p",
                "delivered_at": (now - ago + 1) if delivered else None}

    msgs = [
        m(1, "coord", "w1", "take #13", ago=600),
        m(2, "w1", "coord", "ack", ago=500),
        m(3, "coord", "w2", "review it", ago=400),
        m(4, "w2", "coord", "no db", kind="blocked", ago=300),
        # one discussion post fans out to two rows - the chat shows it once
        m(5, "coord", "w1", "one db?", kind="say", ago=200, thread=7),
        m(6, "coord", "w2", "one db?", kind="say", ago=200, thread=7),
        m(7, "w1", "coord", "yes", kind="agree", ago=100, thread=7),
        m(8, "w1", "w2", "yes", kind="agree", ago=100, thread=7),
        # relay's own wake-up is system noise, not a conversation
        m(9, "relay", "w1", "[relay] wake", kind="wake", ago=50),
        m(10, "coord", "w9", "alive?", ago=10, delivered=False),
    ]
    threads = [{"id": 7, "topic": "one db?", "state": "open", "outcome": "",
                "participants": "coord,w1,w2", "created_at": now - 200}]

    convs = swarm.conversations(msgs, threads, now)
    keys = [c["key"] for c in convs]
    ok &= check("discussions come first, then pairs in stable name order",
                keys == ["thread:7", "pair:coord|w1", "pair:coord|w2",
                         "pair:coord|w9"])
    th = convs[0]
    ok &= check("a discussion post fanned out to N recipients is ONE line",
                [x["body"] for x in th["msgs"]] == ["one db?", "yes"])
    ok &= check("the discussion carries its topic and state",
                "one db?" in th["title"] and "open" in th["meta"])
    pw1 = convs[1]
    ok &= check("a pair chat holds only the direct traffic, no thread posts, "
                "no relay wake-ups",
                [x["body"] for x in pw1["msgs"]] == ["take #13", "ack"])
    ok &= check("no pair is formed with relay itself",
                not any("relay" in k for k in keys))
    ok &= check("a pair whose last word was blocked is flagged",
                convs[2]["flag"] and not pw1["flag"])
    ok &= check("a pair whose last message is still queued is flagged",
                convs[3]["flag"])
    ok &= check("a conversation's age is that of its last message",
                int(pw1["age_s"]) == 500 and int(convs[3]["age_s"]) == 10)

    # --- rendering -------------------------------------------------------
    lines = swarm.render_chat(convs, cursor=2, width=100, height=10, now=now)
    ok &= check("render_chat fills exactly the height it is given",
                len(lines) == 10)
    plain = [_plain(x) for x in lines]
    ok &= check("no rendered row exceeds the width",
                all(len(x) <= 100 for x in plain))
    ok &= check("the cursor row is highlighted",
                any("[reverse]" in x and "coord" in x and "w2" in x
                    for x in lines))
    ok &= check("the transcript shows the selected pair's bodies",
                any("review it" in x for x in plain)
                and any("no db" in x for x in plain))
    ok &= check("...and not another pair's",
                not any("take #13" in x for x in plain))
    ok &= check("a non-info kind is tagged and coloured",
                any("[blocked]" in x and "[yellow]" in x for x in lines))
    ok &= check("every conversation is listed on the left",
                sum(1 for x in plain if "coord" in x.split("│")[0]) == 3)

    lines = swarm.render_chat(convs, cursor=3, width=100, height=6, now=now)
    plain = [_plain(x) for x in lines]
    ok &= check("an undelivered message says so",
                any("alive?" in x and "queued" in x for x in plain))

    lines = swarm.render_chat(convs, cursor=0, width=100, height=8, now=now)
    plain = [_plain(x) for x in lines]
    ok &= check("a discussion transcript shows each post once",
                sum(1 for x in plain[1:]
                    if "one db?" in x.split("│", 1)[-1]) == 1)
    ok &= check("agree posts are tagged",
                any("[agree]" in x and "yes" in x for x in plain))

    # a long body wraps instead of being clipped
    long = [m(1, "a", "b", "word " * 40, ago=5)]
    convs2 = swarm.conversations(long, [], now)
    lines = swarm.render_chat(convs2, cursor=0, width=80, height=12, now=now)
    plain = [_plain(x) for x in lines]
    body_rows = [x for x in plain if "word" in x]
    ok &= check("a long body wraps onto several rows",
                len(body_rows) >= 3)
    ok &= check("...none wider than the pane",
                all(len(x) <= 80 for x in plain))

    # scrolling: a transcript taller than the pane shows its tail, and
    # scroll=N walks back up by N rows
    many = [m(i, "a", "b", f"msg{i:02d}", ago=1000 - i) for i in range(1, 41)]
    convs3 = swarm.conversations(many, [], now)
    tail = [_plain(x) for x in swarm.render_chat(convs3, 0, 100, 8, now=now)]
    ok &= check("a long transcript shows its newest rows",
                any("msg40" in x for x in tail)
                and not any("msg01" in x for x in tail))
    back = [_plain(x) for x in swarm.render_chat(convs3, 0, 100, 8, now=now,
                                                  scroll=30)]
    ok &= check("scroll walks back into history",
                any("msg10" in x for x in back)
                and not any("msg40" in x for x in back))
    far = [_plain(x) for x in swarm.render_chat(convs3, 0, 100, 8, now=now,
                                                 scroll=10_000)]
    ok &= check("scroll clamps at the oldest row",
                any("msg01" in x for x in far))
    ok &= check("scroll_max says how far back a transcript goes",
                swarm.chat_scroll_max(convs3, 0, 100, 8) == 40 - 7)

    empty = swarm.render_chat([], 0, 100, 6, now=now)
    ok &= check("no conversations at all teaches how one starts",
                len(empty) == 6 and any("relay send" in _plain(x)
                                         for x in empty))

    # native (socket) traffic: glyph, tick, meta
    def pm(id, f, t, body, ago, received=True):
        r = m(id, f, t, body, ago=ago)
        r["via"] = "peer"
        r["received_at"] = (now - ago + 2) if received else None
        return r
    native = [pm(1, "peera-d0", "peerb-fa", "hello from A", 60),
              pm(2, "peerb-fa", "peera-d0", "got it", 30, received=False)]
    cn = swarm.conversations(native, [], now)
    ok &= check("a pair of only native messages says so in its meta",
                cn[0]["meta"] == "direct messages · native")
    lines = swarm.render_chat(cn, 0, 100, 8, now=now)
    plain = [_plain(x) for x in lines]
    ok &= check("native rows carry the » glyph",
                sum(1 for x in plain if "» " in x.split("│", 1)[-1]) == 2)
    ok &= check("a confirmed row ends with a tick, an unconfirmed one does not",
                any("hello from A" in x and x.rstrip().endswith("✓") for x in plain)
                and any("got it" in x and not x.rstrip().endswith("✓") for x in plain))
    ok &= check("the left list marks a conversation whose last word was native",
                any("»" in x.split("│")[0] and "peera-d0" in x for x in plain))

    # a row that is still queued can never have been received: no tick, and
    # the dimmed [queued] tail stays the literal end of the row so plain and
    # markup agree
    odd = [pm(5, "peera-d0", "peerb-fa", "ghost", 5, received=True)]
    odd[0]["delivered_at"] = None
    co = swarm.conversations(odd, [], now)
    lines = swarm.render_chat(co, 0, 100, 6, now=now)
    plain = [_plain(x) for x in lines]
    row = next(x for x in plain if "ghost" in x)
    ok &= check("a queued native row shows [queued] and no tick",
                row.rstrip().endswith("[queued]") and "✓" not in row)
    prow, mrow = next(p for p in swarm._transcript_rows(co[0], 70)
                      if "ghost" in p[0])
    ok &= check("...and the row's markup, stripped, equals its plain twin",
                _plain(mrow) == prow and prow.rstrip().endswith("[queued]"))

    mixed = native + [m(3, "peera-d0", "peerb-fa", "via relay now", ago=10)]
    cm = swarm.conversations(mixed, [], now)
    ok &= check("a pair with both transports says mixed",
                cm[0]["meta"] == "direct messages · mixed")
    lines = swarm.render_chat(cm, 0, 100, 8, now=now)
    plain = [_plain(x) for x in lines]
    ok &= check("a relay row has no glyph",
                any("via relay now" in x and "» " not in x for x in plain))
    ok &= check("...and the left list has no » when the last word was relay's",
                not any("»" in x.split("│")[0] for x in plain))
    ok &= check("old rows without a via field still render as relay's",
                swarm.conversations([m(9, "a", "b", "x", ago=1)], [], now)[0]["meta"]
                == "direct messages")
    return ok


def test_dir_conversations():
    print("\n== conversations_for_dir / transcript_text ==")
    ok = True
    now = 2_000_000.0

    def m(id, f, t, body, ago, cwd="", via="relay"):
        return {"id": id, "from_name": f, "to_name": t, "body": body,
                "kind": "info", "created_at": now - ago,
                "delivered_at": now - ago + 1, "received_at": None,
                "thread_id": None, "project": "p", "via": via,
                "from_cwd": cwd}

    msgs = [
        m(1, "api-30", "web-1c", "split?", 600, cwd="/w/api", via="peer"),
        m(2, "web-1c", "api-30", "sse", 500, cwd="/w/web", via="peer"),
        m(3, "coord", "w1", "take 13", 400),          # relay traffic, no cwd
        m(4, "w1", "coord", "ack", 300),
        m(5, "ops-9", "api-30", "deploy?", 100, cwd="/w/ops", via="peer"),
    ]
    convs = swarm.conversations(msgs, [], now)
    ok &= check("norm_dir strips trailing slashes only",
                swarm.norm_dir("/w/api/") == "/w/api"
                and swarm.norm_dir("/w/api") == "/w/api"
                and swarm.norm_dir("") == "")

    here = swarm.conversations_for_dir(convs, "/w/api/", set())
    ok &= check("a directory finds every conversation a message was sent from",
                [c["key"] for c in here] == ["pair:api-30|ops-9",
                                             "pair:api-30|web-1c"])
    ok &= check("...freshest first", here[0]["age_s"] < here[1]["age_s"])
    ok &= check("a directory nobody sent from finds nothing",
                swarm.conversations_for_dir(convs, "/w/none", set()) == [])
    ok &= check("relay traffic is found through the names registered there",
                [c["key"] for c in
                 swarm.conversations_for_dir(convs, "/x", {"w1"})]
                == ["pair:coord|w1"])
    ok &= check("names and cwd combine without duplicates",
                len(swarm.conversations_for_dir(convs, "/w/api", {"api-30"}))
                == 2)

    txt = swarm.transcript_text(here[1], width=80)
    ok &= check("transcript_text is plain rows, oldest first",
                "split?" in txt.splitlines()[0] and "sse" in txt.splitlines()[-1]
                and "[" not in txt.replace("[queued]", ""))
    ok &= check("transcript_text --last keeps the tail",
                swarm.transcript_text(here[1], width=80, last=1).strip()
                .endswith("sse"))

    # the one line a resumed session gets
    line = swarm.resume_line(convs, "/w/api", set(), now)
    ok &= check("resume_line names the freshest counterpart and age",
                line.startswith("[relay] ")
                and "ops-9" in line and "1m ago" in line)
    ok &= check("...quotes the last thing said, and points at relay chat",
                '"deploy?"' in line and "relay chat --here" in line)
    ok &= check("...names at most two counterparts",
                line.count("web-1c") == 1 and "coord" not in line)
    ok &= check("resume_line is empty for a directory with no history",
                swarm.resume_line(convs, "/w/none", set(), now) == "")
    ok &= check("resume_line is one line",
                "\n" not in line)
    ok &= check("a long last message is clipped",
                len(swarm.resume_line(
                    [dict(here[0], msgs=[dict(here[0]["msgs"][-1],
                                              body="x" * 500)])],
                    "/w/api", set(), now)) < 320)
    ok &= check("...and framed as verbatim text from another session",
                "last, verbatim from that session:" in line)

    multiline = dict(here[1], msgs=here[1]["msgs"][:-1]
                     + [dict(here[1]["msgs"][-1], body="line one\nline two")])
    line_nl = swarm.resume_line([multiline], "/w/api", set(), now)
    ok &= check("a body with embedded newlines still yields one line",
                line_nl and "\n" not in line_nl)

    hostile_name = "x\ny" + "A" * 100
    hostile_conv = dict(here[1], a="api-30", b=hostile_name)
    line_hostile = swarm.resume_line([hostile_conv], "/w/api", set(), now)
    ok &= check("a hostile counterpart name still yields one line "
                "under 320 chars",
                line_hostile and "\n" not in line_hostile
                and len(line_hostile) < 320)

    # a relay name reused in another directory's roster must not drag that
    # other project's history in here too.
    def mp(id, f, t, body, ago, project):
        return {"id": id, "from_name": f, "to_name": t, "body": body,
                "kind": "info", "created_at": now - ago,
                "delivered_at": now - ago + 1, "received_at": None,
                "thread_id": None, "project": project, "via": "relay",
                "from_cwd": ""}

    proj_conv = swarm.conversations(
        [mp(20, "w1", "coord", "shared secret", 10, "a")], [], now)
    ok &= check("a name whose roster project differs from the message's "
                "project does not match",
                swarm.conversations_for_dir(
                    proj_conv, "/elsewhere", {"w1"}, {"w1": "b"}) == [])
    ok &= check("...but matches when the roster project agrees",
                len(swarm.conversations_for_dir(
                    proj_conv, "/elsewhere", {"w1"}, {"w1": "a"})) == 1)
    ok &= check("...and matches when the roster name carries no project",
                len(swarm.conversations_for_dir(
                    proj_conv, "/elsewhere", {"w1"}, {"w1": ""})) == 1)
    return ok


if __name__ == "__main__":
    ok = run()
    ok = test_resolve_scope() and ok
    ok = test_intervene_targets() and ok
    ok = test_session_working() and ok
    ok = test_input_row_and_drafts() and ok
    ok = test_bracket_line() and ok
    ok = test_selection_dialog_and_readiness() and ok
    ok = test_conversations_and_chat() and ok
    ok = test_dir_conversations() and ok
    sys.exit(0 if ok else 1)
