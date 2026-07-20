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
from gates import classify, Action, Decision, reconstruct_lines, detect_state


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


def notify_mac(title: str, message: str, sound: Optional[str]) -> None:
    """Fire a macOS notification + optional sound. Best-effort, never raises."""
    try:
        # osascript notification (no extra deps). Escape backslash FIRST (else a
        # trailing '\' would escape the closing quote of the AppleScript string),
        # then swap double quotes for apostrophes so they can't end the string.
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


class Watcher:
    def __init__(self, connection,
                 alert_sound=None,
                 done_sound=None,
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
        self.alert_sound = alert_sound or cfg.alert_sound
        self.done_sound = done_sound or cfg.done_sound
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
        self.stale_after = cfg.stale_minutes * 60.0
        self._gone_notified: set = set()   # names alerted as gone-with-queue
        self._arm_seen: dict = {}          # sid -> time first seen registered
        self.arm_grace = 20.0              # spawn pre-arm window (s), > boot delay
        self._mode_restored: set = set()   # sids whose persisted mode was restored
        # --- tab-title prefixes (style from config; off = fully inert) ---
        self._titled: set = set()          # session ids we wrote a prefix to
        self._title_err_noted: set = set() # sessions with a logged write error
        # --- swarm: closed-session marking ---
        self._miss = {}            # session name -> consecutive missed ticks
        self.close_misses = 2      # misses before marking closed (debounce)
        self.orphan_count = 0      # closed sessions owning non-done work
        self._roster_ok = False    # did THIS tick's sync succeed?

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
                self._check_escalations()
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

    async def _sync_sessions(self, app) -> None:
        seen = set()
        for wi, w in enumerate(app.windows):
            for ti, tab in enumerate(w.tabs):
                for s in tab.sessions:
                    sid = s.session_id
                    seen.add(sid)
                    info = self.sessions.get(sid)
                    title = await self._session_label(s, tab)
                    raw_title = title
                    title = titles.strip_prefix(raw_title)
                    if info is None:
                        info = SessionInfo(session_id=sid, title=title,
                                           window_idx=wi, tab_idx=ti,
                                           _iterm_session=s)
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
        decision: Decision = classify(raw, hard)
        info.last_decision = decision.reason

        if decision.action == Action.NONE:
            # No actionable prompt: read the screen for a real working/idle
            # signal instead of blindly claiming "working".
            info.state = detect_state(reconstruct_lines(raw, hard))
            return

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
            audit.record("escalated", info.title, decision.command or "",
                         decision.reason)
            self._note(f"NOTIFY {info.title}: {decision.reason}")
            notify_mac(f"Relay - {info.title}",
                       decision.reason + (f": {decision.command[:80]}"
                                          if decision.command else ""),
                       self.alert_sound)
            return

        # --- approve path ---
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
                       self.alert_sound)
            return
        await info._iterm_session.async_send_text("\r")
        info.state = "cleared"
        info.n_approved += 1
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
                                   f"armed {req} on spawn", self.alert_sound)
                    else:
                        self._note(f"REFUSED arm escalation {d['name']} -> {req}")
                        notify_mac(f"Relay - {d['name']}",
                                   f"refused arm escalation to {req} "
                                   f"(request outside spawn window)",
                                   self.alert_sound)
                # Restore a persisted arm level after a restart: only at first
                # sight, only if no fresh spawn arm_request took precedence, and
                # only once (later ticks must not re-apply a stale stored value
                # over a human's live change). The stored mode was written by a
                # prior watcher run; direct DB writes are danger.sh-blocked in
                # safe mode, and restoration only happens across a restart.
                elif (sid in self.sessions and sid not in self._mode_restored):
                    stored = d.get("mode") or ""
                    self._mode_restored.add(sid)
                    if stored in ("safe", "wild", "insane") and \
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

    def _check_escalations(self) -> None:
        """A worker sending --kind escalation is calling for a human. Ping
        (sound + notification) the moment the message is queued - even if the
        target session is busy - once per message. Runs in dry-run too:
        notify is the zero-blast-radius half, same as prompt alerts."""
        try:
            msgs = swarmdb.undelivered(self._swarm_conn())
            for m in swarm.escalation_pings(msgs, self._escalation_pinged):
                self._escalation_pinged.add(m["id"])
                self._note(f"ESCALATION from {m['from_name']} -> "
                           f"{m['to_name']}: {m['body'][:80]}")
                notify_mac(f"Relay - escalation from {m['from_name']}",
                           m["body"][:120], self.alert_sound)
        except Exception as e:
            # Never let a bad row escape into start()'s tick loop (it has no
            # per-tick except; an escape would kill the watcher outright).
            self._note(f"escalation check error: {e}")

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
                           self.alert_sound)
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

    _MODE_CYCLE = {"off": "safe", "safe": "wild", "wild": "insane", "insane": "off"}
    MODES = ("off", "safe", "wild", "insane")

    # --- iTerm2 status-bar component (opt-in) ---------------------------------

    async def _register_statusbar(self) -> None:
        """Register the per-tab arm badge on relay's connection (config-gated).
        render() reads this process's real arm state; on_click toggles it - the
        same state the TUI shows, so bar and panel stay in sync. A click is an
        un-spoofable human action, which is why arm-from-the-tab is safe. Best
        effort: a failure here must never stop the watcher starting."""
        if not getattr(self.cfg, "statusbar_enabled", False):
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
            self._note("statusbar component registered (add 'Relay' to your "
                       "iTerm2 status bar)")
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
