"""Relay-iTerm - the TUI control panel.

A single Textual app that BOTH holds the iTerm2 connection (via Watcher) and
renders a dark, Total-Commander-style control panel. Tool on === this app open;
quit === everything stops. No daemon, no auto-launch, no shared state files.

  relay            run it
  relay --dry-run  watch + notify but never inject (safe first run)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import iterm2  # noqa: E402
from rich.markup import escape  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Vertical  # noqa: E402
from textual.widgets import DataTable, Static, Log  # noqa: E402

import audit  # noqa: E402
import db as swarmdb  # noqa: E402
import swarm as swarmlogic  # noqa: E402
from watcher import Watcher  # noqa: E402

# Retro phosphor-green CRT terminal aesthetic. Big block logo (ANSI Shadow figlet).
BANNER = r"""
 ██████╗ ███████╗██╗      █████╗ ██╗   ██╗
 ██╔══██╗██╔════╝██║     ██╔══██╗╚██╗ ██╔╝
 ██████╔╝█████╗  ██║     ███████║ ╚████╔╝
 ██╔══██╗██╔══╝  ██║     ██╔══██║  ╚██╔╝
 ██║  ██║███████╗███████╗██║  ██║   ██║
 ╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝   ╚═╝""".strip("\n")

# --- themes -------------------------------------------------------------------
# Three shipped palettes; picked via [theme] name in ~/.relay/config. Every
# color in the app resolves through the active palette - no stray hex
# literals - so a theme swap recolors everything, CSS included.
THEMES = {
    "phosphor": {"bright": "#3aff7a", "accent": "#2fc866", "dim": "#2a7d4f",
                 "dimmer": "#1d5c38", "warn": "#ffb000", "danger": "#ff5555",
                 "cyan": "#41ffd0", "hot": "#6effa0", "ember": "#7a1d1d",
                 "bg": "#020a04", "bg_alt": "#04120a", "bg_deep": "#010602",
                 "bg_head": "#061a0e", "bg_cursor": "#0d3a22"},
    "amber":    {"bright": "#ffb347", "accent": "#e09a2b", "dim": "#8a5a18",
                 "dimmer": "#5c3d10", "warn": "#ffd700", "danger": "#ff5555",
                 "cyan": "#ffe8b0", "hot": "#ffd28a", "ember": "#7a1d1d",
                 "bg": "#0a0602", "bg_alt": "#120a04", "bg_deep": "#060301",
                 "bg_head": "#1a0f06", "bg_cursor": "#3a2a0d"},
    "ice":      {"bright": "#7ad7ff", "accent": "#4fb3d9", "dim": "#2a5d7d",
                 "dimmer": "#1d3f5c", "warn": "#ffb000", "danger": "#ff6a7a",
                 "cyan": "#b0ffe8", "hot": "#a8e8ff", "ember": "#7a1d2a",
                 "bg": "#02060a", "bg_alt": "#040a12", "bg_deep": "#010306",
                 "bg_head": "#06101a", "bg_cursor": "#0d2a3a"},
}


def _active_theme() -> dict:
    import config as _config
    name = getattr(_config.load()[0], "theme", "phosphor")
    return THEMES.get(name, THEMES["phosphor"])


TH = _active_theme()
# Short aliases for the markup call sites.
BRIGHT, ACCENT, DIM, DIMMER = (TH["bright"], TH["accent"], TH["dim"],
                               TH["dimmer"])
WARN, DANGER, CYAN, EMBER = TH["warn"], TH["danger"], TH["cyan"], TH["ember"]


def _theme_css(tpl: str) -> str:
    """Resolve $token color placeholders in the CSS template. Longest token
    first, so $dim never eats the front of $dimmer (nor $bg of $bg_cursor)."""
    for k in sorted(TH, key=len, reverse=True):
        tpl = tpl.replace("$" + k, TH[k])
    return tpl


# Retro-terminal state labels - meaning kept obvious, dressed for the CRT theme.
STATE_STYLE = {
    "idle":      ("◌ STANDBY",  DIM),   # dim phosphor
    "working":   ("▸ ACTIVE",   BRIGHT),   # bright phosphor
    "prompting": ("‼ AWAITING", WARN),   # amber alert
    "blocked":   ("⊘ LOCKED",   DANGER),   # red lockdown
    "cleared":   ("✓ CLEARED",  CYAN),   # cyan-green ok
}

# Per-mode arm chrome: glyph + label + color. Glyphs must be SINGLE-WIDTH -
# emoji (🔥/⚡) render double-width and shift the column out of alignment. These
# box/geometric symbols are all 1 cell wide.
MODE_STYLE = {
    "off":    ("○", "MANUAL",  DIM),
    "safe":   ("◉", "SAFE",    BRIGHT),
    "wild":   ("▲", "WILD",    WARN),
    "insane": ("✦", "INSANE",  DANGER),
}


# Two-line key bar. The Textual Footer crams every binding onto one row and
# hides the overflow; with a dozen keys that truncates on a narrow window, so we
# render our own two lines grouped by meaning: line 1 acts on the SELECTED
# session, line 2 is fleet-wide + app. The bindings work regardless of this bar
# (it is display only). Key glyphs amber, labels green, separators dim - the CRT
# palette. Built once at import; keys don't change at runtime.
def _keys(pairs) -> str:
    return f" [{DIM}]·[/] ".join(
        f"[{WARN} bold]{k}[/] [{BRIGHT}]{label}[/]" for k, label in pairs)


KEYBAR = (
    _keys([("↑↓", "move"), ("SPACE", "arm"), ("ENTER", "answer"),
           ("1/2/3", "send"), ("n", "go to tab"), ("x", "hide"),
           ("v", "audit")])
    + "\n"
    + _keys([("a", "arm all"), ("d", "disarm all"), ("TAB", "swarm"),
             ("R×2", "restore"), ("W×2", "wipe"), ("?", "help"),
             ("q", "quit")]))


def relay_self_panel(width, *, units, armed, approvals, escalations, orphans,
                     db_path, dry_run) -> str:
    """Shown in the feed pane when relay's OWN row is selected. Relay never
    watches its own tab, so instead of dead space this answers 'what about
    relay itself?' - this run's tallies + how to get around. Plain text (the
    pane is markup=False; CSS gives it the phosphor color)."""
    w = max(40, width)
    bar = "═" * w
    orphan_line = (f"   {orphans} orphaned task-owner(s) - press R twice to "
                   f"restore, W twice to wipe\n" if orphans else "")
    sim = "  [SIMULATION / dry-run]" if dry_run else ""
    return (
        f"╔{bar}╗\n"
        f" ▓ RELAY CONTROL // this panel{sim}\n"
        f"╚{bar}╝\n"
        "\n"
        " Relay does not watch or act on its own tab.\n"
        "\n"
        " THIS RUN\n"
        f"   sessions {units} ({armed} armed) · {approvals} approved · "
        f"{escalations} escalated\n"
        f"{orphan_line}"
        f"   db  {db_path}\n"
        "\n"
        " GETTING AROUND\n"
        "   ↑↓ pick a session · SPACE arm it · n jump to its tab\n"
        "   TAB swarm board · q quit  (full key bar is at the bottom)\n"
        "\n"
        " Add more to control: open a tab and start a long job or a\n"
        " Claude Code session - it shows up in the list above.\n")


def reactor_pressure(sessions) -> float:
    """Instantaneous 'heat input' from how much is happening UNATTENDED right
    now (0.0+). The reactor heats toward this and vents below it - so when you
    engage / sessions go idle, temp falls. Pure function for testing.

      + armed tabs running unattended (more for hotter modes)
      + sessions waiting on the human (held prompts, blocked - unhandled)
      + recent auto-approval activity
      + stale armed sessions (supposedly working unattended, visibly dead)
    """
    p = 0.0
    for i in sessions:
        if i.mode == "insane":
            p += 0.9
        elif i.mode == "wild":
            p += 0.6
        elif i.mode == "safe":
            p += 0.3
        if i.state in ("blocked", "prompting"):
            p += 1.2                  # a human is the bottleneck, unhandled
        elif i.state == "working" and i.active:
            p += 0.4
        if getattr(i, "stale", False):
            p += 0.6                  # quiet-dead is risk, not calm
    return p


def mascot_frame(tick: int, band: str, *, alarmed: bool,
                 working: bool) -> str:
    """The header core-creature, keyed to the 0.5s reactor tick. It reacts to
    REAL state, in priority order: something awaits a human (alarmed) beats
    a CRITICAL core beats recent activity (working) beats idle blinking."""
    if alarmed:
        return "(⊙_⊙)!" if tick % 2 == 0 else "(⊙_⊙) "
    if band == "☢ CRITICAL":
        return "(x_x)"
    if working:
        return "(◕‿◕)⌁" if tick % 2 == 0 else "(◕‿◕) "
    return "(￣‿￣)" if tick % 8 == 0 else "(－‿－)"


# Reactor temperature bands -> (label, color, pulsing?).
def reactor_band(temp: float):
    if temp >= 8.0:
        return ("☢ CRITICAL", DANGER, True)
    if temp >= 4.0:
        return ("⚠ ELEVATED", WARN, False)
    if temp >= 1.0:
        return ("◷ WARM", BRIGHT, False)
    return ("STABLE", DIM, False)


def getting_started_panel(width: int) -> str:
    """Shown in the preview pane when relay has nothing to control (only its own
    tab is open). Relay acts on OTHER sessions, so an empty roster is the moment
    to teach that, not blank space. Pure text (the pane renders markup=False)."""
    w = max(40, width)
    bar = "═" * w
    return (
        f"╔{bar}╗\n"
        f" ▓ NOTHING TO CONTROL YET\n"
        f"╚{bar}╝\n"
        "\n"
        " Relay is a control panel for OTHER terminal sessions - it has\n"
        " nothing to do with only itself running.\n"
        "\n"
        "   1. Open a tab and start a long job or a Claude Code session\n"
        "   2. Come back here - it shows up in the list above\n"
        "   3. Press SPACE to arm it. Relay auto-clears its safe prompts\n"
        "      and pings you on the dangerous ones. Then walk away.\n"
        "\n"
        " Running a swarm of Claude sessions? Spawn armed workers with:\n"
        "\n"
        "     relay spawn --name w1 --arm wild \"your task here\"\n"
        "\n"
        " Keys:  ↑↓ move · SPACE arm · TAB swarm view · q quit\n")


def help_text() -> str:
    """The `?` overlay: key map + arm-level cheat sheet. Pure so it's
    testable; markup is static (no dynamic text to escape)."""
    A, G, D = WARN, BRIGHT, DIM

    def row(key, what):
        return f"  [{A}]{key:<9}[/] [{G}]{what}[/]"

    return "\n".join([
        f"[{A}]RELAY KEYS[/]   [{D}]press ? or ESC to close this help[/]",
        "",
        row("↑↓ / j k", "move (continuous through NEEDS ACTION and list)"),
        row("ENTER", "send Enter to the selected session (answer by hand)"),
        row("1 2 3", "send that digit (pick a menu option by hand)"),
        row("SPACE", "cycle arm: off -> safe -> wild -> insane -> off"),
        row("a / d", "arm all (safe) / disarm all"),
        row("n", "jump to the selected session's iTerm2 tab"),
        row("x", "hide / show the selected session"),
        row("v", "audit view: what relay approved for this session"),
        row("TAB", "swarm view (kanban + interactions + feed)"),
        row("R R", "restore dead task-owners (double-press confirms)"),
        row("W W", "WIPE dead sessions' work (double-press confirms)"),
        row("q", "quit (asks twice only when something is live)"),
        row("?", "close this help"),
        "",
        f"[{A}]ARM LEVELS[/]  [{D}](what relay may auto-approve)[/]",
        "",
        row("○ MANUAL", "never acts - watch and notify only"),
        row("◉ SAFE", "auto-approves commands classified safe; escalates rest"),
        row("▲ WILD", "approves any 'Do you want to proceed?' unclassified"),
        row("✦ INSANE", "approves ANY tool prompt, even fail-safe cases"),
        "",
        f"[{D}]A real question (multi-choice) is ALWAYS yours - no mode"
        f" auto-answers decisions.[/]",
    ])


def audit_view_text(entries, title: str, width: int, now=None) -> str:
    """The `v` view: what relay decided for ONE session while you weren't
    looking - approvals, escalations, deliveries, newest last. Plain text
    (the preview pane renders markup-free). Filters by the audit log's
    session field, which stores the session title."""
    import time as _t
    w = max(40, width)
    bar = "═" * w
    head = (f"╔{bar}╗\n"
            f" ▓ AUDIT // {title[:w - 12]}\n"
            f" what relay decided unattended · press v or ESC to return to "
            f"the live feed\n"
            f"╚{bar}╝\n")
    mine = [e for e in entries if e.get("session") == title]
    if not mine:
        return head + (
            "\n no recorded decisions for this session yet.\n\n"
            " every unattended approval, escalation, and delivery is\n"
            " written to ~/.relay/audit.jsonl (kept 7 days) BEFORE relay\n"
            " acts - this view is that record, per session.")
    mark = {"auto-approved": "✓", "escalated": "⊘", "delivered": "→",
            "would-approve": "≈", "would-deliver": "≈"}
    lines = []
    for e in mine[-200:]:
        t = _t.strftime("%m-%d %H:%M:%S", _t.localtime(e.get("ts", 0)))
        m = mark.get(e.get("verdict", ""), "?")
        lines.append(f" {t}  {m} {str(e.get('verdict', '?')):<13} "
                     f"{str(e.get('command', ''))[:max(10, w - 38)]}")
    return head + "\n".join(lines)


def needs_action(state: str, stale: bool) -> bool:
    """A session a human should look at NOW: it is holding a prompt relay
    escalated (or is not allowed to clear), it is blocked, or it went stale.
    These rows group under the NEEDS ACTION divider at the top of the table."""
    return bool(stale) or state in ("prompting", "blocked")


def quit_stakes_text(n_armed: int, n_queued: int, n_doing: int) -> str:
    """What quitting would walk away from, as the confirm hint - or '' when
    nothing is at stake and q should quit instantly. Quitting stops
    auto-approval (armed sessions), message delivery (queued), and the stall
    watchdog (doing tasks); an idle panel loses nothing."""
    bits = []
    if n_armed:
        bits.append(f"{n_armed} armed")
    if n_queued:
        bits.append(f"{n_queued} msg(s) queued")
    if n_doing:
        bits.append(f"{n_doing} task(s) doing")
    return ", ".join(bits)


class RelayApp(App):
    CSS = _theme_css("""
    /* phosphor-green CRT terminal */
    Screen { background: $bg; color: $bright; }
    #banner { color: $bright; text-style: bold; height: auto; padding: 1 2 0 2; }
    #subtitle { color: $dim; height: 1; padding: 0 2; }
    #reactor { height: 1; padding: 0 2; }
    /* Stacked layout: the list on top, the live terminal feed below - both
       full width so the 8-column list and 80-col terminal output each get the
       room they need (side-by-side left the preview too narrow to read). */
    #middle { height: 1fr; }
    DataTable {
        width: 1fr; height: 2fr; background: $bg; color: $bright;
        border-bottom: solid $dimmer;
    }
    DataTable > .datatable--cursor { background: $bg_cursor; color: $hot; text-style: bold; }
    DataTable > .datatable--header {
        background: $bg_head; color: $warn; text-style: bold;
    }
    DataTable > .datatable--odd-row { background: $bg_alt; }
    DataTable > .datatable--even-row { background: $bg; }
    #preview {
        width: 1fr; height: 3fr;
        background: $bg_deep; color: $accent;
        padding: 0 1;
    }
    #log {
        height: 5; border-top: solid $dimmer;
        background: $bg_deep; color: $dim;
    }
    #swarmview, #helpview {
        display: none; height: 1fr; padding: 0 2;
        background: $bg_deep; color: $accent;
    }
    #keybar { height: 2; background: $bg_head; padding: 0 2; }
    """)
    BINDINGS = [
        Binding("up,k", "cursor_up", "Up", show=False),
        Binding("down,j", "cursor_down", "Down", show=False),
        # NOTE: Enter is handled ONLY via on_data_table_row_selected (the
        # DataTable consumes Enter). Do NOT also bind "enter" here or it
        # double-fires -> two \r sent into the session.
        Binding("1", "send('1')", "Send 1"),
        Binding("2", "send('2')", "Send 2"),
        Binding("3", "send('3')", "Send 3"),
        Binding("n", "focus", "Go to tab"),
        Binding("space", "toggle", "Arm: off/safe/wild/insane"),
        Binding("a", "all", "Arm all"),
        Binding("d", "none", "Disarm all"),
        Binding("x", "hide", "Hide/show"),
        Binding("v", "audit_view", "Audit view", show=False),
        Binding("tab", "swarm_view", "Swarm view", priority=True),
        Binding("R", "restore", "Restore orphaned", show=True),
        Binding("W", "wipe", "Wipe orphaned", show=True),
        Binding("question_mark", "help", "Help", show=False),
        Binding("escape", "dismiss_view", "Back", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, dry_run: bool = False):
        super().__init__()
        self.dry_run = dry_run
        self.watcher: Watcher | None = None
        self._connection = None
        self._caffeinate = None
        self._row_sids: list[str] = []
        self._temp = 0.0          # reactor temperature (integrates toward pressure)
        self._tick = 0            # frame counter for the CRITICAL pulse
        self.reactor_off = bool(os.environ.get("RELAY_NO_REACTOR"))
        self._swarm_visible = False
        self._help_visible = False
        self._audit_visible = False
        self._swarm_db = None
        self._restore_armed = False
        self._wipe_armed = False
        self._quit_armed = False
        # Relay runs inside its own iTerm2 tab; know its bare session UUID so we
        # can tell "just me" from "sessions worth controlling". $ITERM_SESSION_ID
        # is "wXtYpZ:UUID"; the watcher keys sessions by the bare UUID.
        self._own_sid = os.environ.get("ITERM_SESSION_ID", "").split(":")[-1] or None

    def _controllable(self):
        """Sessions relay could actually act on: everything except its own tab."""
        if not self.watcher:
            return []
        return [i for i in self.watcher.sessions.values()
                if i.session_id != self._own_sid]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(BANNER, id="banner")
            yield Static("", id="subtitle")
            if not self.reactor_off:
                yield Static("", id="reactor")
            with Vertical(id="middle"):
                yield DataTable(id="grid", cursor_type="row", zebra_stripes=True)
                yield Static("", id="preview", markup=False)
            yield Static("", id="swarmview")
            yield Static(help_text(), id="helpview")
            yield Log(id="log", max_lines=200)
        yield Static(KEYBAR, id="keybar")

    def on_mount(self) -> None:
        # Prune audit entries older than the retention window, once, at launch.
        try:
            audit.prune_old()
        except Exception:
            pass
        try:
            _mc = swarmdb.connect()
            swarmdb.prune_messages(
                _mc, float(os.environ.get("RELAY_MSG_RETENTION_DAYS", "7")))
            _mc.close()
        except Exception:
            pass
        table = self.query_one(DataTable)
        table.add_columns("MODE", "STATUS", "↻", "UNIT", "ROLE", "TASK NOW",
                          "✓/⊘", "LAST DIRECTIVE")
        # Keep the Mac awake while open.
        if not os.environ.get("RELAY_NO_CAFFEINATE"):
            try:
                self._caffeinate = subprocess.Popen(
                    ["caffeinate", "-dimsu", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        # Launch the iTerm2 connection in the background; it shares this loop.
        self._conn_worker = self.run_worker(self._connect(), exclusive=True)
        self.set_interval(1.0, self._refresh)  # periodic repaint (ages, etc.)
        if not self.reactor_off:
            self.set_interval(0.5, self._tick_reactor)  # smooth heat + pulse

    async def _connect(self) -> None:
        # async_create() is designed to run inside an existing event loop
        # (the iTerm2 docs' apython/REPL case) - which is what Textual gives us.
        try:
            connection = await iterm2.Connection.async_create()
            self._connection = connection
            self.watcher = Watcher(
                connection,
                on_change=self._safe_refresh,
                dry_run=self.dry_run,
                own_sid=self._own_sid,
            )
            # One poll loop reads every visible/armed session every 2s.
            await self.watcher.start(interval=2.0)
        except Exception as e:
            self.query_one(Log).write_line(f"connection error: {e}")

    def _safe_refresh(self) -> None:
        # Watcher calls this from the same loop; schedule a repaint.
        try:
            self.call_later(self._refresh)
        except Exception:
            pass

    def _refresh(self) -> None:
        if not self.watcher:
            return
        table = self.query_one(DataTable)
        prev_sid = self._selected_sid()   # track by IDENTITY, not row index
        prev_row = table.cursor_row       # ...nearest occurrence wins (dups)
        table.clear()
        self._row_sids = []           # sid per row; None marks the divider row
        by_pos = lambda i: (i.window_idx, i.tab_idx)
        shown = sorted((i for i in self.watcher.sessions.values() if not i.hidden), key=by_pos)
        hidden = sorted((i for i in self.watcher.sessions.values() if i.hidden), key=by_pos)

        DIM = DIMMER   # dimmed phosphor for hidden rows

        def add(info, dim=False, attention=False):
            label, color = STATE_STYLE.get(info.state, ("? UNKNOWN", BRIGHT))
            if getattr(info, "stale", False):
                label, color = "▲ STALE", WARN
            glyph, mlabel, mcolor = MODE_STYLE.get(info.mode, (" ", "MANUAL", DIM))
            arm = f"{glyph} {mlabel}" if glyph.strip() else f"  {mlabel}"
            # Heartbeat: age since the screen last changed (LOC's find-the-tab
            # job is what `n` does better; freshness earns the column more).
            hb_ts = getattr(info, "_screen_changed_ts", 0) or 0
            wt = swarmlogic.fmt_age(time.time() - hb_ts) if hb_ts else "-"
            # ESCAPE terminal-derived text: a command/title containing '[' (e.g.
            # sed 's/[a-z]/x/', ls foo[1]) would otherwise be parsed as Textual
            # markup and either swallow text or raise MarkupError on render.
            raw_cmd = info.last_command[:46] + "…" if len(info.last_command) > 47 else info.last_command
            cmd = escape(raw_cmd) if raw_cmd else ""
            title = escape(info.title[:26])
            if info.session_id == self._own_sid:
                title = f"{title} [{DIMMER}](this panel)[/]"
            reg = (self.watcher.registry or {}).get(info.session_id)
            role = {"coordinator": "coord", "worker": "work"}.get(
                reg["role"], "") if reg else ""
            task_now = escape((reg["task_now"] or "")[:28]) if reg else ""
            a, e = info.n_approved, info.n_escalated
            if dim:
                arm = f"[{DIM}]{arm}[/]"
                label = f"[{DIM}]{label}[/]"
                title = f"[{DIM}]{title}[/]"
                cmd = f"[{DIM}]{cmd or '-'}[/]"
                counts = f"[{DIM}]{a}/{e}[/]" if (a or e) else f"[{DIM}]-[/]"
                role = f"[{DIM}]{role or '-'}[/]"
                task_now = f"[{DIM}]{task_now or '-'}[/]"
            else:
                arm = f"[{mcolor}]{arm}[/]"
                label = f"[{color}]{label}[/]"
                counts = (f"[{CYAN}]{a}[/][{DIM}]/[/][{DANGER}]{e}[/]"
                          if (a or e) else f"[{DIM}]-[/]")
                if cmd and info.state == "prompting":
                    # The held command is the one demanding your judgement.
                    cmd = f"[{DANGER}]{cmd}[/]"
                cmd = cmd or f"[{DIM}]-[/]"
                role = f"[{CYAN}]{role}[/]" if role else f"[{DIM}]-[/]"
                task_now = task_now or f"[{DIM}]-[/]"
            if attention:
                # The duplicate strip row: same data, unmissable name.
                title = f"[bold {DANGER}]‼ {title}[/]"
            table.add_row(arm, label, wt, title, role, task_now, counts, cmd)
            self._row_sids.append(info.session_id)

        def divider(text, color):
            table.add_row("", f"[{color}]▼▼▼[/]", "", f"[{color}]{text}[/]",
                          "", "", "", "")
            self._row_sids.append(None)        # divider: not selectable

        # NEEDS ACTION is a strip of DUPLICATE rows on top - the main list
        # below keeps its stable tab order ALWAYS. Rows must never teleport
        # between sections: the duplicate appears/disappears, the original
        # stays put (moving rows around cost the human their muscle memory).
        # Relay's own panel tab can never need action (relay never acts on
        # itself; its screen-state detection reads the TUI's own chrome).
        attention = [i for i in shown
                     if i.session_id != self._own_sid
                     and needs_action(i.state, getattr(i, "stale", False))]
        if attention:
            divider(f"── NEEDS ACTION ({len(attention)}) ──", DANGER)
            for info in attention:
                add(info, attention=True)
            divider("── SESSIONS ──", DIM)
        for info in shown:
            # Own row greyed out: it is display-only by design.
            add(info, dim=info.session_id == self._own_sid)
        if hidden:
            divider(f"── QUARANTINED ({len(hidden)}) ──", DIMMER)
            for info in hidden:
                add(info, dim=True)

        # sync log (append-only). watcher.log is capped at 200; track by a
        # monotonic id we stamp, not by len() (which stops growing once capped).
        log = self.query_one(Log)
        logshown = getattr(self, "_log_shown", 0)
        total = self.watcher.log_total if hasattr(self.watcher, "log_total") else len(self.watcher.log)
        new = self.watcher.log[-(total - logshown):] if total > logshown else []
        for line in new:
            log.write_line(line)
        self._log_shown = total
        # Restore cursor to the SAME session (by sid). A session in the
        # attention strip appears TWICE - keep the occurrence nearest to where
        # the cursor was, so it doesn't teleport between strip and main list.
        if self._row_sids:
            target = self._row_index_near(prev_sid, prev_row)
            target = self._nearest_selectable(target if target is not None
                                              else 0)
            if target is not None:
                table.move_cursor(row=target)
        # Status line: live armed-count + total approvals.
        sess = list(self.watcher.sessions.values())
        armed = sum(1 for i in sess if i.active)
        appr = sum(i.n_approved for i in sess)
        esc = sum(i.n_escalated for i in sess)
        dry = f" [bold {WARN}]◆ SIMULATION (dry-run)[/]" if self.dry_run else ""
        # Onboarding hints, in priority order: nothing to control -> point at
        # the preview panel; sessions present but none armed -> tell them Space.
        n_ctrl = len(self._controllable())
        orphans = getattr(self.watcher, "orphan_count", 0)
        if orphans:
            hint = (f"  [{DANGER}]· {orphans} task-owner(s) dead - press R twice "
                    f"to restore, W twice to wipe[/]")
        elif n_ctrl == 0:
            hint = f"  [{DIM}]· open another session to control (see panel ->)[/]"
        elif armed == 0:
            hint = f"  [{WARN}]· nothing armed - SPACE to arm a session, then walk away[/]"
        else:
            hint = ""
        # Attention counts: only the parts that are non-zero earn header space.
        # Own panel row excluded - it can never legitimately await a human.
        others = [i for i in sess if i.session_id != self._own_sid]
        # prompting AND blocked: the header must never contradict the strip.
        awaiting = sum(1 for i in others
                       if i.state in ("prompting", "blocked"))
        n_stale = sum(1 for i in others if getattr(i, "stale", False))
        queued_n = 0
        try:
            if self._swarm_db is None:
                self._swarm_db = swarmdb.connect()
            queued_n = len(swarmdb.undelivered(self._swarm_db))
        except Exception:
            pass
        attn = ""
        if awaiting:
            attn += f" [{DIM}]·[/] [{WARN}]{awaiting} awaiting[/]"
        if n_stale:
            attn += f" [{DIM}]·[/] [{DANGER}]{n_stale} stale[/]"
        if queued_n:
            attn += f" [{DIM}]·[/] [{CYAN}]{queued_n} msgs queued[/]"
        self.query_one("#subtitle", Static).update(
            f"[{DIM}]RELAY · SESSION CONTROL ·[/] "
            f"[{BRIGHT}]{len(sess)} units[/] [{DIM}]·[/] "
            f"[{WARN}]{armed} armed[/] [{DIM}]·[/] "
            f"[{CYAN}]{appr}✓[/] [{DANGER}]{esc}⊘[/]{attn}{dry}{hint}")
        self._update_preview()
        if self._swarm_visible:
            self._render_swarm_view()

    def _tick_reactor(self) -> None:
        """Integrate reactor temp toward current pressure and render the meter.
        Rises quickly (things heating up grabs attention), vents slowly (a calm
        you have to earn). Runs every 0.5s for a smooth bar + CRITICAL pulse."""
        if self.reactor_off or not self.watcher:
            return
        self._tick += 1
        target = reactor_pressure(
            i for i in self.watcher.sessions.values()
            if i.session_id != self._own_sid)   # own chrome is not pressure
        # asymmetric easing: heat in fast, vent slow.
        rate = 0.35 if target > self._temp else 0.06
        self._temp += (target - self._temp) * rate
        if self._temp < 0.02:
            self._temp = 0.0

        label, color, pulse = reactor_band(self._temp)
        # 10-cell bar; full scale ~= CRITICAL threshold.
        filled = max(0, min(10, round(self._temp / 8.0 * 10)))
        bar = "▰" * filled + "▱" * (10 - filled)
        # CRITICAL pulses: dim the whole line every other half-second.
        dimmed = pulse and (self._tick % 2 == 0)
        c = EMBER if dimmed else color
        # Mascot inputs: recent log activity = "working" (~3s afterglow);
        # any non-own session holding a prompt = alarmed.
        total = getattr(self.watcher, "log_total", 0)
        if total != getattr(self, "_mascot_seen_log", -1):
            self._mascot_seen_log = total
            self._mascot_active_until = self._tick + 6
        alarmed = any(i.state == "prompting"
                      and i.session_id != self._own_sid
                      for i in self.watcher.sessions.values())
        face = mascot_frame(
            self._tick, label, alarmed=alarmed,
            working=self._tick < getattr(self, "_mascot_active_until", 0))
        fc = DANGER if alarmed else color
        try:
            self.query_one("#reactor", Static).update(
                f"[{c}]CORE TEMP[/] [{color}]{bar}[/]  [{c}]{label}[/]"
                f"   [{fc}]{face}[/]")
        except Exception:
            pass

    def _update_preview(self) -> None:
        if not self.watcher:
            return
        preview = self.query_one("#preview", Static)
        # Onboarding: nothing to control (only relay's own tab) -> teach instead
        # of showing a blank/[no signal] pane.
        if not self._controllable():
            preview.update(getting_started_panel(preview.size.width - 2))
            return
        sid = self._selected_sid()
        info = self.watcher.sessions.get(sid) if sid else None
        if not info:
            preview.update("")
            return
        if self._audit_visible and sid != self._own_sid:
            preview.update(audit_view_text(
                audit.read_tail(), info.title, preview.size.width - 2))
            return
        if sid == self._own_sid:
            # Previewing relay's own tab would mirror the whole UI into itself
            # (an infinite RELAY-inside-RELAY), and relay never acts on its own
            # session - so show relay's OWN status + orientation instead.
            ctrl = self._controllable()
            preview.update(relay_self_panel(
                preview.size.width - 2,
                units=len(ctrl),
                armed=sum(1 for i in ctrl if i.active),
                approvals=sum(i.n_approved for i in ctrl),
                escalations=sum(i.n_escalated for i in ctrl),
                orphans=getattr(self.watcher, "orphan_count", 0),
                db_path=swarmdb.default_path(),
                dry_run=self.dry_run))
            return
        mode = {"safe": "SAFE", "wild": "WILD", "insane": "INSANE"}.get(info.mode, "MANUAL")
        loc = "QUARANTINED" if info.hidden else "ACTIVE"
        # Size the frame to the pane width so the header bars span the full pane.
        w = max(40, preview.size.width - 2)
        bar = "═" * w
        # markup=False on this pane (terminal content renders literally), so the
        # header is plain text - the phosphor-green comes from CSS.
        # Why this session needs you, when it does (plain text, like the pane).
        attn = ""
        if info.state == "prompting":
            why = info.last_command[:w - 14] if info.last_command \
                else "a question / unreadable prompt"
            attn = f" ‼ AWAITING: {why}\n"
        elif getattr(info, "stale", False):
            attn = " ⧗ STALE: no visible progress\n"
        elif info.state == "blocked":
            attn = " ⊘ LOCKED\n"
        header = (f"╔{bar}╗\n"
                  f" ▓ LIVE FEED // {info.title[:w-16]}\n"
                  f" MODE:{mode}  LINK:{loc}  "
                  f"CLEARED:{info.n_approved}  HELD:{info.n_escalated}\n"
                  f"{attn}"
                  f"╚{bar}╝\n")
        body = "\n".join(info.last_screen) if info.last_screen else "[ no signal ]"
        preview.update(header + body)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Live-update the preview as the cursor moves between rows, and pull a
        # fresh screen for the now-selected session so it reflects reality NOW.
        self._update_preview()
        sid = self._selected_sid()
        if sid and sid != self._own_sid and self.watcher:
            # exclusive=True: fast j/k scrolling cancels the prior pull instead
            # of piling up N concurrent iTerm2 reads on the shared connection.
            # Skip relay's own tab: reading its screen is what feeds the mirror.
            self.run_worker(self._pull_and_show(sid), exclusive=True,
                            group="preview-pull")

    async def _pull_and_show(self, sid: str) -> None:
        await self.watcher.refresh_screen(sid)
        if self._selected_sid() == sid:   # still selected after the await
            self._update_preview()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable consumes Enter for its own row-select before our binding
        # sees it, so this is the real Enter handler: manually send Enter.
        self.action_send_enter()

    def _selected_sid(self) -> str | None:
        table = self.query_one(DataTable)
        r = table.cursor_row
        if 0 <= r < len(self._row_sids):
            return self._row_sids[r]
        return None

    # --- manual send (works even in dry-run / un-armed: deliberate human act) --
    def action_send_enter(self) -> None:
        self._manual_send("\r")

    def action_send(self, key: str) -> None:
        self._manual_send(key)

    def _manual_send(self, text: str) -> None:
        sid = self._selected_sid()
        if sid and self.watcher:
            self.run_worker(self.watcher.send_keys(sid, text), exclusive=False)

    # --- navigate to the real iTerm2 tab --------------------------------------
    def action_focus(self) -> None:
        sid = self._selected_sid()
        if sid and self.watcher:
            self.run_worker(self.watcher.focus_session(sid), exclusive=False)

    # --- arming ---------------------------------------------------------------
    def action_toggle(self) -> None:
        sid = self._selected_sid()
        if not (sid and self.watcher):
            return
        if sid == self._own_sid:
            self.query_one(Log).write_line(
                "arm: relay never acts on its own panel tab - nothing to arm here")
            return
        self.watcher.toggle(sid)
        self._refresh()

    def action_all(self) -> None:
        if self.watcher:
            self.watcher.set_all(True)
            self._refresh()

    def action_none(self) -> None:
        if self.watcher:
            self.watcher.set_all(False)
            self._refresh()

    # --- swarm view (TAB toggles a full-width kanban board) -------------------
    def action_swarm_view(self) -> None:
        if self._help_visible:
            self.action_help()            # close help first; TAB then flips
        self._swarm_visible = not self._swarm_visible
        on = self._swarm_visible
        self.query_one("#middle").styles.display = "none" if on else "block"
        self.query_one("#log").styles.display = "none" if on else "block"
        self.query_one("#swarmview").styles.display = "block" if on else "none"
        if on:
            self._render_swarm_view()

    # --- audit view (v): the preview pane shows this session's decisions -----
    def action_audit_view(self) -> None:
        self._audit_visible = not self._audit_visible
        self._update_preview()

    # --- ESC: universal "take me back" for every overlay ----------------------
    def action_dismiss_view(self) -> None:
        if self._help_visible:
            self.action_help()
        elif self._audit_visible:
            self.action_audit_view()
        elif self._swarm_visible:
            self.action_swarm_view()

    # --- help overlay (?) -----------------------------------------------------
    def action_help(self) -> None:
        if self._swarm_visible and not self._help_visible:
            self.action_swarm_view()      # leave the swarm view first
        self._help_visible = not self._help_visible
        on = self._help_visible
        self.query_one("#middle").styles.display = "none" if on else "block"
        self.query_one("#log").styles.display = "none" if on else "block"
        self.query_one("#helpview").styles.display = "block" if on else "none"

    def _render_swarm_view(self) -> None:
        import time as _time
        try:
            if self._swarm_db is None:
                self._swarm_db = swarmdb.connect()
            sessions = [dict(r) for r in swarmdb.list_sessions(self._swarm_db)]
            tasks = [dict(r) for r in swarmdb.list_tasks(self._swarm_db)]
            # 200 not 8: the interaction map aggregates history; the feed
            # itself still shows only the last 8 lines.
            msgs = [dict(r) for r in swarmdb.message_history(self._swarm_db,
                                                             limit=200)]
            stale, activity = set(), {}
            reg = (self.watcher.registry or {}) if self.watcher else {}
            for sid, row in reg.items():
                info = self.watcher.sessions.get(sid)
                if info is None:
                    continue
                if getattr(info, "stale", False):
                    stale.add(row["name"])
                ts = getattr(info, "_screen_changed_ts", 0) or 0
                if ts:
                    activity[row["name"]] = ts
            w = max(60, self.query_one("#swarmview").size.width - 4)
            text = swarmlogic.render_swarm(sessions, tasks, msgs,
                                           _time.time(), width=w,
                                           stale=stale, activity=activity)
        except Exception as e:
            text = f"swarm db unavailable: {e}"
        self.query_one("#swarmview", Static).update(text)

    # --- hide / show ----------------------------------------------------------
    def action_hide(self) -> None:
        sid = self._selected_sid()
        if sid and self.watcher:
            prev_row = self.query_one(DataTable).cursor_row
            self.watcher.toggle_hidden(sid)
            self._refresh()
            # Keep the cursor on the row we just acted on (it moved sections)
            # - nearest occurrence, same rule as _refresh (duplicate rows).
            target = self._row_index_near(sid, prev_row)
            if target is not None:
                self.query_one(DataTable).move_cursor(row=target)
                self._update_preview()

    def action_unhide_all(self) -> None:
        if self.watcher:
            self.watcher.unhide_all()
            self._refresh()

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor(+1)

    def _move_cursor(self, step: int) -> None:
        # Move one row, skipping the non-selectable divider (sid is None).
        table = self.query_one(DataTable)
        n = len(self._row_sids)
        if n == 0:
            return
        r = table.cursor_row + step
        while 0 <= r < n and self._row_sids[r] is None:
            r += step
        if 0 <= r < n:
            table.move_cursor(row=r)

    def _row_index_near(self, sid, near_row: int):
        """Row index of `sid` nearest to `near_row` - a session in the
        NEEDS ACTION strip appears twice, and the cursor must never teleport
        to the other occurrence. None for divider sids (None) or absent."""
        if sid is None:
            return None
        occ = [i for i, s in enumerate(self._row_sids) if s == sid]
        if not occ:
            return None
        return min(occ, key=lambda i: abs(i - near_row))

    def _nearest_selectable(self, row: int):
        """Return the nearest row index whose sid is not the divider (None),
        searching outward from `row`. None if there are no selectable rows."""
        n = len(self._row_sids)
        if n == 0:
            return None
        row = max(0, min(row, n - 1))
        for d in range(n):
            for r in (row - d, row + d):
                if 0 <= r < n and self._row_sids[r] is not None:
                    return r
        return None

    # Window (seconds) to land the confirming second press of R / W.
    _CONFIRM_WINDOW = 5.0

    # --- restore (respawn dead task-owner workers) -----------------------
    def action_restore(self) -> None:
        if not getattr(self.watcher, "orphan_count", 0):
            self.query_one(Log).write_line(
                "restore: nothing orphaned - R acts only on CLOSED sessions "
                "(tab gone) that still own tasks")
            return
        if not self._restore_armed:
            self._restore_armed = True
            self.set_timer(self._CONFIRM_WINDOW,
                           lambda: setattr(self, "_restore_armed", False))
            n = self.watcher.orphan_count
            self.query_one(Log).write_line(
                f"restore ARMED: press R again to respawn {n} dead worker(s) "
                f"(auto-cancels in {int(self._CONFIRM_WINDOW)}s)")
            return
        self._restore_armed = False
        self._shell_verb("restore", "respawning dead workers")

    # --- wipe (delete orphaned task-owner work) --------------------------
    def action_wipe(self) -> None:
        if not getattr(self.watcher, "orphan_count", 0):
            self.query_one(Log).write_line(
                "wipe: nothing orphaned - W deletes work owned by CLOSED "
                "sessions. To clear a whole project use: relay wipe "
                "--project <p> --all")
            return
        if not self._wipe_armed:
            self._wipe_armed = True
            self.set_timer(self._CONFIRM_WINDOW,
                           lambda: setattr(self, "_wipe_armed", False))
            n = self.watcher.orphan_count
            self.query_one(Log).write_line(
                f"wipe ARMED: press W again to DELETE {n} dead session(s)' work "
                f"(auto-cancels in {int(self._CONFIRM_WINDOW)}s)")
            return
        self._wipe_armed = False
        self._shell_verb("wipe", "deleting orphaned work")

    def _shell_verb(self, verb: str, doing: str) -> None:
        here = os.path.dirname(os.path.abspath(__file__))
        relay_bin = os.path.join(here, "..", "bin", "relay")
        try:
            subprocess.Popen([relay_bin, verb, "--yes"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.query_one(Log).write_line(f"{verb}: {doing}...")
        except Exception as e:
            self.query_one(Log).write_line(f"{verb} failed: {e}")

    def _quit_stakes(self) -> str:
        """Counts for quit_stakes_text, best-effort (a DB hiccup must never
        block quitting - unknown counts as zero, which only makes quitting
        EASIER, the safe direction for a quit guard)."""
        n_armed = 0
        try:
            if self.watcher:
                n_armed = sum(1 for i in self.watcher.sessions.values()
                              if i.active)
        except Exception:
            pass
        n_queued = n_doing = 0
        try:
            if self._swarm_db is None:
                self._swarm_db = swarmdb.connect()
            n_queued = len(swarmdb.undelivered(self._swarm_db))
            n_doing = sum(1 for t in swarmdb.list_tasks(self._swarm_db)
                          if t["state"] == "doing")
        except Exception:
            pass
        return quit_stakes_text(n_armed, n_queued, n_doing)

    async def action_quit(self) -> None:
        # Double-press guard, but ONLY when quitting abandons something live
        # (same confirm pattern as R/W). An idle panel quits on a single q.
        stakes = self._quit_stakes()
        if stakes and not self._quit_armed:
            self._quit_armed = True
            self.set_timer(self._CONFIRM_WINDOW,
                           lambda: setattr(self, "_quit_armed", False))
            self.query_one(Log).write_line(
                f"quit ARMED ({stakes}): auto-approval and delivery stop on "
                f"quit - press q again to confirm "
                f"(auto-cancels in {int(self._CONFIRM_WINDOW)}s)")
            return
        # Signal the poll loop (interruptible - wakes immediately), then WAIT
        # for the connection worker to actually finish its teardown (restore
        # every title, close the iTerm2 connection) before we exit(). A blind
        # sleep raced that teardown: exit() cancels the worker mid-restore, so
        # tabs kept their prefixes. Bounded so a wedged connection can't hang
        # quit; on timeout we exit anyway (best-effort residue, documented).
        if self.watcher:
            try:
                await self.watcher.stop()
            except Exception:
                pass
        worker = getattr(self, "_conn_worker", None)
        if worker is not None:
            try:
                await asyncio.wait_for(worker.wait(), timeout=3.0)
            except Exception:
                pass
        if self._caffeinate:
            try:
                self._caffeinate.terminate()
            except Exception:
                pass
        self.exit()


def acquire_singleton_lock(path=None):
    """Take an exclusive advisory lock so only ONE relay TUI runs at a time.
    Two panels would each poll and deliver every queued swarm message, typing
    every wake-up into its target twice. Returns a held handle on success
    (keep it alive for the process lifetime), or None if another relay already
    holds the lock. Best-effort: if fcntl is unavailable, returns a truthy
    sentinel (cannot enforce, but must not block startup)."""
    p = path or os.path.expanduser(
        os.environ.get("RELAY_LOCK", "~/.relay/relay.lock"))
    try:
        import fcntl
    except Exception:  # pragma: no cover - non-POSIX
        return "no-fcntl"
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        fh = open(p, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    except Exception:
        return "lock-error"   # can't lock for some other reason: don't block
    try:
        fh.write(str(os.getpid()))
        fh.flush()
    except Exception:
        pass
    return fh


def main() -> None:
    lock = acquire_singleton_lock()
    if lock is None:
        sys.stderr.write(
            "relay: another relay panel is already running.\n"
            "Two panels would double-deliver swarm messages (every wake-up "
            "typed twice).\nQuit the other one first (q), or set RELAY_LOCK to "
            "a different path if you\nreally mean to run two.\n")
        sys.exit(3)
    args = sys.argv[1:]
    dry = any(a in ("--dry-run", "--dryrun", "-n") for a in args)
    # Reject unknown flags - a typo'd '--dry-run' must NOT silently run LIVE
    # (auto-approving for real), which is the opposite of the intended safety.
    unknown = [a for a in args if a not in ("--dry-run", "--dryrun", "-n")]
    if unknown:
        sys.stderr.write(
            f"relay: unknown argument(s): {' '.join(unknown)}\n"
            f"Did you mean --dry-run? Refusing to start so a typo can't run live.\n")
        sys.exit(2)
    # mouse=False: relay is keyboard-first, and capturing the mouse steals
    # iTerm2's own gestures (two-finger swipe between tabs, native scroll).
    # The terminal keeps its input; relay keeps its keys.
    RelayApp(dry_run=dry).run(mouse=False)


if __name__ == "__main__":
    main()
