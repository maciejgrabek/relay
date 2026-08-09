"""TUI tests: markup-escaping, cursor-by-identity, divider safety, single Enter.

Run: python3 iterm/test_app.py
Uses Textual's headless run_test() with a stub watcher (no iTerm2 needed).
"""
import asyncio
import inspect
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

# Same idea for the audit log - Task 4's intervene test drives real (non-dry-
# run) execution, which calls audit.record(). Without this it would append to
# the developer's real ~/.relay/audit.jsonl (see test_audit.py, same guard).
os.environ["RELAY_AUDIT_LOG"] = os.path.join(
    tempfile.mkdtemp(prefix="relay-test-audit-"), "audit.jsonl")

sys.path.insert(0, os.path.dirname(__file__))
import app as appmod  # noqa: E402
import audit as auditmod  # noqa: E402
import config as cfgmod  # noqa: E402
from textual.widgets import Static  # noqa: E402
from watcher import SessionInfo  # noqa: E402


class StubWatcher:
    def __init__(self, sessions):
        self.sessions = sessions
        self.log = []
        self.log_total = 0
        self.sent = []
        self.registry = {}
        # config editor: a real Config plus the master mute and the four
        # live-editable sound attributes, mirroring the real Watcher's shape.
        self.cfg = cfgmod.Config()
        self.sounds_enabled = self.cfg.sounds_enabled
        self.alert_sound = self.cfg.alert_sound
        self.done_sound = self.cfg.done_sound
        self.danger_sound = self.cfg.danger_sound
        self.message_sound = self.cfg.message_sound
        # Mirror the real Watcher's pause interface the app calls, so the
        # app-level pause key path is exercisable headless (not just the pure
        # mascot function).
        self.paused = False
        # The real Watcher always sets this; the timers overlay's r/restore
        # path reads it, so the stub must have it to be pilot-testable.
        self.pending_timer_sids = set()

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

    def clear_extreme(self, sid):
        """Mirrors the real Watcher.clear_extreme: extreme -> insane, budget
        zeroed, True only when it actually was extreme. intervene's disarm
        now goes through this (not a hand-rolled mutation), so the stub has
        to have it to be pilot-testable."""
        info = self.sessions.get(sid)
        if info is None or info.mode != "extreme":
            return False
        info.mode = "insane"
        info.extreme_fires_left = 0
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
        # Cursor starts at 0 = the SOUNDS master mute: flipping it lands on the
        # running watcher live and persists, with no restart.
        await pilot.press("right")
        await pilot.pause()
        chk("right on the mute row silences the live watcher",
            ce.watcher.sounds_enabled is False
            and cfgmod.load()[0].sounds_enabled is False)
        await pilot.press("right")
        await pilot.pause()
        chk("right again un-mutes", ce.watcher.sounds_enabled is True)
        # Next row down is the first sound; changing it is live too.
        await pilot.press("down")
        await pilot.pause()
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

    # --- mascot picker: cycle the creature from the settings overlay ----------
    chk("mascot is an editable APPEARANCE enum",
        ("APPEARANCE", "mascot", "enum",
         cfgmod.MASCOT_NAMES) in appmod.settingsmod.SETTINGS)
    chk("changing the mascot needs no restart",
        appmod.settingsmod.is_live("mascot")
        and appmod.settingsmod.is_app_live("mascot"))
    chk("the picker cycles through every creature",
        appmod.settingsmod.change(cfgmod.Config(), "mascot", 1).mascot
        == cfgmod.MASCOT_NAMES[1]
        and appmod.settingsmod.change(cfgmod.Config(), "mascot", -1).mascot
        == cfgmod.MASCOT_NAMES[-1])

    was = appmod.ACTIVE_MASCOT
    try:
        mp = _TestApp(_one(), dry_run=True)
        async with mp.run_test() as pilot:
            await pilot.pause()
            await pilot.press("comma")
            await pilot.pause()
            mp._settings_cursor = [
                s[1] for s in appmod.settingsmod.SETTINGS].index("mascot")
            await pilot.press("right")
            await pilot.pause()
            chk("picking a creature applies live (no restart)",
                appmod.ACTIVE_MASCOT == cfgmod.MASCOT_NAMES[1])
            chk("picking a creature persists",
                cfgmod.load()[0].mascot == cfgmod.MASCOT_NAMES[1])
            chk("the banner redraws as the new creature",
                "▀▀▄▄" in str(mp.query_one("#banner",
                                           appmod.Static).render()))
            await pilot.press("left")
            await pilot.pause()
            chk("cycling back restores the CRT",
                appmod.ACTIVE_MASCOT == "crt"
                and "▀▀▄▄" not in str(mp.query_one("#banner",
                                                   appmod.Static).render()))
    finally:
        appmod.ACTIVE_MASCOT = was

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

    # --- mascot skins: one state machine, fifteen bodies ----------------------
    # A skin may only change the body drawn around the eyes. The header's
    # layout depends on every skin being the same shape, so this is checked
    # for every skin, in every mood, at every tick phase.
    from app import MASCOT_SKINS
    import config as _cfg

    chk("every configurable mascot name has a skin",
        sorted(MASCOT_SKINS) == sorted(_cfg.MASCOT_NAMES))
    chk("crt is the default skin", appmod.ACTIVE_MASCOT == "crt")

    moods = [{}, {"armed": 2}, {"working": True}, {"awaiting": 2},
             {"band": "☢ CRITICAL"}, {"paused": True},
             {"armed": 1, "reaction": "done"},
             {"armed": 1, "reaction": "danger"},
             # timers weave a clock into the guard/idle chatter, but only in
             # the second half of each 96-tick window - a 24-tick sweep would
             # never render that face at all.
             {"armed": 2, "timers_on": 1}, {"timers_on": 1}]
    shape_ok, glyph_ok = True, True
    for name in MASCOT_SKINS:
        for kw in moods:
            for t in range(96):
                kw2 = dict(kw)
                rows = mfb(t, kw2.pop("band", "ok"), skin=name, **kw2)
                if len(rows) != 6:
                    shape_ok = False
                    break
                # Rows 4-5 are body only, so they define the body width; rows
                # 1-3 are that same body plus the (equal-length) speech
                # bubble; row 0 is the antenna and may be short (the face's
                # right edge is allowed to be ragged, its columns are not).
                w = len(rows[5])
                if (len(rows[4]) != w or len(rows[0]) > w
                        or len({len(rows[i]) for i in (1, 2, 3)}) != 1
                        or len(rows[1]) <= w):
                    shape_ok = False
    chk("every skin is 6 rows with an aligned body, every mood, every tick",
        shape_ok)

    # Tokens must be wired through, not hardcoded per skin: the mood's eye
    # glyph has to reach the face whichever body is drawn around it.
    for name in MASCOT_SKINS:
        alarmed = " ".join(mfb(0, "ok", awaiting=2, skin=name))
        critical = " ".join(mfb(0, "☢ CRITICAL", armed=1, skin=name))
        if "⊙" not in alarmed or "x" not in critical:
            glyph_ok = False
    chk("every skin shows the mood's own eyes", glyph_ok)

    # Every state's screen cue must survive in EVERY body, not just the CRT's
    # screen. Checked on the body columns only - the speech bubble would
    # otherwise pass these for free, since it repeats the mood in words.
    def body(name, tick=0, **kw):
        rows = mfb(tick, kw.pop("band", "ok"), skin=name, **kw)
        w = len(rows[5])
        return "".join(r[:w] for r in rows)

    # (label, kwargs, a cue that ONLY the screen row carries - never the
    # antenna, so a skin that drops the screen cannot pass by accident)
    screens = [
        ("celebrate", {"armed": 1, "reaction": "done"}, "✓"),
        ("working", {"armed": 1, "working": True}, "·"),
        ("critical", {"band": "☢ CRITICAL", "armed": 1}, "░▒▓"),
        # ◷ excluded on purpose: it is also the clock window's antenna, so
        # only the other three hands prove the screen itself is ticking.
        ("timer clock", {"armed": 2, "timers_on": 1}, "◴◵◶"),
    ]
    missing = []
    for name in MASCOT_SKINS:
        for label, kw, cues in screens:
            # the clock and the roll cycle, so try the tick phases they use
            if not any(any(c in body(name, tick=t, **dict(kw)) for c in cues)
                       for t in range(0, 96)):
                missing.append(f"{name}/{label}")
    if missing:
        print("      missing screens:", ", ".join(missing[:8]),
              f"(+{len(missing) - 8} more)" if len(missing) > 8 else "")
    chk("every skin keeps every state's screen cue", not missing)

    chk("unknown skin name falls back to crt",
        mfb(0, "ok", armed=2, skin="wombat") == mfb(0, "ok", armed=2,
                                                    skin="crt"))
    chk("a skin does not change the mood ladder",
        appmod.effective_mascot_state("ok", awaiting=1, working=False,
                                      armed=1) == "alarmed")
    chk("skins differ from one another",
        mfb(0, "ok", armed=2, skin="invader") != mfb(0, "ok", armed=2,
                                                     skin="crt"))
    # "▀▀▄▄" is the invader's feet and appears nowhere in the RELAY logo.
    chk("the banner draws the skin it is given",
        "▀▀▄▄" in appmod.banner_with_face(0, "ok", armed=2, skin="invader")
        and "▀▀▄▄" not in appmod.banner_with_face(0, "ok", armed=2,
                                                  skin="crt"))

    # --- timers woven into the NORMAL guard/idle chatter (no takeover mood) ---
    # Timers do not get their own mood; a timer line surfaces every other phrase
    # window (tick//48 odd), keeping the base mood's visual.
    chk("timers do not create a new mood (still guarding/idle)",
        ems("ok", awaiting=0, working=False, armed=1) == "guarding"
        and ems("ok", awaiting=0, working=False, armed=0) == "idle")

    def _is_timer_line(ln):
        return any(w in ln for w in ("tick", "clock", "next in", "time",
                                     "counting", "cron"))
    # base window (tick 0): guarding says a GUARD line, no clock glyph
    g0 = "".join(mfb(0, "ok", armed=3, timers_on=2, timer_next=185))
    chk("guard base window: guard chatter, no clock",
        not _is_timer_line(g0) and not any(c in g0 for c in "◴◵◶◷"))
    # clock window (tick 48): a timer line + clock cue woven in, still aligned
    g48 = mfb(48, "ok", armed=3, timers_on=2, timer_next=185)
    chk("guard clock window: timer line + clock cue, aligned",
        _is_timer_line("".join(g48))
        and any(c in "".join(g48) for c in "◴◵◶◷")
        and len(g48) == 6 and all(g48[i][11] == "│" for i in (2, 3, 4)))
    # same weaving happens off-duty (idle) with a timer set
    i48 = "".join(mfb(48, "ok", armed=0, timers_on=1, timer_next=185))
    chk("idle clock window weaves a timer line too", _is_timer_line(i48))
    # no timers -> never a timer line, even in the clock window
    chk("no timers -> pure guard chatter (no timer line ever)",
        not _is_timer_line("".join(mfb(48, "ok", armed=3, timers_on=0))))
    chk("_mascot_countdown formats seconds/minutes/imminent",
        appmod._mascot_countdown(45) == "45s"
        and appmod._mascot_countdown(185) == "3m"
        and appmod._mascot_countdown(None) == "moments")

    # --- timers overlay -------------------------------------------------------
    # Row 0 is CLI-registered (non-empty `key`); row 1 is an operator row added
    # with `a` in this overlay - which DOES carry a label (app.py sets it to
    # the tab title, or to the raw session GUID when there is no SessionInfo).
    # `key`, not `label`, is the discriminator for the self: tag.
    _trows = [
        {"id": 1, "interval_min": 5, "payload": "check PRs", "mode": "idle",
         "enabled": 1, "active": 1, "last_fired_at": 1000.0,
         "max_fires": 10, "fire_count": 3, "label": "self:pr-duty",
         "key": "pr-duty"},
        {"id": 2, "interval_min": 9, "payload": "second", "mode": "now",
         "enabled": 1, "active": 1, "last_fired_at": 1000.0,
         "max_fires": 0, "fire_count": 0, "label": "relay", "key": ""}]
    _tv = appmod.timers_view_text(_trows, now=1000.0, session_title="api",
                                  width=90, cursor=1)
    chk("timers_view_text lists interval + payload", "5m" in _tv
        and "check PRs" in _tv and "second" in _tv)
    chk("timers_view_text shows fire-cap progress + unlimited",
        "3/10" in _tv and "∞" in _tv)
    chk("timers_view_text marks the cursor row (▸ on row index 1)",
        "▸" in _tv and _tv.count("▸") == 1)
    chk("timers_view_text tags the keyed (CLI-registered) row self:<key>",
        "self:pr-duty check PRs" in _tv)
    # An operator row (key == '') must render bare, no matter what its label
    # says - the label is the tab title and would duplicate the overlay header.
    chk("an operator row's label is never rendered as a tag",
        "relay second" not in _tv and _tv.count("relay") == 0)
    # A long operator label must not eat the payload budget or overflow `w`:
    # the tag is derived from `key`, so a 40-char label costs nothing.
    _wide = appmod.timers_view_text(
        [{"id": 1, "interval_min": 5, "payload": "p" * 100, "mode": "idle",
          "enabled": 1, "active": 1, "last_fired_at": 1000.0, "max_fires": 10,
          "fire_count": 0, "label": "3F1A22B9-0C4D-4E77-9A1B-77C0DE12AB34",
          "key": ""}],
        now=1000.0, session_title="api", width=90, cursor=99)  # nothing selected
    _wide_row = [ln for ln in _wide.splitlines() if "ppp" in ln]
    chk("a long operator label leaves the rendered row inside the width",
        len(_wide_row) == 1 and len(_wide_row[0]) <= 90)
    # a disabled (off) timer shows no countdown and is greyed out
    _off_row = {"id": 1, "interval_min": 5, "payload": "paused one",
                "mode": "now", "enabled": 0, "active": 1,
                "last_fired_at": 500.0, "max_fires": 10, "fire_count": 0,
                "label": "", "key": ""}
    _off = appmod.timers_view_text([_off_row], now=1000.0, session_title="api",
                                   width=90, cursor=99)          # not selected
    chk("off timer shows no countdown", "in " not in _off
        and "○ off" in _off)
    chk("off timer row is greyed out",
        f"[{appmod.DIM}]" in _off)
    # an empty key must not change the row at all: no tag, payload sits
    # directly after the 'next' column exactly as it did before the tag
    # existed (operator rows must render byte for byte as they always have).
    chk("empty key inserts no tag - payload follows the 'next' column bare",
        f"{'-':<18} paused one" in _off)
    # payload with a '[' must be escaped (view renders with markup on)
    _esc = appmod.timers_view_text(
        [{"id": 1, "interval_min": 5, "payload": "sed 's/[a-z]/x/'",
          "mode": "now", "enabled": 1, "active": 1, "last_fired_at": 1000.0,
          "max_fires": 10, "fire_count": 0, "label": "", "key": ""}],
        now=1000.0, session_title="api", width=90)
    chk("timers_view_text escapes '[' in the payload", "\\[a-z]" in _esc)

    # `b` overlay: the whole parked pile, oldest first, with a cursor. Unlike
    # the capture modal's five-row preview, this view must not cap the list -
    # being unable to see the rest is the reason it exists.
    _prows = [{"id": 3, "title": "this is something next to park", "owner": None},
              {"id": 7, "title": "retry backoff on inject", "owner": None},
              {"id": 9, "title": "widget shows parked count", "owner": "bff"}]
    _po = appmod.parked_overlay_text(_prows, "/Work/relay", 60, 0)
    _po_lines = _po.splitlines()
    chk("parked_overlay_text title row", any("PARKED" in r for r in _po_lines))
    chk("parked_overlay_text scope label in the header",
        any("/Work/relay" in r for r in _po_lines))
    chk("parked_overlay_text row 3",
        any("#3" in r and "something next to park" in r for r in _po_lines))
    chk("parked_overlay_text owner shown when set",
        any("#9" in r and "@bff" in r for r in _po_lines))
    chk("parked_overlay_text no owner marker when unowned",
        not any("#3" in r and "@" in r for r in _po_lines))
    chk("parked_overlay_text cursor on row 0",
        any(r.lstrip().startswith("▸") and "#3" in r for r in _po_lines))
    _po1 = appmod.parked_overlay_text(_prows, "/Work/relay", 60, 2)
    _po1_lines = _po1.splitlines()
    chk("parked_overlay_text cursor moves",
        any(r.lstrip().startswith("▸") and "#9" in r for r in _po1_lines))
    chk("parked_overlay_text exactly one cursor",
        sum(1 for r in _po1_lines if r.lstrip().startswith("▸")) == 1)
    _po_empty = appmod.parked_overlay_text([], "/Work/relay", 60, 0)
    chk("parked_overlay_text empty state teaches i",
        "i" in _po_empty and "park" in _po_empty.lower())
    _po_narrow = appmod.parked_overlay_text(
        [{"id": 1, "title": "y" * 200, "owner": None}], "/Work/x", 40, 0)
    chk("parked_overlay_text width clamped",
        all(len(r) <= 40 for r in _po_narrow.splitlines()))
    _po_clamped = appmod.parked_overlay_text(_prows, "/Work/relay", 60, 99)
    chk("parked_overlay_text out-of-range cursor clamps",
        sum(1 for r in _po_clamped.splitlines()
            if r.lstrip().startswith("▸")) == 1)

    # `b` discoverability: the earlier `i` key shipped without reaching the
    # key bar or help screen and had to be retrofitted - this is the "don't
    # repeat that" check for `b`. A bare "b" is not a safe substring test
    # (it also occurs in the theme's hex colors and in "bold"), so this
    # matches the exact rendered key markup instead.
    chk("b is in the key bar", "]b[/]" in appmod.KEYBAR
        and "parked" in appmod.KEYBAR.lower())
    chk("b is in the help screen", "parked" in appmod.help_text().lower())

    # live-feed header timers summary (one line, plain text)
    _sm = appmod.timers_summary(
        [{"interval_min": 5, "payload": "check PRs", "mode": "idle",
          "enabled": 1, "active": 1, "last_fired_at": 880.0,
          "max_fires": 10, "fire_count": 0},
         {"interval_min": 9, "payload": "x", "mode": "now", "enabled": 1,
          "active": 1, "last_fired_at": 0, "max_fires": 3, "fire_count": 3},
         {"interval_min": 1, "payload": "y", "mode": "now", "enabled": 1,
          "active": 0, "last_fired_at": 0, "max_fires": 10, "fire_count": 0}],
        now=1000.0)
    chk("timers_summary: on-count + next fire + done + needs-restore",
        "TIMERS:" in _sm and "1 on" in _sm and "check PRs" in _sm
        and "1 done" in _sm and "1 need restore" in _sm)
    chk("timers_summary empty when no timers",
        appmod.timers_summary([], now=1000.0) == "")
    chk("help advertises timers", "timers" in appmod.help_text().lower())
    chk("timer cell: active count, pending flag, else empty",
        appmod.timer_cell(active=2, pending=False) == "2"
        and appmod.timer_cell(active=0, pending=True) == "?"
        and appmod.timer_cell(active=0, pending=False) == "")

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

        # fire-cap edit: '[' lowers, ']' raises max_fires on the selected timer
        cap_id = [r for r in rows if r["payload"] == "check PRs"][0]["id"]
        _mf = lambda: [r for r in _db.list_timers(
            to._swarm_db_conn(), to._selected_sid())
            if r["id"] == cap_id][0]["max_fires"]
        chk("new timer defaults to fire cap 10", _mf() == 10)
        await pilot.press("left_square_bracket")
        await pilot.pause()
        chk("[ lowers the fire cap (10 -> 9)", _mf() == 9)
        await pilot.press("right_square_bracket")
        await pilot.pause()
        chk("] raises the fire cap (9 -> 10)", _mf() == 10)

        # the overlay re-renders on the periodic _refresh (not only on keypress),
        # so the countdown stays live while you watch it. Spy that _refresh calls
        # _render_timers while the overlay is open, and NOT once it's closed.
        import types
        _real_rt = appmod.RelayApp._render_timers
        _calls = {"n": 0}
        def _spy(self):
            _calls["n"] += 1
            return _real_rt(self)
        to._render_timers = types.MethodType(_spy, to)
        to._refresh()
        chk("open timers overlay re-renders on _refresh (live countdown)",
            _calls["n"] == 1)
        # (and not when it's closed)
        to._timers_visible = False
        to._refresh()
        chk("_refresh does not re-render a closed timers overlay",
            _calls["n"] == 1)
        to._timers_visible = True
        to._render_timers = types.MethodType(_real_rt, to)

        # r is state-dependent: a capped ('done') timer RESTARTS (fire_count->0).
        _db.update_timer(to._swarm_db_conn(), cap_id, fire_count=99)
        await pilot.press("r")
        await pilot.pause()
        fc = [r for r in _db.list_timers(to._swarm_db_conn(),
                                         to._selected_sid())
              if r["id"] == cap_id][0]
        chk("r restarts a capped timer (fire_count -> 0, still active)",
            fc["fire_count"] == 0 and fc["active"] == 1)

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

    # --- regression: arrow keys must not leak through the open timers
    # overlay onto the hidden session list. event.stop() in on_key only
    # halts DOM bubbling, not the App-level cursor_up/cursor_down Bindings -
    # so before the action_cursor_up/down guard, pressing 'down' while the
    # timers overlay was open moved BOTH the overlay's own timer cursor AND
    # the hidden background DataTable's cursor. Since _selected_sid() reads
    # the table's cursor_row, a subsequent overlay action (a/space/x/g/r)
    # would then silently target a DIFFERENT session than the one the
    # overlay is showing.
    def _two():
        return {
            "s0": SessionInfo("s0", title="t0", window_idx=0, tab_idx=0,
                              last_screen=["x"]),
            "s1": SessionInfo("s1", title="t1", window_idx=0, tab_idx=1,
                              last_screen=["x"]),
        }

    tl = _TestApp(_two(), dry_run=True)
    async with tl.run_test() as pilot:
        await pilot.pause()
        tl._refresh()
        await pilot.pause()
        table = tl.query_one(appmod.DataTable)
        table.move_cursor(row=tl._row_sids.index("s0"))
        await pilot.pause()
        chk("leak-test setup: s0 selected before opening timers",
            tl._selected_sid() == "s0")
        await pilot.press("t")
        await pilot.pause()
        chk("timers overlay open on s0", tl._timers_visible)
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        chk("down while the timers overlay is open must NOT move the "
            "hidden list's cursor: _selected_sid() stays s0, the session "
            "the overlay is showing",
            tl._selected_sid() == "s0")
        await pilot.press("t")               # close the overlay
        await pilot.pause()
        chk("timers overlay closed", not tl._timers_visible)
        await pilot.press("down")
        await pilot.pause()
        chk("normal list navigation still works with no overlay open: "
            "down now moves the selection",
            tl._selected_sid() == "s1")

    # --- park (i) project derivation: a '' project is invisible to every ----
    # cleanup path relay has (list_projects filters project != '',
    # wipe_project matches an exact string, an unowned item has no owner for
    # W's orphan-wipe either) - so an unregistered tab must derive a project
    # from its workdir basename (the _default_project convention, cli.py:160)
    # rather than park one with project=''. A registered tab keeps using its
    # own session's project. Driven directly through action_park/_park_save
    # (not simulated keystrokes) so the assertion is on the row park_task
    # actually writes, read back from a real (tempdir) db.
    import db as _db2
    import tempfile as _tempfile

    def _one_at(sid, workdir):
        return {sid: SessionInfo(sid, title=sid, window_idx=0, tab_idx=0,
                                 last_screen=["x"], workdir=workdir)}

    park_dir = _tempfile.mkdtemp(prefix="relay-park-proj-")
    expected_project = os.path.basename(park_dir)

    pu = _TestApp(_one_at("pu1", park_dir), dry_run=True)
    async with pu.run_test() as pilot:
        await pilot.pause()
        pu._refresh()
        await pilot.pause()
        chk("park setup: unregistered session selected",
            pu._selected_sid() == "pu1")
        pu.action_park()
        chk("i opens the park modal for a known workdir",
            pu._park is not None)
        pu._park["buf"] = "unregistered idea"
        pu._park_save()
        rows = [dict(r) for r in _db2.list_parked(
            pu._swarm_db_conn(), park_dir)]
        chk("unregistered park saved", len(rows) == 1
            and rows[0]["title"] == "unregistered idea")
        chk("unregistered park's project is the workdir basename, not '' "
            "(a '' project would be invisible to list_projects / "
            "wipe_project / Z zap)",
            rows[0]["project"] == expected_project and expected_project)
        chk("unregistered park has no owner (DIR scope: no swarm name "
            "exists to own it)", rows[0]["owner"] is None)

    reg_dir = _tempfile.mkdtemp(prefix="relay-park-reg-")
    _rconn = _db2.connect()
    _db2.register(_rconn, "park-worker", "pr1", "worker", "explicit-project")

    pr = _TestApp(_one_at("pr1", reg_dir), dry_run=True)
    async with pr.run_test() as pilot:
        await pilot.pause()
        pr._refresh()
        await pilot.pause()
        pr.action_park()
        chk("i opens the park modal for a registered session",
            pr._park is not None)
        pr._park["buf"] = "registered idea"
        pr._park_save()
        rows = [dict(r) for r in _db2.list_parked(
            pr._swarm_db_conn(), reg_dir)]
        chk("registered park saved", len(rows) == 1
            and rows[0]["title"] == "registered idea")
        chk("registered park keeps the session's own project, not the "
            "workdir basename",
            rows[0]["project"] == "explicit-project")
        chk("registered park defaults to SESSION scope (owner is the "
            "swarm name)", rows[0]["owner"] == "park-worker")

    # --- park (i) CRITICAL fix: a registered session with project='' -------
    # (`relay register --role worker` with no --project, cli.py:151) must
    # not park an unreachable row. Same fallback as the unregistered branch:
    # workdir basename, then refuse if that's empty too.
    noproj_dir = _tempfile.mkdtemp(prefix="relay-park-noproj-")
    expected_noproj = os.path.basename(noproj_dir)
    _db2.register(_rconn, "noproj-worker", "np1", "worker", "")

    pn = _TestApp(_one_at("np1", noproj_dir), dry_run=True)
    async with pn.run_test() as pilot:
        await pilot.pause()
        pn._refresh()
        await pilot.pause()
        pn.action_park()
        chk("a registered session with project='' still opens the park "
            "modal (refusal is a last resort, not the first move)",
            pn._park is not None)
        pn._park["buf"] = "empty project idea"
        pn._park_save()
        rows = [dict(r) for r in _db2.list_parked(
            pn._swarm_db_conn(), noproj_dir)]
        chk("registered-but-projectless park saved", len(rows) == 1
            and rows[0]["title"] == "empty project idea")
        chk("registered-but-projectless park falls back to the workdir "
            "basename, not '' - a '' project is invisible to "
            "list_projects/wipe_project/Z zap, exactly the unreachable-row "
            "bug this fix closes",
            rows[0]["project"] == expected_noproj and expected_noproj)

    pd = _TestApp(_one_at("pd1", "/"), dry_run=True)
    async with pd.run_test() as pilot:
        await pilot.pause()
        pd._refresh()
        await pilot.pause()
        pd.action_park()
        chk("a workdir with no usable basename ('/') refuses the park "
            "instead of stranding it with an unreachable '' project",
            pd._park is None and pd._modal_open)

    # --- park (i) context stamp: "last" comes off SessionInfo.last_command, -
    # cached by the watcher for EVERY tab regardless of registration - the
    # spec's own `"last": "grep -rn ..."` example, and the field
    # swarm.parked_item_text already renders with no producer before this
    # fix. Checked on an UNREGISTERED tab since that is the spec's main
    # persona and the case that previously got an empty "{}" stamp.
    import json as _json2

    def _one_at_cmd(sid, workdir, cmd):
        return {sid: SessionInfo(sid, title=sid, window_idx=0, tab_idx=0,
                                 last_screen=["x"], workdir=workdir,
                                 last_command=cmd)}

    ctx_dir = _tempfile.mkdtemp(prefix="relay-park-ctx-")
    pc = _TestApp(_one_at_cmd("pc1", ctx_dir, 'grep -rn "TODO" src/'),
                 dry_run=True)
    async with pc.run_test() as pilot:
        await pilot.pause()
        pc._refresh()
        await pilot.pause()
        pc.action_park()
        pc._park["buf"] = "context idea"
        pc._park_save()
        rows = [dict(r) for r in _db2.list_parked(
            pc._swarm_db_conn(), ctx_dir)]
        chk("context-stamp park saved", len(rows) == 1)
        ctx = _json2.loads(rows[0]["context"] or "{}")
        chk("unregistered tab's context stamp carries 'last' from "
            "SessionInfo.last_command - real context for the spec's main "
            "persona, where it used to be an empty '{}'",
            ctx.get("last") == 'grep -rn "TODO" src/')

    # --- intervene (!): the operator's brake-and-broadcast modal --------------
    def _one():
        # A second session so STOP has something to skip: every stub session
        # is idle by default, and STOP correctly ignores idle sessions. A
        # third (s2) is mutated to mode="extreme" further down to prove the
        # brake disarms it - left idle/off here so it doesn't perturb the
        # working-session counts the earlier ALL-scope assertions depend on.
        return {
            "s0": SessionInfo("s0", title="t0", window_idx=0, tab_idx=0,
                              last_screen=["x"]),
            "s1": SessionInfo("s1", title="t1", window_idx=0, tab_idx=1,
                              last_screen=["x"]),
            "s2": SessionInfo("s2", title="t2", window_idx=0, tab_idx=2,
                              last_screen=["x"]),
        }

    # dry_run=False - this block exercises the real interrupt/broadcast path,
    # not just the modal's field editing.
    a = _TestApp(_one(), dry_run=False)
    async with a.run_test() as pilot:
        await pilot.pause()
        a._refresh()
        await pilot.pause()

        await pilot.press("exclamation_mark")
        await pilot.pause()
        chk("! opens the intervene modal", a._intervene is not None)
        chk("default mode is stop_tell", a._intervene["mode"] == "stop_tell")
        chk("default scope is project", a._intervene["scope"] == "project")

        # --- finding 1: the natural panic reflex is '!' then ENTER, and the
        # default mode (stop_tell) refuses an empty buffer. Without a hint
        # THIS keeps the operator ignorant of why nothing happened. It must
        # be visible while composing - before ENTER, not only in a report.
        chk("empty stop_tell buffer shows the 'message required' hint "
            "in the COMPOSING modal, before ENTER",
            "text required" in a.query_one("#modal", Static).content)

        for ch in "stop now":
            await pilot.press(ch if ch != " " else "space")
        await pilot.pause()
        chk("typing fills the buffer", a._intervene["buf"] == "stop now")

        await pilot.press("backspace")
        await pilot.pause()
        chk("backspace deletes", a._intervene["buf"] == "stop no")

        await pilot.press("tab")
        await pilot.pause()
        chk("TAB cycles mode", a._intervene["mode"] == "stop")
        chk("TAB did not open the swarm view", a._swarm_visible is False)

        await pilot.press("right")
        await pilot.pause()
        chk("right cycles scope", a._intervene["scope"] == "all")
        await pilot.press("left")
        await pilot.press("left")
        await pilot.pause()
        chk("left cycles back past project", a._intervene["scope"] == "selected")

        await pilot.press("escape")
        await pilot.pause()
        chk("ESC closes with nothing executed", a._intervene is None)
        chk("ESC executed nothing", a._intervene_calls == [])

        # --- intervene executes -----------------------------------------------
        # StubWatcher.registry is {} so every stub session is UNREGISTERED, and
        # SessionInfo.state defaults to "idle" - STOP skips idle sessions, so
        # without this the brake would correctly send nothing and the test would
        # be asserting against an empty list. Mark one working, and use ALL scope
        # below because PROJECT cannot reach an unregistered tab.
        a.watcher.sessions["s1"].state = "working"
        a.watcher.sent.clear()
        await pilot.press("exclamation_mark")
        await pilot.press("tab")          # stop_tell -> stop
        await pilot.press("right")        # project -> all
        await pilot.press("enter")
        await pilot.pause()
        esc_sends = [s for s in a.watcher.sent if s[1] == "\x1b"]
        chk("STOP sent an ESC to the working session", len(esc_sends) == 1)
        chk("STOP targeted the working session", esc_sends[0][0] == "s1")
        chk("STOP sent ESC and nothing else",
            all(s[1] == "\x1b" for s in a.watcher.sent))
        chk("STOP never appended a return",
            not any(s[1].endswith("\r") for s in a.watcher.sent))
        chk("STOP skipped the idle sessions", len(a.watcher.sent) == 1)
        chk("a report modal is shown", a._modal_open)
        await pilot.press("space")        # dismiss the report
        await pilot.pause()

        # --- extreme is disarmed on every braked session; other modes are not
        # An interrupted tab is idle, and extreme pushes a prompt into an idle
        # tab after a dwell - without the disarm relay would restart the very
        # work the operator just stopped, within a minute. A plain arm level
        # (safe/wild/insane) only auto-approves prompts a stopped agent will
        # not raise, so it must be left alone - proven with s1 as a control.
        a.watcher.sessions["s1"].mode = "safe"      # control: braked, not extreme
        a.watcher.sessions["s2"].mode = "extreme"
        a.watcher.sessions["s2"].state = "working"
        a.watcher.sessions["s2"].extreme_fires_left = 3
        a.watcher.sent.clear()
        await pilot.press("exclamation_mark")
        await pilot.press("tab")          # stop_tell -> stop
        await pilot.press("right")        # project -> all
        await pilot.press("enter")
        await pilot.pause()
        chk("extreme session disarmed to insane on brake",
            a.watcher.sessions["s2"].mode == "insane")
        chk("extreme budget zeroed on disarm",
            a.watcher.sessions["s2"].extreme_fires_left == 0)
        chk("a 'safe' control session is left untouched",
            a.watcher.sessions["s1"].mode == "safe")
        await pilot.press("space")
        await pilot.pause()

        # --- Behaviour change A: the disarm is SCOPE-WIDE on a brake, not
        # only on sessions that were actually interrupted. An idle session
        # armed to extreme is exactly the one about to push a prompt on its
        # own - if STOP left it alone just because it was already idle (and
        # therefore never interrupted), the brake would fail the very job
        # section 5 of the spec says it exists for, within a minute.
        a.watcher.sessions["s1"].state = "idle"       # s1 was left "working" above
        a.watcher.sessions["s2"].mode = "extreme"
        a.watcher.sessions["s2"].state = "idle"       # NOT working: not interrupted
        a.watcher.sessions["s2"].extreme_fires_left = 3
        a.watcher.sent.clear()
        await pilot.press("exclamation_mark")
        await pilot.press("tab")          # stop_tell -> stop
        await pilot.press("right")        # project -> all
        await pilot.press("enter")
        await pilot.pause()
        chk("STOP sent nothing - every session in scope is idle",
            a.watcher.sent == [])
        chk("an IDLE extreme session is disarmed scope-wide on STOP anyway",
            a.watcher.sessions["s2"].mode == "insane")
        await pilot.press("space")
        await pilot.pause()

        # --- TELL-only must never touch arm state - a pure broadcast is not
        # a brake, and changing arm levels behind a TELL would be a bigger
        # promise than this mode makes.
        a.watcher.sessions["s2"].mode = "extreme"
        a.watcher.sessions["s2"].extreme_fires_left = 3
        a.watcher.sent.clear()
        await pilot.press("exclamation_mark")
        await pilot.press("tab")
        await pilot.press("tab")          # -> tell
        await pilot.press("right")        # project -> all
        for ch in "hi":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        chk("TELL sent no keystrokes", a.watcher.sent == [])
        chk("TELL-only leaves extreme untouched (never in TELL-only mode)",
            a.watcher.sessions["s2"].mode == "extreme")
        await pilot.press("space")
        await pilot.pause()
        a.watcher.sessions["s2"].mode = "insane"      # reset for the blocks below

        # --- TELL reaches unregistered targets but honestly reports none ---
        # PROJECT scope can't reach an unregistered tab at all (targets ==
        # []), which would make "TELL sent no keystrokes" trivially true
        # regardless of the unregistered-skip logic - ALL scope gives it
        # real (unregistered) targets to skip instead, and this block checks
        # the report's WORDING, not just that nothing was sent.
        a.watcher.sessions["s2"].state = "working"
        a.watcher.sent.clear()
        await pilot.press("exclamation_mark")
        await pilot.press("tab")
        await pilot.press("tab")          # -> tell
        await pilot.press("right")        # project -> all
        for ch in "hi":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        chk("TELL still sent no keystrokes with real (unregistered) targets",
            a.watcher.sent == [])
        report = a.query_one("#modal", Static).content
        chk("the report honestly says 'queued 0' - unregistered has no mailbox",
            "queued 0" in report)
        chk("the report never claims delivery with the word 'told'",
            "told" not in report)
        chk("the report says queuing is not delivery",
            "delivered on next idle prompt" in report)
        await pilot.press("space")
        await pilot.pause()

        await pilot.press("exclamation_mark")
        await pilot.press("tab")
        await pilot.press("tab")          # -> tell, empty buffer
        await pilot.press("enter")
        await pilot.pause()
        chk("TELL refuses an empty buffer", a._intervene is not None)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("exclamation_mark")
        await pilot.press("tab")          # -> stop, empty buffer
        await pilot.press("enter")
        await pilot.pause()
        chk("STOP commits on an empty buffer", a._intervene is None)
        await pilot.press("space")
        await pilot.pause()

        # --- parked overlay ---------------------------------------------------
        # The selected session needs a known workdir - action_park refuses
        # (CANNOT PARK) otherwise, and that's exactly what the 'i from the
        # overlay opens capture' assertion below exercises.
        a.watcher.sessions[a._selected_sid()].workdir = "/tmp/pk"
        conn = a._swarm_db_conn()
        import db as _db
        _db.park_task(conn, "first parked", "/tmp/pk", project="pk")
        _db.park_task(conn, "second parked", "/tmp/pk", project="pk")

        await pilot.press("b")
        await pilot.pause()
        chk("b opens the parked overlay", a._parked_visible)
        chk("cursor starts at the top", a._parked_cursor == 0)
        chk("scope starts on this directory", a._parked_scope == "dir")

        await pilot.press("right")
        await pilot.pause()
        chk("right widens the scope", a._parked_scope == "all")
        chk("all scope sees the items", len(a._parked_rows()) >= 2)

        await pilot.press("down")
        await pilot.pause()
        chk("down moves the cursor", a._parked_cursor == 1)
        await pilot.press("up")
        await pilot.press("up")
        await pilot.pause()
        chk("up stops at the top", a._parked_cursor == 0)

        before = len(a._parked_rows())
        first_id = a._parked_rows()[0]["id"]
        await pilot.press("d")
        await pilot.pause()
        chk("d drops one item", len(a._parked_rows()) == before - 1)
        chk("d dropped the selected row",
            first_id not in {r["id"] for r in a._parked_rows()})

        await pilot.press("escape")
        await pilot.pause()
        chk("escape closes the overlay", not a._parked_visible)

        await pilot.press("b")
        await pilot.press("i")
        await pilot.pause()
        chk("i from the overlay closes it", not a._parked_visible)
        chk("i from the overlay opens capture", a._park is not None)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("t")
        await pilot.pause()
        chk("t still opens timers", a._timers_visible)
        await pilot.press("t")
        await pilot.press("tab")
        await pilot.pause()
        chk("TAB still opens the swarm view", a._swarm_visible)
        await pilot.press("tab")
        await pilot.pause()

        # --- fix round 1: the overlay must not stack with its siblings -----
        # Driven live, not read from the code - all three were missed by the
        # tests above because they only ever opened ONE overlay at a time.
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        chk("TAB closes parked before opening swarm - only one pane visible",
            a._swarm_visible and not a._parked_visible)
        await pilot.press("tab")
        await pilot.pause()
        chk("swarm view closed again, nothing left stacked",
            not a._swarm_visible and not a._parked_visible)

        await pilot.press("t")
        await pilot.pause()
        await pilot.press("b")
        await pilot.pause()
        chk("b closes timers before opening parked - only one pane visible",
            a._parked_visible and not a._timers_visible)

        await pilot.press("b")
        await pilot.pause()
        chk("a second b re-toggles and closes the parked overlay, "
            "matching every other overlay in relay", not a._parked_visible)

        # --- fix round 2: EVERY opener must close parked first, not just
        # swarm and timers - narrowing the on_key swallow (round 1) let an
        # unmatched key fall through to any opener, and t/,/? had no
        # _parked_visible guard at all. Checked for every opener, not only
        # the three the finding named.
        def _n_overlays_open():
            return sum([a._swarm_visible, a._help_visible,
                        a._settings_visible, a._timers_visible,
                        a._parked_visible])

        for key, flag_name in (("t", "_timers_visible"),
                               ("comma", "_settings_visible"),
                               ("question_mark", "_help_visible"),
                               ("tab", "_swarm_visible")):
            await pilot.press("b")
            await pilot.pause()
            await pilot.press(key)
            await pilot.pause()
            chk(f"{key!r} closes parked before opening - exactly one pane "
                f"visible, parked not left stacked underneath",
                _n_overlays_open() == 1 and not a._parked_visible
                and getattr(a, flag_name))
            await pilot.press("escape")
            await pilot.pause()
            chk(f"{key!r}'s pane closes cleanly, nothing left open",
                _n_overlays_open() == 0)

        # --- the assertion that actually would have caught the finding:
        # on_key checks _parked_visible FIRST regardless of which pane is
        # actually on screen - so if an opener fails to close parked before
        # showing its own pane, 'd' while LOOKING AT TIMERS still reaches
        # the parked branch and destroys the highlighted parked row, with
        # nothing on screen showing a parked item.
        before_parked = {r["id"] for r in _db.list_parked(conn, None)}
        await pilot.press("b")
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        after_parked = {r["id"] for r in _db.list_parked(conn, None)}
        chk("t then d while parked was open drops NOTHING - d belongs to "
            "whichever pane the operator can actually see",
            after_parked == before_parked)
        await pilot.press("escape")
        await pilot.pause()

        # --- parked overlay: ENTER hands an item over --------------------------
        conn = a._swarm_db_conn()
        import db as _db
        _db.register(conn, "taker", a.watcher.sessions["s1"].session_id,
                     "worker", project="pk")
        # task_now isn't a sessions column - watcher.py's real refresh adds it
        # by joining tasks; the periodic 1s repaint (_refresh -> add) reads it
        # unconditionally, so a hand-built registry entry needs it too or the
        # app crashes on the next tick.
        taker_reg = dict(_db.get_session(conn, "taker"))
        taker_reg["task_now"] = ""
        a.watcher.registry[a.watcher.sessions["s1"].session_id] = taker_reg
        pid = _db.park_task(conn, "hand this over", "/tmp/pk", project="pk")

        a._selected_sid = lambda: a.watcher.sessions["s1"].session_id
        await pilot.press("b")
        await pilot.press("right")            # all scope, so the row is visible
        await pilot.pause()
        idx = [r["id"] for r in a._parked_rows()].index(pid)
        for _ in range(idx):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        row = _db.get_task(conn, pid)
        chk("ENTER assigns the owner", row["owner"] == "taker")
        chk("ENTER un-parks it", row["parked"] == 0)
        chk("ENTER sets doing", row["state"] == "doing")
        wakes = [m for m in _db.undelivered(conn)
                 if m["to_name"] == "taker" and m["kind"] == "wake"]
        chk("ENTER queues exactly one wake-up", len(wakes) == 1)
        chk("the wake-up names the task", str(pid) in wakes[0]["body"])
        chk("the item left the parked list",
            pid not in {r["id"] for r in a._parked_rows()})
        await pilot.press("escape")
        await pilot.pause()

        # An unregistered tab has no swarm name, so there is nothing to own it.
        pid2 = _db.park_task(conn, "cannot hand over", "/tmp/pk", project="pk")
        a._selected_sid = lambda: a.watcher.sessions["s0"].session_id
        a.watcher.registry.pop(a.watcher.sessions["s0"].session_id, None)
        await pilot.press("b")
        await pilot.press("right")
        await pilot.pause()
        idx2 = [r["id"] for r in a._parked_rows()].index(pid2)
        for _ in range(idx2):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        chk("ENTER on an unregistered tab refuses", a._modal_open)
        chk("the item stays parked", _db.get_task(conn, pid2)["parked"] == 1)
        await pilot.press("space")
        await pilot.pause()

        # --- fix round 1: ENTER/d must target the item IDENTITY that was on
        # screen, not a numeric cursor position re-resolved against a list
        # refetched at keypress time. The overlay can sit open arbitrarily
        # long while another session claims or drops the highlighted row via
        # `relay next` or the CLI - exactly the window ALREADY TAKEN exists
        # for - so a stale re-fetch can silently act on whatever backfilled
        # that slot instead.
        await pilot.press("escape")
        await pilot.pause()
        a._selected_sid = lambda: a.watcher.sessions["s1"].session_id  # taker

        # Two items, cursor on the second, the second raced away underneath
        # the operator - ENTER must leave the FIRST item alone and say so,
        # not silently grab it.
        a.watcher.sessions["s1"].workdir = "/tmp/pk-race"
        pid_c = _db.park_task(conn, "item C", "/tmp/pk-race", project="pk")
        pid_d = _db.park_task(conn, "item D", "/tmp/pk-race", project="pk")
        await pilot.press("b")
        await pilot.pause()
        ids_now = [r["id"] for r in a._parked_rows()]
        chk("both C and D are visible before the race",
            pid_c in ids_now and pid_d in ids_now)
        idx_d = ids_now.index(pid_d)
        for _ in range(idx_d):
            await pilot.press("down")
        await pilot.pause()
        chk("cursor is on D before the race",
            a._parked_ids[a._parked_cursor] == pid_d)
        raced = _db.claim_parked_by_id(conn, pid_d, "rival")
        chk("the simulated race actually claimed D", raced is not None)
        wakes_before = len(_db.undelivered(conn))
        await pilot.press("enter")
        await pilot.pause()
        row_c = _db.get_task(conn, pid_c)
        chk("ENTER after a race leaves the untouched on-screen item alone",
            row_c["parked"] == 1 and not row_c["owner"])
        chk("ENTER after a race shows a modal instead of grabbing another row",
            a._modal_open)
        chk("ENTER after a race queues no wake-up for the wrong item",
            len(_db.undelivered(conn)) == wakes_before)
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        # One item, raced away entirely - ENTER must show a modal rather than
        # doing nothing (the `and rows` guard used to swallow this case).
        a.watcher.sessions["s1"].workdir = "/tmp/pk-solo"
        pid_solo = _db.park_task(conn, "solo item", "/tmp/pk-solo", project="pk")
        await pilot.press("b")
        await pilot.pause()
        chk("solo scope shows exactly the one item",
            [r["id"] for r in a._parked_rows()] == [pid_solo])
        _db.claim_parked_by_id(conn, pid_solo, "rival")
        await pilot.press("enter")
        await pilot.pause()
        chk("ENTER on a raced-away sole item shows a modal, not a silent "
            "no-op", a._modal_open)
        await pilot.press("space")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        # Same two races for 'd': the surviving item must stay parked.
        a.watcher.sessions["s1"].workdir = "/tmp/pk-race2"
        pid_e = _db.park_task(conn, "item E", "/tmp/pk-race2", project="pk")
        pid_f = _db.park_task(conn, "item F", "/tmp/pk-race2", project="pk")
        await pilot.press("b")
        await pilot.pause()
        ids_now2 = [r["id"] for r in a._parked_rows()]
        idx_f = ids_now2.index(pid_f)
        for _ in range(idx_f):
            await pilot.press("down")
        await pilot.pause()
        chk("cursor is on F before the race (d scenario)",
            a._parked_ids[a._parked_cursor] == pid_f)
        chk("the simulated race actually dropped F",
            _db.drop_parked(conn, pid_f))
        await pilot.press("d")
        await pilot.pause()
        # Checked BEFORE the parked-state assertion below: a stale, unclamped
        # index (rows[self._parked_cursor] on a list that just got shorter)
        # raises IndexError inside on_key. Textual swallows that into
        # app._exception and keeps the message loop running rather than
        # stopping it there, so "E is still parked" would otherwise pass by
        # accident - the crash happened before drop_parked ever ran, not
        # because the code correctly identified E. Checking for the swallowed
        # exception directly is what makes this a real proof rather than a
        # coincidental one.
        chk("d after a race raises no unhandled exception",
            getattr(a, "_exception", None) is None)
        chk("d after a race leaves the untouched on-screen item parked",
            _db.get_task(conn, pid_e)["parked"] == 1)
        await pilot.press("escape")
        await pilot.pause()

        a.watcher.sessions["s1"].workdir = "/tmp/pk-solo2"
        pid_g = _db.park_task(conn, "solo drop target", "/tmp/pk-solo2",
                              project="pk")
        await pilot.press("b")
        await pilot.pause()
        chk("solo scope shows exactly the one item (d scenario)",
            [r["id"] for r in a._parked_rows()] == [pid_g])
        chk("the simulated race actually dropped the sole item",
            _db.drop_parked(conn, pid_g))
        await pilot.press("d")
        await pilot.pause()
        chk("d on a raced-away sole item raises no unhandled exception",
            getattr(a, "_exception", None) is None)
        chk("d on a raced-away sole item does not crash the overlay",
            a._parked_visible)
        await pilot.press("escape")
        await pilot.pause()
        a.watcher.sessions["s1"].workdir = "/tmp/pk"

    # --- dry-run mutates nothing: no sends, no queueing, DRY RUN report ----
    # A separate dry_run=True app - the block above flipped to dry_run=False
    # to exercise the real paths, which left the `if self.dry_run:` branch
    # itself unexercised. A panic button that quietly does something real
    # under dry-run is the worst possible failure here.
    ad = _TestApp(_one(), dry_run=True)
    async with ad.run_test() as pilot:
        await pilot.pause()
        ad._refresh()
        await pilot.pause()
        ad.watcher.sessions["s1"].state = "working"
        await pilot.press("exclamation_mark")
        for ch in "hi":
            await pilot.press(ch)
        await pilot.press("right")        # project -> all: real targets exist
        await pilot.pause()
        # --- finding 2: the COMPOSING modal must say it's inert under
        # dry-run, not only the report after ENTER - a silent no-op in a
        # panic button is the worst possible dry-run behaviour.
        chk("the composing modal (before ENTER) already says DRY RUN",
            "DRY RUN" in ad.query_one("#modal", Static).content)
        await pilot.press("enter")
        await pilot.pause()
        chk("dry-run sent no keystrokes despite a real working target",
            ad.watcher.sent == [])
        chk("dry-run shows the DRY RUN modal, not the real report",
            "DRY RUN" in ad.query_one("#modal", Static).content)
        await pilot.press("space")
        await pilot.pause()

        # --- finding 5 (dry-run half): every other relay action audits
        # dry-run too - intervene wrote nothing at all before this fix.
        dry_entries = [e for e in auditmod.read_tail()
                      if e.get("verdict") == "would-intervene"]
        chk("dry-run writes a 'would-intervene' audit line",
            len(dry_entries) == 1)
        if dry_entries:
            chk("the dry-run audit line names the mode and a target",
                "mode=stop_tell" in dry_entries[0]["command"] and "s1" in
                dry_entries[0]["command"])

    # --- finding 3: the count line must show TELL's REAL reach, not just
    # the raw session count - a registered/tellable split, at minimum in the
    # TELL-capable modes.
    def _mixed_registration_sessions():
        return {
            "r1": SessionInfo("r1", title="w1", window_idx=0, tab_idx=0,
                              last_screen=["x"], state="working"),
            "u1": SessionInfo("u1", title="scratch", window_idx=0, tab_idx=1,
                              last_screen=["x"], state="working"),
            "u2": SessionInfo("u2", title="scratch2", window_idx=0, tab_idx=2,
                              last_screen=["x"], state="working"),
        }

    at = _TestApp(_mixed_registration_sessions(), dry_run=False)
    async with at.run_test() as pilot:
        await pilot.pause()
        at.watcher.registry["r1"] = {"name": "w1", "project": "demo", "role": "worker", "task_now": ""}
        at._refresh()
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.press("right")        # project -> all: reaches every tab
        await pilot.pause()
        content = at.query_one("#modal", Static).content
        chk("ALL scope with 1 registered + 2 unregistered shows 3 sessions",
            "3 sessions" in content)
        chk("but only 1 is actually tellable - shown separately, not folded "
            "into 'sessions'",
            "1 tellable" in content)
        await pilot.press("escape")
        await pilot.pause()

    # --- finding 4: a queue-write failure must not be invisible, and the
    # report must not overstate a queue write as a delivery.
    def _queue_fail_sessions():
        return {
            "r1": SessionInfo("r1", title="w1", window_idx=0, tab_idx=0,
                              last_screen=["x"], state="working"),
        }

    aq = _TestApp(_queue_fail_sessions(), dry_run=False)
    async with aq.run_test() as pilot:
        await pilot.pause()
        aq.watcher.registry["r1"] = {"name": "w1", "project": "demo", "role": "worker", "task_now": ""}
        aq._refresh()
        await pilot.pause()
        real_queue_message = appmod.swarmdb.queue_message
        appmod.swarmdb.queue_message = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("db is locked"))
        try:
            await pilot.press("exclamation_mark")
            await pilot.press("tab")
            await pilot.press("tab")      # -> tell
            await pilot.press("right")    # project -> all
            for ch in "hi":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
        finally:
            appmod.swarmdb.queue_message = real_queue_message
        content = aq.query_one("#modal", Static).content
        chk("a queue-write failure is surfaced in the report, not only the "
            "Log", "queue write FAILED" in content)
        chk("queued count reflects the failure (0, not overstated)",
            "queued 0" in content)
        await pilot.press("space")
        await pilot.pause()

    # --- finding 5: the audit line must carry target names and all four
    # counts, and its 'session' field must not pretend to be one session's
    # title (audit_view_text does an exact match on that field).
    def _audit_sessions():
        return {
            "r1": SessionInfo("r1", title="w1", window_idx=0, tab_idx=0,
                              last_screen=["x"], state="working"),
            "r2": SessionInfo("r2", title="w2", window_idx=0, tab_idx=1,
                              last_screen=["x"], state="idle"),
        }

    aa = _TestApp(_audit_sessions(), dry_run=False)
    async with aa.run_test() as pilot:
        await pilot.pause()
        aa.watcher.registry["r1"] = {"name": "w1", "project": "demo", "role": "worker", "task_now": ""}
        aa.watcher.registry["r2"] = {"name": "w2", "project": "demo", "role": "worker", "task_now": ""}
        aa._refresh()
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.press("right")        # project -> all
        for ch in "hi":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        entries = [e for e in auditmod.read_tail() if e.get("verdict") == "intervene"]
        chk("a real intervene writes an audit line", len(entries) >= 1)
        if entries:
            e = entries[-1]
            chk("the audit 'session' field is not a real session's title "
                "('w1'/'w2') - audit_view_text would otherwise never show "
                "this entry, or worse, mismatch one",
                e["session"] not in ("w1", "w2"))
            chk("the audit line names both targets",
                "w1" in e["command"] and "w2" in e["command"])
            chk("the audit reason carries all four counts",
                all(f"{k}=" in e["reason"]
                    for k in ("interrupted", "skipped", "told", "disarmed")))
        await pilot.press("space")
        await pilot.pause()

    # --- findings 7 & 8: SELECTED on relay's own row must look and behave
    # differently from SELECTED on a real (but unregistered) worker tab.
    def _own_scope_sessions():
        return {
            "panel": SessionInfo("panel", title="relay-panel", window_idx=0,
                                 tab_idx=0, last_screen=["x"]),
            "unreg": SessionInfo("unreg", title="scratch", window_idx=0,
                                 tab_idx=1, last_screen=["x"], state="working"),
        }

    ao2 = _TestApp(_own_scope_sessions(), dry_run=False)
    async with ao2.run_test() as pilot:
        await pilot.pause()
        ao2._own_sid = "panel"
        ao2._refresh()
        await pilot.pause()

        t2 = ao2.query_one(appmod.DataTable)
        t2.move_cursor(row=ao2._row_sids.index("unreg"))
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.press("left")         # project -> selected
        await pilot.pause()
        chk("! opens normally with the cursor on a real unregistered row",
            ao2._intervene is not None)
        label_content = ao2.query_one("#modal", Static).content
        chk("an unregistered SELECTED tab is not labelled as relay's own "
            "panel (finding 7)",
            "relay's own panel" not in label_content)
        chk("an unregistered SELECTED tab gets its own distinct label",
            "unregistered tab" in label_content)
        await pilot.press("escape")
        await pilot.pause()

        t2.move_cursor(row=ao2._row_sids.index("panel"))
        await pilot.pause()
        await pilot.press("exclamation_mark")
        await pilot.press("left")         # project -> selected: lands on own row
        await pilot.pause()
        chk("SELECTED on relay's own row is refused through the "
            "display-only modal path (finding 8) - composing state is "
            "cleared, not left open on a zero-target modal",
            ao2._intervene is None)
        chk("a display-only modal appears instead", ao2._modal_open)
        chk("no intervene call was recorded", ao2._intervene_calls == [])
        await pilot.press("space")
        await pilot.pause()

    chk("! is in the key bar", "!" in appmod.KEYBAR)
    chk("! is in the help screen", "!" in appmod.help_text())
    chk("help explains what ! does",
        "intervene" in appmod.help_text().lower()
        or "stop" in appmod.help_text().lower())

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

    # --- zap (Z Z): whole-project delete, advertised + bound -----------------
    chk("KEYBAR advertises zap", "Z×2" in appmod.KEYBAR
        and "zap" in appmod.KEYBAR.lower())
    chk("help covers zap", "zap" in appmod.help_text().lower())
    chk("RelayApp binds Z to zap",
        any(getattr(b, "key", None) == "Z"
            and getattr(b, "action", "") == "zap"
            for b in appmod.RelayApp.BINDINGS))
    chk("action_zap exists", hasattr(appmod.RelayApp, "action_zap"))
    chk("W hint points at Z for a whole-project clear",
        "Z" in inspect.getsource(appmod.RelayApp.action_wipe))

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = lock_tests()
    sys.exit(0 if (r1 and r2) else 1)
