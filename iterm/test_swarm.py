"""Tests for the pure swarm decision logic. No iTerm2, no sqlite.

Run: python3 iterm/test_swarm.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import swarm  # noqa: E402
from swarm import (  # noqa: E402
    parse_blockers, unblocked_by_completion, wakeup_assignment_body,
    wakeup_unblocked_body, delivery_text, claude_prompt_ready, stale_reason,
    render_swarm, restore_candidates, clean_candidates, restore_plan_text,
    clean_plan_text, resume_prompt, wipe_candidates, wipe_blocker_warnings,
    wipe_plan_text, parse_pr_ref, resolve_pr_route,
)


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
    ok &= check("render: interactions section", "INTERACTIONS" in vs)
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
    ok &= check("the attention strip sits above the separator",
                text.index("acme/api#482")
                < text.index("─") < text.rindex("acme/api#482"))

    # Empty renders like MESSAGES does - header plus a "(none)" line - so the
    # section never looks like a missing feature, and the empty state teaches
    # how it gets filled.
    empty = swarm.render_prs([], 100)
    ok &= check("render_prs still renders a header with no PRs",
                empty[0] == "PULL REQUESTS")
    ok &= check("the empty pane says none and names the verb that fills it",
                len(empty) == 2 and "(none" in empty[1]
                and "relay pr claim" in empty[1])

    ok &= check("the fleet line counts PRs and how many need work",
                "PRs 3 · 2 need work"
                in swarm.fleet_line(sess, [], prs=rows))

    full = swarm.render_swarm(sess, [], [], now, width=100, prs=prs)
    ok &= check("render_swarm includes the PR pane", "PULL REQUESTS" in full)
    noprs = swarm.render_swarm(sess, [], [], now, width=100)
    ok &= check("render_swarm shows the PR pane even with no prs argument",
                "PULL REQUESTS" in noprs)
    ok &= check("that empty pane carries the (none) line, not stray rows",
                "(none" in noprs.split("PULL REQUESTS")[1].split("MESSAGES")[0])

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

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
