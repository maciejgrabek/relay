"""Relay config - ~/.relay/config (INI), the durable home for preferences.

    [titles]
    style = off            ; off | glyphs | words | hybrid

    [sounds]
    alert = /System/Library/Sounds/Sosumi.aiff
    done  = /System/Library/Sounds/Glass.aiff

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


@dataclass(frozen=True)
class Config:
    title_style: str = "off"
    alert_sound: str = "/System/Library/Sounds/Sosumi.aiff"
    done_sound: str = "/System/Library/Sounds/Glass.aiff"
    stale_minutes: float = 10.0
    notify_cooldown: float = 30.0


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
    cp = configparser.ConfigParser()
    try:
        cp.read(p)
    except configparser.Error as e:
        warns.append(f"config: {p} is malformed ({e.__class__.__name__}) - "
                     f"using defaults")
        cp = configparser.ConfigParser()

    d = Config()  # defaults
    style = cp.get("titles", "style", fallback=d.title_style).strip().lower()
    if style not in TITLE_STYLES:
        warns.append(f"config: [titles] style = {style!r} is not one of "
                     f"{'/'.join(TITLE_STYLES)} - using 'off'")
        style = "off"

    stale = _get_float(cp, "swarm", "stale_minutes", d.stale_minutes, warns)
    cooldown = _get_float(cp, "swarm", "notify_cooldown", d.notify_cooldown,
                          warns)

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
        stale_minutes=stale,
        notify_cooldown=cooldown,
    ), warns
