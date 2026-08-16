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
    # Monotonic session tally: counts the approval, and survives the tab closing
    # (unlike summed n_approved, which the mascot used to read and would dip).
    chk("monotonic approvals tally counts it", w._approvals == 1)
    del w.sessions["s"]
    chk("tally survives the tab closing", w._approvals == 1)

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

    # last_decision retains the last ACTIONABLE reason - a NONE screen (idle/
    # working) must not overwrite it with classifier noise.
    fld = FakeSession()
    ld = SessionInfo("ld", title="lastdec", _iterm_session=fld, mode="safe")
    w.sessions["ld"] = ld
    sraw2, shard2 = _safe()
    await w._handle(ld, sraw2, shard2)
    chk("last_decision set on a safe prompt", ld.last_decision == "safe permission prompt")
    await w._handle(ld, ["just some quiet screen text", "nothing to do here"], [True, True])
    chk("NONE screen does not overwrite last_decision", ld.last_decision == "safe permission prompt")

    # --- exited Claude: a shell in the foreground means there is no live prompt.
    # After you quit Claude the tab drops back to the shell, but Claude's last
    # permission frame can linger on the visible screen. The foreground job is
    # now a shell, so relay must NOT read that stale frame as actionable: no
    # blocked/LOCKED, no notify, and no stray Enter typed into your bare shell.
    notify["n"] = 0
    rows.clear()
    fex = FakeSession()
    ex = SessionInfo("ex", title="exited", _iterm_session=fex, mode="safe",
                     job="-zsh")
    w.sessions["ex"] = ex
    for _ in range(3):
        await w._handle(ex, draw, dhard)   # leftover DANGER frame under a shell
    chk("exited/shell: not blocked (no LOCKED)", ex.state != "blocked")
    chk("exited/shell: no notify", notify["n"] == 0)

    fexs = FakeSession()
    exs = SessionInfo("exs", title="exited-safe", _iterm_session=fexs,
                      mode="safe", job="zsh")
    w.sessions["exs"] = exs
    sraw3, shard3 = _safe()
    await w._handle(exs, sraw3, shard3)     # leftover SAFE frame under a shell
    chk("exited/shell: no stray Enter into shell", fexs.sent == [])

    # A live Claude prompt (foreground job 'node') must still escalate as before.
    notify["n"] = 0
    rows.clear()
    fnode = FakeSession()
    live = SessionInfo("live", title="live", _iterm_session=fnode, mode="safe",
                       job="node")
    w.sessions["live"] = live
    await w._handle(live, draw, dhard)
    chk("live claude (job=node): still escalates danger", notify["n"] == 1)
    chk("live claude (job=node): shows blocked", live.state == "blocked")

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


# A minimal idle Claude tail that satisfies swarm.claude_prompt_ready: a
# bracketed input row (box border above AND below - claude_prompt_ready's
# condition 1, shared with prompt_line_empty, requires chrome on both sides)
# with a shortcuts footer beneath it.
_READY_SCREEN = [
    "╭──────────────────────────────────────────╮",
    "│ >                                        │",
    "╰──────────────────────────────────────────╯",
    "  ? for shortcuts",
]

# Two screens that carry a perfectly good bracketed input row and are still
# NOT safe to type into. Both exist to pin the readiness call itself on the
# paths that inject arbitrary text: `info.state` is relay's own coarse
# tracking of a session and cannot see either of these, so a state=="idle"
# check alone lets relay type into a live turn (where the text lands
# mid-stream) or into a chooser (where the keystrokes NAVIGATE the menu and
# pick an entry). Bare rules, not corner boxes: session_working anchors on
# the first "^─+$" line, so a corner-drawn box would make the working screen
# read as idle and the test vacuous.
_WORKING_SCREEN = [
    "─" * 40,
    "❯",
    "─" * 40,
    "  ⏵⏵ accept edits on (shift+tab to cycle) · esc to interrupt · ← for agents",
]

_DIALOG_SCREEN = [
    "─" * 40,
    "❯",
    "─" * 40,
    "  Enter to select · ↑/↓ to navigate · Esc to cancel",
]

# The same ready screen with the operator half-way through typing a message.
# claude_prompt_ready says YES to this (it is an idle, live, non-dialog Claude
# prompt - the box just has text in it), so nothing except the draft check can
# refuse it: typing a body plus a bare "\r" here appends to the operator's
# sentence and SUBMITS it.
_DRAFT_SCREEN = [
    "╭──────────────────────────────────────────╮",
    "│ > fix the login bug before               │",
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
    body = S.batch_delivery_text([{"id": 7, "from_name": "coord",
                                   "body": "hi"}])
    chk("deliver: body sent then Enter (two sends)", fs.sent == [body, "\r"])
    chk("deliver: marked delivered after the sends", delivered == [7])
    chk("deliver: audited once", len(audited) == 1)

    # BATCH: three queued messages must cost ONE injected turn, be audited
    # once EACH (the audit trail still accounts for every message), and share
    # a single delivered_at - the timestamp `relay reply` uses to recognise
    # "the last batch" and refuse an ambiguous bare reply.
    audited.clear()
    delivered.clear()
    stamps = []
    W.swarmdb.mark_delivered = lambda conn, mid, **k: (
        delivered.append(mid), stamps.append(k.get("now")))[0]
    W.swarmdb.undelivered = lambda conn, name=None: [
        {"id": 21, "from_name": "a", "body": "one"},
        {"id": 22, "from_name": "b", "body": "two"},
        {"id": 23, "from_name": "a", "body": "three"}]
    infoB, fsB = _mk(w, "sidB", "worker-batch")
    await w._deliver(infoB)
    chk("batch: one injected turn for three messages", len(fsB.sent) == 2)
    chk("batch: all three marked delivered",
        sorted(delivered) == [21, 22, 23])
    chk("batch: one shared delivered_at",
        len(set(stamps)) == 1 and stamps[0] is not None)
    chk("batch: audited once per message", len(audited) == 3)
    chk("batch: the turn is a pointer, not three bodies",
        "relay inbox" in fsB.sent[0] and "\n" not in fsB.sent[0])
    W.swarmdb.mark_delivered = lambda conn, mid, **k: delivered.append(mid)
    delivered.clear()
    audited.clear()
    W.audit.record = lambda *a, **k: (audited.append(a), True)[1]

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

    # RESERVED NAME: even with a live registry row bound to 'human' (the
    # legacy-DB reproduction from Finding 1 - a row db.register would refuse
    # to create today but an upgraded DB can still contain), _deliver must
    # refuse to inject, without even querying the DB.
    q5 = {"n": 0}

    def _counting_undelivered5(conn, name=None):
        q5["n"] += 1
        return [{"id": 13, "from_name": "coord", "body": "escalation leak"}]
    W.swarmdb.undelivered = _counting_undelivered5
    info5, fs5 = _mk(w, "sid5", "human")
    w.dry_run = False
    await w._deliver(info5)
    chk("reserved name: no DB query", q5["n"] == 0)
    chk("reserved name: nothing sent", fs5.sent == [])
    chk("reserved name: not marked delivered", 13 not in delivered)

    # SHELL JOB (Task 3 fix round 3, CRITICAL): the operator quit Claude in a
    # registered swarm tab. iTerm2 now reports a login shell as the foreground
    # job, but Claude's box+footer chrome is still painted on the visible
    # screen - so claude_prompt_ready alone can read that dead frame as ready
    # and _deliver would type the message body, then a bare "\r", straight
    # into a live shell: the body executes as a command. _handle already
    # returns early for shells, but the poll loop calls _deliver regardless,
    # so the refusal has to live here too. The screen is deliberately the
    # fully-ready one - the job is the ONLY thing saying no.
    q6 = {"n": 0}

    def _counting_undelivered6(conn, name=None):
        q6["n"] += 1
        return [{"id": 17, "from_name": "coord", "body": "rm -rf ~/work"}]
    W.swarmdb.undelivered = _counting_undelivered6
    info6, fs6 = _mk(w, "sid6", "worker-6")
    info6.job = "-zsh"
    await w._deliver(info6)
    chk("shell job: no DB query", q6["n"] == 0)
    chk("shell job: nothing typed into the shell", fs6.sent == [])
    chk("shell job: not marked delivered", 17 not in delivered)

    # Control: the identical session with a live Claude foreground job still
    # delivers - the guard must key on the job, not have broken delivery.
    info7, fs7 = _mk(w, "sid7", "worker-7")
    info7.job = "node"
    await w._deliver(info7)
    chk("live claude job: still delivers", len(fs7.sent) == 2)

    # SCREEN READINESS (final branch review, Finding 2). _deliver's screen gate
    # was entirely unpinned: deleting `swarm.claude_prompt_ready(...)` from its
    # condition left every suite green, so the branch's headline property -
    # relay no longer types into a working session or a chooser - was unproven
    # on the path that sends arbitrary operator text. `info.state` is relay's
    # own coarse tracking and says "idle" on both screens below; only the
    # screen knows better, so these two cases isolate the readiness call.
    #
    # First, prove the screens are unready for the REASON claimed, so a future
    # edit cannot make these pass vacuously (e.g. by breaking the input-row
    # match, which would make BOTH screens unready for the wrong reason).
    chk("working screen: has a real bracketed input row",
        S._bracketed_input_rows(_WORKING_SCREEN) != [])
    chk("working screen: is unready because it is mid-turn",
        S.session_working(_WORKING_SCREEN) is True
        and S.claude_prompt_ready(_WORKING_SCREEN) is False)
    chk("dialog screen: has a real bracketed input row",
        S._bracketed_input_rows(_DIALOG_SCREEN) != [])
    chk("dialog screen: is unready because it is a chooser",
        S.selection_dialog(_DIALOG_SCREEN) is True
        and S.session_working(_DIALOG_SCREEN) is False
        and S.claude_prompt_ready(_DIALOG_SCREEN) is False)

    for screen, sid, why in ((_WORKING_SCREEN, "sid8", "a working screen"),
                             (_DIALOG_SCREEN, "sid9", "a selection dialog")):
        qn = {"n": 0}

        def _counting(conn, name=None, _qn=qn):
            _qn["n"] += 1
            return [{"id": 31, "from_name": "coord", "body": "do the thing"}]
        W.swarmdb.undelivered = _counting
        infoS, fsS = _mk(w, sid, "worker-" + sid)
        infoS.job = "node"
        infoS.last_screen = list(screen)
        await w._deliver(infoS)
        chk(f"{why} with state=idle: nothing typed", fsS.sent == [])
        chk(f"{why} with state=idle: no DB query", qn["n"] == 0)
        chk(f"{why} with state=idle: not marked delivered",
            31 not in delivered)

    # Control: the same session, same job, same queue - only the screen
    # changes - and delivery happens. The gate is the screen, not a blanket
    # refusal that would make the two cases above pass for free.
    W.swarmdb.undelivered = lambda conn, name=None: [
        {"id": 32, "from_name": "coord", "body": "do the thing"}]
    infoR, fsR = _mk(w, "sid10", "worker-sid10")
    infoR.job = "node"
    infoR.last_screen = list(_READY_SCREEN)
    await w._deliver(infoR)
    chk("ready screen, same job and queue: still delivers", len(fsR.sent) == 2)

    # DRAFT PROTECTION ([swarm] respect_draft, default ON). _fire_extreme has
    # always refused to type over a half-written message; _deliver did not, so
    # a queued swarm message landing in that window appended to the operator's
    # sentence and pressed Enter - text destroyed, turn spent. The screen below
    # is READY by every other measure, so the draft check is the only thing
    # that can refuse it (proved first, so this cannot pass vacuously).
    import config as C
    chk("draft screen is ready but not empty (not a vacuous fixture)",
        S.claude_prompt_ready(_DRAFT_SCREEN) is True
        and S.prompt_line_empty(_DRAFT_SCREEN) is False)

    qd = {"n": 0}

    def _counting_draft(conn, name=None):
        qd["n"] += 1
        return [{"id": 41, "from_name": "coord", "body": "do the thing"}]
    W.swarmdb.undelivered = _counting_draft
    wD = Watcher(connection=None, dry_run=False, cfg=C.Config())
    wD._db = object()
    infoD, fsD = _mk(wD, "sid11", "worker-draft")
    infoD.job = "node"
    infoD.last_screen = list(_DRAFT_SCREEN)
    await wD._deliver(infoD)
    chk("respect_draft on: nothing typed over an operator draft",
        fsD.sent == [])
    chk("respect_draft on: the message stays queued (no DB query, not marked)",
        qd["n"] == 0 and 41 not in delivered)

    # Control: the SAME watcher (respect_draft still on), same job, same queue
    # - only the input box is empty - and it delivers. Without this a blanket
    # refusal would pass the case above for free.
    W.swarmdb.undelivered = lambda conn, name=None: [
        {"id": 42, "from_name": "coord", "body": "do the thing"}]
    infoDC, fsDC = _mk(wD, "sid12", "worker-draft-control")
    infoDC.job = "node"
    infoDC.last_screen = list(_READY_SCREEN)
    await wD._deliver(infoDC)
    chk("respect_draft on: an empty input box still delivers",
        len(fsDC.sent) == 2)

    # OFF -> exactly today's behaviour: it types over the draft.
    W.swarmdb.undelivered = lambda conn, name=None: [
        {"id": 43, "from_name": "coord", "body": "do the thing"}]
    wOff = Watcher(connection=None, dry_run=False,
                   cfg=C.Config(respect_draft=False))
    wOff._db = object()
    infoOff, fsOff = _mk(wOff, "sid13", "worker-draft-off")
    infoOff.job = "node"
    infoOff.last_screen = list(_DRAFT_SCREEN)
    await wOff._deliver(infoOff)
    chk("respect_draft off: delivers over the draft (opted-out behaviour)",
        len(fsOff.sent) == 2)

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


async def session_path_tests():
    """Watcher._session_path and SessionInfo.workdir: the in-memory cwd cache
    that gives an UNREGISTERED tab a workdir at all (see set_watcher_workdir
    in db.py for the registered-only DB counterpart)."""
    from watcher import Watcher, SessionInfo

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    chk("SessionInfo defaults workdir to ''", SessionInfo("s").workdir == "")

    # async_get_variable absent/raising -> '' (same defensive shape as
    # _session_job/jobName; FakeSession has no async_get_variable at all, so
    # calling it exercises the except branch directly).
    fs = FakeSession()
    got = await Watcher._session_path(fs)
    chk("_session_path: unreadable variable -> ''", got == "")

    class BoomVarSession(FakeSession):
        async def async_get_variable(self, name):
            raise RuntimeError("boom")
    got2 = await Watcher._session_path(BoomVarSession())
    chk("_session_path: async_get_variable raising -> ''", got2 == "")

    class PathSession(FakeSession):
        async def async_get_variable(self, name):
            assert name == "path"
            return "  /work/relay  "
    got3 = await Watcher._session_path(PathSession())
    chk("_session_path: strips the variable's value", got3 == "/work/relay")

    class BlankPathSession(FakeSession):
        async def async_get_variable(self, name):
            return "   "
    got4 = await Watcher._session_path(BlankPathSession())
    chk("_session_path: whitespace-only value -> ''", got4 == "")

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


class FakeIT2Session(FakeSession):
    """A fake iterm2.Session with the bits _sync_sessions reads: session_id
    plus async_get_variable for path/jobName/titleOverride/autoName."""
    def __init__(self, session_id, path="", job=""):
        super().__init__()
        self.session_id = session_id
        self._path = path
        self._job = job

    async def async_get_variable(self, name):
        if name == "path":
            return self._path
        if name == "jobName":
            return self._job
        return None   # titleOverride/autoName: unset, falls through to sid


class FakeIT2Tab:
    """A fake iterm2.Tab: sessions plus the titleOverride lookup
    _session_label falls back to."""
    def __init__(self, sessions):
        self.sessions = sessions

    async def async_get_variable(self, name):
        return None


class FakeIT2Window:
    def __init__(self, tabs):
        self.tabs = tabs


class FakeIT2App:
    def __init__(self, windows, selected=None):
        self.windows = windows
        # iTerm2 exposes the focused session through this chain. `selected` is
        # the session_id the operator is sitting in, or None for "no window".
        self.current_terminal_window = (
            _FakeCurrentWindow(selected) if selected is not None else None)


class _FakeCurrentSession:
    def __init__(self, sid):
        self.session_id = sid


class _FakeCurrentTab:
    def __init__(self, sid):
        self.current_session = _FakeCurrentSession(sid)


class _FakeCurrentWindow:
    def __init__(self, sid):
        self.current_tab = _FakeCurrentTab(sid)


async def selected_tab_tests():
    """The burn badge holds its clock at zero for the tab you are sitting in,
    so _sync_sessions has to stamp which session iTerm2 has selected. A
    timestamp rather than a flag: a deselected tab keeps its last value, so a
    caller can ask how long ago you left."""
    from watcher import Watcher
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w._db = object()
    w.registry = {}
    s0 = FakeIT2Session("S0", path="/work/a", job="node")
    s1 = FakeIT2Session("S1", path="/work/b", job="node")
    tabs = [FakeIT2Tab([s0]), FakeIT2Tab([s1])]

    await w._sync_sessions(FakeIT2App([FakeIT2Window(tabs)], selected="S1"))
    chk("the selected session is stamped", w.sessions["S1"].selected_at > 0)
    chk("an unselected session is not", w.sessions["S0"].selected_at == 0.0)
    t1 = w.sessions["S1"].selected_at

    await w._sync_sessions(FakeIT2App([FakeIT2Window(tabs)], selected="S0"))
    chk("the stamp moves with the selection", w.sessions["S0"].selected_at > 0)
    chk("a deselected tab KEEPS its stamp (timestamp, not flag)",
        w.sessions["S1"].selected_at == t1)

    # No window at all (every terminal closed but relay still running) must
    # cost the stamp, not the sync.
    await w._sync_sessions(FakeIT2App([FakeIT2Window(tabs)], selected=None))
    chk("no current window does not raise", w.sessions["S0"].selected_at > 0)

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def sync_sessions_workdir_persist_tests():
    """Regression test for 5530f90: the per-tick workdir write was gated on
    the in-memory SessionInfo.workdir (the previous tick's cache), which is
    populated for EVERY tab regardless of registration. In the normal
    ordering the watcher tracks a tab first and `relay register` runs some
    ticks later, so that cache was already non-empty by the time the session
    registered - the gate was false forever and set_watcher_workdir was
    never called for a real registration. The fix gates on the DB-persisted
    state (reg["workdir"], from self.registry) instead."""
    from watcher import Watcher
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    calls = []
    W.swarmdb.set_watcher_workdir = lambda conn, name, path: calls.append((name, path))

    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w._db = object()   # _swarm_conn must not try a real connect()

    sid = "SID1"
    sess = FakeIT2Session(sid, path="/work/proj1", job="node")
    app = FakeIT2App([FakeIT2Window([FakeIT2Tab([sess])])])

    # tick 1: tab tracked, NOT registered -> no write, but the in-memory
    # cache still picks up the cwd (that's the whole point of Task 4).
    w.registry = {}
    await w._sync_sessions(app)
    chk("tick1 unregistered: no write", calls == [])
    chk("tick1 unregistered: cwd cached in memory anyway",
        w.sessions[sid].workdir == "/work/proj1")

    # tick 2: now registered, DB row's workdir still empty (relay register
    # just ran; nothing has persisted it yet) -> exactly one write, even
    # though info.workdir was already set on the previous tick.
    w.registry = {sid: {"name": "w1", "workdir": ""}}
    await w._sync_sessions(app)
    chk("tick2 just registered: write fires exactly once",
        calls == [("w1", "/work/proj1")])

    # tick 3: DB row now reflects the persisted workdir -> no further write
    # (the optimization this commit exists for).
    w.registry = {sid: {"name": "w1", "workdir": "/work/proj1"}}
    await w._sync_sessions(app)
    chk("tick3 workdir persisted: no further write", calls == [("w1", "/work/proj1")])

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

    marked, cleared, orphaned = [], [], []
    W.swarmdb.mark_closed = lambda conn, name, ts: marked.append(name) or True
    W.swarmdb.clear_closed = lambda conn, name: cleared.append(name)
    W.swarmdb.list_tasks = lambda conn, project=None, owner=None: []
    W.swarmdb.orphan_parked = lambda conn, name: orphaned.append(name)

    w = Watcher(connection=None, dry_run=False, cfg=C.Config())
    w._db = object()
    # DB says 'w1' registered (not closed); live tabs = {} (its tab is gone).
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 0}]
    w.sessions = {}   # no live tabs

    w._mark_closed_sessions()
    chk("miss 1: not yet marked", marked == [])
    chk("miss 1: not yet orphaned", orphaned == [])
    w._mark_closed_sessions()
    chk("miss 2: marked closed", marked == ["w1"])
    chk("miss 2: parked work orphaned to the directory", orphaned == ["w1"])
    # once the DB row reflects closed_at != 0, the `not closed` guard stops a
    # re-mark, so orphan_parked (which only fires on a successful mark_closed)
    # is not called again either.
    marked.clear()
    orphaned.clear()
    W.swarmdb.list_sessions = lambda conn: [
        {"name": "w1", "iterm_session_id": "S1", "role": "worker",
         "project": "p", "closed_at": 123.0}]
    w._mark_closed_sessions()
    chk("already-closed row is not re-marked", marked == [])
    chk("already-closed row is not re-orphaned", orphaned == [])

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
        W.notify_mac = lambda t, m, s, **k: pings.append(m)
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


async def statusbar_registration_tests():
    """The badge has ONE owner. relay registers in-process ONLY when the
    AutoLaunch provider is not installed; when the provider symlink exists,
    relay defers to it (never a second registration -> no DUPLICATE freeze)."""
    from watcher import Watcher
    import config as C
    import statusbar as SB
    import tempfile

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    # Fake the iTerm2 component so the in-process branch needs no real
    # connection - we only care WHETHER relay tries to register.
    registered = {"n": 0}

    class FakeComponent:
        def __init__(self, **kw):
            pass

        async def async_register(self, conn, render, onclick=None):
            registered["n"] += 1

    real_component = W.iterm2.StatusBarComponent
    real_rpc = W.iterm2.StatusBarRPC
    W.iterm2.StatusBarComponent = FakeComponent
    W.iterm2.StatusBarRPC = lambda fn: fn      # identity: no live connection

    tmp = tempfile.mkdtemp()
    link = os.path.join(tmp, "relay_statusbar.py")
    alive = os.path.join(tmp, "provider.alive")
    saved = {k: os.environ.get(k) for k in
             ("RELAY_STATUSBAR_AUTOLAUNCH", "RELAY_STATUSBAR_ALIVE")}
    os.environ["RELAY_STATUSBAR_AUTOLAUNCH"] = link
    os.environ["RELAY_STATUSBAR_ALIVE"] = alive
    cfg = C.Config(statusbar_enabled=True)
    try:
        # (a) provider NOT installed -> relay renders in-process.
        w = Watcher(connection=None, dry_run=False, cfg=cfg)
        registered["n"] = 0
        await w._register_statusbar()
        chk("no provider -> relay registers in-process", registered["n"] == 1)

        # (b) provider installed, not running -> relay must NOT register in
        # process; instead it auto-starts the provider (statusbar_ensure). Inject
        # a fake ensure so the test stays hermetic (no real osascript / spawn).
        open(link, "w").close()
        import statusbar_ensure as SE
        real_ensure = SE.ensure
        SE.ensure = lambda: "start"
        try:
            w2 = Watcher(connection=None, dry_run=False, cfg=cfg)
            registered["n"] = 0
            await w2._register_statusbar()
        finally:
            SE.ensure = real_ensure
        chk("provider installed -> relay does NOT register (no collision)",
            registered["n"] == 0)
        chk("provider installed but idle -> relay auto-starts it",
            any("started" in l for l in w2.log))

        # (c) provider installed AND alive -> defer, say so.
        SB.touch_provider_alive(path=alive)
        w3 = Watcher(connection=None, dry_run=False, cfg=cfg)
        registered["n"] = 0
        await w3._register_statusbar()
        chk("provider alive -> relay does NOT register", registered["n"] == 0)
        chk("provider alive -> served-by note",
            any("served by AutoLaunch" in l for l in w3.log))

        # (d) statusbar disabled -> inert (no registration, no note).
        w4 = Watcher(connection=None, dry_run=False,
                     cfg=C.Config(statusbar_enabled=False))
        registered["n"] = 0
        await w4._register_statusbar()
        chk("statusbar disabled -> nothing",
            registered["n"] == 0 and not w4.log)
    finally:
        W.iterm2.StatusBarComponent = real_component
        W.iterm2.StatusBarRPC = real_rpc
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def parked_badge_cache_tests():
    """The parked count on the badge must agree whether it is served by the
    AutoLaunch provider (_statusbar_publish writes the state file it reads)
    or rendered in-process (the StatusBarRPC render callback, used only when
    no AutoLaunch provider is installed). Both must show the same count from
    the SAME per-tick bucket - a session that has not run ./install.sh must
    not silently see 0 parked while relay task add --park tells them to look
    at this exact badge."""
    from watcher import Watcher, SessionInfo
    import config as C
    import statusbar as SB
    import tempfile as _tempfile

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    # A freshly constructed watcher always carries the cache attribute, even
    # before any tick has published - so an RPC render that fires before the
    # first publish (or while the status bar is disabled/dry-run, in which
    # case _statusbar_publish returns early and never populates it) reads {}
    # instead of raising.
    w0 = Watcher(connection=None, dry_run=False)
    chk("fresh watcher has _parked_by_workdir", w0._parked_by_workdir == {})

    real_list_parked = W.swarmdb.list_parked
    real_component = W.iterm2.StatusBarComponent
    real_rpc = W.iterm2.StatusBarRPC
    # realpath'd up front: macOS puts tempdirs under a symlink (/var ->
    # /private/var), and the real list_parked/_statusbar_label pair both
    # resolve symlinks before comparing workdirs (db._norm_workdir /
    # swarm.real_workdir) - resolving here keeps the fake rows' workdir
    # column exactly what the real DB would have stored, instead of testing
    # a mismatch that is an artifact of this harness, not the code.
    tmp = os.path.realpath(_tempfile.mkdtemp())
    saved = {k: os.environ.get(k) for k in
             ("RELAY_STATUSBAR_AUTOLAUNCH", "RELAY_STATUSBAR_ALIVE",
              "RELAY_STATUSBAR_STATE")}
    os.environ["RELAY_STATUSBAR_AUTOLAUNCH"] = os.path.join(tmp, "nope.py")
    os.environ["RELAY_STATUSBAR_ALIVE"] = os.path.join(tmp, "nope.alive")
    state_path = os.path.join(tmp, "statusbar.json")
    os.environ["RELAY_STATUSBAR_STATE"] = state_path
    try:
        workdir = os.path.join(tmp, "proj")
        os.makedirs(workdir, exist_ok=True)
        W.swarmdb.list_parked = lambda conn: [
            {"workdir": workdir}, {"workdir": workdir}, {"workdir": workdir}]

        cfg = C.Config(statusbar_enabled=True)
        w = Watcher(connection=None, dry_run=False, cfg=cfg)
        w._swarm_conn = lambda: None
        sid = "s1"
        w.sessions[sid] = SessionInfo(sid, title="t",
                                      _iterm_session=FakeSession(),
                                      mode="safe", workdir=workdir)

        # --- publish path: builds the bucket once and caches it -------------
        w._statusbar_publish()
        chk("publish caches the bucket on the watcher",
            w._parked_by_workdir.get(W.swarm.real_workdir(workdir)) == 3)
        published = SB.read_state_label(sid, path=state_path)
        chk("publish path badge shows the parked count",
            "3 PARKED" in published)

        # --- RPC render path: drive the real render() closure through
        # _register_statusbar on the SAME watcher, so it reads the SAME
        # cache _statusbar_publish just built (no second query). ------------
        captured = {}

        class FakeComponent:
            def __init__(self, **kw):
                pass

            async def async_register(self, conn, render, onclick=None):
                captured["render"] = render

        W.iterm2.StatusBarComponent = FakeComponent
        W.iterm2.StatusBarRPC = lambda fn: fn      # identity: no live connection
        await w._register_statusbar()
        rendered = await captured["render"](None, session_id=sid)
        chk("RPC render path agrees with the publish path",
            rendered == published and "3 PARKED" in rendered)
    finally:
        W.swarmdb.list_parked = real_list_parked
        W.iterm2.StatusBarComponent = real_component
        W.iterm2.StatusBarRPC = real_rpc
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def legible_spine_tests():
    """Watcher emits differentiated sounds + a _last_event pulse, and detects
    task completions edge-triggered (silent on the first tick)."""
    from watcher import Watcher, _notify_sound
    from gates import DANGEROUS_COMMAND
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    # Pure sound routing.
    chk("danger reason -> danger sound",
        _notify_sound(DANGEROUS_COMMAND, danger="D", alert="A") == "D")
    chk("other reason -> alert sound",
        _notify_sound("real question - hands off", danger="D", alert="A") == "A")

    # Escalations play the MESSAGE sound (was alert).
    captured = []
    real_notify = W.notify_mac
    real_undeliv = W.swarmdb.undelivered
    real_pings = W.swarm.escalation_pings
    try:
        row = {"id": 1, "kind": "escalation", "from_name": "w1",
               "to_name": "c", "body": "help"}
        W.notify_mac = lambda t, m, s, **k: captured.append(s)
        W.swarmdb.undelivered = lambda conn: [row]
        W.swarm.escalation_pings = lambda msgs, seen: msgs
        w = Watcher(connection=None, dry_run=False)
        w._swarm_conn = lambda: None
        w._check_escalations()
        chk("escalation uses message_sound",
            captured and captured[0] == w.message_sound)
    finally:
        W.notify_mac = real_notify
        W.swarmdb.undelivered = real_undeliv
        W.swarm.escalation_pings = real_pings

    # Completions: seed silently on the first tick, fire on a NEW done id.
    fired = []
    real_notify2 = W.notify_mac
    real_list = W.swarmdb.list_tasks
    try:
        tasks = [{"id": 1, "state": "done"}]
        W.swarmdb.list_tasks = lambda conn: list(tasks)
        W.notify_mac = lambda t, m, s, **k: fired.append(s)
        w2 = Watcher(connection=None, dry_run=False)
        w2._swarm_conn = lambda: None
        w2._check_completions()
        chk("first tick seeds, does NOT fire", fired == []
            and w2._last_event is None)
        tasks.append({"id": 2, "state": "done"})
        w2._check_completions()
        chk("new done id fires done event + chime",
            len(fired) == 1 and fired[0] == w2.done_sound
            and w2._last_event is not None and w2._last_event[0] == "done")
        w2._check_completions()
        chk("no new done -> no repeat fire", len(fired) == 1)
    finally:
        W.notify_mac = real_notify2
        W.swarmdb.list_tasks = real_list

    # A malformed task row (missing "id") must not raise out of the whole
    # method - the try must cover the set comprehension + notify, not just
    # the list_tasks call.
    real_list2 = W.swarmdb.list_tasks
    try:
        W.swarmdb.list_tasks = lambda conn: [{"state": "done"}]
        w3 = Watcher(connection=None, dry_run=False)
        w3._swarm_conn = lambda: None
        try:
            w3._check_completions()
            chk("malformed task row does not raise", True)
        except Exception:
            chk("malformed task row does not raise", False)
    finally:
        W.swarmdb.list_tasks = real_list2

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def pause_tests():
    """Paused relay freezes the hands (no inject, no delivery) but keeps the
    eyes (still classifies + notifies danger). Resume restores acting."""
    from watcher import Watcher, SessionInfo
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    notify = {"n": 0}
    rows = []
    real_notify = W.notify_mac
    real_record = W.audit.record
    W.notify_mac = lambda *a, **k: notify.__setitem__("n", notify["n"] + 1)
    W.audit.record = lambda *a, **k: (rows.append(a), True)[1]
    try:
        w = Watcher(connection=None, dry_run=False)

        # PAUSED: an armed-safe tab with a safe prompt is NOT injected.
        w.paused = True
        fs = FakeSession()
        s = SessionInfo("s", title="x", _iterm_session=fs, mode="safe")
        w.sessions["s"] = s
        sraw, shard = _safe()
        await w._handle(s, sraw, shard)
        chk("paused: safe prompt not injected", fs.sent == [])
        chk("paused: not counted approved", s.n_approved == 0)

        # PAUSED: a dangerous prompt STILL notifies (eyes stay open).
        notify["n"] = 0
        fd = FakeSession()
        d = SessionInfo("d", title="d", _iterm_session=fd, mode="safe")
        w.sessions["d"] = d
        draw, dhard = _danger()
        await w._handle(d, draw, dhard)
        chk("paused: danger still notifies", notify["n"] == 1)
        chk("paused: danger never injects", fd.sent == [])

        # RESUME: the same safe prompt now injects.
        w.paused = False
        fs2 = FakeSession()
        s2 = SessionInfo("s2", title="x2", _iterm_session=fs2, mode="safe")
        w.sessions["s2"] = s2
        await w._handle(s2, sraw, shard)
        chk("resumed: safe prompt injected", fs2.sent == ["\r"])

        # toggle_pause flips state and records the transition.
        rows.clear()
        was = w.toggle_pause()
        chk("toggle_pause returns new state (paused)", was is True and w.paused)
        chk("pause audited", rows and rows[-1][0] == "paused")
        w.toggle_pause()
        chk("resume audited", rows[-1][0] == "resumed" and not w.paused)
    finally:
        W.notify_mac = real_notify
        W.audit.record = real_record

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def shadow_tests():
    """Shadow tab records what safe WOULD do, never injects, never notifies."""
    from watcher import Watcher, SessionInfo
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    notify = {"n": 0}
    rows = []
    real_notify = W.notify_mac
    real_record = W.audit.record
    W.notify_mac = lambda *a, **k: notify.__setitem__("n", notify["n"] + 1)
    W.audit.record = lambda *a, **k: (rows.append(a), True)[1]
    try:
        w = Watcher(connection=None, dry_run=False)

        # Shadow + safe prompt -> would-approve, no inject, no notify.
        fs = FakeSession()
        s = SessionInfo("s", title="x", _iterm_session=fs, mode="shadow")
        w.sessions["s"] = s
        chk("shadow is not active", s.active is False)
        sraw, shard = _safe()
        await w._handle(s, sraw, shard)
        chk("shadow safe: would-approve recorded",
            rows and rows[-1][0] == "would-approve")
        chk("shadow safe: never injected", fs.sent == [])
        chk("shadow safe: never notified", notify["n"] == 0)
        chk("shadow safe: shows cleared", s.state == "cleared")
        # Debounce: same prompt does not re-record.
        n_before = len(rows)
        await w._handle(s, sraw, shard)
        chk("shadow: debounced (no re-record)", len(rows) == n_before)

        # Shadow + dangerous prompt -> would-escalate, still silent.
        rows.clear()
        notify["n"] = 0
        fd = FakeSession()
        d = SessionInfo("d", title="d", _iterm_session=fd, mode="shadow")
        w.sessions["d"] = d
        draw, dhard = _danger()
        await w._handle(d, draw, dhard)
        chk("shadow danger: would-escalate recorded",
            rows and rows[-1][0] == "would-escalate")
        chk("shadow danger: never notified", notify["n"] == 0)
        chk("shadow danger: never injected", fd.sent == [])

        # toggle_shadow flips shadow <-> off; Space from shadow -> safe.
        fz = FakeSession()
        z = SessionInfo("z", title="z", _iterm_session=fz, mode="off")
        w.sessions["z"] = z
        w.toggle_shadow("z")
        chk("toggle_shadow: off -> shadow", z.mode == "shadow")
        w.toggle("z")
        chk("Space from shadow -> safe", z.mode == "safe")
        w.toggle_shadow("z")
        chk("toggle_shadow: from any -> shadow", z.mode == "shadow")
        w.toggle_shadow("z")
        chk("toggle_shadow: shadow -> off", z.mode == "off")
    finally:
        W.notify_mac = real_notify
        W.audit.record = real_record

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def notify_mac_tests():
    """notify_mac routes through terminal-notifier (attributed to iTerm, click
    jumps to the session) when it's on PATH, and falls back to osascript
    otherwise. Asserts on the spawned argv, not on real notifications."""
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    # Earlier tests overwrite W.notify_mac with counter stubs and don't restore
    # it, so reload to get the real implementation back before exercising it.
    import importlib
    importlib.reload(W)

    calls = []
    real_popen = W.subprocess.Popen
    real_tn = W._TERMINAL_NOTIFIER
    try:
        W.subprocess.Popen = lambda cmd, **k: calls.append(cmd)

        # --- terminal-notifier present, with a session_id -> click jumps ---
        W._TERMINAL_NOTIFIER = "/opt/tn"
        calls.clear()
        W.notify_mac("Relay - w1", "danger", None, session_id="ABC-123")
        argv = calls[0]
        chk("uses terminal-notifier binary", argv[0] == "/opt/tn")
        chk("attributed to iTerm via -sender",
            "-sender" in argv
            and argv[argv.index("-sender") + 1] == W.ITERM_BUNDLE_ID)
        chk("click focuses the session via -execute + focus script",
            "-execute" in argv
            and argv[argv.index("-execute") + 1]
                == f'"{W._FOCUS_SCRIPT}" ABC-123')
        chk("no -activate when a session is targeted", "-activate" not in argv)

        # --- terminal-notifier present, no session_id -> just activate iTerm ---
        calls.clear()
        W.notify_mac("Relay - done", "1 task", None)
        argv = calls[0]
        chk("global notify activates iTerm generally",
            "-activate" in argv
            and argv[argv.index("-activate") + 1] == W.ITERM_BUNDLE_ID
            and "-execute" not in argv)

        # --- sound still fires afplay alongside the notification ---
        calls.clear()
        W.notify_mac("Relay - w1", "x", "/S.aiff", session_id="ABC")
        chk("sound spawns afplay too",
            any(c[:1] == ["afplay"] and "/S.aiff" in c for c in calls))

        # --- terminal-notifier absent -> osascript fallback (no click) ---
        W._TERMINAL_NOTIFIER = None
        calls.clear()
        W.notify_mac("Relay - w1", 'has "quote" and \\ back', None)
        argv = calls[0]
        chk("falls back to osascript", argv[0] == "osascript")
        chk("osascript neutralizes the message's double quotes",
            "has 'quote'" in argv[-1] and 'has "quote"' not in argv[-1])
    finally:
        W.subprocess.Popen = real_popen
        W._TERMINAL_NOTIFIER = real_tn

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def mute_tests():
    """[sounds] enabled = false silences every notify sound without losing the
    four picks, and flipping it back (as the settings editor does, live)
    restores them."""
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    import importlib
    importlib.reload(W)
    import dataclasses
    import config as C

    cfg = dataclasses.replace(C.Config(), sounds_enabled=False)
    w = W.Watcher(connection=None, dry_run=False, cfg=cfg)
    chk("muted: all four sounds read as silence",
        (w.alert_sound, w.done_sound, w.danger_sound, w.message_sound)
        == ("", "", "", ""))
    chk("muted: the danger/alert pick is silent too",
        W._notify_sound(W.DANGEROUS_COMMAND, danger=w.danger_sound,
                        alert=w.alert_sound) == "")

    # Live un-mute (settings editor does exactly this setattr).
    w.sounds_enabled = True
    chk("un-mute restores the original picks, not defaults-from-nothing",
        w.alert_sound == cfg.alert_sound and w.done_sound == cfg.done_sound
        and w.danger_sound == cfg.danger_sound
        and w.message_sound == cfg.message_sound)

    # Live mute on a running watcher started un-muted.
    w2 = W.Watcher(connection=None, dry_run=False, cfg=C.Config())
    chk("default watcher is audible", w2.alert_sound != "")
    w2.sounds_enabled = False
    chk("live mute silences it", w2.alert_sound == "")

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


async def timer_tests():
    """Watcher fires due timers: now fires immediately, idle waits for ready,
    pause freezes, require_armed gates, past-reconfirm deactivates."""
    import tempfile
    from watcher import Watcher, SessionInfo
    import db as D
    import config as C

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    W.notify_mac = lambda *a, **k: None
    W.audit.record = lambda *a, **k: True

    # Hermetic temp DB shared between D.connect() (this test) and the
    # watcher's own _swarm_conn() (both honor RELAY_DB, read at call time).
    tmp = tempfile.mkdtemp()
    saved_relay_db = os.environ.get("RELAY_DB")
    os.environ["RELAY_DB"] = os.path.join(tmp, "relay-timers-test.db")
    try:
        conn = D.connect()
        # A recent (not epoch-0) bound_at/last_fired_at: overdue by the 1-minute
        # interval (due), but nowhere near timers_reconfirm_days old (not
        # stale) - isolates the now/idle/pause/require_armed checks below from
        # the stale-binding guard, which s5 tests on its own further down.
        fresh = _time.time() - 120.0
        D.add_timer(conn, iterm_session_id="s1", label="api", interval_min=1,
                    payload="run lint", mode="now", now=fresh)
        w = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs = FakeSession()
        info = SessionInfo("s1", title="api", _iterm_session=fs, mode="safe",
                           state="working")
        # Every SessionInfo below carries a screen, not the empty default: with
        # [swarm] respect_draft on (the default), _fire_timers consults
        # prompt_line_empty, which reads a screen with no input row at all as
        # "not free". An empty last_screen is not a state a live session is
        # ever in when _fire_timers runs (the poll loop captures the screen
        # first), so this is a fixture completing itself, not a relaxed
        # assertion - each case still tests exactly what it tested before.
        info.last_screen = list(_READY_SCREEN)
        w.sessions["s1"] = info
        fired1 = await w._fire_timers(info)
        chk("now-mode fires immediately (busy ok)",
            any("run lint" in s for s in fs.sent))
        chk("now-mode fire returns True (poll loop's one-per-tick gate)",
            fired1 is True)

        D.add_timer(conn, iterm_session_id="s2", label="w", interval_min=1,
                    payload="check PRs", mode="idle", now=fresh)
        w2 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs2 = FakeSession()
        info2 = SessionInfo("s2", title="w", _iterm_session=fs2, mode="safe",
                            state="working")
        info2.last_screen = list(_READY_SCREEN)
        w2.sessions["s2"] = info2
        fired2a = await w2._fire_timers(info2)
        chk("idle-mode waits while busy", fs2.sent == [])
        chk("idle-mode-waits returns False (not this tick's injection)",
            fired2a is False)
        info2.state = "idle"
        # Bracketed on both sides (see _READY_SCREEN above for why).
        info2.last_screen = ["╭─────╮", "│ >   │", "╰─────╯",
                             "? for shortcuts"]
        fired2b = await w2._fire_timers(info2)
        chk("idle-mode fires at a ready prompt",
            any("check PRs" in s for s in fs2.sent))
        chk("idle-mode fire returns True",
            fired2b is True)

        D.add_timer(conn, iterm_session_id="s3", label="w", interval_min=1,
                    payload="x", mode="now", now=fresh)
        w3 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        w3.paused = True
        fs3 = FakeSession()
        info3 = SessionInfo("s3", title="w", _iterm_session=fs3, mode="safe")
        info3.last_screen = list(_READY_SCREEN)
        w3.sessions["s3"] = info3
        fired3 = await w3._fire_timers(info3)
        chk("pause freezes timers", fs3.sent == [])
        chk("pause-frozen returns False", fired3 is False)

        D.add_timer(conn, iterm_session_id="s4", label="w", interval_min=1,
                    payload="y", mode="now", now=fresh)
        w4 = Watcher(connection=None, dry_run=False,
                     cfg=C.Config(timers_require_armed=True))
        fs4 = FakeSession()
        info4 = SessionInfo("s4", title="w", _iterm_session=fs4, mode="off")
        info4.last_screen = list(_READY_SCREEN)
        w4.sessions["s4"] = info4
        await w4._fire_timers(info4)
        chk("require_armed blocks an unarmed session", fs4.sent == [])

        # a binding older than reconfirm_days deactivates instead of firing
        tid5 = D.add_timer(conn, iterm_session_id="s5", label="w",
                           interval_min=1, payload="z", mode="now",
                           now=0.0)   # bound_at = 0
        w5 = Watcher(connection=None, dry_run=False,
                     cfg=C.Config(timers_reconfirm_days=7))
        fs5 = FakeSession()
        info5 = SessionInfo("s5", title="w", _iterm_session=fs5, mode="safe")
        info5.last_screen = list(_READY_SCREEN)
        w5.sessions["s5"] = info5
        await w5._fire_timers(info5)     # now() >> 7 days after bound_at=0
        chk("past-reconfirm timer does not fire",
            fs5.sent == []
            and D.list_timers(conn, "s5")[0]["active"] == 0
            and "s5" in w5.pending_timer_sids)

        # dry-run: would-fire is audited, but nothing is ever injected, and the
        # timer is still marked fired (so it doesn't re-fire every tick).
        audited = []
        W.audit.record = lambda *a, **k: (audited.append(a), True)[1]
        D.add_timer(conn, iterm_session_id="s6", label="w", interval_min=1,
                    payload="dry payload", mode="now", now=fresh)
        w6 = Watcher(connection=None, dry_run=True, cfg=C.Config())
        fs6 = FakeSession()
        info6 = SessionInfo("s6", title="w", _iterm_session=fs6, mode="safe")
        info6.last_screen = list(_READY_SCREEN)
        w6.sessions["s6"] = info6
        fired6 = await w6._fire_timers(info6)
        chk("dry-run: never injects", fs6.sent == [])
        chk("dry-run would-fire still returns True (counts as this tick's "
            "injection for the extreme-push gate)", fired6 is True)
        chk("dry-run: audits would-fire",
            any(a[0] == "would-fire" for a in audited))
        chk("dry-run: still marked fired (no immediate re-fire)",
            D.list_timers(conn, "s6")[0]["last_fired_at"] > 0)
        W.audit.record = lambda *a, **k: True

        # SHELL JOB (Task 3 fix round 3, CRITICAL): a tab whose foreground job
        # is a login shell has no Claude in it, whatever chrome is still
        # painted on screen. "now" mode deliberately ignores readiness, so
        # without a job-level refusal a timer payload would be typed straight
        # into the shell and executed. Nothing about the screen is what saves
        # this one - only the job.
        D.add_timer(conn, iterm_session_id="s9", label="w", interval_min=1,
                    payload="deploy prod", mode="now", now=fresh)
        w9 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs9 = FakeSession()
        info9 = SessionInfo("s9", title="w", _iterm_session=fs9, mode="safe",
                            state="idle", job="-zsh")
        info9.last_screen = list(_READY_SCREEN)
        w9.sessions["s9"] = info9
        fired9 = await w9._fire_timers(info9)
        chk("shell job: timer never types into the shell", fs9.sent == [])
        chk("shell job: timer fire returns False", fired9 is False)
        chk("shell job: the timer is not consumed (clock not advanced, so it "
            "is still due the moment Claude comes back)",
            D.list_timers(conn, "s9")[0]["last_fired_at"] == fresh
            and D.list_timers(conn, "s9")[0]["fire_count"] == 0)

        # Control: same timer shape, live Claude foreground job -> still fires.
        D.add_timer(conn, iterm_session_id="s10", label="w", interval_min=1,
                    payload="deploy prod", mode="now", now=fresh)
        w10 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs10 = FakeSession()
        info10 = SessionInfo("s10", title="w", _iterm_session=fs10,
                             mode="safe", state="idle", job="node")
        info10.last_screen = list(_READY_SCREEN)
        w10.sessions["s10"] = info10
        chk("live claude job: timer still fires",
            await w10._fire_timers(info10) is True)

        # SCREEN READINESS (final branch review, Finding 2). Deleting
        # `swarm.claude_prompt_ready(...)` from _fire_timers' `ready` left
        # every suite green: the idle-mode branch was pinned only against
        # info.state, which reads "idle" on both screens below. An idle-mode
        # timer exists precisely so its payload lands at a free prompt, and
        # both of these would land it in a live turn or in a chooser, where
        # the payload's characters navigate a menu and pick an entry.
        import swarm as S
        for sid, screen, why in (("s11", _WORKING_SCREEN, "a working screen"),
                                 ("s12", _DIALOG_SCREEN, "a selection dialog")):
            D.add_timer(conn, iterm_session_id=sid, label="w", interval_min=1,
                        payload="ship it", mode="idle", now=fresh)
            wS = Watcher(connection=None, dry_run=False, cfg=C.Config())
            fsS = FakeSession()
            infoS = SessionInfo(sid, title="w", _iterm_session=fsS,
                                mode="safe", state="idle", job="node")
            infoS.last_screen = list(screen)
            wS.sessions[sid] = infoS
            firedS = await wS._fire_timers(infoS)
            chk(f"idle-mode timer: {why} with state=idle never fires",
                fsS.sent == [] and firedS is False)
            chk(f"idle-mode timer: {why} does not consume the timer",
                D.list_timers(conn, sid)[0]["fire_count"] == 0)
            chk(f"{why} is genuinely unready (not a vacuous screen)",
                S._bracketed_input_rows(screen) != []
                and S.claude_prompt_ready(screen) is False)

        # Control: same timer shape and state, a READY screen -> it fires. The
        # screen is the gate, not something else refusing for free.
        D.add_timer(conn, iterm_session_id="s13", label="w", interval_min=1,
                    payload="ship it", mode="idle", now=fresh)
        w13 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs13 = FakeSession()
        info13 = SessionInfo("s13", title="w", _iterm_session=fs13,
                             mode="safe", state="idle", job="node")
        info13.last_screen = list(_READY_SCREEN)
        w13.sessions["s13"] = info13
        chk("idle-mode timer: a ready screen still fires",
            await w13._fire_timers(info13) is True
            and any("ship it" in s for s in fs13.sent))

        # DRAFT PROTECTION ([swarm] respect_draft, default ON). A timer payload
        # typed into a half-written message appends to it and submits it. The
        # screen is otherwise fully ready, and the timer is `now` mode (which
        # ignores readiness entirely), so ONLY the draft check can refuse this.
        chk("draft screen is ready but not empty (not a vacuous fixture)",
            S.claude_prompt_ready(_DRAFT_SCREEN) is True
            and S.prompt_line_empty(_DRAFT_SCREEN) is False)
        D.add_timer(conn, iterm_session_id="s14", label="w", interval_min=1,
                    payload="ship it", mode="now", now=fresh)
        w14 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs14 = FakeSession()
        info14 = SessionInfo("s14", title="w", _iterm_session=fs14,
                             mode="safe", state="idle", job="node")
        info14.last_screen = list(_DRAFT_SCREEN)
        w14.sessions["s14"] = info14
        fired14 = await w14._fire_timers(info14)
        chk("respect_draft on: timer never types over an operator draft",
            fs14.sent == [] and fired14 is False)
        chk("respect_draft on: the timer clock is not advanced (still due the "
            "moment the box is clear)",
            D.list_timers(conn, "s14")[0]["last_fired_at"] == fresh
            and D.list_timers(conn, "s14")[0]["fire_count"] == 0)

        # Control: same timer shape, same watcher config - only the input box
        # is empty - and it fires.
        D.add_timer(conn, iterm_session_id="s15", label="w", interval_min=1,
                    payload="ship it", mode="now", now=fresh)
        w15 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs15 = FakeSession()
        info15 = SessionInfo("s15", title="w", _iterm_session=fs15,
                             mode="safe", state="idle", job="node")
        info15.last_screen = list(_READY_SCREEN)
        w15.sessions["s15"] = info15
        chk("respect_draft on: an empty input box still fires",
            await w15._fire_timers(info15) is True
            and any("ship it" in s for s in fs15.sent))

        # OFF -> exactly today's behaviour: it fires over the draft.
        D.add_timer(conn, iterm_session_id="s16", label="w", interval_min=1,
                    payload="ship it", mode="now", now=fresh)
        w16 = Watcher(connection=None, dry_run=False,
                      cfg=C.Config(respect_draft=False))
        fs16 = FakeSession()
        info16 = SessionInfo("s16", title="w", _iterm_session=fs16,
                             mode="safe", state="idle", job="node")
        info16.last_screen = list(_DRAFT_SCREEN)
        w16.sessions["s16"] = info16
        chk("respect_draft off: timer fires over the draft (opted-out "
            "behaviour)",
            await w16._fire_timers(info16) is True
            and any("ship it" in s for s in fs16.sent))

        # _load_timers_on_start: the restore gate. Default config
        # (autostart=false) deactivates every saved timer and flags present
        # sessions' sids as pending a restore/re-confirm decision.
        D.add_timer(conn, iterm_session_id="s7", label="w", interval_min=1,
                    payload="restore me", mode="now", now=fresh)
        w7 = Watcher(connection=None, dry_run=False, cfg=C.Config())
        fs7 = FakeSession()
        info7 = SessionInfo("s7", title="w", _iterm_session=fs7, mode="safe")
        info7.last_screen = list(_READY_SCREEN)
        w7.sessions["s7"] = info7
        w7._load_timers_on_start()
        chk("restore gate (autostart=false): saved timer deactivated",
            D.list_timers(conn, "s7")[0]["active"] == 0)
        chk("restore gate (autostart=false): present sid marked pending",
            "s7" in w7.pending_timer_sids)

        # With [timers] autostart = true, present sessions' timers are
        # restored active and nothing is left pending.
        D.add_timer(conn, iterm_session_id="s8", label="w", interval_min=1,
                    payload="autostart me", mode="now", now=fresh)
        w8 = Watcher(connection=None, dry_run=False,
                     cfg=C.Config(timers_autostart=True))
        fs8 = FakeSession()
        info8 = SessionInfo("s8", title="w", _iterm_session=fs8, mode="safe")
        w8.sessions["s8"] = info8
        w8._load_timers_on_start()
        chk("restore gate (autostart=true): present session's timer active",
            D.list_timers(conn, "s8")[0]["active"] == 1)
        chk("restore gate (autostart=true): nothing left pending",
            w8.pending_timer_sids == set())
    finally:
        if saved_relay_db is None:
            os.environ.pop("RELAY_DB", None)
        else:
            os.environ["RELAY_DB"] = saved_relay_db

    print("\nALL PASS" if ok else "\nFAILURES ABOVE")
    return ok


def close_threads_tests():
    """_close_threads against a REAL temp DB: it touches enough of the thread
    API that monkeypatching would test the mocks, not the behaviour."""
    import tempfile
    from watcher import Watcher
    import db as realdb

    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), name)
        ok = ok and cond

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = realdb.connect(path)
    W.notify_mac = lambda *a, **k: None

    w = Watcher(connection=None, dry_run=False)
    w._db = conn

    def _pings():
        return conn.execute("SELECT * FROM messages WHERE to_name='human' "
                            "AND kind='escalation'").fetchall()

    # UNANIMOUS -> agreed, one ping carrying the outcome.
    t1 = realdb.create_thread(conn, "one DB or many?", "a", ["a", "b"],
                              project="p", rounds_cap=3)
    realdb.queue_message(conn, "a", "b", "per service", "p", kind="agree",
                         thread_id=t1)
    realdb.queue_message(conn, "b", "a", "per service", "p", kind="agree",
                         thread_id=t1)
    w._close_threads()
    chk("unanimous -> agreed",
        realdb.get_thread(conn, t1)["state"] == "agreed")
    chk("agreed records the outcome",
        "per service" in realdb.get_thread(conn, t1)["outcome"])
    chk("agreed pings the human once", len(_pings()) == 1)
    # Idempotent: a second tick must not re-close or re-ping.
    w._close_threads()
    chk("closing is idempotent across ticks", len(_pings()) == 1)

    # CAP SPENT with no agreement: relay must do NOTHING. Closing it here
    # would be relay deciding the agents failed and that a human should
    # settle it - a decision that belongs to the sessions having it.
    t2 = realdb.create_thread(conn, "shared cache?", "a", ["a", "b"],
                              project="p", rounds_cap=1)
    realdb.queue_message(conn, "a", "b", "yes cache", "p", kind="say",
                         thread_id=t2, now=10.0)
    realdb.queue_message(conn, "b", "a", "no cache", "p", kind="say",
                         thread_id=t2, now=11.0)
    w._close_threads()
    th2 = realdb.get_thread(conn, t2)
    chk("a spent budget does not close the thread", th2["state"] == "open")
    chk("relay never declares a discussion unresolved", th2["outcome"] == "")
    chk("a spent budget does not ping the human", len(_pings()) == 1)
    # Long-running disagreement, still relay's business to stay out of.
    for i in range(6):
        realdb.queue_message(conn, "a" if i % 2 else "b", "b" if i % 2 else "a",
                             f"round {i}", "p", kind="say", thread_id=t2,
                             now=20.0 + i)
    w._close_threads()
    chk("relay leaves a long argument alone",
        realdb.get_thread(conn, t2)["state"] == "open")
    chk("a long argument never pings the human", len(_pings()) == 1)

    # An OPEN thread short of a verdict is left alone.
    t3 = realdb.create_thread(conn, "still going", "a", ["a", "b"],
                              project="p", rounds_cap=3)
    realdb.queue_message(conn, "a", "b", "opening", "p", kind="say",
                         thread_id=t3)
    w._close_threads()
    chk("an undecided thread stays open",
        realdb.get_thread(conn, t3)["state"] == "open")
    chk("an undecided thread does not ping", len(_pings()) == 1)

    conn.close()
    return ok


if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = asyncio.run(deliver_tests())
    r3 = asyncio.run(title_tests())
    r3b = asyncio.run(session_path_tests())
    r3c = asyncio.run(sync_sessions_workdir_persist_tests())
    r3d = asyncio.run(selected_tab_tests())
    r4 = arm_request_tests()
    r5 = closed_tests()
    r6 = asyncio.run(own_tab_name_tests())
    r7 = escalation_ratelimit_tests()
    r8 = asyncio.run(statusbar_registration_tests())
    r8b = asyncio.run(parked_badge_cache_tests())
    r9 = legible_spine_tests()
    r10 = asyncio.run(pause_tests())
    r11 = asyncio.run(shadow_tests())
    r12 = notify_mac_tests()
    r13 = mute_tests()
    r_timer = asyncio.run(timer_tests())
    r_thr = close_threads_tests()
    sys.exit(0 if (r1 and r2 and r3 and r3b and r3c and r3d and r4 and r5
                   and r6 and r7
                   and r8 and r8b and r9 and r10 and r11 and r12 and r13
                   and r_timer and r_thr) else 1)
