"""Extreme insane mode suite: the draft-guard helper, config knobs, watcher
arming/firing/exhaustion, TUI + statusbar chrome.

Run: python3 iterm/test_extreme.py
"""
import asyncio
import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
# Hermetic: never read the developer's real ~/.relay/config in tests.
os.environ["RELAY_CONFIG"] = "/nonexistent/relay-test-config"

import app as appmod  # noqa: E402

ok = True


def chk(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    ok = ok and cond


# Screen tails. READY is a Claude idle screen with an EMPTY input box;
# DRAFT has operator text typed after the '>' but not submitted.
READY = ["╭──────────────╮", "│ >            │", "╰──────────────╯",
         "  ? for shortcuts"]
DRAFT = ["╭──────────────╮", "│ > fix the login bug   │",
         "╰──────────────╯", "  ? for shortcuts"]
SHELL = ["some output", "~/Work $"]
# Claude's footer sometimes wraps to TWO lines (a "⏵⏵ accept edits" status
# line above "? for shortcuts") - the input row then sits 4 lines from the
# bottom of the non-blank tail, past prompt_line_empty's old 3-line window.
READY_TWO_LINE_FOOTER = [
    "╭──────────────╮",
    "│ >            │",
    "╰──────────────╯",
    "  ⏵⏵ accept edits on",
    "  ? for shortcuts",
]


def test_prompt_line_empty():
    import swarm
    chk("empty input box -> True", swarm.prompt_line_empty(READY))
    chk("typed draft -> False", not swarm.prompt_line_empty(DRAFT))
    chk("shell prompt (no box) -> False", not swarm.prompt_line_empty(SHELL))
    chk("empty screen -> False", not swarm.prompt_line_empty([]))
    chk("READY still passes claude_prompt_ready",
        swarm.claude_prompt_ready(READY))
    chk("two-line footer still passes claude_prompt_ready",
        swarm.claude_prompt_ready(READY_TWO_LINE_FOOTER))
    chk("two-line footer: empty input box -> True (input row is 4 lines "
        "from the bottom, past the old 3-line window)",
        swarm.prompt_line_empty(READY_TWO_LINE_FOOTER))


def test_config_knobs():
    import tempfile
    import config as C
    d = C.Config()
    chk("default extreme_fires = 5", d.extreme_fires == 5)
    chk("default extreme_dwell = 45.0", d.extreme_dwell == 45.0)
    path = os.path.join(tempfile.mkdtemp(), "config")
    with open(path, "w") as f:
        f.write("[swarm]\nextreme_fires = 3\nextreme_dwell = 10\n")
    cfg, warns = C.load(path)
    chk("parses extreme_fires = 3", cfg.extreme_fires == 3)
    chk("parses extreme_dwell = 10.0", cfg.extreme_dwell == 10.0)
    with open(path, "w") as f:
        f.write("[swarm]\nextreme_fires = 0\nextreme_dwell = -5\n")
    cfg2, _ = C.load(path)
    chk("extreme_fires clamps to >= 1", cfg2.extreme_fires == 1)
    chk("extreme_dwell clamps to >= 0", cfg2.extreme_dwell == 0.0)
    chk("dump() round-trips the knobs",
        "extreme_fires" in C.dump(cfg) and "extreme_dwell" in C.dump(cfg))


class FakeSession:
    def __init__(self):
        self.sent = []

    async def async_send_text(self, t):
        self.sent.append(t)


def _mk_watcher():
    import watcher as W
    W.notify_mac = lambda *a, **k: None
    W.audit.record = lambda *a, **k: True
    return W, W.Watcher(connection=None, dry_run=False)


def test_arming_plumbing():
    W, w = _mk_watcher()
    chk("watcher reads extreme_fires from cfg default",
        w.extreme_fires == 5)
    chk("watcher reads extreme_dwell from cfg default",
        w.extreme_dwell == 45.0)

    info = W.SessionInfo("s1", title="t1", _iterm_session=FakeSession(),
                         mode="insane")
    w.sessions["s1"] = info
    chk("set_extreme refuses a safe session",
        not (setattr(info, "mode", "safe") or w.set_extreme("s1", "push")))
    info.mode = "insane"
    chk("set_extreme refuses an empty prompt",
        not w.set_extreme("s1", "   "))
    chk("set_extreme arms an insane session", w.set_extreme("s1", "push on"))
    chk("mode is extreme", info.mode == "extreme")
    chk("budget filled from config", info.extreme_fires_left == 5)
    chk("prompt stored stripped", info.extreme_prompt == "push on")
    chk("extreme session is active (armed)", info.active)
    chk("re-arm while extreme is allowed", w.set_extreme("s1", "push more"))
    chk("clear_extreme drops to insane",
        w.clear_extreme("s1") and info.mode == "insane")
    chk("prompt survives disarm (re-arm convenience)",
        info.extreme_prompt == "push more")
    chk("clear_extreme on a non-extreme session is False",
        not w.clear_extreme("s1"))
    w.own_sid = "s1"
    info.mode = "insane"
    chk("set_extreme never arms relay's own tab",
        not w.set_extreme("s1", "push"))
    w.own_sid = None
    chk("extreme is not spawn-requestable",
        "extreme" not in W.swarmdb.ARM_REQUEST_MODES)
    chk("extreme is not set_mode-able (MODES unchanged)",
        "extreme" not in w.MODES)
    chk("extreme is not in the SPACE cycle",
        "extreme" not in w._MODE_CYCLE)


def test_extreme_approves_like_insane():
    W, w = _mk_watcher()
    fs = FakeSession()
    info = W.SessionInfo("s2", title="t2", _iterm_session=fs, mode="extreme")
    w.sessions["s2"] = info
    raw = [" Bash command", "", "   grep foo src/", "   search", "",
           "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    asyncio.run(w._handle(info, raw, [True] * len(raw)))
    chk("extreme approves a permission prompt (insane superset)",
        fs.sent == ["\r"])


def test_persist_maps_extreme_to_insane():
    W, w = _mk_watcher()
    stored = {}
    W.swarmdb.set_session_mode = \
        lambda conn, name, mode: stored.__setitem__(name, mode)
    w._swarm_conn = lambda: None
    w.registry["s3"] = {"name": "worker3"}
    w.sessions["s3"] = W.SessionInfo("s3", title="t3", mode="insane")
    w._persist_mode("s3", "extreme")
    chk("persisting extreme writes 'insane'", stored.get("worker3") == "insane")
    w._persist_mode("s3", "wild")
    chk("other modes persist verbatim", stored.get("worker3") == "wild")


def _extreme_info(W, fs, *, dwell_ok=True):
    info = W.SessionInfo("x1", title="ex", _iterm_session=fs,
                         mode="extreme", state="idle")
    info.extreme_prompt = "keep going"
    info.extreme_fires_left = 2
    info.last_screen = list(READY)
    info._idle_since = time.time() - (999 if dwell_ok else 0)
    return info


def test_extreme_fires():
    W, w = _mk_watcher()
    fs = FakeSession()
    info = _extreme_info(W, fs)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("push sends prompt then Enter", fs.sent == ["keep going", "\r"])
    chk("budget decremented", info.extreme_fires_left == 1)
    chk("idle anchor reset after fire", info._idle_since == 0.0)


def test_extreme_gates():
    W, w = _mk_watcher()

    fs = FakeSession()
    info = _extreme_info(W, fs)
    info.last_screen = list(DRAFT)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("draft on the input line blocks the push", fs.sent == [])

    fs2 = FakeSession()
    i2 = _extreme_info(W, fs2)
    i2._idle_since = time.time()  # just went idle: dwell not elapsed
    asyncio.run(w._fire_extreme(i2))
    chk("dwell not elapsed blocks the push", fs2.sent == [])

    fs3 = FakeSession()
    i3 = _extreme_info(W, fs3)
    i3.state = "working"
    asyncio.run(w._fire_extreme(i3))
    chk("non-idle state blocks and resets the anchor",
        fs3.sent == [] and i3._idle_since == 0.0)

    fs4 = FakeSession()
    i4 = _extreme_info(W, fs4)
    w.paused = True
    asyncio.run(w._fire_extreme(i4))
    chk("paused blocks the push", fs4.sent == [])
    w.paused = False

    fs5 = FakeSession()
    i5 = _extreme_info(W, fs5)
    i5.extreme_fires_left = 0
    asyncio.run(w._fire_extreme(i5))
    chk("zero budget never fires", fs5.sent == [])

    fs6 = FakeSession()
    i6 = _extreme_info(W, fs6)
    i6.mode = "insane"
    asyncio.run(w._fire_extreme(i6))
    chk("non-extreme mode never fires", fs6.sent == [])

    fs7 = FakeSession()
    i7 = _extreme_info(W, fs7)
    w.own_sid = "x1"
    asyncio.run(w._fire_extreme(i7))
    chk("own tab never fires", fs7.sent == [])
    w.own_sid = None

    fs8 = FakeSession()
    i8 = _extreme_info(W, fs8)
    w.registry["x1"] = {"name": "wx"}
    _orig_undelivered = W.swarmdb.undelivered
    W.swarmdb.undelivered = lambda conn, name: [{"id": 1}]
    w._swarm_conn = lambda: None
    try:
        asyncio.run(w._fire_extreme(i8))
        chk("queued inbox mail blocks the push", fs8.sent == [])
    finally:
        W.swarmdb.undelivered = _orig_undelivered
        del w.registry["x1"]


def test_extreme_audit_before_act():
    W, w = _mk_watcher()
    W.audit.record = lambda *a, **k: False   # durable write fails
    fs = FakeSession()
    info = _extreme_info(W, fs)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("audit failure means NO push", fs.sent == [])
    chk("audit failure keeps the budget", info.extreme_fires_left == 2)
    W.audit.record = lambda *a, **k: True


def test_extreme_exhaustion():
    W, w = _mk_watcher()
    fs = FakeSession()
    info = _extreme_info(W, fs)
    info.extreme_fires_left = 1
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("last push still sends", fs.sent == ["keep going", "\r"])
    chk("exhaustion reverts to insane", info.mode == "insane")
    chk("exhaustion noted in the log",
        any("exhausted" in l for l in w.log))


def test_extreme_dry_run():
    W, w = _mk_watcher()
    w.dry_run = True
    fs = FakeSession()
    info = _extreme_info(W, fs)
    w.sessions["x1"] = info
    asyncio.run(w._fire_extreme(info))
    chk("dry-run never sends", fs.sent == [])
    chk("dry-run keeps the budget", info.extreme_fires_left == 2)
    chk("dry-run resets the anchor (no per-tick spam)",
        info._idle_since == 0.0)


class _ExtremeStubWatcher:
    """Just enough of the real Watcher for RelayApp to mount headless and
    for action_extreme() to run: sessions + the config/log/registry shape
    _refresh() and the settings pane read."""

    def __init__(self, sessions):
        import config as cfgmod
        self.sessions = sessions
        self.log = []
        self.log_total = 0
        self.registry = {}
        self.cfg = cfgmod.Config()
        self.sounds_enabled = self.cfg.sounds_enabled
        self.alert_sound = self.cfg.alert_sound
        self.done_sound = self.cfg.done_sound
        self.danger_sound = self.cfg.danger_sound
        self.message_sound = self.cfg.message_sound
        self.paused = False
        self.pending_timer_sids = set()
        self.extreme_fires = 5

    def toggle(self, s):
        pass

    def toggle_hidden(self, s):
        pass

    async def refresh_screen(self, s):
        pass


class _TestApp(appmod.RelayApp):
    """Headless RelayApp wired to a stub watcher - shared scaffold for the
    extreme-arm and modal pilot tests below."""

    def __init__(self, sessions, **k):
        super().__init__(**k)
        self._stub = sessions

    async def _connect(self):
        self.watcher = _ExtremeStubWatcher(self._stub)
        self._running_cfg = self.watcher.cfg
        self._working_cfg = self.watcher.cfg


def test_extreme_arm_sid_binding():
    """E is a double-press confirm, like R/W/Z - but unlike those (which act
    globally), it must be bound to the SELECTED session: arming on session A
    then moving the cursor to session B must not let B's first E land as the
    confirming second press meant for A."""
    from watcher import SessionInfo

    sessions = {
        "e1": SessionInfo("e1", title="one", window_idx=0, tab_idx=0,
                          mode="insane"),
        "e2": SessionInfo("e2", title="two", window_idx=0, tab_idx=1,
                          mode="insane"),
    }

    async def run():
        a = _TestApp(sessions, dry_run=True)
        async with a.run_test() as pilot:
            await pilot.pause()
            a._refresh()
            await pilot.pause()
            t = a.query_one(appmod.DataTable)

            t.move_cursor(row=a._row_sids.index("e1"))
            await pilot.pause()
            a.action_extreme()
            chk("first E on e1 arms e1", a._extreme_armed == "e1")
            chk("first E does not open the form yet",
                a._extreme_form is None)

            t.move_cursor(row=a._row_sids.index("e2"))
            await pilot.pause()
            a.action_extreme()
            chk("E on a DIFFERENT session (e2) re-arms for e2, not a "
                "confirming press for e1", a._extreme_armed == "e2")
            chk("moving off e1 before the second E must not open its form",
                a._extreme_form is None)

            a.action_extreme()
            chk("second E on the SAME session (e2) opens the form",
                a._extreme_form is not None
                and a._extreme_form["sid"] == "e2")
            chk("arm clears once the form opens", a._extreme_armed is None)
            a._extreme_form_close()

            # Refusal path (non-insane/extreme mode) must not leave a stale
            # arm behind for a session that later becomes armable.
            sessions["e1"].mode = "safe"
            t.move_cursor(row=a._row_sids.index("e1"))
            await pilot.pause()
            a.action_extreme()
            chk("refusal on a non-insane session clears any stale arm",
                a._extreme_armed is None)

    asyncio.run(run())


def test_modal():
    """The E E refusal floats a DOS modal; any key closes it and is
    swallowed (no binding fires); the overlay guard holds while open."""
    from watcher import SessionInfo

    sessions = {
        "m1": SessionInfo("m1", title="one", window_idx=0, tab_idx=0,
                          mode="safe"),
        "m2": SessionInfo("m2", title="two", window_idx=0, tab_idx=1,
                          mode="insane"),
    }

    async def run():
        a = _TestApp(sessions, dry_run=True)
        async with a.run_test() as pilot:
            await pilot.pause()
            a._refresh()
            await pilot.pause()
            t = a.query_one(appmod.DataTable)

            t.move_cursor(row=a._row_sids.index("m1"))
            await pilot.pause()
            a.action_extreme()
            chk("E on a safe session opens the modal", a._modal_open)
            m = a.query_one("#modal")
            chk("#modal is displayed and filled",
                str(m.styles.display) != "none"
                and "NOT AVAILABLE" in str(m.render()))
            chk("overlay guard holds while modal open",
                a._any_overlay_open())

            a.action_extreme()
            chk("actions are inert behind the modal (no arm)",
                a._extreme_armed is None)

            await pilot.press("q")
            chk("any key closes the modal", not a._modal_open)
            chk("the key is swallowed - q did not arm quit",
                not a._quit_armed)
            chk("app still running after swallowed q", a.is_running)

            # Tab is bound with priority=True (so it works while an Input
            # holds focus), which means it reaches action_swarm_view BEFORE
            # on_key's modal-swallow guard ever sees it - action_swarm_view
            # needs its own guard, or Tab would flip the swarm view open
            # underneath the still-floating modal.
            a.action_extreme()
            chk("re-opening the modal for the tab case", a._modal_open)
            await pilot.press("tab")
            chk("tab closes the modal too", not a._modal_open)
            chk("tab does not also open the swarm view",
                not a._swarm_visible)

            t.move_cursor(row=a._row_sids.index("m2"))
            await pilot.pause()
            a.action_extreme()
            chk("INSANE session arms without a modal",
                a._extreme_armed == "m2" and not a._modal_open)

    asyncio.run(run())


def test_tui_chrome():
    import app as appmod
    chk("MODE_STYLE has an extreme entry",
        appmod.MODE_STYLE.get("extreme") == ("✷", "EXTREME", appmod.DANGER))
    chk("E is bound",
        any(getattr(b, "key", "") == "E" for b in appmod.RelayApp.BINDINGS))
    chk("keybar advertises E×2", "E×2" in appmod.KEYBAR)
    chk("help covers extreme", "EXTREME" in appmod.help_text())
    chk("action_extreme exists", hasattr(appmod.RelayApp, "action_extreme"))
    chk("form open/save/close helpers exist",
        hasattr(appmod.RelayApp, "_extreme_form_open")
        and hasattr(appmod.RelayApp, "_extreme_form_save")
        and hasattr(appmod.RelayApp, "_extreme_form_close"))
    chk("preview pane mode label knows extreme",
        appmod.PREVIEW_MODE_LABEL.get("extreme") == "EXTREME")
    chk("_extreme_armed defaults to None (sid-bound, not a bare bool)",
        "_extreme_armed = None" in inspect.getsource(appmod.RelayApp.__init__))


def test_push_line():
    import app as appmod
    import watcher as W
    now = 1000.0
    info = W.SessionInfo("p1", title="p", mode="extreme", state="idle")
    info.extreme_fires_left = 4
    info._idle_since = now - 10.0
    line = appmod.extreme_push_line(info, 45.0, now, 60)
    chk("countdown while dwell runs", "PUSH: in 36s (4 left)" in line)
    info._idle_since = now - 60.0
    chk("due but gated names the holders",
        "PUSH: due" in appmod.extreme_push_line(info, 45.0, now, 60))
    info.state = "working"
    chk("non-idle waits for idle",
        "waiting for idle" in appmod.extreme_push_line(info, 45.0, now, 60))
    info.state = "idle"
    info._idle_since = 0.0
    chk("idle_since unset also waits",
        "waiting for idle" in appmod.extreme_push_line(info, 45.0, now, 60))
    info.mode = "insane"
    chk("non-extreme renders nothing",
        appmod.extreme_push_line(info, 45.0, now, 60) == "")


def test_dos_modal_text():
    import app as appmod
    t = appmod.dos_modal_text("EXTREME - NOT AVAILABLE",
                              ["This session is not INSANE.",
                               "SPACE cycles the arm level."], 80)
    rows = t.splitlines()
    chk("top border is double-line", rows[0].startswith("╔")
        and rows[0].rstrip().endswith("╗") and "═" in rows[0])
    chk("title row present", "EXTREME - NOT AVAILABLE" in rows[1])
    chk("separator row present", rows[2].startswith("╠")
        and rows[2].rstrip("▓").endswith("╣"))
    chk("body line present", any("not INSANE" in r for r in rows))
    chk("footer prompt present", any("press any key" in r for r in rows))
    chk("bottom border + shadow", any(r.startswith("╚") for r in rows)
        and rows[-1].strip().strip("▓") == "")
    box = [r for r in rows if r and r[0] in "╔║╠╚"]
    chk("all box rows same width",
        len({len(r.rstrip("▓")) for r in box}) == 1)
    t2 = appmod.dos_modal_text("T", ["x" * 200], 40)
    chk("long body line truncated to width",
        all(len(r) <= 40 for r in t2.splitlines()))
    t3 = appmod.dos_modal_text("T", ["hi"], 10)
    chk("narrow width clamps sanely (min inner width holds)",
        "press any key" in t3 or "hi" in t3)
    t4 = appmod.dos_modal_text("T", ["hi"], 60, footer="TAB scope · ENTER park")
    chk("custom footer renders", any("ENTER park" in r for r in t4.splitlines()))
    chk("custom footer replaces the default",
        not any("press any key" in r for r in t4.splitlines()))
    t5 = appmod.dos_modal_text("T", ["hi"], 60)
    chk("default footer unchanged",
        any("press any key" in r for r in t5.splitlines()))


def test_park_modal_text():
    import app as appmod
    t = appmod.park_modal_text("retry backoff on inject", "bff-worker", False,
                               ["widget shows parked count",
                                "statusbar click queue is O(n)"], 80)
    rows = t.splitlines()
    chk("park title row", any("PARK AN IDEA" in r for r in rows))
    chk("buffer is shown", any("retry backoff on inject" in r for r in rows))
    chk("cursor glyph follows the buffer",
        any("retry backoff on inject_" in r for r in rows))
    chk("scope row names the session", any("bff-worker" in r for r in rows))
    chk("session scope is the marked one",
        any("[·] bff-worker" in r for r in rows))
    chk("existing items listed",
        any("widget shows parked count" in r for r in rows))
    chk("footer teaches the keys",
        any("ENTER park" in r for r in rows) and any("ESC" in r for r in rows))

    d = appmod.park_modal_text("x", "bff-worker", True, [], 80)
    chk("dir scope marks DIR", any("[·] DIR" in r for r in d.splitlines()))
    chk("empty existing list renders nothing about parked",
        not any("already parked" in r for r in d.splitlines()))

    many = appmod.park_modal_text("x", "w", False,
                                  [f"item {i}" for i in range(9)], 80)
    mrows = many.splitlines()
    chk("existing list caps at 5", sum(1 for r in mrows if "item " in r) == 5)
    chk("overflow is counted", any("+4 more" in r for r in mrows))
    chk("count in the header line", any("(9)" in r for r in mrows))

    unreg = appmod.park_modal_text("x", "", True, [], 80)
    urows = unreg.splitlines()
    chk("no name renders a DIR-only scope row",
        any("[·] DIR" in r for r in urows))
    chk("no name says why there is no toggle",
        any("not registered" in r for r in urows))

    narrow = appmod.park_modal_text("y" * 200, "w", False, [], 40)
    chk("width clamped", all(len(r) <= 40 for r in narrow.splitlines()))

    empty = appmod.park_modal_text("", "w", False, [], 80)
    chk("empty buffer still shows a cursor",
        any(r.strip().startswith("_") for r in empty.splitlines()))


def test_statusbar_label():
    import statusbar
    chk("extreme circle is purple",
        statusbar.MODE_CIRCLE.get("extreme") == "\U0001f7e3")
    chk("extreme text on the badge",
        "RELAY:extreme" in statusbar.label("extreme"))


if __name__ == "__main__":
    test_prompt_line_empty()
    test_config_knobs()
    test_arming_plumbing()
    test_extreme_approves_like_insane()
    test_persist_maps_extreme_to_insane()
    test_extreme_fires()
    test_extreme_gates()
    test_extreme_audit_before_act()
    test_extreme_exhaustion()
    test_extreme_dry_run()
    test_extreme_arm_sid_binding()
    test_modal()
    test_tui_chrome()
    test_push_line()
    test_dos_modal_text()
    test_park_modal_text()
    test_statusbar_label()
    print("ALL PASSED" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
