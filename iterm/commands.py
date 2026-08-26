"""The TUI command table - one source of truth for every capability.

The key bar, the `?` overlay and `:` completion are all PROJECTIONS of this
table. That is the point: relay used to keep the key bar in one hand-written
list and the help overlay in a second one, and when `w`/`S` were added to
BINDINGS neither list was touched, so two working keys shipped invisible.
A capability that is not in this table does not exist; one that is in it is
discoverable by construction.

Pure stdlib, no textual/iterm2/sqlite/db imports, so test_commands.py runs
standalone. `action` names a method on the app as a STRING - app.py resolves
it with getattr - which is what keeps Textual out of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

# Worker protocol verbs: a Claude session runs these ABOUT ITSELF. From
# relay's own panel they are meaningless at best. `register` and `join` are
# the dangerous pair - either would bind relay's own tab as a swarm session.
# The exclusion is by construction: an unlisted verb has no table entry, so
# it cannot be typed, completed or bound.
NEVER_EXPOSE = frozenset({
    "register", "join", "send", "status", "inbox", "next", "reply", "ask",
    "say", "agree", "close", "discuss", "thread", "task", "timer", "pr",
    "help",
})


@dataclass(frozen=True)
class Cmd:
    name: str                 # what you type after `:`
    help: str                 # one line, shown in `?` and in completion
    action: str = ""          # name of an app method, OR
    cli: str = ""             # a relay CLI verb to shell out to
    key: str = ""             # optional accelerator, Textual binding syntax
    hot: bool = False         # appears in the one-line key bar
    subject: bool = False     # first argument defaults to the cursor row
    confirm: bool = False     # requires an explicit `!` before running
    args: str = ""            # usage hint, e.g. "<name>" or "on|off"
    pass_args: bool = False   # the action takes the typed args as parameters


def key_tokens(cmd: Cmd) -> List[str]:
    """The individual keys an entry claims. `up,k` claims two."""
    return [t.strip() for t in cmd.key.split(",") if t.strip()]


def validate(table) -> List[str]:
    """Problems with a table, as human-readable strings. Empty means good."""
    problems: List[str] = []
    seen = {}
    for cmd in table:
        if bool(cmd.action) == bool(cmd.cli):
            problems.append(
                f"{cmd.name}: needs exactly one of action/cli")
        if not cmd.help.strip():
            problems.append(f"{cmd.name}: missing help")
        if cmd.cli and cmd.cli in NEVER_EXPOSE:
            problems.append(
                f"{cmd.name}: {cmd.cli} is a worker protocol verb and must "
                f"never be exposed in the TUI")
        # Check for empty tokens in key string (e.g., "a,,b")
        if cmd.key:
            for part in cmd.key.split(","):
                if not part.strip():
                    problems.append(
                        f"{cmd.name}: empty token in key {cmd.key!r}")
                    break
        for tok in key_tokens(cmd):
            if tok in seen:
                problems.append(
                    f"key {tok!r} claimed by both {seen[tok]} and {cmd.name}")
            seen[tok] = cmd.name
    return problems


def parse(line: str) -> Tuple[str, List[str], bool]:
    """A typed line into (verb, args, confirmed).

    Trailing `!` characters are a suffix on the INVOCATION, not part of a name:
    `:wipe!`, `:wipe!!`, and `:wipe !` all resolve to the `wipe` entry with
    confirmed set. Completion therefore never offers a name ending in `!`.
    """
    parts = line.strip().lstrip(":").split()
    if not parts:
        return "", [], False
    bang = False
    if parts[-1] == "!":
        bang = True
        parts = parts[:-1]
        if not parts:
            return "", [], True
    verb = parts[0]
    # Strip ALL trailing ! and set confirmed if there was at least one
    original_verb = verb
    verb = verb.rstrip("!")
    if verb != original_verb:
        bang = True
    return verb, parts[1:], bang


def complete(prefix: str, table) -> List[str]:
    """Command names matching a prefix, sorted. Empty prefix offers all."""
    return sorted(c.name for c in table if c.name.startswith(prefix))


def hot_pairs(table) -> List[Tuple[str, str]]:
    """(key, label) for the one-line key bar - hot entries only."""
    return [(_bar_key(c), _bar_label(c)) for c in table if c.hot]


def _bar_key(cmd: Cmd) -> str:
    """How a key reads in the bar: `up,k` is shown as the arrows it is."""
    toks = key_tokens(cmd)
    return "/".join(toks) if toks else f":{cmd.name}"


def _bar_label(cmd: Cmd) -> str:
    """The bar is one line - use the first clause of the help line."""
    return cmd.help.split(" - ")[0].split(":")[0].strip()


def help_rows(table) -> List[Tuple[str, str]]:
    """(key-or-command, help) for the `?` overlay - EVERY entry."""
    rows = []
    for cmd in table:
        toks = key_tokens(cmd)
        left = " ".join(toks) if toks else f":{cmd.name}"
        right = cmd.help
        if toks:
            right = f"{right}   (:{cmd.name})"
        rows.append((left, right))
    return rows


# The table. `hot=True` is the hot path only - what an operator does while
# watching, dozens of times an hour. Everything else stays bound to its key
# but leaves the bar, and is reachable and discoverable through `:`.
CMD = (
    Cmd(name="up", help="move up", action="action_cursor_up", key="up,k",
        hot=True),
    Cmd(name="down", help="move down", action="action_cursor_down",
        key="down,j", hot=True),
    Cmd(name="arm", help="cycle arm: off -> safe -> wild -> insane",
        action="action_toggle", key="space", hot=True, subject=True,
        args="[session] [level]"),
    # ENTER is NOT in BINDINGS: the DataTable consumes it and app.py warns
    # that binding it too would double-fire. So this entry carries no key,
    # and KEYBAR names ENTER literally (Task 2 step 4).
    Cmd(name="answer", help="send Enter to the selected session",
        action="action_send_enter"),
    Cmd(name="tab", help="jump to the selected session's iTerm2 tab",
        action="action_focus", key="n", hot=True),
    Cmd(name="swarm", help="swarm view: kanban, interactions and feed",
        action="action_swarm_view", key="tab", hot=True),
    Cmd(name="pause", help="pause or resume relay's acting",
        action="action_pause", key="p", hot=True),
    Cmd(name="quit", help="quit relay", action="action_quit", key="q",
        hot=True),

    Cmd(name="armall", help="arm every session at safe", action="action_all",
        key="a"),
    Cmd(name="disarmall", help="disarm every session", action="action_none",
        key="d"),
    Cmd(name="shadow", help="shadow-arm: dry-run, records without acting",
        action="action_shadow", key="s", subject=True),
    Cmd(name="hide", help="hide or show the selected session",
        action="action_hide", key="x", subject=True),
    Cmd(name="audit", help="audit view: what relay approved for this session",
        action="action_audit_view", key="v"),
    Cmd(name="feed", help="show or hide the live terminal feed",
        action="action_toggle_preview", key="f"),
    Cmd(name="timers", help="timers bound to the selected session",
        action="action_timers", key="t"),
    Cmd(name="park", help="park an idea against the selected session",
        action="action_park", key="i", subject=True),
    Cmd(name="parked", help="the parked pile", action="action_parked",
        key="b"),
    Cmd(name="mascot", help="float the mascot above other apps",
        action="action_mascot", key="m"),
    Cmd(name="caffeinate", help="keep the Mac awake, or take it back",
        action="action_caffeinate", key="c"),
    Cmd(name="settings", help="settings editor", action="action_settings",
        key="comma"),
    Cmd(name="workspaces", help="saved tab sets in ~/.relay/workspaces.toml",
        action="action_workspaces", key="w"),
    Cmd(name="savelayout", help="save this window as a workspace",
        action="action_ws_save", key="S"),
    Cmd(name="intervene", help="stop running sessions and broadcast to them",
        action="action_intervene", key="exclamation_mark"),
    Cmd(name="help", help="this help", action="action_help",
        key="question_mark"),

    Cmd(name="restore", help="respawn dead task-owners", action="action_restore",
        key="R", confirm=True),
    Cmd(name="wipe", help="delete dead sessions' work", action="action_wipe",
        key="W", confirm=True),
    Cmd(name="zap", help="delete a whole project", action="action_zap",
        key="Z", confirm=True),
    Cmd(name="extreme", help="EXTREME an INSANE session", action="action_extreme",
        key="E", confirm=True, subject=True),

    # action_send is parameterised - `action_send(key)` - so these carry the
    # digit in `args` and the dispatcher passes it. Do NOT invent
    # action_send_1/2/3; they do not exist.
    Cmd(name="send", help="send a digit to the selected session",
        action="action_send", key="1,2,3", hot=True, args="1|2|3",
        pass_args=True),
    Cmd(name="settingsleft", help="settings editor: change value left",
        action="action_settings_left", key="left"),
    Cmd(name="settingsright", help="settings editor: change value right",
        action="action_settings_right", key="right"),
    Cmd(name="back", help="close the open overlay", action="action_dismiss_view",
        key="escape"),

    Cmd(name="ws", help="workspaces: save, up, list, rm", cli="ws",
        args="save|up|list|rm <name>"),
    Cmd(name="doctor", help="swarm health from outside the TUI", cli="doctor"),
    Cmd(name="recap", help="what happened while you were away", cli="recap"),
)
