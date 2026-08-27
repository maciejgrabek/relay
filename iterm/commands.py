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

# NEVER_EXPOSE is a blacklist - it stops the worker-protocol verbs, but says
# nothing about what a `cli=` entry MAY point at. `bin/relay` dispatches any
# verb it recognizes, including ones with their own full-screen TUI (a bare
# `relay` with no verb, for instance); routing a table entry at one of those
# would launch a SECOND Textual app underneath this one, which _cmd_run_cli
# would then dutifully capture as text and kill at the 8s timeout. EXPOSE is
# the allowlist: only a verb named here may ever appear as `cli=` on a table
# entry, so a typo or a future verb added to bin/relay cannot be shelled out
# by accident.
EXPOSE = frozenset({"ws", "doctor", "recap"})


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
    scope: bool = False       # the trailing argument is a SCOPE (all | here
                               # | <session> | <workspace>), resolved against
                               # live rows by app.py. Distinct from
                               # `subject`: a subject names ONE session and
                               # moves the cursor to it, where a scope names
                               # a SET and moves nothing.
    key_args: str = ""        # the fixed arguments this entry's KEY fires
                               # with. `a` is not a capability of its own, it
                               # is `arm safe all` on one press. Keeping the
                               # arguments in the table is what stops a key
                               # from drifting away from its typed form, and
                               # a key invocation is exempt from the bang
                               # gate: the keystroke against a legend on the
                               # bar IS the deliberate act the bang demands.
    bar: str = ""             # SHORT label for the one-line key bar (hot
                               # entries only); `help` stays the long form
                               # used by the `?` overlay. A full help
                               # sentence on the bar is how it overflowed
                               # an 80-column terminal in the first place.
    palette: bool = True      # offered by the `/` palette and by TAB
                               # completion. False for an entry that is a KEY
                               # and not a verb: cursor movement, the
                               # overlay-closer, the settings editor's own
                               # ←→ nudges, and the palette's opener. They
                               # keep their keys and stay documented in `?`;
                               # they are simply not things anyone types. An
                               # earlier version DEMOTED them instead, which
                               # only applied to the empty query - so typing
                               # "settings" still answered with
                               # `settings settingsleft settingsright`.
    bar_key: str = ""         # display-only key for an entry that claims no
                               # real Textual binding (`key=""`) - e.g. ENTER,
                               # which the DataTable already consumes, so
                               # binding it too would double-fire. Never fed
                               # to key_tokens()/BINDINGS agreement: it is not
                               # a claim on a key, only a legend for one that
                               # exists outside this table.


def key_tokens(cmd: Cmd) -> List[str]:
    """The individual keys an entry claims. `up,k` claims two."""
    return [t.strip() for t in cmd.key.split(",") if t.strip()]


# Textual's binding syntax names some keys rather than spelling their glyph
# (`key="comma"` for `,`), because that syntax is what BINDINGS and this
# table both have to agree on literally (see the agreement test). But the
# BAR and the `?` OVERLAY are for a human, and "exclamation_mark" is not a
# key a person recognizes at a glance - worse, it is 16 characters where the
# glyph is 1, which is exactly what let a long key silently threaten to blow
# `help_text()`'s fixed-width row (row() budgets on a `:<9` key column).
# Anything not in this map is already legible as-is (a letter, a digit, or a
# short word like "left"/"escape") and passes through unchanged.
_KEY_DISPLAY = {
    "slash": "/",
    "comma": ",",
    "exclamation_mark": "!",
    "question_mark": "?",
    "space": "SPACE",
    "escape": "ESC",
    "colon": ":",
    "tab": "TAB",
}


def _display_key(tok: str) -> str:
    """A key token as a human reads it, not as Textual spells it."""
    return _KEY_DISPLAY.get(tok, tok)


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
        if cmd.cli and cmd.cli not in EXPOSE:
            problems.append(
                f"{cmd.name}: cli={cmd.cli!r} is not in EXPOSE - a `cli=` "
                f"entry may only name a verb explicitly allowlisted there")
        # `help` is exempt: NEVER_EXPOSE governs which CLI verbs may be
        # SHELLED OUT, and the TUI's `?`-overlay command never shells out
        # anything - it shares a stem with the worker protocol's `help`
        # verb and nothing else. Every other name still collides for real
        # (see `digit`, not `send`, below).
        if cmd.key_args and not cmd.key:
            problems.append(
                f"{cmd.name}: key_args without a key - fixed arguments with "
                f"nothing to fire them is a row that can never run")
        if cmd.scope and cmd.subject:
            problems.append(
                f"{cmd.name}: scope and subject are exclusive - a subject "
                f"names one session and moves the cursor to it, a scope "
                f"names a set and moves nothing")
        if cmd.name in NEVER_EXPOSE and cmd.name != "help":
            problems.append(
                f"{cmd.name}: the entry's own NAME collides with a worker "
                f"protocol verb - :{cmd.name} would be ambiguous between "
                f"the TUI command and the swarm verb")
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
    """Typeable command names matching a prefix, sorted.

    Same `palette` filter as `filter_cmds`: TAB and the palette must offer
    the same set, or TAB completes a name the list in front of you does not
    contain.
    """
    return sorted(c.name for c in table
                  if c.palette and c.name.startswith(prefix))


def hot_pairs(table) -> List[Tuple[str, str]]:
    """(key, label) for the one-line key bar - hot entries only."""
    return [(_bar_key(c), _bar_label(c)) for c in table if c.hot]


def _bar_key(cmd: Cmd) -> str:
    """How a key reads in the bar: displayed, not Textual's raw token.

    `bar_key` wins over the bound tokens. An entry can carry several keys
    (`colon,slash` both open the palette) and listing them all just spends
    cells on a bar with none to spare - naming the one an operator should
    reach for is more use than naming every alias.
    """
    if cmd.bar_key:
        return cmd.bar_key
    toks = key_tokens(cmd)
    if toks:
        return "/".join(_display_key(t) for t in toks)
    return f":{cmd.name}"


def _bar_label(cmd: Cmd) -> str:
    """The bar is one line - `bar` is the short label written for it.

    Falling back to the long `help` line is what let a full sentence like
    "jump to the selected session's iTerm2 tab" end up on the bar and push
    it past the terminal width; every `hot=True` entry below sets `bar`.
    """
    return cmd.bar or cmd.help.split(" - ")[0].split(":")[0].strip()


def help_rows(table) -> List[Tuple[str, str]]:
    """(key-or-command, help) for the `?` overlay - EVERY entry."""
    rows = []
    for cmd in table:
        toks = key_tokens(cmd)
        if toks:
            left = " ".join(_display_key(t) for t in toks)
        elif cmd.bar_key:
            # A key with no real Textual binding (ENTER, consumed by the
            # DataTable) still gets a legend here - see `bar_key` on Cmd.
            left = cmd.bar_key
        else:
            left = f":{cmd.name}"
        right = cmd.help
        if toks or cmd.bar_key:
            right = f"{right}   (:{cmd.name})"
        rows.append((left, right))
    return rows


# The table. `hot=True` is the hot path only - what an operator does while
# watching, dozens of times an hour. Everything else stays bound to its key
# but leaves the bar, and is reachable and discoverable through `:`.
def filter_cmds(query: str, table) -> List["Cmd"]:
    """Commands matching `query`, best match first.

    SUBSTRING matching, not prefix: the palette exists so nobody has to know a
    command's first letters. Typing "work" must find "workspaces". Prefix
    matches still rank above interior ones, so "au" puts `audit` on top rather
    than burying it under something that merely contains "au".

    Only the first token is matched. Everything after the first space is this
    command's ARGUMENTS, and a filter that swallowed them answered a
    half-typed `arm all` with "no match" for a command that runs fine.

    An empty query returns every TYPEABLE command. Opening the palette IS the
    answer to "what can I do here?" - that is the whole point of it.

    `palette=False` entries are excluded at every query, not sorted last. A
    key that is not a verb (`up`, `back`, `settingsleft`, the palette's own
    opener) is noise in a list of things you can do, and sorting it last only
    hid it from the EMPTY query - `settings` still dragged `settingsleft` and
    `settingsright` up with it, which is where this was first noticed.
    """
    offered = [c for c in table if c.palette]
    # Only the VERB filters. The whole line used to be matched against
    # command names, so the first space an operator typed turned a valid
    # command into "no match for 'arm all'" - and every verb here that takes
    # an argument was unreachable through the palette the moment its
    # argument was typed. A trailing `!` is a suffix on the INVOCATION, not
    # part of a name (see `parse`), so it comes off here too.
    q = query.strip().lstrip("/:").split(" ")[0].rstrip("!").lower()
    if not q:
        return offered
    starts = [c for c in offered if c.name.lower().startswith(q)]
    inside = [c for c in offered
              if q in c.name.lower() and not c.name.lower().startswith(q)]
    return starts + inside


def _palette_name(cmd) -> str:
    """The name as the palette shows it, with the arguments it accepts.

    A verb whose arguments are invisible is a verb nobody types correctly:
    `arm` alone reads as a toggle, where `arm [level] [scope]` hands over the
    whole grammar without anyone having to open `?` to find it.
    """
    return f"{cmd.name} {cmd.args}".strip() if cmd.args else cmd.name


def palette_lines(query: str, table, cursor: int, width: int,
                  limit: int = 8) -> List[str]:
    """The filtered list drawn under the command line.

    Pure so it can be tested without a terminal. `cursor` is clamped rather
    than trusted: it is driven by arrow keys against a list that changes shape
    on every keystroke, so an out-of-range value is normal, not a bug.
    """
    matches = filter_cmds(query, table)
    if not matches:
        return [_fit_line(f"  no match for {query.strip().lstrip('/:')!r}",
                          width)]
    cursor = max(0, min(cursor, len(matches) - 1))
    # Keep the cursor on screen when it walks past the window.
    first = max(0, min(cursor - limit + 1, len(matches) - limit))
    first = max(0, first)
    window = matches[first:first + limit]
    name_w = max((len(_palette_name(c)) for c in window), default=0)
    out = []
    for i, cmd in enumerate(window, start=first):
        mark = "\u25b8" if i == cursor else " "
        out.append(_fit_line(
            f" {mark} {_palette_name(cmd):<{name_w}}  {cmd.help}", width))
    hidden = len(matches) - len(window)
    if hidden > 0:
        out.append(_fit_line(
            f"   ... {hidden} more of {len(matches)}", width))
    return out


def _fit_line(text: str, width: int) -> str:
    """Clip to `width`, never pad. The palette floats over the roster, so a
    line that overruns would paint across rows the operator is watching."""
    if width <= 1 or len(text) <= width:
        return text
    return text[:max(1, width - 1)] + "\u2026"


# Two columns are only an improvement when BOTH fit their descriptions.
# Measured, not guessed: the longest entry needs 71 cells, so a clean pair
# needs ~144. Below this the overlay stays single-column, where the full
# width is available and nothing truncates.
_TWO_COLUMN_MIN = 150


def help_columns(rows: List[Tuple[str, str]], width: int) -> List[str]:
    """`help_rows` laid out in TWO columns.

    One column ran 36 entries tall, which pushed the arm-level cheat sheet
    off the bottom of an ordinary terminal - the overlay documenting every
    capability could not itself be read. Two columns halves the height; the
    per-column width is what stops the descriptions truncating.
    """
    if not rows:
        return []
    # Two columns trade WIDTH for HEIGHT, and only one of those is ever the
    # binding constraint. On a narrow terminal each column would be too thin
    # for the descriptions and we would have swapped an unreadable-because-
    # too-tall overlay for an unreadable-because-truncated one. So the layout
    # is chosen, not assumed: one column below the threshold.
    if width < _TWO_COLUMN_MIN:
        return [_fit_line(f"  {k:<9} {h}", width) for k, h in rows]
    half = width // 2
    mid = (len(rows) + 1) // 2
    left, right = rows[:mid], rows[mid:]
    out = []
    for i in range(mid):
        lk, lh = left[i]
        cell = _fit_line(f"  {lk:<9} {lh}", half - 1).ljust(half - 1)
        if i < len(right):
            rk, rh = right[i]
            cell += " " + _fit_line(f"  {rk:<9} {rh}", half - 1)
        out.append(cell.rstrip())
    return out


CMD = (
    Cmd(name="up", help="move up", action="action_cursor_up", key="up,k",
        hot=True, bar="move", palette=False),
    Cmd(name="down", help="move down", action="action_cursor_down",
        key="down,j", hot=True, bar="move", palette=False),
    # `[level]` is honest now: action_arm SETS a named level where
    # action_toggle could only cycle. watcher.set_mode existed the whole
    # time and nothing in the TUI could reach it.
    Cmd(name="arm", help="arm: no level cycles, a level sets it",
        action="action_arm", key="space", hot=True, scope=True,
        args="[level] [scope]", bar="arm"),
    # Its own action rather than `key_args="off"`: key_args is what a KEY
    # fires with, and disarm has no key - its `off` is a default argument of
    # the typed verb. One field, one meaning.
    Cmd(name="disarm", help="disarm: sugar for `arm off`",
        action="action_disarm", scope=True, pass_args=True, args="[scope]"),
    # ENTER is NOT in BINDINGS: the DataTable consumes it and app.py warns
    # that binding it too would double-fire. So this entry carries no `key`
    # (nothing to agree with BINDINGS over) but still claims a legend via
    # `bar_key` - the exact bug class this table exists to prevent is a key
    # that works but is invisible, and an unbound key is no exception.
    Cmd(name="answer", help="send Enter to the selected session",
        action="action_send_enter", key="", bar_key="⏎", hot=True,
        bar="answer"),
    Cmd(name="tab", help="jump to the selected session's iTerm2 tab",
        action="action_focus", key="n", hot=True, bar="jump"),
    Cmd(name="swarm", help="swarm view: kanban, interactions and feed",
        action="action_swarm_view", key="tab", hot=True, bar="swarm"),
    Cmd(name="pause", help="pause or resume relay's acting",
        action="action_pause", key="p", hot=True, bar="pause"),
    Cmd(name="quit", help="quit relay", action="action_quit", key="q",
        hot=True, bar="quit"),

    # `a` and `d` are not capabilities of their own - they are `arm` at a
    # scope, which is exactly the argument the operator had no way to say
    # before. These two rows exist ONLY to give those keys a home the
    # BINDINGS agreement test can check; `palette=False` means neither is a
    # name anyone can type, so one capability stops wearing three.
    Cmd(name="armall", help="arm every session at safe (the `a` key)",
        action="action_arm", key="a", key_args="safe all", palette=False),
    Cmd(name="disarmall", help="disarm every session (the `d` key)",
        action="action_arm", key="d", key_args="off all", palette=False),
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
    # `help` collides with a NEVER_EXPOSE worker-protocol verb by NAME, but
    # validate() exempts this one entry deliberately (review round 2,
    # finding 1): the TUI's `?`-overlay command never shells out the swarm's
    # `help` verb, and `:help` is the most discoverable name a discoverability
    # feature could have - a blanket rename here cost more than the collision
    # it avoided.
    # `hot`: the hand-written bar named `? help` and the generated one did
    # not, which is the one entry an operator needs when the bar itself is
    # not enough - discoverability's own entry point, discoverable nowhere.
    # STOP and TELL were MODES inside the intervene overlay, cycled with the
    # arrow keys after pressing `!`. Neither had a key, so a table built from
    # BINDINGS could not see them and `:stop` matched nothing at all. Both
    # dispatch into the overlay's own _intervene_execute: one delivery path,
    # two front doors.
    Cmd(name="stop", help="brake: ESC into every working session in scope",
        action="action_stop", scope=True, confirm=True, pass_args=True,
        args="[scope]"),
    Cmd(name="tell", help="type a message into every session in scope",
        action="action_tell", scope=True, pass_args=True,
        args="[scope] <message>"),

    Cmd(name="help", help="this help", action="action_help",
        key="question_mark", hot=True, bar="help"),
    # `/` is the primary opener - it is what a Claude Code user reaches for -
    # and `:` stays because it shipped first and vim hands expect it. Both
    # land on the same action, so the palette has one entrance, not two.
    # palette=False: it is already open. Typing it re-entered
    # action_command_mode from inside the Input's own message handler, which
    # mounted a second #cmdline over one that Textual had not finished
    # pruning, and the pump deadlocked - the whole panel dead until a
    # timeout let go. app.py guards the mount too; this makes it untypeable.
    Cmd(name="commands", help="open the command line", action="action_command_mode",
        key="colon,slash", bar_key="/", bar="cmd", hot=True, palette=False),

    # "(double-press confirms)" used to live in these four `help` strings -
    # dropped (review's fix 4): a `!` on the cmdline and a second key press
    # are BOTH just the arm step for these, not two different confirm
    # protocols, and the sentence claimed both were "confirm" at once. The
    # cmdline confirm gate (app.py's _cmdline_submit) and the ARMED log
    # lines below carry the accurate wording now.
    Cmd(name="restore", help="respawn dead task-owners",
        action="action_restore", key="R", confirm=True),
    Cmd(name="wipe", help="delete dead sessions' work",
        action="action_wipe", key="W", confirm=True),
    Cmd(name="zap", help="delete a whole project",
        action="action_zap", key="Z", confirm=True),
    Cmd(name="extreme", help="EXTREME an INSANE session",
        action="action_extreme", key="E", confirm=True, subject=True),

    # action_send is parameterised - `action_send(key)` - so these carry the
    # digit in `args` and the dispatcher passes it. Do NOT invent
    # action_send_1/2/3; they do not exist. Named `digit`, not `send`: `send`
    # is a NEVER_EXPOSE worker-protocol verb, and this is the TUI's own
    # digit-key action, not that verb - `validate()` now refuses an entry
    # whose NAME collides with NEVER_EXPOSE, so this could not be `send`.
    Cmd(name="digit", help="send a digit to the selected session",
        action="action_send", key="1,2,3", hot=True, args="1|2|3",
        pass_args=True, bar="send"),
    # The three below are keys, not verbs: ←→ do nothing at all unless the
    # settings editor is already open, and ESC closes an overlay you must
    # already be in. Typing them is never the way anyone reaches them.
    Cmd(name="settingsleft", help="settings editor: change value left",
        action="action_settings_left", key="left", palette=False),
    Cmd(name="settingsright", help="settings editor: change value right",
        action="action_settings_right", key="right", palette=False),
    Cmd(name="back", help="close the open overlay", action="action_dismiss_view",
        key="escape", palette=False),

    Cmd(name="ws", help="workspaces: save, up, list, rm", cli="ws",
        args="save|up|list|rm <name>"),
    Cmd(name="doctor", help="swarm health from outside the TUI", cli="doctor"),
    Cmd(name="recap", help="what happened while you were away", cli="recap"),
)
