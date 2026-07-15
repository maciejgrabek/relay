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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import iterm2  # noqa: E402
from rich.markup import escape  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Vertical  # noqa: E402
from textual.widgets import DataTable, Footer, Static, Log  # noqa: E402

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

# Retro-terminal state labels - meaning kept obvious, dressed for the CRT theme.
STATE_STYLE = {
    "idle":      ("◌ STANDBY",  "#2a7d4f"),   # dim phosphor
    "working":   ("▸ ACTIVE",   "#3aff7a"),   # bright phosphor
    "prompting": ("‼ AWAITING", "#ffb000"),   # amber alert
    "blocked":   ("⊘ LOCKED",   "#ff5555"),   # red lockdown
    "cleared":   ("✓ CLEARED",  "#41ffd0"),   # cyan-green ok
}

# Per-mode arm chrome: glyph + label + color. Glyphs must be SINGLE-WIDTH -
# emoji (🔥/⚡) render double-width and shift the column out of alignment. These
# box/geometric symbols are all 1 cell wide.
MODE_STYLE = {
    "off":    ("○", "MANUAL",  "#2a7d4f"),
    "safe":   ("◉", "SAFE",    "#3aff7a"),
    "wild":   ("▲", "WILD",    "#ffb000"),
    "insane": ("✦", "INSANE",  "#ff5555"),
}


def reactor_pressure(sessions) -> float:
    """Instantaneous 'heat input' from how much is happening UNATTENDED right
    now (0.0+). The reactor heats toward this and vents below it - so when you
    engage / sessions go idle, temp falls. Pure function for testing.

      + armed tabs running unattended (more for hotter modes)
      + sessions waiting on the human (unacknowledged escalations)
      + recent auto-approval activity
    """
    p = 0.0
    for i in sessions:
        if i.mode == "insane":
            p += 0.9
        elif i.mode == "wild":
            p += 0.6
        elif i.mode == "safe":
            p += 0.3
        if i.state == "blocked":      # something needs a human, unhandled
            p += 1.2
        elif i.state == "working" and i.active:
            p += 0.4
    return p


# Reactor temperature bands -> (label, color, pulsing?).
def reactor_band(temp: float):
    if temp >= 8.0:
        return ("☢ CRITICAL", "#ff5555", True)
    if temp >= 4.0:
        return ("⚠ ELEVATED", "#ffb000", False)
    if temp >= 1.0:
        return ("◷ WARM", "#3aff7a", False)
    return ("STABLE", "#2a7d4f", False)


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


class RelayApp(App):
    CSS = """
    /* phosphor-green CRT terminal */
    Screen { background: #020a04; color: #3aff7a; }
    #banner { color: #3aff7a; text-style: bold; height: auto; padding: 1 2 0 2; }
    #subtitle { color: #2a7d4f; height: 1; padding: 0 2; }
    #reactor { height: 1; padding: 0 2; }
    /* Stacked layout: the list on top, the live terminal feed below - both
       full width so the 8-column list and 80-col terminal output each get the
       room they need (side-by-side left the preview too narrow to read). */
    #middle { height: 1fr; }
    DataTable {
        width: 1fr; height: 2fr; background: #020a04; color: #3aff7a;
        border-bottom: solid #1d5c38;
    }
    DataTable > .datatable--cursor { background: #0d3a22; color: #6effa0; text-style: bold; }
    DataTable > .datatable--header {
        background: #061a0e; color: #ffb000; text-style: bold;
    }
    DataTable > .datatable--odd-row { background: #04120a; }
    DataTable > .datatable--even-row { background: #020a04; }
    #preview {
        width: 1fr; height: 3fr;
        background: #010602; color: #2fc866;
        padding: 0 1;
    }
    #log {
        height: 5; border-top: solid #1d5c38;
        background: #010602; color: #2a7d4f;
    }
    #swarmview {
        display: none; height: 1fr; padding: 0 2;
        background: #010602; color: #2fc866;
    }
    Footer { background: #061a0e; color: #2a7d4f; }
    Footer > .footer--key { background: #0d3a22; color: #ffb000; text-style: bold; }
    Footer > .footer--description { color: #3aff7a; }
    """
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
        Binding("tab", "swarm_view", "Swarm view", priority=True),
        Binding("R", "restore", "Restore orphaned", show=True),
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
        self._swarm_db = None
        self._restore_armed = False
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
            yield Static("", id="swarmview", markup=False)
            yield Log(id="log", max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        # Prune audit entries older than the retention window, once, at launch.
        try:
            audit.prune_old()
        except Exception:
            pass
        try:
            import db as _swarmdb
            _swarmdb.prune_messages(
                _swarmdb.connect(),
                float(os.environ.get("RELAY_MSG_RETENTION_DAYS", "7")))
        except Exception:
            pass
        table = self.query_one(DataTable)
        table.add_columns("MODE", "STATUS", "LOC", "UNIT", "ROLE", "TASK NOW",
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
        table.clear()
        self._row_sids = []           # sid per row; None marks the divider row
        by_pos = lambda i: (i.window_idx, i.tab_idx)
        shown = sorted((i for i in self.watcher.sessions.values() if not i.hidden), key=by_pos)
        hidden = sorted((i for i in self.watcher.sessions.values() if i.hidden), key=by_pos)

        DIM = "#1d5c38"   # dimmed phosphor for hidden rows

        def add(info, dim=False):
            label, color = STATE_STYLE.get(info.state, ("? UNKNOWN", "#3aff7a"))
            if getattr(info, "stale", False):
                label, color = "▲ STALE", "#ffb000"
            glyph, mlabel, mcolor = MODE_STYLE.get(info.mode, (" ", "MANUAL", DIM))
            arm = f"{glyph} {mlabel}" if glyph.strip() else f"  {mlabel}"
            wt = f"{info.window_idx}.{info.tab_idx}"
            # ESCAPE terminal-derived text: a command/title containing '[' (e.g.
            # sed 's/[a-z]/x/', ls foo[1]) would otherwise be parsed as Textual
            # markup and either swallow text or raise MarkupError on render.
            raw_cmd = info.last_command[:46] + "…" if len(info.last_command) > 47 else info.last_command
            cmd = escape(raw_cmd) if raw_cmd else ""
            title = escape(info.title[:26])
            if info.session_id == self._own_sid:
                title = f"{title} [#1d5c38](this panel)[/]"
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
                counts = (f"[#41ffd0]{a}[/][{DIM}]/[/][#ff5555]{e}[/]"
                          if (a or e) else f"[{DIM}]-[/]")
                cmd = cmd or f"[{DIM}]-[/]"
                role = f"[#41ffd0]{role}[/]" if role else f"[{DIM}]-[/]"
                task_now = task_now or f"[{DIM}]-[/]"
            table.add_row(arm, label, wt, title, role, task_now, counts, cmd)
            self._row_sids.append(info.session_id)

        for info in shown:
            add(info)
        if hidden:
            table.add_row("", f"[#1d5c38]▼▼▼[/]", "",
                          f"[#1d5c38]── QUARANTINED ({len(hidden)}) ──[/]",
                          "", "", "", "")
            self._row_sids.append(None)        # divider: not selectable
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
        # Restore cursor to the SAME session (by sid), skipping the divider.
        if self._row_sids:
            target = self._row_sids.index(prev_sid) if prev_sid in self._row_sids else 0
            target = self._nearest_selectable(target)
            if target is not None:
                table.move_cursor(row=target)
        # Status line: live armed-count + total approvals.
        sess = list(self.watcher.sessions.values())
        armed = sum(1 for i in sess if i.active)
        appr = sum(i.n_approved for i in sess)
        esc = sum(i.n_escalated for i in sess)
        dry = " [bold #ffb000]◆ SIMULATION (dry-run)[/]" if self.dry_run else ""
        # Onboarding hints, in priority order: nothing to control -> point at
        # the preview panel; sessions present but none armed -> tell them Space.
        n_ctrl = len(self._controllable())
        orphans = getattr(self.watcher, "orphan_count", 0)
        if orphans:
            hint = (f"  [#ff5555]· {orphans} task-owner(s) dead - press R to "
                    f"restore, or run 'relay clean'[/]")
        elif n_ctrl == 0:
            hint = "  [#2a7d4f]· open another session to control (see panel ->)[/]"
        elif armed == 0:
            hint = "  [#ffb000]· nothing armed - SPACE to arm a session, then walk away[/]"
        else:
            hint = ""
        self.query_one("#subtitle", Static).update(
            f"[#2a7d4f]RELAY · SESSION CONTROL ·[/] "
            f"[#3aff7a]{len(sess)} units[/] [#2a7d4f]·[/] "
            f"[#ffb000]{armed} armed[/] [#2a7d4f]·[/] "
            f"[#41ffd0]{appr}✓[/] [#ff5555]{esc}⊘[/]{dry}{hint}")
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
        target = reactor_pressure(self.watcher.sessions.values())
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
        c = "#7a1d1d" if dimmed else color
        try:
            self.query_one("#reactor", Static).update(
                f"[{c}]CORE TEMP[/] [{color}]{bar}[/]  [{c}]{label}[/]")
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
        if sid == self._own_sid:
            # Previewing relay's own tab would mirror the whole UI into itself
            # (an infinite RELAY-inside-RELAY). Relay also never acts on its own
            # session, so there is nothing to watch here.
            w = max(40, preview.size.width - 2)
            bar = "═" * w
            preview.update(
                f"╔{bar}╗\n"
                f" ▓ THIS IS THE RELAY PANEL\n"
                f"╚{bar}╝\n"
                "\n"
                " Relay does not monitor or act on its own tab - there is\n"
                " nothing to preview here. Move the cursor to another session\n"
                " to see its live terminal feed.\n")
            return
        mode = {"safe": "SAFE", "wild": "WILD", "insane": "INSANE"}.get(info.mode, "MANUAL")
        loc = "QUARANTINED" if info.hidden else "ACTIVE"
        # Size the frame to the pane width so the header bars span the full pane.
        w = max(40, preview.size.width - 2)
        bar = "═" * w
        # markup=False on this pane (terminal content renders literally), so the
        # header is plain text - the phosphor-green comes from CSS.
        header = (f"╔{bar}╗\n"
                  f" ▓ LIVE FEED // {info.title[:w-16]}\n"
                  f" MODE:{mode}  LINK:{loc}  "
                  f"CLEARED:{info.n_approved}  HELD:{info.n_escalated}\n"
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
        if sid and self.watcher:
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
        self._swarm_visible = not self._swarm_visible
        on = self._swarm_visible
        self.query_one("#middle").styles.display = "none" if on else "block"
        self.query_one("#log").styles.display = "none" if on else "block"
        self.query_one("#swarmview").styles.display = "block" if on else "none"
        if on:
            self._render_swarm_view()

    def _render_swarm_view(self) -> None:
        import time as _time
        try:
            if self._swarm_db is None:
                self._swarm_db = swarmdb.connect()
            sessions = [dict(r) for r in swarmdb.list_sessions(self._swarm_db)]
            tasks = [dict(r) for r in swarmdb.list_tasks(self._swarm_db)]
            msgs = [dict(r) for r in swarmdb.message_history(self._swarm_db,
                                                             limit=8)]
            w = max(60, self.query_one("#swarmview").size.width - 4)
            text = swarmlogic.render_swarm(sessions, tasks, msgs,
                                           _time.time(), width=w)
        except Exception as e:
            text = f"swarm db unavailable: {e}"
        self.query_one("#swarmview", Static).update(text)

    # --- hide / show ----------------------------------------------------------
    def action_hide(self) -> None:
        sid = self._selected_sid()
        if sid and self.watcher:
            self.watcher.toggle_hidden(sid)
            self._refresh()
            # Keep the cursor on the row we just acted on (it moved sections).
            if sid in self._row_sids:
                self.query_one(DataTable).move_cursor(row=self._row_sids.index(sid))
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

    # --- restore (respawn dead task-owner workers) -----------------------
    def action_restore(self) -> None:
        if not getattr(self.watcher, "orphan_count", 0):
            return
        if not self._restore_armed:
            self._restore_armed = True
            self.set_timer(3.0, lambda: setattr(self, "_restore_armed", False))
            self.query_one(Log).write_line(
                "restore: press R again within 3s to respawn dead workers")
            return
        self._restore_armed = False
        here = os.path.dirname(os.path.abspath(__file__))
        relay_bin = os.path.join(here, "..", "bin", "relay")
        try:
            subprocess.Popen([relay_bin, "restore", "--yes"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.query_one(Log).write_line("restore: launching dead workers...")
        except Exception as e:
            self.query_one(Log).write_line(f"restore failed: {e}")

    async def action_quit(self) -> None:
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
    RelayApp(dry_run=dry).run()


if __name__ == "__main__":
    main()
