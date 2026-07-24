"""Tests for the pure status-bar logic. No iTerm2 (the live halves are the
watcher and the AutoLaunch provider; both lean on these helpers).

Run: python3 iterm/test_statusbar.py    or    ./test/run.sh
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
import statusbar  # noqa: E402
from statusbar import label, MODE_CIRCLE  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True

    # per-mode: circle + RELAY:<mode>
    ok &= check("off", label("off") == f"{MODE_CIRCLE['off']} RELAY:off")
    ok &= check("safe", label("safe") == f"{MODE_CIRCLE['safe']} RELAY:safe")
    ok &= check("wild", label("wild") == f"{MODE_CIRCLE['wild']} RELAY:wild")
    ok &= check("insane",
                label("insane") == f"{MODE_CIRCLE['insane']} RELAY:insane")
    ok &= check("unknown mode falls back to off circle",
                label("bogus").startswith(MODE_CIRCLE["off"]))

    # the five circles are distinct (so color-by-mode is real)
    ok &= check("five distinct circles", len(set(MODE_CIRCLE.values())) == 5)

    ok &= check("shadow badge uses its own circle",
                statusbar.MODE_CIRCLE.get("shadow") == "\U0001f535"
                and statusbar.label("shadow")
                == f"{statusbar.MODE_CIRCLE['shadow']} RELAY:shadow")

    # own panel tab: neutral, non-mode badge
    ok &= check("own panel badge", label("safe", own_panel=True) == "⬛ RELAY: panel")
    ok &= check("own panel ignores swarm name",
                label("insane", own_panel=True, name="coord",
                      role="coordinator") == "⬛ RELAY: panel")

    # swarm session: name + short role appended
    ok &= check("swarm worker appends name + short role",
                label("safe", name="bff-worker", role="worker")
                == f"{MODE_CIRCLE['safe']} RELAY:safe · bff-worker (work)")
    ok &= check("swarm coordinator short role",
                label("wild", name="coord", role="coordinator")
                == f"{MODE_CIRCLE['wild']} RELAY:wild · coord (coord)")
    ok &= check("name without role -> just name",
                label("safe", name="w1") == f"{MODE_CIRCLE['safe']} RELAY:safe · w1")
    ok &= check("no name -> bare mode",
                label("off") == f"{MODE_CIRCLE['off']} RELAY:off")

    # --- published state (the AutoLaunch provider's view) --------------------
    tmp = tempfile.mkdtemp()
    state = os.path.join(tmp, "statusbar.json")
    clicks = os.path.join(tmp, "clicks.jsonl")

    ok &= check("missing state -> offline badge",
                statusbar.read_state_label("S1", path=state)
                == statusbar.OFFLINE_LABEL)
    ok &= check("missing state -> not fresh",
                not statusbar.state_fresh(path=state))

    statusbar.write_state({"S1": label("wild")}, now=1000.0, path=state)
    ok &= check("fresh state -> published label",
                statusbar.read_state_label("S1", now=1001.0, path=state)
                == label("wild"))
    ok &= check("fresh state, unknown tab -> off label",
                statusbar.read_state_label("S9", now=1001.0, path=state)
                == label("off"))
    ok &= check("fresh state -> state_fresh true",
                statusbar.state_fresh(now=1001.0, path=state))
    ok &= check("stale state -> offline badge",
                statusbar.read_state_label(
                    "S1", now=1000.0 + statusbar.STATE_STALE_S + 1,
                    path=state) == statusbar.OFFLINE_LABEL)

    with open(state, "w") as f:
        f.write("{not json")
    ok &= check("garbled state -> offline badge, no raise",
                statusbar.read_state_label("S1", path=state)
                == statusbar.OFFLINE_LABEL)

    statusbar.write_state({"S1": label("safe")}, now=1000.0, path=state)
    statusbar.clear_state(path=state)
    ok &= check("clear_state removes the file", not os.path.exists(state))
    statusbar.clear_state(path=state)   # second call must not raise
    ok &= check("clear_state idempotent", True)

    # --- provider heartbeat (symlink existing != provider running) -----------
    alive = os.path.join(tmp, "provider.alive")
    ok &= check("no heartbeat file -> provider not alive",
                not statusbar.provider_alive(path=alive))
    statusbar.touch_provider_alive(path=alive)
    ok &= check("touched -> alive", statusbar.provider_alive(path=alive))
    os.utime(alive, (1000.0, 1000.0))   # backdate far into the past
    ok &= check("stale heartbeat -> not alive",
                not statusbar.provider_alive(path=alive))

    # --- provider installed: the stable ownership signal ---------------------
    link = os.path.join(tmp, "relay_statusbar.py")
    ok &= check("no symlink -> provider not installed",
                not statusbar.provider_installed(path=link))
    open(link, "w").close()
    ok &= check("symlink present -> provider installed",
                statusbar.provider_installed(path=link))
    # A dangling symlink (target gone) still counts as installed (lexists).
    dangling = os.path.join(tmp, "dangling.py")
    os.symlink(os.path.join(tmp, "does-not-exist.py"), dangling)
    ok &= check("dangling symlink -> still installed",
                statusbar.provider_installed(path=dangling))

    # --- provider_script_path: resolve the symlink target, else repo file ----
    target = os.path.join(tmp, "the_provider.py")
    open(target, "w").close()
    sl = os.path.join(tmp, "linked_provider.py")
    os.symlink(target, sl)
    ok &= check("script path resolves an installed symlink to its target",
                statusbar.provider_script_path(path=sl)
                == os.path.realpath(target))
    missing = os.path.join(tmp, "nope.py")
    ok &= check("script path falls back to the repo autolaunch file",
                statusbar.provider_script_path(path=missing)
                == os.path.join(os.path.dirname(
                    os.path.abspath(statusbar.__file__)),
                    "statusbar_autolaunch.py"))

    # --- plan_provider_start: the pure auto-start verdict --------------------
    ok &= check("not installed -> absent",
                statusbar.plan_provider_start(False, False, True) == "absent")
    ok &= check("installed + alive -> alive (never double-register)",
                statusbar.plan_provider_start(True, True, True) == "alive")
    ok &= check("installed + dead + no cookie -> no-cookie",
                statusbar.plan_provider_start(True, False, False)
                == "no-cookie")
    ok &= check("installed + dead + cookie -> start",
                statusbar.plan_provider_start(True, False, True) == "start")

    # --- statusbar_ensure.ensure: the live orchestration (fakes injected) ----
    import statusbar_ensure
    elink = os.path.join(tmp, "ensure_link.py")
    ealive = os.path.join(tmp, "ensure.alive")
    os.environ["RELAY_STATUSBAR_AUTOLAUNCH"] = elink
    os.environ["RELAY_STATUSBAR_ALIVE"] = ealive

    calls = []
    fake_spawn = lambda script, cookie: calls.append((script, cookie))
    ready = lambda: True          # interpreter has iterm2
    came_alive = lambda: True     # provider confirmed up after launch

    # installed but dead + a cookie + confirmed alive -> spawns once -> start.
    open(elink, "w").close()
    got = statusbar_ensure.ensure(cookie_getter=lambda: "COOKIE123",
                                  spawn=fake_spawn, interpreter_ready=ready,
                                  confirm=came_alive)
    ok &= check("ensure starts the provider when installed+dead+cookie",
                got == "start" and len(calls) == 1
                and calls[0][1] == "COOKIE123")

    # launched but the provider never heartbeats -> start-unconfirmed (spawned).
    calls.clear()
    got = statusbar_ensure.ensure(cookie_getter=lambda: "COOKIE123",
                                  spawn=fake_spawn, interpreter_ready=ready,
                                  confirm=lambda: False)
    ok &= check("ensure reports start-unconfirmed when it never comes alive",
                got == "start-unconfirmed" and len(calls) == 1)

    # interpreter can't import iterm2 -> no-iterm2, and we DON'T spawn a doomed
    # process.
    calls.clear()
    got = statusbar_ensure.ensure(cookie_getter=lambda: "COOKIE123",
                                  spawn=fake_spawn, interpreter_ready=lambda: False,
                                  confirm=came_alive)
    ok &= check("ensure refuses to spawn when the interpreter lacks iterm2",
                got == "no-iterm2" and calls == [])

    # no cookie (iTerm2 not running) -> no-cookie, no spawn.
    calls.clear()
    got = statusbar_ensure.ensure(cookie_getter=lambda: "", spawn=fake_spawn,
                                  interpreter_ready=ready, confirm=came_alive)
    ok &= check("ensure returns no-cookie and does not spawn without a cookie",
                got == "no-cookie" and calls == [])

    # alive -> never spawns (would DUPLICATE-register and freeze the badge).
    statusbar.touch_provider_alive(path=ealive)
    calls.clear()
    got = statusbar_ensure.ensure(cookie_getter=lambda: "COOKIE123",
                                  spawn=fake_spawn, interpreter_ready=ready,
                                  confirm=came_alive)
    ok &= check("ensure leaves a live provider alone (no spawn)",
                got == "alive" and calls == [])

    # not installed -> absent, no cookie fetched, no spawn.
    os.remove(elink)
    os.remove(ealive)
    calls.clear()
    def _boom():
        raise AssertionError("must not request a cookie when not installed")
    got = statusbar_ensure.ensure(cookie_getter=_boom, spawn=fake_spawn,
                                  interpreter_ready=ready, confirm=came_alive)
    ok &= check("ensure is absent (skips cookie + spawn) when not installed",
                got == "absent" and calls == [])

    # _confirm_alive: returns fast when already alive; times out via injected
    # sleep without real waiting when it never appears.
    statusbar.touch_provider_alive(path=ealive)
    os.environ["RELAY_STATUSBAR_ALIVE"] = ealive
    ok &= check("_confirm_alive true when heartbeat already fresh",
                statusbar_ensure._confirm_alive(sleep=lambda s: None) is True)
    os.remove(ealive)
    slept = []
    ok &= check("_confirm_alive false (and polled) when never alive",
                statusbar_ensure._confirm_alive(
                    timeout=0.5, interval=0.25,
                    sleep=lambda s: slept.append(s)) is False
                and len(slept) == 2)

    os.environ.pop("RELAY_STATUSBAR_AUTOLAUNCH", None)
    os.environ.pop("RELAY_STATUSBAR_ALIVE", None)

    # --- click queue ---------------------------------------------------------
    ok &= check("no click file -> no clicks",
                statusbar.consume_clicks(path=clicks) == [])
    statusbar.append_click("S1", now=1000.0, path=clicks)
    statusbar.append_click("S2", now=1000.0, path=clicks)
    statusbar.append_click("S3", now=10.0, path=clicks)   # ancient -> dropped
    with open(clicks, "a") as f:
        f.write("garbage line\n")
    got = statusbar.consume_clicks(now=1001.0, path=clicks)
    ok &= check("consume returns fresh clicks in order", got == ["S1", "S2"])
    ok &= check("consume drains the queue (at-most-once)",
                statusbar.consume_clicks(now=1001.0, path=clicks) == [])

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
