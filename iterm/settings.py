"""Pure settings model for the TUI config editor. No Textual, no iTerm2 - like
titles.py / statusbar.py. One ordered descriptor list drives both the rendered
overlay and the arrow-key editing, so they cannot drift."""
import dataclasses
import glob
import os

import config as _config

SYSTEM_SOUNDS_DIR = "/System/Library/Sounds"

# (group, field, kind, spec). kind: sound | enum | toggle | number.
#   enum   -> spec is the tuple of valid values
#   number -> spec is (min, step)
#   toggle -> spec None
#   sound  -> spec None (options are dynamic, see sound_options)
SETTINGS = [
    ("SOUNDS", "alert_sound", "sound", None),
    ("SOUNDS", "done_sound", "sound", None),
    ("SOUNDS", "danger_sound", "sound", None),
    ("SOUNDS", "message_sound", "sound", None),
    ("APPEARANCE", "theme", "enum", _config.THEME_NAMES),
    ("APPEARANCE", "title_style", "enum", _config.TITLE_STYLES),
    ("APPEARANCE", "preview_panel", "toggle", None),
    ("BEHAVIOR", "statusbar_enabled", "toggle", None),
    ("BEHAVIOR", "spawn_arm", "enum", _config.SPAWN_ARM_MODES),
    ("BEHAVIOR", "stale_minutes", "number", (1.0, 1.0)),
    ("BEHAVIOR", "notify_cooldown", "number", (0.0, 5.0)),
    ("BEHAVIOR", "danger_preset", "enum", _config.DANGER_PRESETS),
    ("TIMERS", "timers_require_armed", "toggle", None),
    ("TIMERS", "timers_autostart", "toggle", None),
    ("TIMERS", "timers_reconfirm_days", "number", (0.0, 1.0)),
]

# _LIVE: applied to the running Watcher without a restart. _APP_LIVE: applied to
# the running TUI (display) instead - same "no restart tag" treatment, but the
# app, not the watcher, is where the change lands.
_LIVE = {"alert_sound", "done_sound", "danger_sound", "message_sound"}
_APP_LIVE = {"preview_panel"}


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
            group = g
            lines.append("")
            lines.append(f"  {g}")
        mark = ">" if i == cursor else " "
        val = _display(kind, getattr(working, f))
        tag = ""
        if not is_live(f) and getattr(working, f) != getattr(running, f):
            tag = "   restart to apply"
        label = f.replace("_", " ")
        lines.append(f" {mark} {label:<18} {val}{tag}")
    lines.append("")
    lines.append("  up/down move   left/right change   p play sound   , close")
    return "\n".join(lines)
