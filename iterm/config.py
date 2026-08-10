"""Relay config - ~/.relay/config (INI), the durable home for preferences.

    [titles]
    style = off            ; off | glyphs | words | hybrid

    [sounds]
    enabled = true                                 ; master mute for all four
    alert   = /System/Library/Sounds/Sosumi.aiff   ; needs-a-look (stale, error)
    done    = /System/Library/Sounds/Glass.aiff     ; a task/epic completed
    danger  = /System/Library/Sounds/Basso.aiff     ; about to run something bad
    message = /System/Library/Sounds/Tink.aiff      ; a worker messaged you

    [swarm]
    stale_minutes   = 10   ; mirrors RELAY_STALE_MINUTES
    notify_cooldown = 30   ; mirrors RELAY_NOTIFY_COOLDOWN
    respect_draft   = true ; never type over a half-written operator message

    [mascot]
    name = crt             ; crt | invader | owl | cat | core | ... (see MASCOT_NAMES)

    [widget]
    enabled = false        ; the floating desktop mascot (a second process)

Precedence: defaults < config file < environment variable. Env always wins,
so existing setups keep working. A missing file, section, or key silently
yields defaults; a malformed file or value yields defaults plus a warning
string (returned, not printed - the caller decides where warnings go).
Session-scoped things (dry-run, RELAY_NO_CAFFEINATE, RELAY_DB) deliberately
stay out of this file.

Pure stdlib, no iterm2/sqlite imports (test_config.py runs it standalone).
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple

TITLE_STYLES = ("off", "glyphs", "words", "hybrid")
SPAWN_ARM_MODES = ("off", "safe", "wild", "insane")
DANGER_PRESETS = ("default", "paranoid")
THEME_NAMES = ("phosphor", "amber", "ice")
# The creature that watches the fleet. Every skin is the SAME state machine
# (moods, tick, colors) - only the body drawn around the eyes changes. `crt`
# is the default and stays relay's brand mark.
MASCOT_NAMES = ("crt", "invader", "owl", "cat", "core", "beacon", "ghost",
                "crab", "droid", "bug", "skull", "toaster", "atom", "moth",
                "tank")


@dataclass(frozen=True)
class Config:
    title_style: str = "off"
    # Master mute. False silences every notification sound without touching the
    # four choices below, so flipping it back on restores them intact.
    sounds_enabled: bool = True
    alert_sound: str = "/System/Library/Sounds/Sosumi.aiff"
    done_sound: str = "/System/Library/Sounds/Glass.aiff"
    danger_sound: str = "/System/Library/Sounds/Basso.aiff"
    message_sound: str = "/System/Library/Sounds/Tink.aiff"
    stale_minutes: float = 10.0
    notify_cooldown: float = 30.0
    spawn_arm: str = "off"
    extreme_fires: int = 5       # pushes per E E arming (TUI extreme mode)
    extreme_dwell: float = 45.0  # seconds idle before an extreme push
    # ON by default, because it is the protective answer: with it on, a queued
    # swarm message and a due timer both refuse to type into a session whose
    # input box already holds an operator draft (the extreme push has always
    # refused). Off restores the old behaviour - relay appends to the draft and
    # presses Enter, submitting whatever the operator had half-written.
    respect_draft: bool = True
    statusbar_enabled: bool = False
    danger_preset: str = "default"
    theme: str = "phosphor"
    mascot: str = "crt"
    preview_panel: bool = True
    timers_require_armed: bool = False
    timers_autostart: bool = False
    timers_reconfirm_days: float = 7.0
    # OFF by default. The floating mascot is genuinely useful while you are in
    # another app, but it is a second process that appears on screen every
    # single launch, and a control panel should not put a creature on your
    # desktop until you ask for one. Turn it on with 'm' in the panel, or
    # [widget] enabled = true here to have it start with relay.
    widget_enabled: bool = False


def default_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_CONFIG", "~/.relay/config"))


def _get_float(cp, section, key, fallback, warns) -> float:
    raw = cp.get(section, key, fallback=None)
    if raw is None:
        return fallback
    try:
        return float(raw)
    except ValueError:
        warns.append(f"config: [{section}] {key} = {raw!r} is not a number - "
                     f"using {fallback}")
        return fallback


def load(path: Optional[str] = None) -> Tuple[Config, List[str]]:
    """Read the config file and apply env overrides. Never raises."""
    p = path or default_path()
    warns: List[str] = []
    # inline_comment_prefixes lets a value line carry a trailing `; ...` or
    # `# ...` comment (as the README's sample config shows); without it the
    # whole rest of the line is read as part of the value and silently invalid.
    cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    try:
        cp.read(p)
    except (configparser.Error, UnicodeDecodeError, OSError) as e:
        # Malformed INI, non-UTF-8 bytes, or an unreadable file must never
        # propagate - that would kill the TUI at startup. Degrade to defaults.
        warns.append(f"config: {p} is malformed ({e.__class__.__name__}) - "
                     f"using defaults")
        cp = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))

    d = Config()  # defaults
    style = cp.get("titles", "style", fallback=d.title_style).strip().lower()
    if style not in TITLE_STYLES:
        warns.append(f"config: [titles] style = {style!r} is not one of "
                     f"{'/'.join(TITLE_STYLES)} - using 'off'")
        style = "off"

    arm = cp.get("swarm", "spawn_arm", fallback=d.spawn_arm).strip().lower()
    if arm not in SPAWN_ARM_MODES:
        warns.append(f"config: [swarm] spawn_arm = {arm!r} is not one of "
                     f"{'/'.join(SPAWN_ARM_MODES)} - using 'off'")
        arm = "off"

    stale = _get_float(cp, "swarm", "stale_minutes", d.stale_minutes, warns)
    cooldown = _get_float(cp, "swarm", "notify_cooldown", d.notify_cooldown,
                          warns)

    e_fires = max(1, int(_get_float(cp, "swarm", "extreme_fires",
                                    float(d.extreme_fires), warns)))
    e_dwell = max(0.0, _get_float(cp, "swarm", "extreme_dwell",
                                  d.extreme_dwell, warns))

    try:
        respect_draft = cp.getboolean("swarm", "respect_draft",
                                      fallback=d.respect_draft)
    except ValueError:
        warns.append("config: [swarm] respect_draft must be true/false - "
                     "using true")
        respect_draft = True

    try:
        sounds_on = cp.getboolean("sounds", "enabled",
                                  fallback=d.sounds_enabled)
    except ValueError:
        warns.append("config: [sounds] enabled must be true/false - "
                     "using true")
        sounds_on = True

    try:
        statusbar = cp.getboolean("statusbar", "enabled",
                                  fallback=d.statusbar_enabled)
    except ValueError:
        warns.append("config: [statusbar] enabled must be true/false - "
                     "using false")
        statusbar = False

    try:
        widget_on = cp.getboolean("widget", "enabled",
                                  fallback=d.widget_enabled)
    except ValueError:
        warns.append("config: [widget] enabled must be true/false - "
                     "using false")
        widget_on = False

    preset = cp.get("danger", "preset",
                    fallback=d.danger_preset).strip().lower()
    if preset not in DANGER_PRESETS:
        warns.append(f"config: [danger] preset = {preset!r} is not one of "
                     f"{'/'.join(DANGER_PRESETS)} - using 'default'")
        preset = "default"

    theme = cp.get("theme", "name", fallback=d.theme).strip().lower()
    if theme not in THEME_NAMES:
        warns.append(f"config: [theme] name = {theme!r} is not one of "
                     f"{'/'.join(THEME_NAMES)} - using 'phosphor'")
        theme = "phosphor"

    mascot = cp.get("mascot", "name", fallback=d.mascot).strip().lower()
    if mascot not in MASCOT_NAMES:
        warns.append(f"config: [mascot] name = {mascot!r} is not one of "
                     f"{'/'.join(MASCOT_NAMES)} - using 'crt'")
        mascot = "crt"

    try:
        preview = cp.getboolean("layout", "preview",
                                fallback=d.preview_panel)
    except ValueError:
        warns.append("config: [layout] preview must be true/false - "
                     "using true")
        preview = True

    try:
        t_armed = cp.getboolean("timers", "require_armed",
                                fallback=d.timers_require_armed)
    except ValueError:
        warns.append("config: [timers] require_armed must be true/false - "
                     "using false")
        t_armed = False
    try:
        t_auto = cp.getboolean("timers", "autostart",
                               fallback=d.timers_autostart)
    except ValueError:
        warns.append("config: [timers] autostart must be true/false - "
                     "using false")
        t_auto = False
    t_recon = _get_float(cp, "timers", "reconfirm_days",
                         d.timers_reconfirm_days, warns)

    # Env wins over the file for the two mirrored keys.
    env_stale = os.environ.get("RELAY_STALE_MINUTES")
    if env_stale is not None:
        try:
            stale = float(env_stale)
        except ValueError:
            warns.append(f"config: RELAY_STALE_MINUTES = {env_stale!r} is "
                         f"not a number - ignored")
    env_cool = os.environ.get("RELAY_NOTIFY_COOLDOWN")
    if env_cool is not None:
        try:
            cooldown = float(env_cool)
        except ValueError:
            warns.append(f"config: RELAY_NOTIFY_COOLDOWN = {env_cool!r} is "
                         f"not a number - ignored")

    return Config(
        title_style=style,
        sounds_enabled=sounds_on,
        alert_sound=cp.get("sounds", "alert", fallback=d.alert_sound).strip(),
        done_sound=cp.get("sounds", "done", fallback=d.done_sound).strip(),
        danger_sound=cp.get("sounds", "danger", fallback=d.danger_sound).strip(),
        message_sound=cp.get("sounds", "message",
                             fallback=d.message_sound).strip(),
        stale_minutes=stale,
        notify_cooldown=cooldown,
        spawn_arm=arm,
        extreme_fires=e_fires,
        extreme_dwell=e_dwell,
        respect_draft=respect_draft,
        statusbar_enabled=statusbar,
        danger_preset=preset,
        theme=theme,
        mascot=mascot,
        preview_panel=preview,
        widget_enabled=widget_on,
        timers_require_armed=t_armed,
        timers_autostart=t_auto,
        timers_reconfirm_days=t_recon,
    ), warns


def dump(cfg: Config) -> str:
    """Render a complete ~/.relay/config from a Config. Round-trips: load() of
    this text yields an equal Config for every field the editor manages."""
    return (
        "; Written by relay's config editor. Edit here or in the panel (,).\n\n"
        "[titles]\n"
        f"style = {cfg.title_style}\n\n"
        "[sounds]\n"
        f"enabled = {'true' if cfg.sounds_enabled else 'false'}\n"
        f"alert   = {cfg.alert_sound}\n"
        f"done    = {cfg.done_sound}\n"
        f"danger  = {cfg.danger_sound}\n"
        f"message = {cfg.message_sound}\n\n"
        "[swarm]\n"
        f"stale_minutes   = {cfg.stale_minutes:g}\n"
        f"notify_cooldown = {cfg.notify_cooldown:g}\n"
        f"spawn_arm       = {cfg.spawn_arm}\n"
        f"extreme_fires   = {cfg.extreme_fires}\n"
        f"extreme_dwell   = {cfg.extreme_dwell:g}\n"
        f"respect_draft   = {'true' if cfg.respect_draft else 'false'}\n\n"
        "[statusbar]\n"
        f"enabled = {'true' if cfg.statusbar_enabled else 'false'}\n\n"
        "[danger]\n"
        f"preset = {cfg.danger_preset}\n\n"
        "[theme]\n"
        f"name = {cfg.theme}\n\n"
        "[mascot]\n"
        f"name = {cfg.mascot}\n\n"
        "[widget]\n"
        f"enabled = {'true' if cfg.widget_enabled else 'false'}\n\n"
        "[layout]\n"
        f"preview = {'true' if cfg.preview_panel else 'false'}\n"
        "\n[timers]\n"
        f"require_armed  = {'true' if cfg.timers_require_armed else 'false'}\n"
        f"autostart      = {'true' if cfg.timers_autostart else 'false'}\n"
        f"reconfirm_days = {cfg.timers_reconfirm_days:g}\n"
    )


def save(cfg: Config, path: Optional[str] = None) -> None:
    """Atomically write dump(cfg) to path (default default_path())."""
    p = path or default_path()
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(dump(cfg))
    os.replace(tmp, p)
