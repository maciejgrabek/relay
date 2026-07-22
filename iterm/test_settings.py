"""Tests for the pure settings model (config editor). No Textual/iTerm2.

Run: python3 iterm/test_settings.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import config  # noqa: E402
import settings  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    c = config.Config()

    ok &= check("is_live only for sounds",
                settings.is_live("alert_sound")
                and not settings.is_live("theme"))

    # enum cycles and wraps
    t = settings.change(c, "theme", +1).theme
    ok &= check("enum cycles to next", t == config.THEME_NAMES[1])
    ok &= check("enum wraps on left from first",
                settings.change(c, "theme", -1).theme == config.THEME_NAMES[-1])

    # toggle flips
    ok &= check("toggle flips",
                settings.change(c, "statusbar_enabled", +1).statusbar_enabled
                is (not c.statusbar_enabled))

    # number steps and respects min
    ok &= check("number steps up",
                settings.change(c, "notify_cooldown", +1).notify_cooldown
                == c.notify_cooldown + 5.0)
    lowered = config.Config()
    import dataclasses
    lowered = dataclasses.replace(lowered, stale_minutes=1.0)
    ok &= check("number clamps at min",
                settings.change(lowered, "stale_minutes", -1).stale_minutes
                == 1.0)

    # sound options include silent + a custom current
    opts = settings.sound_options("/my/custom.aiff")
    ok &= check("sound options include silent + custom",
                "" in opts and "/my/custom.aiff" in opts)

    # unknown field is a no-op
    ok &= check("unknown field no-op", settings.change(c, "nope", +1) == c)

    # render shows cursor + a restart tag only on a changed restart field
    changed = settings.change(c, "theme", +1)
    txt = settings.render(changed, c, 0, 60)
    ok &= check("render marks the cursor row", ">" in txt)
    ok &= check("render shows restart tag on changed restart field",
                "restart" in txt)
    live_changed = settings.change(c, "alert_sound", +1)
    txt2 = settings.render(live_changed, c, 0, 60)
    ok &= check("no restart tag for a live (sound) change",
                "restart" not in txt2)

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
