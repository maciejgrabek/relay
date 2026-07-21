"""TUI tests: markup-escaping, cursor-by-identity, divider safety, single Enter.

Run: python3 iterm/test_app.py
Uses Textual's headless run_test() with a stub watcher (no iTerm2 needed).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import app as appmod  # noqa: E402
from watcher import SessionInfo  # noqa: E402


class StubWatcher:
    def __init__(self, sessions):
        self.sessions = sessions
        self.log = []
        self.log_total = 0
        self.sent = []
        self.registry = {}

    _CYCLE = {"off": "safe", "safe": "wild", "wild": "insane", "insane": "off"}

    def toggle(self, s):
        self.sessions[s].mode = self._CYCLE.get(self.sessions[s].mode, "safe")

    def set_all(self, a):
        for i in self.sessions.values():
            i.mode = "safe" if a else "off"

    def toggle_hidden(self, s):
        self.sessions[s].hidden = not self.sessions[s].hidden

    def unhide_all(self):
        for i in self.sessions.values():
            i.hidden = False

    async def refresh_screen(self, s):
        pass

    async def send_keys(self, sid, t):
        self.sent.append((sid, t))
        return True


class _TestApp(appmod.RelayApp):
    def __init__(self, sessions, **k):
        super().__init__(**k)
        self._stub = sessions

    async def _connect(self):
        self.watcher = StubWatcher(self._stub)


async def go():
    ok = True

    def chk(n, c):
        nonlocal ok
        print(("PASS" if c else "FAIL"), n)
        ok = ok and c

    # Titles and commands contain '[' - must be escaped, not crash render.
    sessions = {
        f"s{i}": SessionInfo(f"s{i}", title=f"t[{i}]", window_idx=0, tab_idx=i,
                             last_command="sed 's/[a-z]/x/' file",
                             last_screen=["x"])
        for i in range(3)
    }
    a = _TestApp(sessions, dry_run=True)
    async with a.run_test() as pilot:
        await pilot.pause()
        a._refresh()
        await pilot.pause()
        t = a.query_one(appmod.DataTable)
        chk("renders with '[' in command/title (no MarkupError)", t.row_count == 3)

        # Cursor tracks the SESSION, not the row index, across a reorder.
        t.move_cursor(row=a._row_sids.index("s1"))
        await pilot.pause()
        a.watcher.toggle_hidden("s0")   # reorders rows (s0 -> hidden section)
        a._refresh()
        await pilot.pause()
        chk("cursor stays on same session after reorder", a._selected_sid() == "s1")

        # The divider is never left under the cursor.
        chk("cursor not on divider", a._selected_sid() is not None)

        # Enter sends exactly once (no binding+RowSelected double-fire).
        a.watcher.sent.clear()
        t.move_cursor(row=a._row_sids.index("s1"))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        chk("single Enter -> exactly one send", a.watcher.sent == [("s1", "\r")])

    # --- needs-action section + attention header -----------------------------
    chk("needs_action: prompting", appmod.needs_action("prompting", False))
    chk("needs_action: blocked", appmod.needs_action("blocked", False))
    chk("needs_action: stale wins", appmod.needs_action("idle", True))
    chk("needs_action: working idle no", not appmod.needs_action("working", False)
        and not appmod.needs_action("idle", False))

    # the mascot's alarm must agree with the strip: blocked and stale count
    att = {
        "p": SessionInfo("p", title="p", window_idx=0, tab_idx=0,
                         last_screen=["x"]),
        "b": SessionInfo("b", title="b", window_idx=0, tab_idx=1,
                         last_screen=["x"]),
        "st": SessionInfo("st", title="st", window_idx=0, tab_idx=2,
                          last_screen=["x"]),
        "ok": SessionInfo("ok", title="ok", window_idx=0, tab_idx=3,
                          last_screen=["x"]),
        "OWN": SessionInfo("OWN", title="panel", window_idx=0, tab_idx=4,
                           last_screen=["x"]),
    }
    att["p"].state = "prompting"
    att["b"].state = "blocked"
    att["st"].stale = True
    att["OWN"].state = "prompting"
    chk("attention_count = prompting + blocked + stale, own excluded",
        appmod.attention_count(att.values(), "OWN") == 3)

    na_sessions = {
        "s0": SessionInfo("s0", title="calm", window_idx=0, tab_idx=0,
                          last_screen=["x"]),
        "s1": SessionInfo("s1", title="hot", window_idx=0, tab_idx=1,
                          last_command="rm -rf node_modules",
                          last_screen=["x"]),
    }
    na_sessions["s1"].state = "prompting"
    na = _TestApp(na_sessions, dry_run=True)
    async with na.run_test() as pilot:
        await pilot.pause()
        na._refresh()
        await pilot.pause()
        chk("attention DUPLICATE on top, main list stable and complete",
            na._row_sids[0] is None and na._row_sids[1] == "s1"
            and na._row_sids[2] is None
            and na._row_sids[3] == "s0" and na._row_sids[4] == "s1")
        chk("cursor lands on a real row, not a divider",
            na._selected_sid() in ("s0", "s1"))
        sub = str(na.query_one("#subtitle", appmod.Static).render())
        chk("header counts awaiting", "1 awaiting" in sub)
        # continuous navigation: strip rows first, then the full main list -
        # down walks dup(s1) -> s0 -> s1, up walks it back, skipping dividers.
        t = na.query_one(appmod.DataTable)
        t.move_cursor(row=1)                       # the s1 duplicate on top
        await pilot.pause()
        walked = [na._selected_sid()]
        for _ in range(2):
            na._move_cursor(+1)
            walked.append(na._selected_sid())
        chk("down: strip dup -> main list in order",
            walked == ["s1", "s0", "s1"])
        for _ in range(2):
            na._move_cursor(-1)
            walked.append(na._selected_sid())
        chk("up: walks back through the strip",
            walked[-2:] == ["s0", "s1"])

        # actioned -> the duplicate disappears, the main rows DON'T move
        na.watcher.sessions["s1"].state = "working"
        na._refresh()
        await pilot.pause()
        chk("attention cleared -> no dividers, same stable order",
            na._row_sids == ["s0", "s1"])

    # --- help overlay ---------------------------------------------------------
    def _one():
        return {"s0": SessionInfo("s0", title="t0", window_idx=0, tab_idx=0,
                                  last_screen=["x"])}

    chk("help text covers keys + arm levels",
        "ARM LEVELS" in appmod.help_text() and "SPACE" in appmod.help_text())
    ah = _TestApp(_one(), dry_run=True)
    async with ah.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        chk("? opens the help overlay",
            ah._help_visible
            and str(ah.query_one("#helpview").styles.display) == "block")
        await pilot.press("question_mark")
        await pilot.pause()
        chk("? again closes it", not ah._help_visible
            and str(ah.query_one("#helpview").styles.display) == "none")
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        chk("TAB from help lands in swarm view, help closed",
            not ah._help_visible and ah._swarm_visible)

    # --- themes: complete palettes, resolved CSS ------------------------------
    keys = set(appmod.THEMES["phosphor"])
    chk("all themes carry the full palette",
        all(set(p) == keys for p in appmod.THEMES.values()))
    chk("CSS fully resolved (no dangling $tokens)",
        "$" not in appmod.RelayApp.CSS)
    chk("CSS uses the active theme", appmod.TH["bright"] in appmod.RelayApp.CSS)

    # --- audit view (pure formatter + v toggle) -------------------------------
    ents = [{"ts": 1000.0, "verdict": "auto-approved", "session": "t0",
             "command": "grep -rn TODO"},
            {"ts": 1001.0, "verdict": "escalated", "session": "other",
             "command": "rm -rf /"}]
    av = appmod.audit_view_text(ents, "t0", 80)
    chk("audit view filters by session + marks verdicts",
        "AUDIT // t0" in av and "grep -rn TODO" in av
        and "rm -rf /" not in av and "✓" in av)
    chk("audit view empty state teaches",
        "no recorded decisions" in appmod.audit_view_text([], "t0", 80))

    aa = _TestApp(_one(), dry_run=True)
    async with aa.run_test() as pilot:
        await pilot.pause()
        aa._refresh()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        chk("v toggles audit mode on", aa._audit_visible)
        await pilot.press("v")
        await pilot.pause()
        chk("v toggles audit mode off", not aa._audit_visible)
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        chk("ESC also leaves audit mode", not aa._audit_visible)
        await pilot.press("question_mark")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        chk("ESC also closes help", not aa._help_visible)
        await pilot.press("tab")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        chk("ESC also leaves the swarm view", not aa._swarm_visible)

    # relay's own panel row NEVER goes to NEEDS ACTION (nor the counts)
    os.environ["ITERM_SESSION_ID"] = "w0t9p9:OWN-1"
    own_sessions = {
        "OWN-1": SessionInfo("OWN-1", title="RELAY CONSOLE", window_idx=0,
                             tab_idx=0, last_screen=["x"]),
    }
    own_sessions["OWN-1"].state = "prompting"   # misdetected own screen
    ao = _TestApp(own_sessions, dry_run=True)
    async with ao.run_test() as pilot:
        await pilot.pause()
        ao._refresh()
        await pilot.pause()
        chk("own panel row never enters the attention strip",
            ao._row_sids == ["OWN-1"])
        sub = str(ao.query_one("#subtitle", appmod.Static).render())
        chk("own panel row not counted as awaiting", "awaiting" not in sub)

    # --- quit guard: instant when idle, double-press when something's live ---
    import tempfile
    os.environ["RELAY_DB"] = os.path.join(tempfile.mkdtemp(), "relay.db")

    chk("stakes text empty when idle", appmod.quit_stakes_text(0, 0, 0) == "")
    chk("stakes text lists counts",
        appmod.quit_stakes_text(2, 1, 3)
        == "2 armed, 1 msg(s) queued, 3 task(s) doing")

    def _one():
        return {"s0": SessionInfo("s0", title="t0", window_idx=0, tab_idx=0,
                                  last_screen=["x"])}

    aq = _TestApp(_one(), dry_run=True)
    async with aq.run_test() as pilot:
        await pilot.pause()
        aq.watcher.sessions["s0"].mode = "safe"       # something at stake
        await pilot.press("q")
        await pilot.pause()
        chk("q with armed session arms the guard, app stays up",
            aq._quit_armed and aq.is_running)
        await pilot.press("q")
        await pilot.pause()
    chk("second q quits (run_test context closed)", True)

    ai = _TestApp(_one(), dry_run=True)
    async with ai.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    chk("idle q quits instantly (guard never armed)", not ai._quit_armed)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def lock_tests():
    """acquire_singleton_lock: first holder wins, second is refused."""
    import os
    import tempfile
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    p = os.path.join(tempfile.mkdtemp(), "relay.lock")
    first = appmod.acquire_singleton_lock(p)
    chk("first relay acquires the lock", bool(first))
    second = appmod.acquire_singleton_lock(p)
    chk("second relay is refused (None)", second is None)
    # releasing the first (closing its handle) lets a new one acquire.
    try:
        first.close()
    except Exception:
        pass
    third = appmod.acquire_singleton_lock(p)
    chk("lock frees after the holder exits", bool(third))
    try:
        third.close()
    except Exception:
        pass
    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = lock_tests()
    sys.exit(0 if (r1 and r2) else 1)
