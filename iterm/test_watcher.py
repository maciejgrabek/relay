"""Watcher-level tests: arm-scoped alerting + poll-loop debounce.

These cover the bugs from the 2026-06-11 spam incident:
  - escalations fired for UNARMED sessions (should be armed-only),
  - the 2s poll loop re-fired the same prompt every tick (debounce broken when
    prompt_id was None / a lossy prefix).

Run: python3 iterm/test_watcher.py
"""
import asyncio
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(__file__))
# Hermetic: never read the developer's real ~/.relay/config in tests.
os.environ["RELAY_CONFIG"] = "/nonexistent/relay-test-config"
import watcher as W  # noqa: E402


class FakeSession:
    def __init__(self):
        self.sent = []
        self.names = []
        self.profiles = []

    async def async_send_text(self, t):
        self.sent.append(t)

    async def async_set_name(self, n):
        self.names.append(n)

    async def async_set_profile_properties(self, p):
        self.profiles.append(p)


def _danger():
    raw = [" Bash command", "", "   git push --force origin main", "   push",
           "", "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    return raw, [True] * len(raw)


def _safe():
    raw = [" Bash command", "", "   grep foo src/", "   search",
           "", "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    return raw, [True] * len(raw)


async def go():
    # Stub side effects so the test asserts on intent, not real notifications.
    notify = {"n": 0}
    rows = []
    W.notify_mac = lambda *a, **k: notify.__setitem__("n", notify["n"] + 1)
    # record() returns True on a durable write; the inject path now requires that
    # (log-before-act). Stub returns True so the happy path proceeds.
    W.audit.record = lambda *a, **k: (rows.append(a), True)[1]
    from watcher import Watcher, SessionInfo

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    w = Watcher(connection=None, dry_run=False)
    draw, dhard = _danger()

    # UNARMED + dangerous, polled 3x -> display only, no alerts/audit.
    fs = FakeSession()
    u = SessionInfo("u", title="unarmed", _iterm_session=fs, mode="off")
    w.sessions["u"] = u
    for _ in range(3):
        await w._handle(u, draw, dhard)
    chk("unarmed: no notify", notify["n"] == 0)
    chk("unarmed: no audit", len(rows) == 0)
    chk("unarmed: state still shows blocked", u.state == "blocked")
    chk("unarmed: never injected", fs.sent == [])

    # ARMED + dangerous, polled 3x -> exactly ONE escalation (debounced).
    fa = FakeSession()
    a = SessionInfo("a", title="armed", _iterm_session=fa, mode="safe")
    w.sessions["a"] = a
    for _ in range(3):
        await w._handle(a, draw, dhard)
    chk("armed danger: exactly 1 notify", notify["n"] == 1)
    chk("armed danger: exactly 1 audit row", len(rows) == 1)
    chk("armed danger: n_escalated == 1", a.n_escalated == 1)
    chk("armed danger: never injected", fa.sent == [])

    # ARMED + safe, polled 3x -> exactly ONE Enter (debounced).
    notify["n"] = 0
    rows.clear()
    fsafe = FakeSession()
    s = SessionInfo("s", title="armsafe", _iterm_session=fsafe, mode="safe")
    w.sessions["s"] = s
    sraw, shard = _safe()
    for _ in range(3):
        await w._handle(s, sraw, shard)
    chk("armed safe: exactly 1 Enter", fsafe.sent == ["\r"])
    chk("armed safe: n_approved == 1", s.n_approved == 1)

    # OWN TAB: even armed safe with a safe prompt, relay must never act on its
    # own panel session (that would be relay pressing keys on itself).
    w.own_sid = "me"
    fown = FakeSession()
    me = SessionInfo("me", title="relay", _iterm_session=fown, mode="safe")
    w.sessions["me"] = me
    for _ in range(3):
        await w._handle(me, sraw, shard)
    chk("own tab: never injected", fown.sent == [])
    chk("own tab: never auto-approved", me.n_approved == 0)
    # OWN TAB is not armable via ANY path (Space/toggle, set_mode, arm-all).
    me.mode = "off"
    w.toggle("me")
    chk("own tab: toggle does not arm it", me.mode == "off")
    w.set_mode("me", "insane")
    chk("own tab: set_mode does not arm it", me.mode == "off")
    other = SessionInfo("other", title="worker", _iterm_session=FakeSession(),
                        mode="off")
    w.sessions["other"] = other
    w.set_all(True)
    chk("arm-all skips own tab but arms others",
        me.mode == "off" and other.mode == "safe")
    del w.sessions["other"]
    w.own_sid = None

    # SAFETY: if the audit write FAILS, must NOT inject (escalate instead).
    W.audit.record = lambda *a, **k: False   # simulate log write failure
    notify["n"] = 0
    ff = FakeSession()
    f = SessionInfo("f", title="logfail", _iterm_session=ff, mode="safe")
    w.sessions["f"] = f
    await w._handle(f, sraw, shard)
    chk("audit-fail: NOT injected", ff.sent == [])
    chk("audit-fail: escalated instead", f.n_escalated == 1 and f.n_approved == 0)
    chk("audit-fail: alerted", notify["n"] == 1)

    # WILD mode: a DANGEROUS proceed-prompt (safe mode would escalate) is
    # approved without classification. Restore a working record() first.
    W.audit.record = lambda *a, **k: (rows.append(a), True)[1]
    fw = FakeSession()
    wild = SessionInfo("w", title="wild", _iterm_session=fw, mode="wild")
    w.sessions["w"] = wild
    await w._handle(wild, draw, dhard)   # the git-push-force danger screen
    chk("wild: dangerous proceed-prompt IS approved", fw.sent == ["\r"])
    chk("wild: counted as approved", wild.n_approved == 1)

    # WILD must still HAND OFF a real question (is_permission False).
    fq = FakeSession()
    wq = SessionInfo("wq", title="wildQ", _iterm_session=fq, mode="wild")
    w.sessions["wq"] = wq
    qraw = ["Which approach?", "❯ 1. Rewrite", "  2. Patch", "  3. Leave"]
    await w._handle(wq, qraw, [True] * len(qraw))
    chk("wild: real question NOT auto-answered", fq.sent == [])

    # INSANE mode: approves even a fail-safe permission prompt that WILD would
    # NOT (cursor NOT on option 1 -> is_proceed False but is_permission True).
    fi = FakeSession()
    ins = SessionInfo("i", title="insane", _iterm_session=fi, mode="insane")
    w.sessions["i"] = ins
    cursor2 = [" Bash command", "", "   rm -rf build", "   clean", "",
               "Do you want to proceed?", "  1. Yes", "❯ 2. No"]
    await w._handle(ins, cursor2, [True] * len(cursor2))
    chk("insane: approves even cursor-not-on-1 permission prompt", fi.sent == ["\r"])

    # INSANE still hands off a real question.
    fiq = FakeSession()
    iq = SessionInfo("iq", title="insaneQ", _iterm_session=fiq, mode="insane")
    w.sessions["iq"] = iq
    await w._handle(iq, qraw, [True] * len(qraw))
    chk("insane: real question STILL not auto-answered", fiq.sent == [])

    # COOLDOWN: a question whose menu text CHURNS (you typing an answer) must
    # NOT re-alert every poll - at most once per notify_cooldown seconds.
    notify["n"] = 0
    w.notify_cooldown = 30
    fc = FakeSession()
    qc = SessionInfo("qc", title="churn", _iterm_session=fc, mode="safe")
    w.sessions["qc"] = qc
    for i in range(8):
        churned = ["Which approach?", f"❯ 1. Rewrite{'.' * (i % 3)}",
                   "  2. Patch", "  3. Leave"]
        await w._handle(qc, churned, [True] * len(churned))
    chk("cooldown: churning question alerts at most once", notify["n"] <= 1)

    # INJECT churn guard: a permission prompt whose menu text churns each poll
    # (defeats prompt_id debounce) must auto-approve EXACTLY ONCE, not mash many
    # Enters. After it clears and a NEW prompt appears, it approves again.
    fz = FakeSession()
    iz = SessionInfo("iz", title="injchurn", _iterm_session=fz, mode="insane")
    w.sessions["iz"] = iz
    for i in range(10):
        scr = [" Bash command", "", "   echo A", "",
               "Do you want to proceed?", f"❯ 1. Yes{'.' * (i % 3)}", "  2. No"]
        await w._handle(iz, scr, [True] * len(scr))
    chk("inject-churn: same prompt approved exactly once", fz.sent == ["\r"])
    # prompt clears (working) -> re-arms
    await w._handle(iz, ["working", "· Manifesting… (1m · ↓ 2k)", "esc to interrupt"],
                    [True] * 3)
    # a genuinely different prompt -> approves again
    scr_b = [" Bash command", "", "   echo B", "",
             "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    await w._handle(iz, scr_b, [True] * len(scr_b))
    chk("inject-churn: new prompt after clear approves again", fz.sent == ["\r", "\r"])

    # BACK-TO-BACK distinct prompts with NO working frame between them (quick
    # Yes/No actions in succession). Each distinct prompt must approve - the
    # second must NOT get stuck. (Regression: an over-broad inject guard once
    # approved A then stuck on B.)
    fb = FakeSession()
    bb = SessionInfo("bb", title="b2b", _iterm_session=fb, mode="insane")
    w.sessions["bb"] = bb
    pa = [" Bash command", "", "   echo A", "", "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    pb = [" Bash command", "", "   echo B", "", "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    pc = [" Bash command", "", "   echo C", "", "Do you want to proceed?", "❯ 1. Yes", "  2. No"]
    await w._handle(bb, pa, [True] * len(pa))
    await w._handle(bb, pb, [True] * len(pb))   # no working frame between
    await w._handle(bb, pc, [True] * len(pc))
    chk("back-to-back distinct prompts each approve", fb.sent == ["\r", "\r", "\r"])

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


# A minimal idle Claude tail that satisfies swarm.claude_prompt_ready:
# a ready marker in the last 3 lines and a chrome bottom line.
_READY_SCREEN = [
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
    "  ? for shortcuts",
]


async def deliver_tests():
    """Drive Watcher._deliver directly against a fake session + monkeypatched
    swarmdb/audit/notify_mac, asserting the audit-before-act delivery contract.
    """
    from watcher import Watcher, SessionInfo
    import swarm as S

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    audited = []
    delivered = []
    W.notify_mac = lambda *a, **k: None
    W.audit.record = lambda *a, **k: (audited.append(a), True)[1]
    W.swarmdb.mark_delivered = lambda conn, mid, **k: delivered.append(mid)

    def _mk(w, sid, name, state="idle"):
        fs = FakeSession()
        info = SessionInfo(sid, title=name, _iterm_session=fs, state=state)
        info.last_screen = list(_READY_SCREEN)
        w.registry[sid] = {"name": name, "iterm_session_id": sid}
        w.sessions[sid] = info
        return info, fs

    # HAPPY PATH: idle + ready + queued + audit ok -> body then \r, THEN marked.
    W.swarmdb.undelivered = lambda conn, name=None: [
        {"id": 7, "from_name": "coord", "body": "hi"}]
    w = Watcher(connection=None, dry_run=False)
    w._db = object()                     # non-None so _swarm_conn won't connect
    info, fs = _mk(w, "sid1", "worker-1")
    await w._deliver(info)
    body = S.delivery_text("coord", "hi")
    chk("deliver: body sent then Enter (two sends)", fs.sent == [body, "\r"])
    chk("deliver: marked delivered after the sends", delivered == [7])
    chk("deliver: audited once", len(audited) == 1)

    # AUDIT FAILS: nothing sent, message NOT marked delivered.
    W.audit.record = lambda *a, **k: False
    delivered.clear()
    info2, fs2 = _mk(w, "sid2", "worker-2")
    await w._deliver(info2)
    chk("audit-fail: nothing sent", fs2.sent == [])
    chk("audit-fail: not marked delivered", delivered == [])

    # NON-IDLE: no DB query, nothing sent.
    W.audit.record = lambda *a, **k: (audited.append(a), True)[1]
    q = {"n": 0}

    def _counting_undelivered(conn, name=None):
        q["n"] += 1
        return [{"id": 9, "from_name": "c", "body": "x"}]
    W.swarmdb.undelivered = _counting_undelivered
    info3, fs3 = _mk(w, "sid3", "worker-3", state="working")
    await w._deliver(info3)
    chk("non-idle: no DB query", q["n"] == 0)
    chk("non-idle: nothing sent", fs3.sent == [])

    # DRY-RUN: nothing sent, not marked, audited once as would-deliver; a
    # second call does NOT re-audit.
    audited.clear()
    W.swarmdb.undelivered = lambda conn, name=None: [
        {"id": 11, "from_name": "c", "body": "y"}]
    w.dry_run = True
    info4, fs4 = _mk(w, "sid4", "worker-4")
    await w._deliver(info4)
    await w._deliver(info4)
    would = [a for a in audited if a and a[0] == "would-deliver"]
    chk("dry-run: nothing sent", fs4.sent == [])
    chk("dry-run: not marked delivered", 11 not in delivered)
    chk("dry-run: audited once, second call does not re-audit", len(would) == 1)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def title_tests():
    """Drive Watcher._apply_title/_restore_titles against fake sessions."""
    from watcher import Watcher, SessionInfo
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    W.notify_mac = lambda *a, **k: None
    cfg = C.Config(title_style="hybrid")

    def _mk(w, sid, bare, mode="off", state="idle"):
        fs = FakeSession()
        info = SessionInfo(sid, title=bare, _iterm_session=fs,
                           mode=mode, state=state)
        info._raw_title = bare
        w.sessions[sid] = info
        return info, fs

    # Armed session gets a prefix written once; unchanged next tick.
    w = Watcher(connection=None, dry_run=False, cfg=cfg)
    info, fs = _mk(w, "s1", "api", mode="safe", state="working")
    await w._apply_title(info)
    chk("write: armed session prefixed once", fs.names == ["◉ api"])
    info._raw_title = "◉ api"            # what iTerm now shows
    await w._apply_title(info)
    chk("write: no rewrite when unchanged", fs.names == ["◉ api"])
    chk("write: session tracked as titled", "s1" in w._titled)

    # State change rewrites; disarm+calm restores the bare name once.
    info.state = "blocked"
    await w._apply_title(info)
    chk("write: state change rewrites", fs.names[-1] == "◉[BLOCKED] api")
    info._raw_title = fs.names[-1]
    info.mode, info.state = "off", "idle"
    await w._apply_title(info)
    chk("restore: disarmed+calm restored bare", fs.names[-1] == "api")
    chk("restore: untracked after restore", "s1" not in w._titled)
    info._raw_title = "api"
    await w._apply_title(info)
    chk("restore: only once", fs.names[-1] == "api" and len(fs.names) == 3)

    # Manual+idle session never touched.
    info2, fs2 = _mk(w, "s2", "notes")
    await w._apply_title(info2)
    chk("manual+idle: never written", fs2.names == [])

    # HIDDEN + disarmed with a stale prefix: the poll loop's
    # `if info.hidden and not info.active: continue` skips _apply_title, so a
    # session that was armed (prefix written, sid in _titled), then hidden,
    # then disarmed would keep its stale prefix forever. The loop now calls
    # _apply_title once for such a titled session BEFORE the continue (see
    # start()); _apply_title itself must restore the bare name and drop the sid.
    infoh, fsh = _mk(w, "sh", "hidden", mode="safe", state="working")
    await w._apply_title(infoh)                # arm -> prefix written
    chk("hidden-stale: prefixed while armed", fsh.names == ["◉ hidden"]
        and "sh" in w._titled)
    infoh.mode, infoh.state, infoh.hidden = "off", "idle", True
    await w._apply_title(infoh)                # what the loop now does pre-skip
    chk("hidden-stale: bare name restored", fsh.names[-1] == "hidden")
    chk("hidden-stale: sid dropped from _titled", "sh" not in w._titled)

    # style=off: fully inert even for armed sessions.
    w_off = Watcher(connection=None, dry_run=False, cfg=C.Config())
    info3, fs3 = _mk(w_off, "s3", "api", mode="insane", state="blocked")
    await w_off._apply_title(info3)
    chk("style off: inert", fs3.names == [])

    # dry-run: no title writes.
    w_dry = Watcher(connection=None, dry_run=True, cfg=cfg)
    info4, fs4 = _mk(w_dry, "s4", "api", mode="safe", state="blocked")
    await w_dry._apply_title(info4)
    chk("dry-run: no writes", fs4.names == [])

    # restore-on-quit restores every titled session.
    w2 = Watcher(connection=None, dry_run=False, cfg=cfg)
    infoa, fsa = _mk(w2, "sa", "alpha", mode="safe", state="working")
    infob, fsb = _mk(w2, "sb", "beta", mode="wild", state="blocked")
    await w2._apply_title(infoa)
    await w2._apply_title(infob)
    await w2._restore_titles()
    chk("quit: all titled sessions restored",
        fsa.names[-1] == "alpha" and fsb.names[-1] == "beta"
        and not w2._titled)

    # a failing async_set_name is logged once and never raises.
    class BoomSession(FakeSession):
        async def async_set_name(self, n):
            raise RuntimeError("boom")
    w3 = Watcher(connection=None, dry_run=False, cfg=cfg)
    fsx = BoomSession()
    infox = SessionInfo("sx", title="x", _iterm_session=fsx,
                        mode="safe", state="working")
    infox._raw_title = "x"
    w3.sessions["sx"] = infox
    await w3._apply_title(infox)
    await w3._apply_title(infox)
    chk("write error: logged once, never raises",
        sum("title write failed" in l for l in w3.log) == 1)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def arm_request_tests():
    """_swarm_refresh_registry applies + clears spawn arm requests."""
    from watcher import Watcher, SessionInfo
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    cleared = []
    notified = {"n": 0}
    W.swarmdb.current_task_for = lambda conn, name: None
    W.swarmdb.clear_arm_request = lambda conn, name: cleared.append(name)
    W.notify_mac = lambda *a, **k: notified.__setitem__("n", notified["n"] + 1)

    # Request present within the grace window -> applied, cleared, and the
    # arming is escalated to the human (audible) since the operator did not
    # arm by hand.
    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w._db = object()
    info = SessionInfo("sidA", title="w1", _iterm_session=FakeSession())
    w.sessions["sidA"] = info
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "sidA", "role": "worker",
         "project": "demo", "status_text": "", "arm_request": "wild"}]
    w._swarm_refresh_registry()
    chk("spawn arm within window applied", info.mode == "wild")
    chk("spawn arm cleared", cleared == ["w1"])
    chk("spawn arm notifies human", notified["n"] == 1)

    # RACE: sid recorded a tick before the request lands (spawn creates the
    # tab, then writes arm_request). Still within grace -> honored, not
    # rejected. Simulate by seeding _arm_seen with a recent timestamp.
    cleared.clear(); notified["n"] = 0
    info_r = SessionInfo("sidR", title="wr", _iterm_session=FakeSession())
    w.sessions["sidR"] = info_r
    w._arm_seen["sidR"] = _time.time() - 2.0   # seen 2s ago, request now
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "wr", "iterm_session_id": "sidR", "role": "worker",
         "project": "demo", "status_text": "", "arm_request": "wild"}]
    w._swarm_refresh_registry()
    chk("race: request just after first sight still honored",
        info_r.mode == "wild" and cleared == ["wr"])

    # Session not seen yet -> request untouched (kept for a later tick).
    cleared.clear()
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w2", "iterm_session_id": "sidB", "role": "worker",
         "project": "demo", "status_text": "", "arm_request": "insane"}]
    w._swarm_refresh_registry()
    chk("no session yet -> request kept", cleared == [])

    # SECURITY: a request surfacing OUTSIDE the grace window on a long-running
    # session is a self-escalation attempt - refused, cleared, escalated.
    cleared.clear(); notified["n"] = 0
    w.set_mode("sidA", "safe")
    w._arm_seen["sidA"] = _time.time() - 3600.0   # first seen an hour ago
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "sidA", "role": "worker",
         "project": "demo", "status_text": "", "arm_request": "insane"}]
    w._swarm_refresh_registry()
    chk("late arm request refused (mode unchanged)", info.mode == "safe")
    chk("late arm request cleared", cleared == ["w1"])
    chk("late arm request escalates to human", notified["n"] == 1)

    # Old-schema row without the key -> no crash, nothing applied.
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w3", "iterm_session_id": "sidA", "role": "worker",
         "project": "demo", "status_text": ""}]
    w._swarm_refresh_registry()
    chk("row without arm_request tolerated", info.mode == "safe")

    # RESTART SURVIVAL: a persisted mode is restored at first sight when there
    # is no fresh arm_request and the session is currently off. Simulate a
    # freshly-started watcher (empty _mode_restored, mode off) seeing a stored
    # 'insane'.
    W.swarmdb.set_session_mode = lambda conn, name, mode: None
    w2 = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w2._db = object()
    ri = SessionInfo("sidP", title="persistw", _iterm_session=FakeSession())
    ri.mode = "off"
    w2.sessions["sidP"] = ri
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "persistw", "iterm_session_id": "sidP", "role": "worker",
         "project": "demo", "status_text": "", "arm_request": "", "mode": "insane"}]
    w2._swarm_refresh_registry()
    chk("persisted mode restored on restart", ri.mode == "insane")
    # ...but only once: a later stored change does NOT override a live human tweak
    ri.mode = "safe"
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "persistw", "iterm_session_id": "sidP", "role": "worker",
         "project": "demo", "status_text": "", "arm_request": "", "mode": "insane"}]
    w2._swarm_refresh_registry()
    chk("restore is first-sight only (human change kept)", ri.mode == "safe")

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def closed_tests():
    """closed_at marking is debounced and only runs after a good roster sync."""
    from watcher import Watcher
    import config as C
    ok = True
    def chk(n, c):
        nonlocal ok
        print(("PASS" if c else "FAIL"), n); ok = ok and c

    marked, cleared = [], []
    W.swarmdb.mark_closed = lambda conn, name, ts: marked.append(name)
    W.swarmdb.clear_closed = lambda conn, name: cleared.append(name)
    W.swarmdb.list_tasks = lambda conn, project=None, owner=None: []

    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w._db = object()
    # DB says 'w1' registered (not closed); live tabs = {} (its tab is gone).
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 0}]
    w.sessions = {}   # no live tabs

    w._mark_closed_sessions()
    chk("miss 1: not yet marked", marked == [])
    w._mark_closed_sessions()
    chk("miss 2: marked closed", marked == ["w1"])
    # once the DB row reflects closed_at != 0, the `not closed` guard stops a
    # re-mark. Simulate that by having list_sessions now report it closed.
    marked.clear()
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 123.0}]
    w._mark_closed_sessions()
    chk("already-closed row is not re-marked", marked == [])

    # tab reappears -> miss counter resets, closed cleared
    w.sessions = {"S1": object()}
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 999.0}]
    w._mark_closed_sessions()
    chk("reappeared -> clear_closed", cleared == ["w1"])

    # orphan_count: 1 closed session owning a non-done task
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "d", "iterm_session_id": "SD", "role": "worker",
         "project": "p", "closed_at": 500.0}]
    W.swarmdb.list_tasks = lambda conn, project=None, owner=None: [
        {"id": 1, "state": "doing", "owner": "d"}]
    w.sessions = {}
    w._recount_orphans()
    chk("orphan_count counts closed owners of non-done work", w.orphan_count == 1)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def own_tab_name_tests():
    """Relay names its OWN tab by design (else it shows 'caffeinate')."""
    from watcher import Watcher, SessionInfo
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    class FakeTab:
        def __init__(self):
            self.titles = []

        async def async_set_title(self, t):
            self.titles.append(t)

    fs = FakeSession()
    ft = FakeTab()
    w = Watcher(connection=None, dry_run=False, own_sid="ME")
    w.sessions["ME"] = SessionInfo("ME", title="caffeinate",
                                   _iterm_session=fs)
    w._own_tab = ft
    await w._name_own_tab()
    chk("own tab named by design", fs.names == [W.OWN_TAB_NAME])
    chk("TAB BAR title set too (session name alone leaves 'caffeinate')",
        ft.titles == [W.OWN_TAB_NAME])
    chk("tab colored relay-green",
        len(fs.profiles) == 1
        and fs.profiles[0].values.get("Use Tab Color") == "true"
        and "Tab Color" in fs.profiles[0].values)
    await w._name_own_tab()
    chk("named only once", fs.names == [W.OWN_TAB_NAME]
        and ft.titles == [W.OWN_TAB_NAME] and len(fs.profiles) == 1)
    await w._restore_own_tab()
    chk("restore clears back to auto-name", fs.names[-1] == ""
        and ft.titles[-1] == "")
    chk("restore turns the tab color off",
        len(fs.profiles) == 2
        and fs.profiles[1].values.get("Use Tab Color") == "false")

    fd = FakeSession()
    wd = Watcher(connection=None, dry_run=True, own_sid="ME")
    wd.sessions["ME"] = SessionInfo("ME", title="caffeinate",
                                    _iterm_session=fd)
    await wd._name_own_tab()
    chk("dry-run never names", fd.names == [])
    await wd._restore_own_tab()
    chk("dry-run restore is a no-op", fd.names == [])

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def escalation_ratelimit_tests():
    """A burst of escalations = ONE sound (naming the count), not a siren."""
    from watcher import Watcher
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    pings = []
    real_notify = W.notify_mac
    real_undeliv = W.swarmdb.undelivered
    rows = [{"id": i, "kind": "escalation", "from_name": f"w{i}",
             "to_name": "c", "body": f"b{i}"} for i in range(3)]
    try:
        W.notify_mac = lambda t, m, s: pings.append(m)
        W.swarmdb.undelivered = lambda conn: rows
        w = Watcher(connection=None, dry_run=False)
        w._swarm_conn = lambda: None
        w._check_escalations()
        chk("burst of 3 -> one sound naming the count",
            len(pings) == 1 and "3 pending" in pings[0])
        chk("all burst ids marked pinged",
            w._escalation_pinged == {0, 1, 2})
        rows.append({"id": 9, "kind": "escalation", "from_name": "w9",
                     "to_name": "c", "body": "late"})
        w._check_escalations()
        chk("within cooldown -> logged + marked, NO extra sound",
            len(pings) == 1 and 9 in w._escalation_pinged)
        w._esc_ping_ts = 0.0     # cooldown elapsed
        rows.append({"id": 10, "kind": "escalation", "from_name": "wA",
                     "to_name": "c", "body": "later"})
        w._check_escalations()
        chk("after cooldown -> pings again", len(pings) == 2)
    finally:
        W.notify_mac = real_notify
        W.swarmdb.undelivered = real_undeliv

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = asyncio.run(deliver_tests())
    r3 = asyncio.run(title_tests())
    r4 = arm_request_tests()
    r5 = closed_tests()
    r6 = asyncio.run(own_tab_name_tests())
    r7 = escalation_ratelimit_tests()
    sys.exit(0 if (r1 and r2 and r3 and r4 and r5 and r6 and r7) else 1)
