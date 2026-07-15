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

sys.path.insert(0, os.path.dirname(__file__))
import watcher as W  # noqa: E402


class FakeSession:
    def __init__(self):
        self.sent = []

    async def async_send_text(self, t):
        self.sent.append(t)


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


if __name__ == "__main__":
    r1 = asyncio.run(go())
    r2 = asyncio.run(deliver_tests())
    sys.exit(0 if (r1 and r2) else 1)
