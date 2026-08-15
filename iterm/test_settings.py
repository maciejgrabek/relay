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

    ok &= check("is_live for sounds + the app-live preview toggle",
                settings.is_live("alert_sound")
                and settings.is_live("preview_panel")
                and not settings.is_live("theme"))
    ok &= check("is_app_live only for preview_panel",
                settings.is_app_live("preview_panel")
                and not settings.is_app_live("alert_sound")
                and not settings.is_app_live("statusbar_enabled"))

    # preview toggle flips like any other toggle.
    ok &= check("preview toggle flips",
                settings.change(c, "preview_panel", +1).preview_panel
                is (not c.preview_panel))
    # ...and being live, it never shows a 'restart to apply' tag.
    prev_changed = settings.change(c, "preview_panel", +1)
    ok &= check("no restart tag for the live preview change",
                "restart" not in settings.render(prev_changed, c, 0, 60))

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

    # master mute: flips, is live (no restart tag), and marks the sound rows.
    muted = settings.change(c, "sounds_enabled", +1)
    ok &= check("sounds_enabled flips and is live",
                muted.sounds_enabled is False
                and settings.is_live("sounds_enabled")
                and not settings.is_app_live("sounds_enabled"))
    mtxt = settings.render(muted, c, 0, 60)
    ok &= check("muted render tags the sound rows, not the toggle",
                mtxt.count("(muted)") == 4 and "restart" not in mtxt)
    ok &= check("un-muted render has no muted tags",
                "(muted)" not in settings.render(c, c, 0, 60))

    ok &= check("timers settings flip/step",
                settings.change(c, "timers_require_armed", +1).timers_require_armed
                is (not c.timers_require_armed)
                and settings.change(c, "timers_autostart", +1).timers_autostart
                is (not c.timers_autostart)
                and settings.change(c, "timers_reconfirm_days", +1).timers_reconfirm_days
                == c.timers_reconfirm_days + 1.0)

    # respect_draft is editable in the overlay like the other [swarm] keys the
    # editor manages, defaults on, and is NOT live (the watcher reads it off
    # cfg at delivery time, so the running process keeps its startup value).
    ok &= check("respect_draft toggles and is not live",
                c.respect_draft is True
                and settings.change(c, "respect_draft", +1).respect_draft
                is False
                and not settings.is_live("respect_draft"))

    # --- power release_after is editable in the panel and applies live -------
    ok &= check("release_after has a settings row",
                any(r[1] == "power_release_after" for r in settings.SETTINGS))
    ok &= check("release_after steps by 5 minutes",
                settings.change(c, "power_release_after", +1)
                .power_release_after == 5.0)
    ok &= check("release_after cannot go below 0",
                settings.change(c, "power_release_after", -1)
                .power_release_after == 0.0)
    ok &= check("release_after is app-live (no restart tag)",
                settings.is_app_live("power_release_after")
                and "restart" not in settings.render(
                    settings.change(c, "power_release_after", +1), c, 0, 60))
    # The overlay pads every label to a fixed column; a longer one pushes its
    # own value out of line with the rest. This caught a pre-existing break:
    # the three TIMERS labels overflowed the old 18-wide column.
    ok &= check("every settings label fits the value column",
                all(len(r[1].replace("_", " ")) <= 21
                    for r in settings.SETTINGS))
    # ...and prove it on the rendered text: every value must begin in the same
    # column, whatever the length of the label to its left.
    rendered = settings.render(c, c, 0, 70).splitlines()
    starts = set()
    for _g, field, _kind, _spec in settings.SETTINGS:
        label = field.replace("_", " ")
        for ln in rendered:
            if ln[3:].startswith(label):          # 3 = " " + cursor mark + " "
                rest = ln[3 + len(label):]
                starts.add(3 + len(label) + len(rest) - len(rest.lstrip()))
                break
    ok &= check("every value starts in the same column",
                len(starts) == 1 and starts == {25})

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
