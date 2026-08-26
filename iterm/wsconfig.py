"""Relay workspace configs - ~/.relay/workspaces.toml, named sets of tabs.

A tab is a directory plus a command. There is no tab-type key: `top`, `ps` and
`claude` are the same kind of thing. The one part that is not a command is
supervision, because relay must register a session BEFORE claude boots for the
arm level to be in place - so `arm` is its own key, and behaviour falls out of
which keys are present:

    cmd                     plain tab, not registered, not armed
    cmd + arm               registered under `name`, pre-armed, no prompt
    cmd + arm + prompt      the existing spawn_worker full-worker path

Pure stdlib, no iterm2/sqlite imports (test_wsconfig.py runs it standalone),
which is also why ARM_MODES/ROLES are duplicated from db.py rather than
imported - test_wsconfig.py asserts the two stay in step.
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


def _q(value: str) -> str:
    """A TOML basic string. Only backslash and quote need escaping here; a
    workspace name or command containing a raw newline is not something we
    round-trip, and load() would reject the result loudly rather than quietly."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def _render_pane(pane: Pane) -> str:
    bits = []
    if pane.cmd:
        bits.append(f"cmd = {_q(pane.cmd)}")
    if pane.dir:
        bits.append(f"dir = {_q(_tilde(pane.dir))}")
    bits.append(f"split = {_q(pane.split)}")
    return "{ " + ", ".join(bits) + " }"


def render(name: str, tabs: List[Tab]) -> str:
    """Tabs back to a TOML block. Only non-default keys are written, so a
    hand-edited file stays as short as what the operator actually meant."""
    lines: List[str] = []
    for tab in tabs:
        lines.append(f"[[{name}]]")
        lines.append(f"name = {_q(tab.name)}")
        lines.append(f"dir = {_q(_tilde(tab.dir))}")
        if tab.cmd:
            lines.append(f"cmd = {_q(tab.cmd)}")
        if tab.arm:
            lines.append(f"arm = {_q(tab.arm)}")
        if tab.prompt:
            lines.append(f"prompt = {_q(tab.prompt)}")
        if tab.prompt and tab.role != "worker":
            lines.append(f"role = {_q(tab.role)}")
        if tab.project:
            lines.append(f"project = {_q(tab.project)}")
        if tab.window != 1:
            lines.append(f"window = {tab.window}")
        if tab.panes:
            lines.append("panes = [ "
                         + ", ".join(_render_pane(p) for p in tab.panes)
                         + " ]")
        lines.append("")
    return "\n".join(lines)


def _is_header(line: str) -> Optional[str]:
    """The table name a line declares, or None. Handles [x] and [[x]]."""
    s = line.strip()
    if s.startswith("[[") and s.endswith("]]"):
        return s[2:-2].strip().strip('"')
    if s.startswith("[") and s.endswith("]"):
        return s[1:-1].strip().strip('"')
    return None


def strip_block(text: str, name: str) -> str:
    """Every [[name]] table removed, everything else preserved verbatim.

    Comments sitting directly above a removed block are orphaned rather than
    removed with it - a comment belongs to whoever wrote it, and guessing
    which ones "belong" to the table below would delete more than asked.
    """
    out: List[str] = []
    dropping = False
    for line in text.splitlines():
        header = _is_header(line)
        if header is not None:
            dropping = header == name
        if not dropping:
            out.append(line)
    return "\n".join(out)


def save(path: str, name: str, tabs: List[Tab], force: bool = False) -> None:
    """Write `name` into the file at `path`, creating it if absent."""
    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""

    if text.strip():
        _, existing = load(path)
        if name in existing:
            if not force:
                raise ConfigError(
                    f'workspace "{name}" already exists - pass force to '
                    f"replace it")
            text = strip_block(text, name)

    body = render(name, tabs)
    joined = (text.rstrip("\n") + "\n\n" + body) if text.strip() else body

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(joined.rstrip("\n") + "\n")


def remove(path: str, name: str) -> bool:
    """Drop a workspace. True when it was there, False when it was not."""
    _, existing = load(path)
    if name not in existing:
        return False
    with open(path) as fh:
        text = fh.read()
    with open(path, "w") as fh:
        fh.write(strip_block(text, name).rstrip("\n") + "\n")
    return True
