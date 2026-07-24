"""Relay config - ~/.relay/config (INI), the durable home for preferences.

    [titles]
    style = off            ; off | glyphs | words | hybrid

    [sounds]
    alert   = /System/Library/Sounds/Sosumi.aiff   ; needs-a-look (stale, error)
    done    = /System/Library/Sounds/Glass.aiff     ; a task/epic completed
    danger  = /System/Library/Sounds/Basso.aiff     ; about to run something bad
    message = /System/Library/Sounds/Tink.aiff      ; a worker messaged you

    [swarm]
    stale_minutes   = 10   ; mirrors RELAY_STALE_MINUTES
    notify_cooldown = 30   ; mirrors RELAY_NOTIFY_COOLDOWN

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


@dataclass(frozen=True)
class Config:
    title_style: str = "off"
    alert_sound: str = "/System/Library/Sounds/Sosumi.aiff"
    done_sound: str = "/System/Library/Sounds/Glass.aiff"
    danger_sound: str = "/System/Library/Sounds/Basso.aiff"
    message_sound: str = "/System/Library/Sounds/Tink.aiff"
    stale_minutes: float = 10.0
    notify_cooldown: float = 30.0
    spawn_arm: str = "off"
    statusbar_enabled: bool = False
    danger_preset: str = "default"
    theme: str = "phosphor"
    preview_panel: bool = True


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

    try:
        statusbar = cp.getboolean("statusbar", "enabled",
                                  fallback=d.statusbar_enabled)
    except ValueError:
        warns.append("config: [statusbar] enabled must be true/false - "
                     "using false")
        statusbar = False

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

    try:
        preview = cp.getboolean("layout", "preview",
                                fallback=d.preview_panel)
    except ValueError:
        warns.append("config: [layout] preview must be true/false - "
                     "using true")
        preview = True

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
        alert_sound=cp.get("sounds", "alert", fallback=d.alert_sound).strip(),
        done_sound=cp.get("sounds", "done", fallback=d.done_sound).strip(),
        danger_sound=cp.get("sounds", "danger", fallback=d.danger_sound).strip(),
        message_sound=cp.get("sounds", "message",
                             fallback=d.message_sound).strip(),
        stale_minutes=stale,
        notify_cooldown=cooldown,
        spawn_arm=arm,
        statusbar_enabled=statusbar,
        danger_preset=preset,
        theme=theme,
        preview_panel=preview,
    ), warns


def dump(cfg: Config) -> str:
    """Render a complete ~/.relay/config from a Config. Round-trips: load() of
    this text yields an equal Config for every field the editor manages."""
    return (
        "; Written by relay's config editor. Edit here or in the panel (,).\n\n"
        "[titles]\n"
        f"style = {cfg.title_style}\n\n"
        "[sounds]\n"
        f"alert   = {cfg.alert_sound}\n"
        f"done    = {cfg.done_sound}\n"
        f"danger  = {cfg.danger_sound}\n"
        f"message = {cfg.message_sound}\n\n"
        "[swarm]\n"
        f"stale_minutes   = {cfg.stale_minutes:g}\n"
        f"notify_cooldown = {cfg.notify_cooldown:g}\n"
        f"spawn_arm       = {cfg.spawn_arm}\n\n"
        "[statusbar]\n"
        f"enabled = {'true' if cfg.statusbar_enabled else 'false'}\n\n"
        "[danger]\n"
        f"preset = {cfg.danger_preset}\n\n"
        "[theme]\n"
        f"name = {cfg.theme}\n\n"
        "[layout]\n"
        f"preview = {'true' if cfg.preview_panel else 'false'}\n"
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
