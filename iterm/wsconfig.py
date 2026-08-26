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
import re
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
    except OSError as exc:
        # Anything else that open()/read() can raise - permission denied, a
        # directory sitting at the path, etc. Kept distinct from the
        # FileNotFoundError case above (caught first, since it is itself an
        # OSError subclass): callers such as _wssave_commit tell "absent"
        # from "broken" apart by trying os.path.exists() first, so this
        # message must not read like the missing-file one.
        raise ConfigError(f"could not read {p}: {exc}")

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


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _header_name(name: str) -> str:
    """A workspace name as a TOML table-array header name: bare when that is
    safe (TOML bare keys are letters, digits, `-` and `_`), quoted otherwise.

    A name with a space, a quote, or a `#` is not bare-key-safe - emitting it
    unquoted produces a header TOML cannot parse (or parses as something
    else entirely, e.g. `#` starts a comment). `_is_header`/`strip_block`
    already strip both quote styles when reading a header back, so a quoted
    name round-trips."""
    return name if _BARE_KEY.match(name) else _q(name)


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
    header = _header_name(name)
    for tab in tabs:
        lines.append(f"[[{header}]]")
        lines.append(f"name = {_q(tab.name)}")
        lines.append(f"dir = {_q(_tilde(tab.dir))}")
        if tab.cmd:
            lines.append(f"cmd = {_q(tab.cmd)}")
        if tab.arm:
            lines.append(f"arm = {_q(tab.arm)}")
        if tab.prompt:
            lines.append(f"prompt = {_q(tab.prompt)}")
        if tab.role != "worker":
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


def _unescape_basic(s: str) -> str:
    """Reverse a TOML basic string's escaping - the inverse of _q(). _q()
    only ever produces \\\\ (backslash) and \\" (quote), and always escapes
    backslashes before quotes, so a left-to-right scan that greedily
    consumes those two two-character sequences decodes it unambiguously,
    including runs like \\\\" that come from a name containing both."""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] in ('"', "\\"):
            out.append(s[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _is_header(line: str) -> Optional[str]:
    """The table name a line declares, or None. Handles [x] and [[x]].

    Also handles inline comments and quoted names (with both " and ').
    A double-quoted header is a TOML basic string (escapes apply) and a
    single-quoted one is a literal string (no escapes) - they are unquoted
    differently, or a name like `say"hi"` compares unequal to itself after
    a save/load round trip.
    """
    s = line.strip()

    # Strip inline comment - it comes after the closing bracket
    if s.startswith("[["):
        if "]]" in s:
            s = s[:s.index("]]") + 2]
    elif s.startswith("["):
        if "]" in s:
            s = s[:s.index("]") + 1]

    # Extract table name
    if s.startswith("[[") and s.endswith("]]"):
        name = s[2:-2].strip()
    elif s.startswith("[") and s.endswith("]"):
        name = s[1:-1].strip()
    else:
        return None

    # Basic string: unescape. Literal string: taken verbatim, no escapes.
    if name.startswith('"') and name.endswith('"'):
        return _unescape_basic(name[1:-1])
    if name.startswith("'") and name.endswith("'"):
        return name[1:-1]
    return name


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
    """Write `name` into the file at `path`, creating it if absent. Atomic."""
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
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(joined.rstrip("\n") + "\n")
    os.replace(tmp, path)


def remove(path: str, name: str) -> bool:
    """Drop a workspace. True when it was there, False when it was not. Atomic."""
    _, existing = load(path)
    if name not in existing:
        return False
    with open(path) as fh:
        text = fh.read()
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(strip_block(text, name).rstrip("\n") + "\n")
    os.replace(tmp, path)
    return True


def snapshot(rows: List[dict]) -> List[Tab]:
    """Live tab records into Tabs.

    `cmd` is deliberately left empty: a running tab cannot report the command
    that was typed into it, and guessing from the foreground job would write
    "zsh" into every entry. The operator fills it in.

    Rows whose name is blank or all whitespace are dropped, not emitted: a tab
    with no name cannot be addressed in a workspace and cannot round-trip
    through render()/load().
    """
    tabs = []
    for r in rows:
        name = str(r.get("name", "")).strip()
        if not name:
            continue
        tabs.append(Tab(name=name,
                        dir=_expand(r.get("dir") or "~"),
                        arm=str(r.get("arm", "") or ""),
                        window=int(r.get("window", 1) or 1)))
    return tabs


def skip_live(tabs: List[Tab], live_tab_names) -> Tuple[List[Tab], List[str]]:
    """Split into (to build, skipped-because-already-live).

    One rule covers both duplicate restores and name theft: db.register()
    rebinds an existing name to a new session id, so building a tab whose name
    is already live would steal that session's identity. Skipping instead makes
    `ws up` idempotent AND safe, without failing the whole restore over one
    conflict.
    """
    live = set(live_tab_names or ())
    build = [t for t in tabs if t.name not in live]
    skipped = [t.name for t in tabs if t.name in live]
    return build, skipped


def plan_text(name: str, tabs: List[Tab], missing_dirs=(),
              skipped=()) -> str:
    """The --dry-run rendering, in the shape of restore_plan_text."""
    missing = set(missing_dirs or ())
    lines = [f"WORKSPACE {name}"]
    for window, wintabs in group_windows(tabs):
        lines.append(f"  window {window}")
        for tab in wintabs:
            marks = []
            if tab.arm:
                marks.append(f"armed {tab.arm}")
            if tab.prompt:
                marks.append("worker")
            if tab.dir in missing:
                marks.append("missing dir - will be skipped")
            suffix = f"   [{', '.join(marks)}]" if marks else ""
            cmd = f"   $ {tab.cmd}" if tab.cmd else ""
            lines.append(f"    {tab.name:<16} {_tilde(tab.dir)}{cmd}{suffix}")
            for pane in tab.panes:
                where = "beside" if pane.split == "v" else "below"
                lines.append(f"      {where}: {pane.cmd or _tilde(pane.dir or tab.dir)}")
    if skipped:
        lines.append(f"  skipped (already live): {', '.join(skipped)}")
    return "\n".join(lines)
