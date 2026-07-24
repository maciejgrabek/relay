"""Relay-iTerm watcher - owns the iTerm2 connection and drives the gates.

One asyncio task per session streams screen updates (ScreenStreamer.async_get
blocks until the screen changes). On each update for an *active* session we run
the gate pipeline and act: INJECT sends Enter, NOTIFY pings the human, NONE is
ignored. NOTIFY fires for ANY session (active or not) so you always hear when a
tab needs you; INJECT only happens for sessions you've toggled active.

This module is UI-agnostic: it maintains an in-memory `sessions` dict and calls
optional callbacks. The Textual TUI subscribes to it; a headless mode can too.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import iterm2

import audit
import db as swarmdb
import swarm
import statusbar as statusbar_mod
import config as relay_config
import titles
import timers as timers_mod
from gates import (classify, Action, Decision, reconstruct_lines,
                   detect_state, DANGEROUS_COMMAND)

# Relay's own tab title, set by design. Without this iTerm2 job-derives the
# title from relay's `caffeinate` child - accurate, but not what the panel IS.
OWN_TAB_NAME = "RELAY CONSOLE"

# Login shells iTerm2 may report as a tab's foreground job. When one of these is
# in front, no program (Claude) is running in the tab.
_SHELL_JOBS = frozenset({
    "zsh", "bash", "sh", "fish", "dash", "ksh", "tcsh", "csh", "ash", "login",
})


def _is_shell_job(job: str) -> bool:
    """True when iTerm2 reports a plain login shell as the tab's foreground job -
    i.e. Claude is NOT running in it. A leading '-' marks a login shell (e.g.
    '-zsh'); strip it before matching. Unknown/empty -> False, so an unreadable
    job never suppresses a real prompt (fail safe toward escalating)."""
    j = (job or "").strip().lstrip("-").lower()
    return j in _SHELL_JOBS


def _own_tab_profile(on: bool):
    """Write-only profile fragment for relay's own tab color: relay-phosphor
    green while the console runs, back to profile default on quit. Session-
    scoped (async_set_profile_properties) - the user's shared profile is
    never modified."""
    p = iterm2.LocalWriteOnlyProfile()
    p.set_use_tab_color(on)
    if on:
        p.set_tab_color(iterm2.Color(58, 255, 122))   # #3aff7a
    return p

# The always-on status-bar provider is statusbar_autolaunch.py. relay decides
# whether to render the badge ITSELF by whether the provider is INSTALLED
# (statusbar.provider_installed - the symlink exists): installed means the
# provider owns the badge and relay must not also register it. The heartbeat
# (statusbar.provider_alive) is only a liveness read for the notes/doctor, not
# the ownership decision - keying ownership on it caused a double-register
# freeze (see _register_statusbar).


@dataclass
class SessionInfo:
    session_id: str
    title: str = ""
    window_idx: int = 0
    tab_idx: int = 0
    # Arm level (per-tab). Real multi-choice QUESTIONS always hand off to you -
    # NO mode auto-answers them.
    #   "off"    - manual; Relay watches but never acts.
    #   "safe"   - classify the command (lib/danger.sh); approve unless it's on
    #              the catastrophic denylist; escalate dangerous / unreadable.
    #   "wild"   - ignore the command; approve any proceed-prompt with the cursor
    #              on Yes (heredocs / unparseable just work).
    #   "insane" - approve ANY tool-permission prompt, even fail-safe cases
    #              (cursor not on option 1, unparseable). Permission prompts only.
    mode: str = "off"
    hidden: bool = False             # user hid it from the list (UI-only filter)
    job: str = ""                    # iTerm2 foreground job name (shell => no live Claude)
    state: str = "idle"              # idle | working | prompting | blocked | cleared
    last_command: str = ""
    last_seen: float = 0.0
    last_decision: str = ""
    last_screen: List[str] = field(default_factory=list)  # sanitized recent lines
    n_approved: int = 0              # auto-approvals in this tab (running tally)
    n_escalated: int = 0             # dangerous/question escalations in this tab
    stale: bool = False              # swarm: flagged unresponsive (see Task 8)
    _screen_changed_ts: float = field(default=0.0, repr=False)
    _stale_notified: bool = field(default=False, repr=False)
    _iterm_session: object = field(default=None, repr=False)
    _last_prompt_id: Optional[str] = field(default=None, repr=False)
    _last_notify_ts: float = field(default=0.0, repr=False)  # notify cooldown
    _raw_title: str = field(default="", repr=False)  # unstripped on-screen title

    @property
    def active(self) -> bool:
        """True when armed in any acting mode - i.e. Relay may auto-approve."""
        return self.mode in ("safe", "wild", "insane")


def _extract_lines(contents) -> tuple[List[str], List[str]]:
    """Pull (raw line strings, hard_eol flags) from a ScreenContents."""
    n = contents.number_of_lines
    raw, hard = [], []
    for i in range(n):
        lc = contents.line(i)
        raw.append(lc.string)
        hard.append(lc.hard_eol)
    return raw, hard


# iTerm2's bundle id and our click-handler script. When terminal-notifier is on
# PATH we route notifications through it so they are attributed to iTerm (its
# icon + name) instead of "Script Editor" - the osascript host that plain
# `display notification` credits - and, more importantly, so CLICKING a
# notification runs focus_session.sh and jumps to the exact iTerm tab instead of
# opening Script Editor. Resolved once at import; None means fall back to
# osascript (no click action - the pre-terminal-notifier behavior).
ITERM_BUNDLE_ID = "com.googlecode.iterm2"
_FOCUS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "focus_session.sh")
_TERMINAL_NOTIFIER = shutil.which("terminal-notifier")


def notify_mac(title: str, message: str, sound: Optional[str],
               session_id: Optional[str] = None) -> None:
    """Fire a macOS notification + optional sound. Best-effort, never raises.

    With terminal-notifier installed the notification shows as iTerm and, when a
    `session_id` is given, clicking it focuses that iTerm session (the tab the
    alert is about) via focus_session.sh; without a session_id the click just
    activates iTerm. When terminal-notifier is absent we fall back to a plain
    osascript notification (shows Script Editor, no click action)."""
    try:
        if _TERMINAL_NOTIFIER:
            cmd = [_TERMINAL_NOTIFIER,
                   "-title", title[:120],
                   "-message", message[:200],
                   "-sender", ITERM_BUNDLE_ID]
            if session_id:
                # -execute wins the click; quote the path (session_id is a
                # GUID - hex + dashes - so no shell/AppleScript injection risk).
                cmd += ["-execute", f'"{_FOCUS_SCRIPT}" {session_id}']
            else:
                cmd += ["-activate", ITERM_BUNDLE_ID]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            # osascript notification (no extra deps). Escape backslash FIRST
            # (else a trailing '\' would escape the closing quote of the
            # AppleScript string), then swap double quotes for apostrophes so
            # they can't end the string.
            t = title.replace("\\", "\\\\").replace('"', "'")[:120]
            m = message.replace("\\", "\\\\").replace('"', "'")[:200]
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{m}" with title "{t}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        if sound:
            subprocess.Popen(["afplay", sound],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _notify_sound(reason, *, danger, alert):
    """The sound a NOTIFY should play: the danger tone for a confirmed
    dangerous command, else the general alert. Pure - no I/O."""
    return danger if reason == DANGEROUS_COMMAND else alert


class Watcher:
    def __init__(self, connection,
                 alert_sound=None,
                 done_sound=None,
                 danger_sound=None,
                 message_sound=None,
                 on_change: Optional[Callable[[], None]] = None,
                 dry_run: bool = False,
                 cfg=None,
                 own_sid=None):
        self.connection = connection
        # Relay's own tab (bare UUID). Relay must never auto-approve prompts in,
        # or inject into, its own session - that would be relay pressing keys on
        # itself. Display-only for the own tab, always.
        self.own_sid = own_sid
        # Config: defaults < ~/.relay/config < env (load() applies all three).
        if cfg is None:
            cfg, cfg_warnings = relay_config.load()
        else:
            cfg_warnings = []
        self.cfg = cfg
        self._cfg_warnings = cfg_warnings
        # [danger] preset flows to lib/danger.sh through the environment the
        # classifier subshell inherits. Precedence: an env var already set by
        # the user wins over the config file, same as every other knob.
        if "RELAY_DANGER_PRESET" not in os.environ:
            os.environ["RELAY_DANGER_PRESET"] = getattr(
                cfg, "danger_preset", "default")
        self.alert_sound = alert_sound or cfg.alert_sound
        self.done_sound = done_sound or cfg.done_sound
        self.danger_sound = danger_sound or cfg.danger_sound
        self.message_sound = message_sound or cfg.message_sound
        self.on_change = on_change or (lambda: None)
        self.dry_run = dry_run            # if True, never actually inject
        self.sessions: Dict[str, SessionInfo] = {}
        self.log: List[str] = []
        self.log_total = 0                # monotonic count (log[] is capped at 200)
        self._stop_event = asyncio.Event()
        self.read_timeout = 1.5           # per-session screen-read timeout (s)
        # Hard backstops against prompt-text churn (text changing every poll
        # defeats the prompt_id debounce). At most one alert / one auto-inject
        # per session per cooldown window, regardless of churn.
        self.notify_cooldown = cfg.notify_cooldown
        # --- swarm: registry + delivery state ---
        self.registry: Dict[str, dict] = {}   # bare iterm UUID -> sessions row
        self._db = None                        # lazy sqlite conn (same loop)
        self._dryrun_delivered: set = set()    # msg ids noted in dry-run
        self._escalation_pinged: set = set()   # msg ids already pinged
        self._esc_ping_ts = 0.0    # last escalation sound (rate limit)
        self.stale_after = cfg.stale_minutes * 60.0
        self._gone_notified: set = set()   # names alerted as gone-with-queue
        self._last_event = None            # (kind, ts): danger|done reaction pulse
        self._done_seen: set = set()       # task ids already seen 'done'
        self._done_seen_init = False       # first tick seeds without firing
        self._arm_seen: dict = {}          # sid -> time first seen registered
        self.arm_grace = 20.0              # spawn pre-arm window (s), > boot delay
        self._mode_restored: set = set()   # sids whose persisted mode was restored
        self.paused = False                # frozen hands (approvals+deliveries)
        self._approvals = 0                # monotonic session tally (survives a
        #                                    tab closing, unlike summed n_approved)
        # --- tab-title prefixes (style from config; off = fully inert) ---
        self._titled: set = set()          # session ids we wrote a prefix to
        self._title_err_noted: set = set() # sessions with a logged write error
        # --- swarm: closed-session marking ---
        self._own_named = False    # own tab renamed to OWN_TAB_NAME this run
        self._own_tab = None       # own iTerm2 Tab (tab-bar title is separate)
        self._miss = {}            # session name -> consecutive missed ticks
        self.close_misses = 2      # misses before marking closed (debounce)
        self.orphan_count = 0      # closed sessions owning non-done work
        self._roster_ok = False    # did THIS tick's sync succeed?
        # --- swarm: session timers ---
        self.pending_timer_sids: set = set()   # sids awaiting a restore/re-confirm decision
        self._timers_loaded = False            # restore gate runs once per run

    def _note(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.log = self.log[-200:]
        self.log_total += 1   # never resets, so the TUI can mirror new lines

    async def start(self, interval: float = 2.0) -> None:
        """Single poll loop. Every `interval`s: re-sync the roster, then read the
        screen of every session that is VISIBLE or ARMED (skip hidden+disarmed -
        nothing to do there), update its state, and run the gates. Polling all
        relevant sessions is cheap at terminal-tab scale (a few KB per local-
        socket read), so we favour this simple, always-fresh model over the old
        per-session streamers."""
        self._stop_event.clear()
        for w in self._cfg_warnings:
            self._note(w)
        self._cfg_warnings = []
        await self._register_statusbar()
        try:
            while not self._stop_event.is_set():
                # One iteration must NEVER kill the loop: a transient iTerm2
                # error or one dead session should be logged and skipped, not
                # leave the monitor permanently bricked (its whole job).
                try:
                    app = await iterm2.async_get_app(self.connection)
                    await self._sync_sessions(app)
                    self._roster_ok = True
                except Exception as e:
                    self._roster_ok = False
                    self._note(f"roster sync error: {e}")
                self._swarm_refresh_registry()
                # Only latch the restore gate on a tick whose roster sync
                # succeeded: on a failed sync self.sessions is empty/stale,
                # so _load_timers_on_start() would deactivate every saved
                # timer while computing an empty pending set - and since the
                # gate only runs once per run, that would orphan every timer
                # (inactive AND absent from the restore prompt) until relay
                # is restarted.
                if self._roster_ok and not self._timers_loaded:
                    self._timers_loaded = True
                    self._load_timers_on_start()
                await self._name_own_tab()
                self._check_escalations()
                self._check_completions()
                self._statusbar_publish()
                if self._roster_ok:
                    self._mark_closed_sessions()
                self._check_gone()
                for info in list(self.sessions.values()):
                    if self._stop_event.is_set():
                        break
                    if info.hidden and not info.active:
                        # Hidden & disarmed: nothing to poll. But if we ever
                        # wrote a prefix here (armed, then hidden, then
                        # disarmed) it would linger forever - _apply_title
                        # below never runs. Restore the bare name once first.
                        if info.session_id in self._titled:
                            try:
                                await self._apply_title(info)
                            except Exception as e:
                                self._note(f"session error {info.title}: {e}")
                        continue  # hidden & disarmed: skip, per the refresh rule
                    try:
                        res = await self._snapshot(info)
                        if res:
                            await self._handle(info, *res)
                            # Only deliver on fresh screen evidence this tick -
                            # a failed snapshot leaves state/last_screen stale,
                            # which must not be used to decide a delivery.
                            await self._deliver(info)
                        # Staleness must be evaluated even on a failed screen
                        # read - a hung session is exactly the stale case.
                        self._check_stale(info)
                        await self._apply_title(info)
                        await self._fire_timers(info)
                    except Exception as e:
                        self._note(f"session error {info.title}: {e}")
                self.on_change()
                # Interruptible sleep: stop() wakes us immediately instead of
                # waiting out the full interval.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            if not self.dry_run:
                # Flip the AutoLaunch badge to RELAY: off immediately on quit
                # (otherwise it waits out the staleness window).
                statusbar_mod.clear_state()
            await self._restore_own_tab()
            await self._restore_titles()
            await self._close_connection()

    async def stop(self) -> None:
        """Signal the poll loop to exit promptly (wakes the interval sleep)."""
        self._stop_event.set()

    async def _close_connection(self) -> None:
        try:
            close = getattr(self.connection, "async_close", None)
            if close:
                await close()
        except Exception:
            pass
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            pass

    @staticmethod
    async def _session_label(s, tab) -> str:
        """Resolve the display name for a session.

        Prefer a name the USER set, so a manually-titled tab/session shows that
        name instead of the job-derived default. iTerm2 stores a manual title
        (Edit Session > Name, or a tab title) in `titleOverride`; we check the
        session first (most specific), then its tab. Fall back to `autoName`
        (iTerm2's auto, job-derived name - the previous default) and finally a
        short session id. Each read is defensive: a variable that's unset or
        absent on some iTerm2 version simply falls through to the next.
        """
        for obj, var in ((s, "titleOverride"), (tab, "titleOverride"),
                         (s, "autoName")):
            try:
                v = await obj.async_get_variable(var)
            except Exception:
                v = None
            if v and v.strip():
                return v.strip()
        return s.session_id[:8]

    @staticmethod
    async def _session_job(s) -> str:
        """iTerm2's foreground job name for the session (e.g. 'node', 'zsh'), or
        '' when unavailable. Used to tell a live Claude tab from one dropped back
        to a shell. Defensive: any iTerm2 version/timeout quirk falls back to ''
        so an unreadable job never suppresses a real prompt."""
        try:
            v = await s.async_get_variable("jobName")
        except Exception:
            v = None
        return v.strip() if isinstance(v, str) and v.strip() else ""

    async def _sync_sessions(self, app) -> None:
        seen = set()
        for wi, w in enumerate(app.windows):
            for ti, tab in enumerate(w.tabs):
                for s in tab.sessions:
                    sid = s.session_id
                    if sid == self.own_sid:
                        self._own_tab = tab   # tab-bar title needs the Tab
                    seen.add(sid)
                    info = self.sessions.get(sid)
                    title = await self._session_label(s, tab)
                    raw_title = title
                    title = titles.strip_prefix(raw_title)
                    job = await self._session_job(s)
                    if info is None:
                        info = SessionInfo(session_id=sid, title=title,
                                           window_idx=wi, tab_idx=ti,
                                           _iterm_session=s, job=job)
                        info._raw_title = raw_title
                        # Grab the current screen once so the list/preview has
                        # content immediately, without holding a streamer open.
                        await self._snapshot(info)
                        self.sessions[sid] = info
                    else:
                        info.title = title
                        info._raw_title = raw_title
                        info.window_idx, info.tab_idx = wi, ti
                        info._iterm_session = s
                        info.job = job
        # Drop sessions whose tabs closed.
        for sid in list(self.sessions):
            if sid not in seen:
                self.sessions.pop(sid, None)

    async def _snapshot(self, info: SessionInfo):
        """One-shot read of a session's current screen. Updates last_screen and
        returns (raw, hard) for gate evaluation, or None on failure/no-session."""
        s = info._iterm_session
        if s is None:
            return None
        try:
            # Per-read timeout so one hung/busy session can't stall the whole
            # poll pass (and freeze the UI, which repaints from this loop).
            contents = await asyncio.wait_for(
                s.async_get_screen_contents(), timeout=self.read_timeout)
            raw, hard = _extract_lines(contents)
            new_screen = [l for l in reconstruct_lines(raw, hard) if l.strip()][-40:]
            if new_screen != info.last_screen:
                info._screen_changed_ts = time.time()
            info.last_screen = new_screen
            info.last_seen = time.time()
            return raw, hard
        except asyncio.TimeoutError:
            self._note(f"read timeout {info.title}")
            return None
        except Exception:
            return None

    async def _handle(self, info: SessionInfo, raw, hard) -> None:
        # Foreground is a plain shell => no Claude is running in this tab, so any
        # prompt text on screen is a leftover frame from an exited Claude, not a
        # live prompt. Treat it exactly like "no actionable prompt": never mark
        # blocked (the "⊘ LOCKED" tag), never inject a stray Enter into the
        # shell. Fail safe: only a positively-identified shell suppresses; an
        # unknown/unreadable job still runs the full classifier.
        if _is_shell_job(info.job):
            info.state = detect_state(reconstruct_lines(raw, hard))
            return

        decision: Decision = classify(raw, hard)

        if decision.action == Action.NONE:
            # No actionable prompt: read the screen for a real working/idle
            # signal instead of blindly claiming "working".
            info.state = detect_state(reconstruct_lines(raw, hard))
            return

        # Record why relay ACTED so the live-feed pane can show it; a NONE
        # reason ("no actionable prompt") would be noise. Retains the last
        # actionable reason between prompts.
        info.last_decision = decision.reason

        if decision.command:
            info.last_command = decision.command

        # There IS an actionable prompt on screen. Update displayed state for
        # ALL sessions (so the list shows blocked/prompting), but only ARMED
        # sessions alert, audit, or get auto-injected. An unarmed session is one
        # you're driving by hand - Relay must stay silent on it.
        if decision.action == Action.NOTIFY:
            info.state = "blocked"
        elif decision.action == Action.INJECT:
            info.state = "prompting" if not info.active else info.state

        # Shadow: a per-tab dry-run of the SAFE pipeline. Record what relay
        # WOULD do (would-approve on a safe prompt, would-escalate otherwise)
        # but never inject and never alarm - nothing real happened, you are
        # watching. Reuses safe's predicate (INJECT == would-approve).
        if info.mode == "shadow" and info.session_id != self.own_sid:
            if decision.prompt_id is not None and \
                    decision.prompt_id == info._last_prompt_id:
                return
            info._last_prompt_id = decision.prompt_id
            if decision.action == Action.INJECT:
                info.state = "cleared"
                audit.record("would-approve", info.title,
                             decision.command or "",
                             f"shadow ({decision.reason})")
            else:
                info.state = "blocked"
                audit.record("would-escalate", info.title,
                             decision.command or "", decision.reason)
            return

        if not info.active:
            return  # unarmed: display only, no alert / no audit / no inject
        if info.session_id == self.own_sid:
            return  # relay's own tab: never act on itself, whatever its mode

        # Armed. Debounce: act at most once per distinct prompt instance.
        if decision.prompt_id is not None and decision.prompt_id == info._last_prompt_id:
            return
        info._last_prompt_id = decision.prompt_id

        # Decide whether to APPROVE, by mode (decreasing caution):
        #   safe   - only INJECT (command classified non-catastrophic)
        #   wild   - any proceed-prompt (cursor on Yes), command ignored
        #   insane - any permission prompt at all, even fail-safe cases
        # Real questions have is_permission=False, so NONE of these touch them.
        if info.mode == "insane":
            approve = decision.is_permission
        elif info.mode == "wild":
            approve = decision.is_proceed
        else:  # safe
            approve = decision.action == Action.INJECT

        if not approve:
            # Cooldown backstop: even if the prompt_id churns (e.g. you're typing
            # an answer to a question and the menu lines keep changing), alert at
            # most once per notify_cooldown seconds per session. State still
            # updates every poll; only the sound/notification/audit row is gated.
            now = time.time()
            if now - info._last_notify_ts < self.notify_cooldown:
                return
            info._last_notify_ts = now
            info.n_escalated += 1
            if decision.reason == DANGEROUS_COMMAND:
                self._last_event = ("danger", time.time())
            audit.record("escalated", info.title, decision.command or "",
                         decision.reason)
            self._note(f"NOTIFY {info.title}: {decision.reason}")
            notify_mac(f"Relay - {info.title}",
                       decision.reason + (f": {decision.command[:80]}"
                                          if decision.command else ""),
                       _notify_sound(decision.reason,
                                     danger=self.danger_sound,
                                     alert=self.alert_sound),
                       session_id=info.session_id)
            return

        # --- approve path ---
        # Paused: relay's hands are frozen - do not inject or audit an approval
        # (nothing happened). The NOTIFY branch above already ran, so danger is
        # never silenced by a pause; the PAUSED banner explains the stillness.
        if self.paused:
            info.state = "prompting"
            return

        # Churn vs distinct-prompts is handled by the prompt_id debounce above
        # (line ~233): prompt_id is now STABLE across a single prompt's redraws
        # (it normalizes the option text) and DIFFERENT for a genuinely new
        # prompt - so the same prompt approves once, and a back-to-back distinct
        # prompt still approves. No separate inject flag needed.
        verdict_reason = (decision.reason if decision.action == Action.INJECT
                          else f"{info.mode}-approve ({decision.reason})")
        if self.dry_run:
            info.state = "cleared"
            audit.record("would-approve", info.title, decision.command or "",
                         verdict_reason)
            self._note(f"DRY-RUN would inject {info.title}: "
                       f"{(decision.command or '<unparsed>')[:60]}")
            return
        # LOG BEFORE ACT: write the audit row first so an unattended approval can
        # never happen un-recorded. If the durable write fails, do NOT send Enter
        # - escalate instead, so a logging outage can't silently auto-approve.
        if not audit.record("auto-approved", info.title,
                             decision.command or "", verdict_reason):
            info.state = "blocked"
            info.n_escalated += 1
            self._note(f"AUDIT-FAIL {info.title}: not injecting (log write failed)")
            notify_mac(f"Relay - {info.title}",
                       "audit log write failed - NOT auto-approving",
                       self.alert_sound, session_id=info.session_id)
            return
        await info._iterm_session.async_send_text("\r")
        info.state = "cleared"
        info.n_approved += 1
        self._approvals += 1
        self._note(f"INJECT {info.title}: {(decision.command or '<unparsed>')[:60]}")

    # --- swarm ------------------------------------------------------------------

    def _swarm_conn(self):
        if self._db is None:
            self._db = swarmdb.connect()
        return self._db

    def _swarm_refresh_registry(self) -> None:
        """Rebuild the name<->session map + TASK NOW strings, once per tick.
        Any DB trouble degrades to 'no swarm data' - never kills the loop."""
        try:
            conn = self._swarm_conn()
            reg = {}
            for r in swarmdb.list_sessions(conn):
                d = dict(r)
                cur = swarmdb.current_task_for(conn, d["name"])
                if cur is None:
                    d["task_now"] = ""
                elif cur["state"] == "blocked":
                    bb = ",".join(str(b) for b in
                                  swarm.parse_blockers(cur["blocked_by"]))
                    d["task_now"] = f"#{cur['id']} ⊘" + (f" by {bb}" if bb else "")
                else:
                    d["task_now"] = f"#{cur['id']} {cur['state']} {cur['title']}"
                # Spawn pre-arming: a pending arm request is honored ONLY
                # within a short grace window after the watcher first sees the
                # session (the spawn boot window). spawn.py creates the tab
                # before it writes the request, so a tick can land in between
                # and record the sid "seen, no request"; a strict first-tick
                # rule would then wrongly reject the legitimate pre-arm. The
                # window absorbs that race while still refusing a request that
                # surfaces on a long-running session - a self-escalation
                # attempt (any Bash-capable session can UPDATE this DB). A
                # refusal, AND every honored arming, is escalated to the human
                # with sound: an arm the operator did not perform by hand must
                # always be audible. The arm state itself lives in this process
                # (SessionInfo.mode).
                req = d.get("arm_request") or ""
                sid = d["iterm_session_id"]
                if sid in self.sessions and sid not in self._arm_seen:
                    self._arm_seen[sid] = time.time()
                if req and sid in self.sessions:
                    within = time.time() - self._arm_seen.get(sid, 0) <= self.arm_grace
                    swarmdb.clear_arm_request(conn, d["name"])
                    if within:
                        self.sessions[sid].mode = req
                        self.sessions[sid]._last_prompt_id = None
                        # persist directly (self.registry isn't updated with this
                        # new sid until the tick ends, so set_mode's persist would
                        # no-op); mark restored so we don't overwrite it below.
                        # A persist hiccup must not abort the arming or its alert.
                        try:
                            swarmdb.set_session_mode(conn, d["name"], req)
                        except Exception:
                            pass
                        self._mode_restored.add(sid)
                        self._note(f"ARMED {d['name']} -> {req} (spawn request)")
                        notify_mac(f"Relay - {d['name']}",
                                   f"armed {req} on spawn", self.alert_sound,
                                   session_id=sid)
                    else:
                        self._note(f"REFUSED arm escalation {d['name']} -> {req}")
                        notify_mac(f"Relay - {d['name']}",
                                   f"refused arm escalation to {req} "
                                   f"(request outside spawn window)",
                                   self.alert_sound, session_id=sid)
                # Restore a persisted arm level after a restart: only at first
                # sight, only if no fresh spawn arm_request took precedence, and
                # only once (later ticks must not re-apply a stale stored value
                # over a human's live change). The stored mode was written by a
                # prior watcher run; direct DB writes are danger.sh-blocked in
                # safe mode, and restoration only happens across a restart.
                elif (sid in self.sessions and sid not in self._mode_restored):
                    stored = d.get("mode") or ""
                    self._mode_restored.add(sid)
                    if stored in ("safe", "wild", "insane", "shadow") and \
                            self.sessions[sid].mode == "off":
                        self.sessions[sid].mode = stored
                        self.sessions[sid]._last_prompt_id = None
                        self._note(f"RESTORED {d['name']} -> {stored} "
                                   f"(persisted arm level)")
                reg[d["iterm_session_id"]] = d
            self.registry = reg
        except Exception as e:
            self._note(f"swarm db error: {e}")

    def _mark_closed_sessions(self) -> None:
        """After a good roster sync: a registered session whose tab is missing
        for close_misses consecutive ticks is stamped closed; a reappeared tab
        resets the counter and clears closed_at. The debounce + sync gate stop
        a transient empty roster from false-marking a live swarm."""
        try:
            conn = self._swarm_conn()
            rows = swarmdb.list_sessions(conn)
        except Exception as e:
            self._note(f"swarm db error: {e}")
            return
        live = set(self.sessions.keys())
        for r in rows:
            name, sid, closed = r["name"], r["iterm_session_id"], r["closed_at"]
            if sid in live:
                self._miss.pop(name, None)
                if closed:
                    try:
                        swarmdb.clear_closed(conn, name)
                    except Exception:
                        pass
                continue
            self._miss[name] = self._miss.get(name, 0) + 1
            if self._miss[name] >= self.close_misses and not closed:
                try:
                    swarmdb.mark_closed(conn, name, time.time())
                    # Reset the miss count so a later respawn under the same
                    # name starts fresh instead of re-closing on its first tick.
                    self._miss.pop(name, None)
                    self._note(f"CLOSED {name} (tab gone)")
                except Exception:
                    pass
        self._recount_orphans()

    def _recount_orphans(self) -> None:
        try:
            conn = self._swarm_conn()
            closed = {r["name"] for r in swarmdb.list_sessions(conn)
                      if r["closed_at"]}
            owners = {t["owner"] for t in swarmdb.list_tasks(conn)
                      if t["state"] != "done" and t["owner"]}
            self.orphan_count = len(closed & owners)
        except Exception:
            pass

    async def _deliver(self, info: SessionInfo) -> None:
        """Deliver AT MOST ONE queued message into a registered session, only
        when it is idle at Claude's input box. Audit before act, like
        approvals. One per tick keeps the injected turns observable."""
        if info.session_id == self.own_sid:
            return  # never type a swarm message into relay's own panel tab
        if self.paused:
            return  # hands frozen: the message stays queued, retries on resume
        reg = self.registry.get(info.session_id)
        if not reg:
            return
        if info.state != "idle" or not swarm.claude_prompt_ready(info.last_screen):
            return
        try:
            msgs = swarmdb.undelivered(self._swarm_conn(), reg["name"])
        except Exception as e:
            self._note(f"swarm db error: {e}")
            return
        if not msgs:
            return
        m = msgs[0]
        text = swarm.delivery_text(m["from_name"], m["body"], swarm.kind_of(m))
        if self.dry_run:
            if m["id"] not in self._dryrun_delivered:
                self._dryrun_delivered.add(m["id"])
                audit.record("would-deliver", info.title, text[:500],
                             f"msg {m['id']} to {reg['name']}")
                self._note(f"DRY-RUN would deliver -> {reg['name']}: "
                           f"{m['body'][:60]}")
            return
        # LOG BEFORE ACT (same contract as approvals).
        if not audit.record("delivered", info.title, text[:500],
                            f"msg {m['id']} from {m['from_name']} "
                            f"to {reg['name']}"):
            # The message stays queued and retries next tick, but the 2s poll
            # would otherwise fire a notification + log line EVERY tick while
            # the log stays unwritable. Gate it on the session's notify
            # cooldown (approvals are debounced; delivery must be too).
            now = time.time()
            if now - info._last_notify_ts >= self.notify_cooldown:
                info._last_notify_ts = now
                self._note(f"AUDIT-FAIL: not delivering msg {m['id']}")
                notify_mac("Relay - swarm", "audit log write failed - "
                           "NOT delivering message", self.alert_sound)
            return
        # Send body then a STANDALONE Enter (bracketed-paste lesson): the TUI
        # treats the body as a paste and waits for a discrete \r.
        await info._iterm_session.async_send_text(text)
        await asyncio.sleep(0.3)
        await info._iterm_session.async_send_text("\r")
        # Mark AFTER the send: if the send raises, the message stays queued
        # and retries next tick (a rare duplicate beats a lost wake-up).
        swarmdb.mark_delivered(self._swarm_conn(), m["id"])
        self._note(f"DELIVER -> {reg['name']}: {m['body'][:60]}")

    async def _fire_timers(self, info: SessionInfo) -> None:
        """Fire at most one due, firable timer for this session per tick. now
        mode injects immediately; idle waits for a ready Claude prompt. Pause
        freezes; require_armed gates on arm level; dry-run would-fire. A binding
        older than reconfirm_days is deactivated (back to pending) instead of
        firing - the stale-session-id guard. Audit BEFORE the send. Best-effort:
        DB/iTerm2 errors are logged, never break the loop."""
        if info.session_id == self.own_sid:
            return
        s = info._iterm_session
        if s is None:
            return
        try:
            conn = self._swarm_conn()
            rows = swarmdb.list_timers(conn, info.session_id)
        except Exception as e:
            self._note(f"timers db error: {e}")
            return
        now = time.time()
        due = timers_mod.due_timers(rows, now)
        if not due:
            return
        ready = (info.state == "idle"
                 and swarm.claude_prompt_ready(info.last_screen))
        armed = info.mode in ("safe", "wild", "insane")
        require_armed = getattr(self.cfg, "timers_require_armed", False)
        reconfirm = getattr(self.cfg, "timers_reconfirm_days", 7.0)
        for t in due:
            if timers_mod.needs_reconfirm(t, now, reconfirm):
                swarmdb.update_timer(conn, t["id"], active=0)
                self.pending_timer_sids.add(info.session_id)
                self._note(f"timer {t['id']} binding stale - re-confirm via t")
                continue
            if not timers_mod.firable(t, ready=ready, paused=self.paused,
                                      armed=armed, require_armed=require_armed):
                continue
            if self.dry_run:
                audit.record("would-fire", info.title, t["payload"][:500],
                             f"timer {t['id']}")
                self._note(f"DRY-RUN would fire timer -> {info.title}: "
                           f"{t['payload'][:60]}")
                swarmdb.mark_timer_fired(conn, t["id"], now=now)
                return
            if not audit.record("timer-fired", info.title, t["payload"][:500],
                                f"timer {t['id']}"):
                now2 = time.time()
                if now2 - info._last_notify_ts >= self.notify_cooldown:
                    info._last_notify_ts = now2
                    self._note(f"AUDIT-FAIL: not firing timer {t['id']}")
                return
            await s.async_send_text(t["payload"])
            await asyncio.sleep(0.3)
            await s.async_send_text("\r")
            swarmdb.mark_timer_fired(conn, t["id"], now=now)
            self._note(f"TIMER -> {info.title}: {t['payload'][:60]}")
            return    # one per tick

    def _load_timers_on_start(self) -> None:
        """Restore gate: unless [timers] autostart, every saved timer starts
        inactive and its session is flagged pending (the app prompts to restore
        via the t overlay). Never raises."""
        try:
            conn = self._swarm_conn()
            if getattr(self.cfg, "timers_autostart", False):
                swarmdb.restore_all_present_timers(conn, list(self.sessions))
                self.pending_timer_sids = set()
            else:
                swarmdb.deactivate_all_timers(conn)
                self.pending_timer_sids = {
                    row["iterm_session_id"]
                    for row in swarmdb.all_timers(conn)
                    if row["iterm_session_id"] in self.sessions}
        except Exception as e:
            self._note(f"timers load error: {e}")

    def _check_escalations(self) -> None:
        """A worker sending --kind escalation is calling for a human. Ping
        (sound + notification) the moment the message is queued - even if the
        target session is busy - once per message. Runs in dry-run too:
        notify is the zero-blast-radius half, same as prompt alerts."""
        try:
            msgs = swarmdb.undelivered(self._swarm_conn())
            fresh = swarm.escalation_pings(msgs, self._escalation_pinged)
            if not fresh:
                return
            for m in fresh:
                self._escalation_pinged.add(m["id"])
                self._note(f"ESCALATION from {m['from_name']} -> "
                           f"{m['to_name']}: {m['body'][:80]}")
            # Rate limit: at most one SOUND per notify_cooldown window - a
            # looping worker must not turn the escalation channel into a
            # siren. Every message is still logged above.
            now = time.time()
            if now - self._esc_ping_ts < self.notify_cooldown:
                return
            self._esc_ping_ts = now
            first = fresh[0]
            if len(fresh) == 1:
                notify_mac(f"Relay - escalation from {first['from_name']}",
                           first["body"][:120], self.message_sound)
            else:
                notify_mac("Relay - escalations",
                           f"{len(fresh)} pending, first from "
                           f"{first['from_name']}: {first['body'][:80]}",
                           self.message_sound)
        except Exception as e:
            # Never let a bad row escape into start()'s tick loop (it has no
            # per-tick except; an escape would kill the watcher outright).
            self._note(f"escalation check error: {e}")

    def _check_completions(self) -> None:
        """Fire a 'done' pulse + chime the first time a task/epic reaches done.
        Seeds silently on the first tick so a pre-existing backlog does not
        chime on startup. Best-effort; never raises into the poll loop."""
        try:
            tasks = swarmdb.list_tasks(self._swarm_conn())
            done_ids = {t["id"] for t in tasks if t["state"] == "done"}
            if self._done_seen_init:
                new_done = done_ids - self._done_seen
                if new_done:
                    self._last_event = ("done", time.time())
                    notify_mac("Relay - done",
                               f"{len(new_done)} task(s) completed",
                               self.done_sound)
            self._done_seen = done_ids
            self._done_seen_init = True
        except Exception:
            return

    def _check_stale(self, info: SessionInfo) -> None:
        """Flag a registered session STALE (and notify ONCE per onset) when a
        queued message can't be delivered for stale_after seconds, or it owns
        a 'doing' task with a quiet screen for stale_after seconds."""
        reg = self.registry.get(info.session_id)
        if not reg:
            info.stale = False
            info._stale_notified = False
            return
        try:
            conn = self._swarm_conn()
            msgs = swarmdb.undelivered(conn, reg["name"])
            cur = swarmdb.current_task_for(conn, reg["name"])
        except Exception:
            return
        oldest = min((m["created_at"] for m in msgs), default=None)
        doing_since = (cur["updated_at"]
                       if cur is not None and cur["state"] == "doing" else None)
        reason = swarm.stale_reason(
            time.time(), self.stale_after,
            oldest_undelivered_ts=oldest, doing_since=doing_since,
            screen_changed_ts=info._screen_changed_ts or None)
        if reason:
            info.stale = True
            if not info._stale_notified:
                info._stale_notified = True
                self._note(f"STALE {reg['name']}: {reason}")
                notify_mac(f"Relay - {reg['name']} STALE", reason,
                           self.alert_sound, session_id=info.session_id)
        else:
            info.stale = False
            info._stale_notified = False

    def _deliverable(self, conn, name: str) -> bool:
        """True when a message TO `name` could actually be delivered right now:
        its sessions row exists, its bound iterm_session_id is live, and the
        registry still maps that id back to THIS name. The round-trip check
        catches name collisions (two names bound to one id - only the current
        binding is deliverable) and never-registered owners (no row at all)."""
        try:
            row = swarmdb.get_session(conn, name)
        except Exception:
            return False
        if row is None:
            return False
        sid = row["iterm_session_id"]
        if sid not in self.sessions:
            return False
        reg = self.registry.get(sid)
        return reg is not None and reg["name"] == name

    def _check_gone(self) -> None:
        """Notify once per name whose queued messages can't reach it. Keyed by
        the RECIPIENT name (not by live session id), so it also covers a name
        that was shadowed by a collision or was never registered at all - both
        would silently black-hole their queue under a session-keyed scan.

        A name is cleared from the notified set once it becomes deliverable
        again (re-registered / rebound) or its queue drains (no undelivered
        rows). Only a name with a message older than stale_after that is not
        currently deliverable fires the alert."""
        now = time.time()
        try:
            conn = self._swarm_conn()
            msgs = swarmdb.undelivered(conn)
        except Exception:
            return
        by_name: Dict[str, float] = {}
        for m in msgs:
            ts = m["created_at"]
            if m["to_name"] not in by_name or ts < by_name[m["to_name"]]:
                by_name[m["to_name"]] = ts
        # Reset names that recovered or drained.
        for name in list(self._gone_notified):
            if name not in by_name or self._deliverable(conn, name):
                self._gone_notified.discard(name)
        for name, oldest in by_name.items():
            if self._deliverable(conn, name):
                continue
            if now - oldest > self.stale_after and name not in self._gone_notified:
                self._gone_notified.add(name)
                self._note(f"STALE {name}: session gone or unreachable, "
                           f"messages queued")
                notify_mac(f"Relay - {name} STALE",
                           "session gone or unreachable, messages queued",
                           self.alert_sound)

    # --- tab-title prefixes -------------------------------------------------

    async def _apply_title(self, info: SessionInfo) -> None:
        """Keep the session's on-screen title in sync with mode + attention
        state. Writes only when the desired title differs from what's on
        screen; restores the bare name once when a previously-prefixed
        session goes manual+calm. Fully inert when style is off or dry-run
        (dry-run mutates nothing, titles included). Best-effort: an iTerm2
        error is logged once per session and never breaks the poll loop."""
        if self.cfg.title_style == "off" or self.dry_run:
            return
        s = info._iterm_session
        if s is None:
            return
        desired = titles.render(self.cfg.title_style, info.mode, info.state,
                                info.stale, info.title)
        if desired == info.title and info.session_id not in self._titled:
            return                       # nothing to add, nothing to restore
        if desired == info._raw_title:
            # Screen already correct; just keep bookkeeping accurate.
            if desired == info.title:
                self._titled.discard(info.session_id)
            else:
                self._titled.add(info.session_id)
            return
        try:
            await s.async_set_name(desired)
            info._raw_title = desired
            if desired == info.title:
                self._titled.discard(info.session_id)   # bare name restored
            else:
                self._titled.add(info.session_id)
        except Exception as e:
            if info.session_id not in self._title_err_noted:
                self._title_err_noted.add(info.session_id)
                self._note(f"title write failed {info.title}: {e}")

    async def _restore_titles(self) -> None:
        """On quit: write the bare name back to every session we prefixed.
        Best-effort - sessions may already be gone.

        Catch BaseException per session, not just Exception: quit cancels the
        worker running start(), so a CancelledError (a BaseException) can land
        on any await here. One cancelled/failed restore must NOT abort the
        rest, or later sessions keep their stale prefix. If we were cancelled,
        we still finish every remaining restore, then re-raise CancelledError
        after the loop so the caller's cancellation semantics are preserved."""
        cancelled: Optional[BaseException] = None
        for sid in list(self._titled):
            info = self.sessions.get(sid)
            if info is not None and info._iterm_session is not None:
                try:
                    await info._iterm_session.async_set_name(info.title)
                except asyncio.CancelledError as e:
                    cancelled = e   # remember, keep restoring the rest
                except Exception:
                    pass
            self._titled.discard(sid)
        if cancelled is not None:
            raise cancelled

    async def focus_session(self, sid: str) -> bool:
        """Bring the real iTerm2 tab for this session to the foreground."""
        info = self.sessions.get(sid)
        if not info or info._iterm_session is None:
            return False
        try:
            await info._iterm_session.async_activate()
            return True
        except Exception as e:
            self._note(f"focus failed {info.title}: {e}")
            return False

    _MODE_CYCLE = {"off": "safe", "safe": "wild", "wild": "insane",
                   "insane": "off", "shadow": "safe"}
    MODES = ("off", "safe", "wild", "insane", "shadow")

    # --- iTerm2 status-bar component (opt-in) ---------------------------------

    async def _register_statusbar(self) -> None:
        """Set up the per-tab arm badge (config-gated). Best effort: a failure
        here must never stop the watcher starting.

        Only ONE renderer may own the "com.relay.arm" RPC - iTerm2 rejects a
        second registration with DUPLICATE_SERVER_ORIGINATED_RPC, freezing the
        badge. So the decision is a hard either/or, keyed on whether the
        AutoLaunch provider is INSTALLED (its symlink exists):

          provider installed -> the provider owns the badge (it renders the
            state relay publishes each tick, and outlives relay so the slot
            never errors while relay is off). relay must NOT also register.
          provider absent     -> relay is the sole claimant, so it renders the
            badge in-process here: render() reads this process's arm state,
            on_click toggles it (an un-spoofable human action). Zero setup, but
            the slot errors while relay is off - install the provider to fix.

        We key on the symlink, not the provider heartbeat: the heartbeat lags a
        just-launched provider, so relay would briefly see "not alive", register
        in-process, and then collide when the provider rendered - the exact
        freeze this replaces. Either way relay keeps publishing state and
        consuming clicks (_statusbar_publish)."""
        if not getattr(self.cfg, "statusbar_enabled", False):
            return
        if statusbar_mod.provider_installed():
            if statusbar_mod.provider_alive():
                self._note("statusbar served by AutoLaunch provider "
                           "(relay_statusbar.py)")
                return
            # Installed but no provider process is running - the usual cause is
            # the symlink being (re)linked (install / `relay update`) AFTER
            # iTerm2 last launched, so iTerm2 never started it. Start it
            # ourselves so the badge heals without an iTerm2 restart. This is
            # safe from the DUPLICATE-register freeze: relay only ever runs
            # inside an already-up iTerm2, so a dead heartbeat here means no
            # provider process exists to collide with (statusbar_ensure also
            # refuses to start a second one over a live heartbeat). The cookie
            # + launch block, so run it off the event loop.
            try:
                import statusbar_ensure
                action = await asyncio.to_thread(statusbar_ensure.ensure)
                msg = statusbar_ensure._MESSAGES.get(action, action)
            except Exception as e:
                action, msg = "error", str(e)
            if action == "alive":
                self._note("statusbar served by AutoLaunch provider "
                           "(relay_statusbar.py)")
            else:
                self._note(f"statusbar: {msg}")
            return
        try:
            component = iterm2.StatusBarComponent(
                short_description="Relay",
                detailed_description="Relay arm state for this tab; "
                                     "click to cycle off/safe/wild/insane.",
                knobs=[],
                exemplar="\U0001f7e2 RELAY:safe",
                update_cadence=1.0,     # refresh so a Space-key change shows too
                identifier="com.relay.arm",
            )

            @iterm2.StatusBarRPC
            async def render(knobs, session_id=iterm2.Reference("id")):
                return self._statusbar_label(session_id)

            async def on_click(session_id):
                self._statusbar_click(session_id)

            await component.async_register(self.connection, render,
                                           onclick=on_click)
            self._note("statusbar rendering in-process (no AutoLaunch "
                       "provider). Add 'Relay' to your bar: Settings > "
                       "Profiles > Session > Configure Status Bar. Run "
                       "./install.sh for the always-on provider so the badge "
                       "survives relay being off.")
        except Exception as e:
            self._note(f"statusbar register failed: {e}")

    def _statusbar_label(self, session_id: str) -> str:
        """The badge text for one tab; never raises (a render error would blank
        the bar)."""
        try:
            if session_id == self.own_sid:
                return statusbar_mod.label("off", own_panel=True)
            info = self.sessions.get(session_id)
            mode = info.mode if info else "off"
            reg = self.registry.get(session_id) or {}
            return statusbar_mod.label(mode, name=reg.get("name"),
                                       role=reg.get("role"))
        except Exception:
            return statusbar_mod.label("off")

    async def _name_own_tab(self) -> None:
        """Name relay's OWN tab by design (once per run, live only - dry-run
        mutates nothing). Best-effort: a rename failure must never touch the
        watcher loop."""
        if self.dry_run or self._own_named or not self.own_sid:
            return
        info = self.sessions.get(self.own_sid)
        if info is None:
            return
        try:
            await info._iterm_session.async_set_name(OWN_TAB_NAME)
            # The TAB BAR title is a separate object from the session name -
            # without this, the tab strip keeps showing the `caffeinate` job.
            if self._own_tab is not None:
                await self._own_tab.async_set_title(OWN_TAB_NAME)
            await info._iterm_session.async_set_profile_properties(
                _own_tab_profile(True))
            self._own_named = True
        except Exception:
            pass

    async def _restore_own_tab(self) -> None:
        """On quit, hand both titles back to iTerm2's auto-naming (empty =
        job-derived), so a closed relay doesn't leave a ghost console label
        behind. Best-effort, mirror of _name_own_tab."""
        if not self._own_named:
            return
        try:
            s = self.sessions[self.own_sid]._iterm_session
            await s.async_set_name("")
            if self._own_tab is not None:
                await self._own_tab.async_set_title("")
            await s.async_set_profile_properties(_own_tab_profile(False))
        except Exception:
            pass

    def _statusbar_publish(self) -> None:
        """Feed the AutoLaunch provider: apply any queued badge clicks (same
        guards as a direct click), then publish every tab's current label.
        Best-effort - the status bar must never break the watcher. Skipped in
        dry-run (publishing/consuming are writes; dry-run mutates nothing) -
        the badge simply shows RELAY: off during a dry run, which is honest."""
        if not getattr(self.cfg, "statusbar_enabled", False) or self.dry_run:
            return
        try:
            for sid in statusbar_mod.consume_clicks():
                self._statusbar_click(sid)
            labels = {sid: self._statusbar_label(sid) for sid in self.sessions}
            if self.own_sid:
                labels[self.own_sid] = self._statusbar_label(self.own_sid)
            statusbar_mod.write_state(labels)
        except Exception as e:
            self._note(f"statusbar publish error: {e}")

    def _statusbar_click(self, session_id: str) -> None:
        """A status-bar click cycles that tab's arm level - same as Space in the
        panel. Never on relay's own tab."""
        if session_id == self.own_sid or session_id not in self.sessions:
            return
        self.toggle(session_id)
        self._note(f"statusbar arm {self.sessions[session_id].title} -> "
                   f"{self.sessions[session_id].mode}")
        self.on_change()   # repaint the panel so its row reflects the new mode

    def _armable(self, sid: str) -> bool:
        """Relay must never arm its OWN panel tab (it never acts on itself), so
        no arm path - Space, status-bar click, arm-all - may change its mode."""
        return sid in self.sessions and sid != self.own_sid

    def toggle(self, sid: str) -> None:
        """Cycle arm level: off -> safe -> wild -> insane -> off."""
        if self._armable(sid):
            info = self.sessions[sid]
            info.mode = self._MODE_CYCLE.get(info.mode, "safe")
            info._last_prompt_id = None   # re-evaluate current prompt under new mode
            self._persist_mode(sid, info.mode)

    def toggle_shadow(self, sid: str) -> None:
        """Toggle a tab between shadow (per-tab dry-run) and off. Shadow is a
        deliberate calibration mode, so it is its own key, not in the Space
        cycle (Space promotes shadow -> safe)."""
        if self._armable(sid):
            info = self.sessions[sid]
            info.mode = "off" if info.mode == "shadow" else "shadow"
            info._last_prompt_id = None
            self._persist_mode(sid, info.mode)

    def set_mode(self, sid: str, mode: str) -> None:
        if self._armable(sid) and mode in self.MODES:
            self.sessions[sid].mode = mode
            self.sessions[sid]._last_prompt_id = None
            self._persist_mode(sid, mode)

    def set_all(self, active: bool) -> None:
        for sid, info in self.sessions.items():
            if sid == self.own_sid:
                continue   # never arm relay's own tab
            info.mode = "safe" if active else "off"
            self._persist_mode(sid, info.mode)

    def _persist_mode(self, sid: str, mode: str) -> None:
        """Mirror a registered session's arm level to the DB so a relay restart
        can restore it. Best-effort; unregistered (ad-hoc) tabs aren't persisted
        - they're ephemeral. Mark it restored so the next tick doesn't overwrite
        this human action with a stale stored value."""
        reg = self.registry.get(sid)
        if not reg:
            return
        try:
            swarmdb.set_session_mode(self._swarm_conn(), reg["name"], mode)
            self._mode_restored.add(sid)
        except Exception as e:
            self._note(f"swarm db error: {e}")

    def toggle_pause(self) -> bool:
        """Freeze/unfreeze relay's HANDS (auto-approvals + swarm deliveries)
        while its eyes stay open (still watches + warns). Holds until toggled
        again - never auto-resumes. Records the transition. Returns new state."""
        self.paused = not self.paused
        audit.record("paused" if self.paused else "resumed", "relay", "", "")
        self._note("PAUSED - relay is NOT acting (approvals + deliveries frozen)"
                   if self.paused
                   else "resumed - relay is acting again")
        return self.paused

    def toggle_hidden(self, sid: str) -> None:
        if sid in self.sessions:
            self.sessions[sid].hidden = not self.sessions[sid].hidden

    def unhide_all(self) -> None:
        for info in self.sessions.values():
            info.hidden = False

    async def send_keys(self, sid: str, text: str) -> bool:
        """Manually send literal text/keys to a session. ALWAYS sends, even in
        dry-run and even for un-armed sessions - this is a deliberate human
        action, not automatic injection. Returns True on success.
        """
        info = self.sessions.get(sid)
        if not info or info._iterm_session is None:
            return False
        try:
            await info._iterm_session.async_send_text(text)
            shown = {"\r": "Enter"}.get(text, text)
            self._note(f"MANUAL send {shown!r} -> {info.title}")
            return True
        except Exception as e:
            self._note(f"manual send failed {info.title}: {e}")
            return False

    async def refresh_screen(self, sid: str) -> None:
        """Pull ONE session's current screen on demand (e.g. when selected), so
        the preview is fresh right now rather than at its last change."""
        info = self.sessions.get(sid)
        if not info or info._iterm_session is None:
            return
        try:
            contents = await info._iterm_session.async_get_screen_contents()
            raw, hard = _extract_lines(contents)
            lines = reconstruct_lines(raw, hard)
            info.last_screen = [l for l in lines if l.strip()][-40:]
        except Exception:
            pass
