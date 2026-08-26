"""Relay workspaces - ~/.relay/workspaces.toml, named sets of tabs.

A tab is a directory plus a command. There is no tab-type key: `top`, `ps` and
`claude` are the same kind of thing. The one part that is not a command is
supervision, because relay must register a session BEFORE claude boots for the
arm level to be in place - so `arm` is its own key, and behaviour falls out of
which keys are present:

    cmd                     plain tab, not registered, not armed
    cmd + arm               registered under `name`, pre-armed, no prompt
    cmd + arm + prompt      the existing spawn_worker full-worker path

Pure stdlib, no iterm2/sqlite imports (test_workspaces.py runs it standalone),
which is also why ARM_MODES/ROLES are duplicated from db.py rather than
imported - test_workspaces.py asserts the two stay in step.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Mirrors db.ARM_REQUEST_MODES / db.ROLES. There is deliberately no "off":
# an unset `arm` is the only way to say "do not supervise this tab".
ARM_MODES = ("safe", "wild", "insane")
ROLES = ("worker", "coordinator")

TAB_KEYS = {"name", "dir", "cmd", "arm", "prompt", "role", "project",
            "window", "panes"}
PANE_KEYS = {"cmd", "dir", "split"}


class ConfigError(Exception):
    """Valid TOML that is not a valid workspace definition."""


@dataclass
class Pane:
    cmd: str = ""
    dir: str = ""            # empty means "inherit the tab's dir"
    split: str = "v"         # "v" side by side, "h" stacked


@dataclass
class Tab:
    name: str
    dir: str = "~"
    cmd: str = ""
    arm: str = ""            # "" = unsupervised
    prompt: str = ""
    role: str = "worker"
    project: str = ""
    window: int = 1
    panes: List[Pane] = field(default_factory=list)

    @property
    def supervised(self) -> bool:
        return bool(self.arm)

    @property
    def is_worker(self) -> bool:
        return bool(self.arm and self.prompt)


def default_path() -> str:
    return os.path.expanduser(
        os.environ.get("RELAY_WORKSPACES", "~/.relay/workspaces.toml"))


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _parse_pane(raw: object, where: str) -> Pane:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each pane must be a table, got "
                          f"{type(raw).__name__}")
    unknown = set(raw) - PANE_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown pane key(s) {sorted(unknown)}; "
                          f"valid keys are {sorted(PANE_KEYS)}")
    split = str(raw.get("split", "v")).lower()[:1] or "v"
    if split not in ("v", "h"):
        raise ConfigError(f'{where}: split must be "v" or "h", got '
                          f'{raw.get("split")!r}')
    return Pane(cmd=str(raw.get("cmd", "")),
                dir=_expand(str(raw["dir"])) if raw.get("dir") else "",
                split=split)


def _parse_tab(raw: object, where: str) -> Tab:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each tab must be a table, got "
                          f"{type(raw).__name__}")
    unknown = set(raw) - TAB_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; "
                          f"valid keys are {sorted(TAB_KEYS)}")
    if not raw.get("name"):
        raise ConfigError(f'{where}: missing required key "name"')

    arm = str(raw.get("arm", ""))
    if arm and arm not in ARM_MODES:
        raise ConfigError(f"{where}: arm must be one of {list(ARM_MODES)} "
                          f"(or absent for an unsupervised tab), got {arm!r}")
    role = str(raw.get("role", "worker"))
    if role not in ROLES:
        raise ConfigError(f"{where}: role must be one of {list(ROLES)}, "
                          f"got {role!r}")
    window = raw.get("window", 1)
    if not isinstance(window, int) or isinstance(window, bool) or window < 1:
        raise ConfigError(f"{where}: window must be an integer >= 1, "
                          f"got {window!r}")
    panes = [_parse_pane(p, f"{where} pane {i + 1}")
             for i, p in enumerate(raw.get("panes", []) or [])]
    return Tab(name=str(raw["name"]),
               dir=_expand(str(raw.get("dir", "~"))),
               cmd=str(raw.get("cmd", "")),
               arm=arm,
               prompt=str(raw.get("prompt", "")),
               role=role,
               project=str(raw.get("project", "")),
               window=window,
               panes=panes)


def load(path: Optional[str] = None) -> Tuple[dict, Dict[str, List[Tab]]]:
    """Return (settings, {workspace: [Tab, ...]}). Raises ConfigError.

    Unlike config.load(), this DOES raise: a typo'd key that silently does
    nothing is worse than a refusal that names it.
    """
    p = path or default_path()
    try:
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"no config at {p}")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}")

    settings = raw.pop("settings", {})
    if not isinstance(settings, dict):
        raise ConfigError('"settings" must be a table')

    spaces: Dict[str, List[Tab]] = {}
    for name, value in raw.items():
        if not isinstance(value, list):
            raise ConfigError(
                f'"{name}" must be a list of tabs - declare tabs as '
                f"[[{name}]], not [{name}]")
        spaces[name] = [_parse_tab(t, f'workspace "{name}" tab {i + 1}')
                        for i, t in enumerate(value)]
    return settings, spaces


def group_windows(tabs: List[Tab]) -> List[Tuple[int, List[Tab]]]:
    """Tabs grouped by their `window` key, in first-seen order."""
    order: List[int] = []
    groups: Dict[int, List[Tab]] = {}
    for tab in tabs:
        if tab.window not in groups:
            groups[tab.window] = []
            order.append(tab.window)
        groups[tab.window].append(tab)
    return [(w, groups[w]) for w in order]


# --- Workspace grouping (for app.py's session view) -------------------------
# Sessions grouped by the directory they were launched in. A workspace is WHERE
# THE SESSION WAS LAUNCHED, full stop. Not the git root, not a name the
# operator assigns. Relay records the directory once, at the point it first
# sees the tab, and never chases it after.
from typing import Callable, Sequence

# (key or None, [items]) - key None means "render these bare, no rail".
Group = Tuple[Optional[str], list]


def group(items: Sequence, key_of: Callable, min_size: int = 2) -> List[Group]:
    """Gather `items` into railed groups by `key_of`, preserving order.

    A key that is empty/None never groups: an unreadable directory is not a
    workspace, and pretending otherwise would put unrelated tabs in one box.
    """
    order: List[str] = []
    buckets = {}
    solo_at = {}          # position in `order` for ungroupable items
    for idx, it in enumerate(items):
        k = key_of(it) or ""
        if not k:
            marker = f"\0solo{idx}"
            order.append(marker)
            solo_at[marker] = it
            continue
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(it)
    out: List[Group] = []
    for k in order:
        if k in solo_at:
            out.append((None, [solo_at[k]]))
            continue
        members = buckets[k]
        if len(members) < min_size:
            out.extend((None, [m]) for m in members)
        else:
            out.append((k, members))
    return out


def freeze(current: str, persisted: str, live: str) -> str:
    """The workspace key to store for a tab, given what is already frozen
    (`current`), what the DB persisted (`persisted`) and what iTerm2 reports
    right now (`live`).

    Two rules, in this order:

      * A key already frozen is NEVER re-frozen. This is the whole feature -
        if anything here "helpfully" refreshed, rows would shuffle the moment
        a session ran `cd`.
      * The persisted column wins over the live reading. sessions.workdir is
        written once, only into an empty column, so it is the OLDEST reading
        relay has and it survives a relay restart - where the live path would
        otherwise re-freeze on wherever the session had wandered to by then.
    """
    if current:
        return current
    return persisted or live or ""


def short_path(p: str, home: str = "") -> str:
    """`/Users/me/Work/relay` -> `~/Work/relay`.

    Display only. The grouping key stays the full path, because two different
    users' homes must never collapse into one workspace just because they
    render the same.
    """
    if not p:
        return ""
    home = home or os.path.expanduser("~")
    if home and (p == home or p.startswith(home.rstrip("/") + "/")):
        return "~" + p[len(home.rstrip("/")):]
    return p


# Compact forms, for a rule that has to fit inside a table column rather
# than across a whole panel. The glyphs are the ones the rows already use, so
# the short form teaches nothing new: ◉ armed, ‼ wants you, ◈ burning.
_COMPACT = {"plain": "{n}", "armed": "◉{n}", "attention": "‼{n}",
            "burning": "◈{n}"}


def summary(members: Sequence, armed_of: Callable, attention_of: Callable,
            burning_of: Optional[Callable] = None,
            compact: bool = False) -> List[Tuple[str, str]]:
    """The counts that ride a group's top rule, as (text, kind) pairs.

    Kinds - 'plain', 'armed', 'attention', 'burning' - name what the count
    MEANS, not what colour it is; the view maps them to its own palette so a
    theme swap cannot strand a hue in here. Zero counts are omitted rather
    than printed as "0 needs you", which reads as news when it is the absence
    of news.

    `compact` returns the glyph form (`3 · ◉2 · ‼1`) for a rule drawn inside a
    table column, where the spelled-out version is silently truncated by
    whatever width the column happened to get.
    """
    def fmt(n, kind, word):
        return (_COMPACT[kind].format(n=n) if compact else word)

    n = len(members)
    out = [(fmt(n, "plain", f"{n} session{'' if n == 1 else 's'}"), "plain")]
    armed = sum(1 for m in members if armed_of(m))
    if armed:
        out.append((fmt(armed, "armed", f"{armed} armed"), "armed"))
    attn = sum(1 for m in members if attention_of(m))
    if attn:
        out.append((fmt(attn, "attention", f"{attn} needs you"), "attention"))
    if burning_of:
        burning = sum(1 for m in members if burning_of(m))
        if burning:
            out.append((fmt(burning, "burning", f"{burning} burning"),
                        "burning"))
    return out
