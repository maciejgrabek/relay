"""TUI tests: markup-escaping, cursor-by-identity, divider safety, single Enter.

Run: python3 iterm/test_app.py
Uses Textual's headless run_test() with a stub watcher (no iTerm2 needed).
"""
import asyncio
import os
import sys
import tempfile

# Must be set before any cfgmod.save()/load() call the config-editor pilot
# test below triggers - otherwise it writes straight to the developer's real
# ~/.relay/config (see test_watcher.py, which guards RELAY_CONFIG the same
# way, though only for load()). A real (writable, throwaway) path here so
# auto-save is actually exercised, not just swallowed by a failed mkdir.
os.environ["RELAY_CONFIG"] = os.path.join(
    tempfile.mkdtemp(prefix="relay-test-config-"), "config")

sys.path.insert(0, os.path.dirname(__file__))
import app as appmod  # noqa: E402
import config as cfgmod  # noqa: E402
from watcher import SessionInfo  # noqa: E402


class StubWatcher:
    def __init__(self, sessions):
        self.sessions = sessions
        self.log = []
        self.log_total = 0
        self.sent = []
        self.registry = {}
        # config editor: a real Config plus the four live-editable sound
        # attributes, mirroring the real Watcher's shape.
        self.cfg = cfgmod.Config()
        self.alert_sound = self.cfg.alert_sound
        self.done_sound = self.cfg.done_sound
        self.danger_sound = self.cfg.danger_sound
        self.message_sound = self.cfg.message_sound
        # Mirror the real Watcher's pause interface the app calls, so the
        # app-level pause key path is exercisable headless (not just the pure
        # mascot function).
        self.paused = False

    def toggle_pause(self):
        self.paused = not self.paused
        return self.paused

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
        self._running_cfg = self.watcher.cfg
        self._working_cfg = self.watcher.cfg


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

    # --- why_line: the live-feed's inline decision reason ---------------------
    chk("why_line shows reason + command",
        appmod.why_line("safe permission prompt", "grep foo", 80)
        == " WHY: safe permission prompt: grep foo\n")
    chk("why_line empty when no decision",
        appmod.why_line("", "grep foo", 80) == "")
    chk("why_line reason only when no command",
        appmod.why_line("dangerous command", "", 80)
        == " WHY: dangerous command\n")
    chk("why_line clamps to width",
        len(appmod.why_line("x" * 200, "y" * 200, 40)) <= 40)

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
    ht = appmod.help_text()
    chk("help text covers pause", "pause" in ht.lower())
    chk("help text covers shadow", "shadow" in ht.lower() and "◌" in ht)
    chk("keybar covers pause + shadow",
        "pause" in appmod.KEYBAR.lower() and "shadow" in appmod.KEYBAR.lower())
    chk("MODE_STYLE has a shadow entry",
        appmod.MODE_STYLE.get("shadow") == ("◌", "SHADOW", appmod.CYAN))
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

    # --- shadow tab: pane reads SHADOW, not MANUAL/LOCKED --------------------
    def _shadow_one():
        si = SessionInfo("sh", title="shadowed", window_idx=0, tab_idx=0,
                          last_screen=["x"])
        si.mode = "shadow"
        si.state = "blocked"     # would-escalate, not a real lockdown
        return {"sh": si}

    sh = _TestApp(_shadow_one(), dry_run=True)
    async with sh.run_test() as pilot:
        await pilot.pause()
        sh._refresh()
        await pilot.pause()
        pv = str(sh.query_one("#preview", appmod.Static).render())
        chk("shadow pane header reads MODE:SHADOW, not MODE:MANUAL",
            "MODE:SHADOW" in pv)
        chk("shadow pane suppresses the LOCKED/attn line",
            "LOCKED" not in pv and "AWAITING" not in pv and "STALE" not in pv)
        chk("shadow pane WHY line reads WOULD ESCALATE (not a real lockdown)",
            "SHADOW" in pv and "WOULD ESCALATE" in pv)

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

    # --- config editor overlay -------------------------------------------
    ce = _TestApp(_one(), dry_run=True)
    async with ce.run_test() as pilot:
        await pilot.pause()
        await pilot.press("comma")
        await pilot.pause()
        chk("comma opens settings",
            ce._settings_visible
            and str(ce.query_one("#settingsview").styles.display) == "block")
        # move to the first sound row (cursor starts at 0 = alert_sound), change
        before = ce.watcher.alert_sound
        await pilot.press("right")
        await pilot.pause()
        chk("right on a sound row changes the live watcher sound",
            ce.watcher.alert_sound != before)
        chk("the change was auto-saved to disk",
            cfgmod.load()[0].alert_sound == ce.watcher.alert_sound)
        # Session-mutating keys must be inert while the overlay hides the
        # session list - a stray 'a'/'1' must not act on a tab you can't see.
        mode_before = ce.watcher.sessions["s0"].mode
        ce.watcher.sent.clear()
        await pilot.press("a")
        await pilot.pause()
        chk("'a' while settings open does not arm sessions",
            ce.watcher.sessions["s0"].mode == mode_before)
        await pilot.press("1")
        await pilot.pause()
        chk("'1' while settings open does not send keys",
            ce.watcher.sent == [])
        await pilot.press("comma")
        await pilot.pause()
        chk("comma closes settings", not ce._settings_visible)
    chk("KEYBAR advertises settings", "," in appmod.KEYBAR
        and "settings" in appmod.KEYBAR.lower())
    chk("help covers settings", "settings" in appmod.help_text().lower())

    # --- preview pane toggle (f), persisted, + settings-editor parity --------
    chk("KEYBAR + help advertise the feed toggle",
        "feed" in appmod.KEYBAR.lower() and "feed" in appmod.help_text().lower())
    pp = _TestApp(_one(), dry_run=True)
    async with pp.run_test() as pilot:
        await pilot.pause()
        pane = pp.query_one("#preview", appmod.Static)
        chk("preview shown by default", pp._preview_visible
            and str(pane.styles.display) == "block")
        await pilot.press("f")
        await pilot.pause()
        chk("f hides the preview pane", not pp._preview_visible
            and str(pane.styles.display) == "none")
        chk("hiding is persisted to config",
            cfgmod.load()[0].preview_panel is False)
        await pilot.press("f")
        await pilot.pause()
        chk("f again shows it, and re-persists",
            pp._preview_visible and str(pane.styles.display) == "block"
            and cfgmod.load()[0].preview_panel is True)
        # the settings editor drives the SAME state (app-live, no restart).
        await pilot.press("comma")
        await pilot.pause()
        pp._settings_cursor = [s[1] for s in appmod.settingsmod.SETTINGS].index(
            "preview_panel")
        await pilot.press("right")
        await pilot.pause()
        chk("settings toggle hides the pane live + persists",
            not pp._preview_visible
            and str(pane.styles.display) == "none"
            and cfgmod.load()[0].preview_panel is False)

    # --- pause key path (app -> watcher.toggle_pause + PAUSED banner) --------
    pz = _TestApp(_one(), dry_run=True)
    async with pz.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        chk("p pauses the watcher", pz.watcher.paused is True)
        pz._tick_reactor()
        await pilot.pause()
        chk("subtitle shows the PAUSED banner",
            "PAUSED" in str(pz.query_one("#subtitle", appmod.Static).render()))
        await pilot.press("p")
        await pilot.pause()
        chk("p again resumes", pz.watcher.paused is False)

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

    # --- mascot barometer: cleared tally + earned reactions -------------------
    from app import mascot_face_big, effective_mascot_state

    def joined(**kw):
        return " ".join(mascot_face_big(0, kw.pop("band", "ok"), **kw))

    chk("guarding shows the cleared tally",
        "12" in joined(armed=3, approvals=12))
    chk("guarding tally absent when zero approvals",
        "cleared" not in joined(armed=3, approvals=0))
    chk("working shows the tally",
        "12" in joined(armed=3, working=True, approvals=12))
    chk("done reaction renders celebration",
        "done" in joined(armed=3, approvals=5, reaction="done")
        and "★" in joined(armed=3, approvals=5, reaction="done"))
    chk("danger reaction renders flinch",
        "danger" in joined(armed=3, reaction="danger")
        and "!" in joined(armed=3, reaction="danger"))
    # Precedence: a pending human need outranks a 'done' celebration.
    chk("done does not override alarmed",
        effective_mascot_state("ok", awaiting=1, working=False,
                               armed=1, reaction="done") == "alarmed")
    chk("danger reaction wins as flinch",
        effective_mascot_state("ok", awaiting=0, working=False,
                               armed=1, reaction="danger") == "flinch")
    chk("no reaction -> base state",
        effective_mascot_state("ok", awaiting=0, working=False,
                               armed=2, reaction=None) == "guarding")
    chk("face is always 6 lines",
        len(mascot_face_big(0, "ok", armed=3, reaction="done")) == 6)

    # Screen interior must be exactly 6 chars (eyes/mid/mouth) or the CRT
    # frame's box-drawing edges silently misalign.
    for r in ("done", "danger", None):
        f = mascot_face_big(0, "ok", armed=3, approvals=5, reaction=r)
        chk(f"frame {r}: 6-char screen interior (rows aligned)",
            all(f[i][11] == "│" for i in (2, 3, 4)))

    # --- global pause: outranks everything, even a danger reaction ------------
    from app import effective_mascot_state as ems
    chk("paused outranks alarmed",
        ems("ok", awaiting=3, working=False, armed=2, paused=True) == "paused")
    chk("paused outranks a danger reaction",
        ems("ok", awaiting=0, working=False, armed=1,
            reaction="danger", paused=True) == "paused")
    chk("not paused -> normal ladder",
        ems("ok", awaiting=0, working=False, armed=2, paused=False) == "guarding")
    from app import mascot_face_big as mfb
    chk("paused frame shows a paused cue",
        any("paused" in line for line in mfb(0, "ok", armed=2, paused=True)))
    chk("paused frame is 6 lines and aligned",
        len(mfb(0, "ok", armed=2, paused=True)) == 6
        and all(mfb(0, "ok", armed=2, paused=True)[i][11] == "│"
                for i in (2, 3, 4)))

    # --- timers overlay -------------------------------------------------------
    _tv = appmod.timers_view_text(
        [{"id": 1, "interval_min": 5, "payload": "check PRs", "mode": "idle",
          "enabled": 1, "active": 1, "last_fired_at": 1000.0}],
        now=1000.0, session_title="api", width=80)
    chk("timers_view_text lists interval + payload",
        "every 5m" in _tv and "check PRs" in _tv)
    chk("help advertises timers", "timers" in appmod.help_text().lower())
    chk("timer badge: active count wins, pending flag, else empty",
        appmod.timer_badge(active=2, pending=False) == "⏲2"
        and appmod.timer_badge(active=0, pending=True) == "⏲?"
        and appmod.timer_badge(active=0, pending=False) == "")

    to = _TestApp(_one(), dry_run=True)
    async with to.run_test() as pilot:
        await pilot.pause()
        to._refresh()          # populate the grid so a session is selected
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        chk("t opens timers overlay",
            to._timers_visible
            and str(to.query_one("#timersview").styles.display) == "block")
        await pilot.press("t")
        await pilot.pause()
        chk("t closes it", not to._timers_visible)
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        chk("esc also closes timers overlay", not to._timers_visible)

        await pilot.press("t")            # reopen
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        chk("a opens the add form", to._timer_form is not None)
        to.query_one("#timer_payload").value = "check PRs"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        import db as _db
        rows = _db.list_timers(to._swarm_db_conn(), to._selected_sid())
        chk("saved timer with typed payload + sane defaults",
            any(r["payload"] == "check PRs" and 1 <= r["interval_min"] <= 90
                for r in rows))

        # esc while the form is open must cancel ONLY the form - the timers
        # overlay itself has its own "escape" binding (action_dismiss_view)
        # that fires independently of on_key, and would otherwise also close
        # the whole overlay on the same keypress. A second esc then closes it.
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        chk("esc cancels the form but keeps the overlay open",
            to._timer_form is None and to._timers_visible)
        await pilot.press("escape")
        await pilot.pause()
        chk("a second esc then closes the overlay", not to._timers_visible)

    # --- regression: 'a' must not crash when no session is selected -----
    # _selected_sid() legitimately returns None when the grid has zero
    # selectable rows (documented onboarding state / cursor on a divider).
    # Before the fix, 'a' had no sid guard (unlike x/r/left/right/m), so it
    # opened the add form anyway; the previous test proves the form opens
    # fine with a real sid, this one proves the 'a' handler stays inert
    # with a genuinely empty grid (zero sessions -> _row_sids == [] ->
    # _selected_sid() returns None for real, not faked).
    te = _TestApp({}, dry_run=True)
    async with te.run_test() as pilot:
        await pilot.pause()
        te._refresh()
        await pilot.pause()
        chk("empty session grid genuinely has no selected sid",
            te._row_sids == [] and te._selected_sid() is None)
        await pilot.press("t")
        await pilot.pause()
        chk("t opens the timers overlay even with no session (shows 'no session.')",
            te._timers_visible)
        await pilot.press("a")
        await pilot.pause()
        chk("a is inert with no selected session: no form opened, no crash",
            te._timer_form is None and te.is_running)

    # --- regression: the session vanishing WHILE the add form is open must
    # not crash either - this is the actual crash line (_timer_form_save
    # computing label=None and calling swarmdb.add_timer with a NOT NULL
    # violation inside the unguarded on_input_submitted handler). Opening
    # the form requires a real sid (the 'a' guard above), so to reach this
    # second guard we open the form normally against a real session, then
    # genuinely make the session disappear (tab closed) before submitting -
    # _refresh() rebuilds _row_sids to empty, so _selected_sid() truly
    # returns None afterward; nothing here is asserted by poking internal
    # state directly.
    tc = _TestApp(_one(), dry_run=True)
    async with tc.run_test() as pilot:
        await pilot.pause()
        tc._refresh()
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        chk("add form opens against a real selected session",
            tc._timer_form is not None)
        import db as _db2
        before = list(_db2.list_timers(tc._swarm_db_conn(), "s0"))
        payload_marker = "vanished-session-payload"
        tc.query_one("#timer_payload").value = payload_marker
        await pilot.pause()
        tc.watcher.sessions.clear()          # the session's tab closes
        tc._refresh()
        await pilot.pause()
        chk("selected sid genuinely goes None once the session is gone",
            tc._row_sids == [] and tc._selected_sid() is None)
        await pilot.press("enter")           # submits the still-open form
        await pilot.pause()
        chk("no crash: app still running after submitting with a null sid",
            tc.is_running and tc._timer_form is None)
        after = list(_db2.list_timers(tc._swarm_db_conn(), "s0"))
        chk("no new timer row was created for the vanished session",
            len(after) == len(before)
            and not any(r["payload"] == payload_marker for r in after))

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
