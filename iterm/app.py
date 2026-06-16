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
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.widgets import DataTable, Footer, Static, Log  # noqa: E402

import audit  # noqa: E402
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


class RelayApp(App):
    CSS = """
    /* phosphor-green CRT terminal */
    Screen { background: #020a04; color: #3aff7a; }
    #banner { color: #3aff7a; text-style: bold; height: auto; padding: 1 2 0 2; }
    #subtitle { color: #2a7d4f; height: 1; padding: 0 2; }
    #reactor { height: 1; padding: 0 2; }
    #middle { height: 1fr; }
    DataTable {
        width: 2fr; height: 1fr; background: #020a04; color: #3aff7a;
        border-right: solid #1d5c38;
    }
    DataTable > .datatable--cursor { background: #0d3a22; color: #6effa0; text-style: bold; }
    DataTable > .datatable--header {
        background: #061a0e; color: #ffb000; text-style: bold;
    }
    DataTable > .datatable--odd-row { background: #04120a; }
    DataTable > .datatable--even-row { background: #020a04; }
    #preview {
        width: 3fr; height: 1fr;
        background: #010602; color: #2fc866;
        padding: 0 1;
    }
    #log {
        height: 7; border-top: solid #1d5c38;
        background: #010602; color: #2a7d4f;
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

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(BANNER, id="banner")
            yield Static("", id="subtitle")
            if not self.reactor_off:
                yield Static("", id="reactor")
            with Horizontal(id="middle"):
                yield DataTable(id="grid", cursor_type="row", zebra_stripes=True)
                yield Static("", id="preview", markup=False)
            yield Log(id="log", max_lines=200)
        yield Footer()

    def on_mount(self) -> None:
        # Prune audit entries older than the retention window, once, at launch.
        try:
            audit.prune_old()
        except Exception:
            pass
        table = self.query_one(DataTable)
        table.add_columns("MODE", "STATUS", "LOC", "UNIT", "✓/⊘", "LAST DIRECTIVE")
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
            glyph, mlabel, mcolor = MODE_STYLE.get(info.mode, (" ", "MANUAL", DIM))
            arm = f"{glyph} {mlabel}" if glyph.strip() else f"  {mlabel}"
            wt = f"{info.window_idx}.{info.tab_idx}"
            # ESCAPE terminal-derived text: a command/title containing '[' (e.g.
            # sed 's/[a-z]/x/', ls foo[1]) would otherwise be parsed as Textual
            # markup and either swallow text or raise MarkupError on render.
            raw_cmd = info.last_command[:46] + "…" if len(info.last_command) > 47 else info.last_command
            cmd = escape(raw_cmd) if raw_cmd else ""
            title = escape(info.title[:26])
            a, e = info.n_approved, info.n_escalated
            if dim:
                arm = f"[{DIM}]{arm}[/]"
                label = f"[{DIM}]{label}[/]"
                title = f"[{DIM}]{title}[/]"
                cmd = f"[{DIM}]{cmd or '-'}[/]"
                counts = f"[{DIM}]{a}/{e}[/]" if (a or e) else f"[{DIM}]-[/]"
            else:
                arm = f"[{mcolor}]{arm}[/]"
                label = f"[{color}]{label}[/]"
                counts = (f"[#41ffd0]{a}[/][{DIM}]/[/][#ff5555]{e}[/]"
                          if (a or e) else f"[{DIM}]-[/]")
                cmd = cmd or f"[{DIM}]-[/]"
            table.add_row(arm, label, wt, title, counts, cmd)
            self._row_sids.append(info.session_id)

        for info in shown:
            add(info)
        if hidden:
            table.add_row("", f"[#1d5c38]▼▼▼[/]", "",
                          f"[#1d5c38]── QUARANTINED ({len(hidden)}) ──[/]", "", "")
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
        self.query_one("#subtitle", Static).update(
            f"[#2a7d4f]RELAY · SESSION CONTROL ·[/] "
            f"[#3aff7a]{len(sess)} units[/] [#2a7d4f]·[/] "
            f"[#ffb000]{armed} armed[/] [#2a7d4f]·[/] "
            f"[#41ffd0]{appr}✓[/] [#ff5555]{esc}⊘[/]{dry}")
        self._update_preview()

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
        sid = self._selected_sid()
        info = self.watcher.sessions.get(sid) if sid else None
        if not info:
            preview.update("")
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
        if sid and self.watcher:
            # exclusive=True: fast j/k scrolling cancels the prior pull instead
            # of piling up N concurrent iTerm2 reads on the shared connection.
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

    async def action_quit(self) -> None:
        # Signal the poll loop (interruptible - wakes immediately), give it a
        # brief moment to fall out of the loop and close the iTerm2 connection.
        if self.watcher:
            try:
                await self.watcher.stop()
                await asyncio.sleep(0.05)
            except Exception:
                pass
        if self._caffeinate:
            try:
                self._caffeinate.terminate()
            except Exception:
                pass
        self.exit()


def main() -> None:
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
