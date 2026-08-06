"""Extreme insane mode suite: the draft-guard helper, config knobs, watcher
arming/firing/exhaustion, TUI + statusbar chrome.

Run: python3 iterm/test_extreme.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
# Hermetic: never read the developer's real ~/.relay/config in tests.
os.environ["RELAY_CONFIG"] = "/nonexistent/relay-test-config"

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


def test_prompt_line_empty():
    import swarm
    chk("empty input box -> True", swarm.prompt_line_empty(READY))
    chk("typed draft -> False", not swarm.prompt_line_empty(DRAFT))
    chk("shell prompt (no box) -> False", not swarm.prompt_line_empty(SHELL))
    chk("empty screen -> False", not swarm.prompt_line_empty([]))
    chk("READY still passes claude_prompt_ready",
        swarm.claude_prompt_ready(READY))


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
    W.swarmdb.undelivered = lambda conn, name: [{"id": 1}]
    w._swarm_conn = lambda: None
    asyncio.run(w._fire_extreme(i8))
    chk("queued inbox mail blocks the push", fs8.sent == [])
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
    test_tui_chrome()
    test_statusbar_label()
    print("ALL PASSED" if ok else "FAILURES")
    sys.exit(0 if ok else 1)
