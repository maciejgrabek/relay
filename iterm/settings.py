"""Pure settings model for the TUI config editor. No Textual, no iTerm2 - like
titles.py / statusbar.py. One ordered descriptor list drives both the rendered
overlay and the arrow-key editing, so they cannot drift."""
import dataclasses
import glob
import os

import chrome
import config as _config

SYSTEM_SOUNDS_DIR = "/System/Library/Sounds"

# (group, field, kind, spec). kind: sound | enum | toggle | number.
#   enum   -> spec is the tuple of valid values
#   number -> spec is (min, step)
#   toggle -> spec None
#   sound  -> spec None (options are dynamic, see sound_options)
SETTINGS = [
    ("SOUNDS", "sounds_enabled", "toggle", None),
    ("SOUNDS", "alert_sound", "sound", None),
    ("SOUNDS", "done_sound", "sound", None),
    ("SOUNDS", "danger_sound", "sound", None),
    ("SOUNDS", "message_sound", "sound", None),
    ("APPEARANCE", "theme", "enum", _config.THEME_NAMES),
    ("APPEARANCE", "mascot", "enum", _config.MASCOT_NAMES),
    ("APPEARANCE", "title_style", "enum", _config.TITLE_STYLES),
    ("APPEARANCE", "preview_panel", "toggle", None),
    # Not _LIVE: toggling this starts or stops a real second process, so it
    # takes effect on the next relay start rather than mid-run.
    ("APPEARANCE", "widget_enabled", "toggle", None),
    ("BEHAVIOR", "statusbar_enabled", "toggle", None),
    ("BEHAVIOR", "spawn_arm", "enum", _config.SPAWN_ARM_MODES),
    ("BEHAVIOR", "stale_minutes", "number", (1.0, 1.0)),
    ("BEHAVIOR", "notify_cooldown", "number", (0.0, 5.0)),
    ("BEHAVIOR", "danger_preset", "enum", _config.DANGER_PRESETS),
    ("BEHAVIOR", "respect_draft", "toggle", None),
    ("TIMERS", "timers_require_armed", "toggle", None),
    ("TIMERS", "timers_autostart", "toggle", None),
    ("TIMERS", "timers_reconfirm_days", "number", (0.0, 1.0)),
    # 0 = never release, and that is the default: relay has always kept the
    # Mac awake for as long as the panel is open.
    ("POWER", "power_release_after", "number", (0.0, 5.0)),
    ("POWER", "burn_window", "number", (0.0, 5.0)),
    # post_url is absent on purpose: the descriptor kinds are
    # sound | enum | toggle | number, there is no string kind, and adding one
    # means new editing, cursor and validation code in the overlay for a single
    # field. It stays config-file-only; `relay doctor` reports whether it is set.
    ("EVENTS", "events_file", "toggle", None),
    ("EVENTS", "events_post_body", "enum", _config.POST_BODIES),
    # min 1.0, like stale_minutes: 0 is a legal config value (never prune),
    # but it must not be three left-presses from the default in an overlay
    # that saves immediately.
    ("EVENTS", "events_retention_days", "number", (1.0, 1.0)),
    # The boot screen. `style` is an enum over boot.BOOT_STYLES rather than a
    # literal tuple, so registering a second style in boot.py makes it
    # selectable here with no edit to this file.
    ("BOOT", "boot_enabled", "toggle", None),
    ("BOOT", "boot_style", "enum", _config.BOOT_STYLES),
]

# _LIVE: applied to the running Watcher without a restart. _APP_LIVE: applied to
# the running TUI (display) instead - same "no restart tag" treatment, but the
# app, not the watcher, is where the change lands.
_LIVE = {"sounds_enabled", "alert_sound", "done_sound", "danger_sound",
         "message_sound"}
_APP_LIVE = {"preview_panel", "mascot", "power_release_after", "burn_window"}


def is_live(field: str) -> bool:
    """True when a change takes effect immediately (no restart) - whether its
    target is the watcher (_LIVE) or the app's display (_APP_LIVE)."""
    return field in _LIVE or field in _APP_LIVE


def is_app_live(field: str) -> bool:
    """True when the live target is the TUI itself (the app applies it), not the
    watcher - so app._settings_change routes it to the display, not setattr."""
    return field in _APP_LIVE


def _descriptor(field):
    for row in SETTINGS:
        if row[1] == field:
            return row
    return None


def sound_options(current: str) -> list:
    """System sounds + '' (silent) + the current value if it is a custom path.
    Never raises."""
    try:
        found = sorted(glob.glob(os.path.join(SYSTEM_SOUNDS_DIR, "*.aiff")))
    except Exception:
        found = []
    opts = [""] + found
    if current and current not in opts:
        opts.append(current)
    return opts


def _cycle(options, current, direction):
    if not options:
        return current
    try:
        i = options.index(current)
    except ValueError:
        return options[0] if direction >= 0 else options[-1]
    return options[(i + direction) % len(options)]


def change(cfg, field, direction):
    """A NEW frozen Config with `field` cycled/stepped by direction (+1 right,
    -1 left). Unknown field -> cfg unchanged. Pure."""
    d = _descriptor(field)
    if d is None:
        return cfg
    _, _, kind, spec = d
    cur = getattr(cfg, field)
    if kind == "toggle":
        new = not cur
    elif kind == "enum":
        new = _cycle(list(spec), cur, direction)
    elif kind == "sound":
        new = _cycle(sound_options(cur), cur, direction)
    elif kind == "number":
        lo, step = spec
        new = max(lo, cur + direction * step)
    else:
        return cfg
    return dataclasses.replace(cfg, **{field: new})


def _display(kind, value):
    if kind == "toggle":
        return "on" if value else "off"
    if kind == "sound":
        return "(silent)" if not value else os.path.basename(value)
    if kind == "number":
        return f"{value:g}"
    return str(value)


def render(working, running, cursor, width):
    """The settings overlay text (plain, CRT style). Marks the cursor row and
    tags a changed restart-field with 'restart to apply'. Pure."""
    lines = []
    group = None
    for i, (g, f, kind, spec) in enumerate(SETTINGS):
        if g != group:
            # A settings group is a SECTION, not a group of rows that bind to
            # each other, so it gets a titled rule rather than a rail - same
            # edge grammar one weight down (chrome.py).
            group = g
            lines.append("")
            lines.append(chrome.rule(str(g).lower(), "", max(20, width - 2)))
        mark = ">" if i == cursor else " "
        val = _display(kind, getattr(working, f))
        if kind == "sound" and not working.sounds_enabled:
            # The pick is kept, it just cannot be heard - say so, or the four
            # sound names read as a promise the muted panel does not keep.
            val += "  (muted)"
        tag = ""
        if not is_live(f) and getattr(working, f) != getattr(running, f):
            tag = "   restart to apply"
        label = f.replace("_", " ")
        # 21 = the longest label ("timers reconfirm days"). It was 18, which
        # the three TIMERS rows already overflowed - their values sat one or
        # more columns out of line with every other row, and 'timers require
        # armed off' ran straight into its value with no gap at all.
        lines.append(f" {mark} {label:<21} {val}{tag}")
    lines.append("")
    lines.append("  up/down move   left/right change   p play sound   , close")
    return "\n".join(lines)
