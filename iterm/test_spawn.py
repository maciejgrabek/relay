"""Tests for spawn_worker's `session=` target. No real iTerm2 window is ever
created - a FakeSession stands in, and Connection.async_create is booby-trapped
so a regression that ignores `session=` fails loudly instead of opening a
window on the developer's screen.

Run: python3 iterm/test_spawn.py    or    ./test/run.sh
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import time

os.environ["RELAY_SPAWN_BOOT_DELAY"] = "0"
# Default to "never wait" so every spawn_worker call in this file resolves
# _wait_ready instantly unless a test overrides it (the readiness tests
# below do, around their own spawn_worker calls). Without this, a plain
# FakeSession() (screens=None, used by the pre-existing tests) has no way
# to answer async_get_screen_contents, so _wait_ready would fall through
# its except-and-retry loop for the full real default timeout.
# Consequence: the w1/w2/w5 cases below print a harmless "not ready" line
# on stderr too (nothing polls the box for them) - only the w4 case below
# actually asserts on that stderr text.
os.environ["RELAY_SPAWN_READY_TIMEOUT"] = "0"
os.environ["RELAY_DB"] = os.path.join(tempfile.mkdtemp(), "relay.sqlite3")

sys.path.insert(0, os.path.dirname(__file__))
import db      # noqa: E402
import spawn   # noqa: E402
import swarm   # noqa: E402

try:
    import iterm2

    async def _boom(*_a, **_k):
        raise AssertionError(
            "spawn_worker must not open a connection when given a session")

    iterm2.Connection.async_create = _boom
except ImportError:
    pass


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


_SCREENS = pathlib.Path(__file__).parent / "fixtures" / "screens"


def _screen(name):
    return [l for l in (_SCREENS / f"{name}.txt").read_text().splitlines()
            if not l.startswith("#")]


class _Line:
    def __init__(self, s):
        self.string = s
        self.hard_eol = True


class _Contents:
    def __init__(self, lines):
        self._lines = lines
        self.number_of_lines = len(lines)

    def line(self, i):
        return _Line(self._lines[i])


class FakeSession:
    def __init__(self, sid="FAKE-SID", screens=None, job=None):
        self.session_id = sid
        self.names = []
        self.sent = []
        # A list of screens the tab shows on successive polls; the last one
        # repeats. None means "this session cannot report its screen".
        self.screens = screens
        self.polls = 0
        self.polls_at_send = []
        # job=None keeps this fake WITHOUT an async_get_variable attribute at
        # all, matching a real iterm2.Session on an old API and the "no such
        # method" branch of spawn's never-ready handling. Only set it when a
        # test needs to answer "what job is in front".
        self.job = job
        if job is not None:
            async def _async_get_variable(name):
                return self.job if name == "jobName" else None
            self.async_get_variable = _async_get_variable

    async def async_set_name(self, name):
        self.names.append(name)

    async def async_send_text(self, text):
        self.sent.append(text)
        self.polls_at_send.append(self.polls)

    async def async_get_screen_contents(self):
        self.polls += 1
        idx = min(self.polls - 1, len(self.screens) - 1)
        return _Contents(self.screens[idx])


class BlindSession:
    """A session that genuinely cannot report its screen - no
    async_get_screen_contents at all, not one that raises. `hasattr` must
    see it as absent, the same as a real iterm2.Session missing the API."""

    def __init__(self, sid="FAKE-SID"):
        self.session_id = sid
        self.names = []
        self.sent = []

    async def async_set_name(self, name):
        self.names.append(name)

    async def async_send_text(self, text):
        self.sent.append(text)


def run():
    ok = True
    fake = FakeSession()
    sid = asyncio.run(spawn.spawn_worker(
        "w1", "proj", "do the thing", "/tmp", "worker", "off", session=fake))

    ok &= check("returns the id of the session it was handed",
                sid == "FAKE-SID")
    ok &= check("names the tab", fake.names == ["w1"])
    ok &= check("cds into the workdir",
                any("/tmp" in t for t in fake.sent))
    ok &= check("sends the first prompt",
                any("relay-worker" in t for t in fake.sent))

    conn = db.connect()
    row = db.get_session(conn, "w1")
    ok &= check("registers the session", row is not None)
    ok &= check("binds the session id we gave it",
                row is not None and row["iterm_session_id"] == "FAKE-SID")
    ok &= check("records the workdir",
                row is not None and row["workdir"] == "/tmp")

    armed = FakeSession("ARMED-SID")
    asyncio.run(spawn.spawn_worker("w2", "proj", "p", "/tmp", "worker",
                                   "wild", session=armed))
    row2 = db.get_session(db.connect(), "w2")
    ok &= check("an armed spawn records the arm request",
                row2 is not None and row2["arm_request"] == "wild")

    # readiness: the prompt is typed only once Claude's input box is idle
    os.environ["RELAY_SPAWN_READY_TIMEOUT"] = "5"
    spawn.READY_POLL = 0.0          # no real waiting in the test
    slow = FakeSession("SLOW", screens=[_screen("shell_zsh")] * 3
                       + [_screen("idle_accept_edits")])
    asyncio.run(spawn.spawn_worker("w3", "proj", "p", "/tmp", "worker",
                                   "off", session=slow))
    body_i = next(i for i, t in enumerate(slow.sent) if "relay-worker" in t)
    ok &= check("the launch command goes out before any screen poll",
                slow.polls_at_send[0] == 0)
    ok &= check("the first prompt waits for the ready screen (4th poll)",
                slow.polls_at_send[body_i] == 4)
    ok &= check("Enter follows the body as its own send",
                slow.sent[body_i + 1] == "\r")

    # never ready: after the timeout it types anyway and says so on stderr
    import io
    from contextlib import redirect_stderr
    os.environ["RELAY_SPAWN_READY_TIMEOUT"] = "0"
    never = FakeSession("NEVER", screens=[_screen("shell_zsh")])
    err = io.StringIO()
    with redirect_stderr(err):
        asyncio.run(spawn.spawn_worker("w4", "proj", "p", "/tmp", "worker",
                                       "off", session=never))
    ok &= check("on timeout the prompt is still typed",
                any("relay-worker" in t for t in never.sent))
    ok &= check("...and stderr says Claude was not seen ready",
                "not ready" in err.getvalue())

    # never ready AND a shell is in front: refuse to type, say so, and
    # return the sid without sending anything.
    shell_fronted = FakeSession("SHELLFRONT", screens=[_screen("shell_zsh")],
                                job="zsh")
    err_shell = io.StringIO()
    with redirect_stderr(err_shell):
        sid_shell = asyncio.run(spawn.spawn_worker(
            "w4a", "proj", "p", "/tmp", "worker", "off",
            session=shell_fronted))
    ok &= check("returns the sid even though it never typed",
                sid_shell == "SHELLFRONT")
    ok &= check("a shell in front after timeout gets NO prompt typed",
                not any("relay-worker" in t for t in shell_fronted.sent))
    ok &= check("...and stderr says so, clearly",
                "NOT typing" in err_shell.getvalue())

    # never ready but claude (not a shell) is in front: still type, same as
    # the plain timeout case above.
    claude_fronted = FakeSession("CLAUDEFRONT", screens=[_screen("shell_zsh")],
                                 job="claude")
    err_claude = io.StringIO()
    with redirect_stderr(err_claude):
        asyncio.run(spawn.spawn_worker("w4b", "proj", "p", "/tmp", "worker",
                                       "off", session=claude_fronted))
    ok &= check("claude (not a shell) in front still gets the prompt typed",
                any("relay-worker" in t for t in claude_fronted.sent))

    # a session that cannot report its screen: type after the boot delay
    blind = BlindSession("BLIND")
    asyncio.run(spawn.spawn_worker("w5", "proj", "p", "/tmp", "worker",
                                   "off", session=blind))
    ok &= check("a screenless session still gets its prompt",
                any("relay-worker" in t for t in blind.sent))

    # the hasattr guard itself: a session with no async_get_screen_contents
    # at all must resolve _wait_ready(...) as False quickly, not hang on a
    # missing attribute somewhere in the poll loop.
    start_blind = time.monotonic()
    blind_ready = asyncio.run(spawn._wait_ready(BlindSession("BLIND2"),
                                                timeout=30))
    elapsed_blind = time.monotonic() - start_blind
    ok &= check("_wait_ready on a blind session is False",
                blind_ready is False)
    ok &= check("...and returns in under 1s (the hasattr guard fires)",
                elapsed_blind < 1)

    # a hung screen read must not stall the wait past RELAY_SPAWN_READY_TIMEOUT.
    # The sleep (5s) is well past both READ_TIMEOUT and RELAY_SPAWN_READY_TIMEOUT
    # below on purpose: only asyncio.wait_for's READ_TIMEOUT bound can cut it
    # short, so a regression that dropped that bound cannot pass by accident
    # (a short sleep could complete before the bound ever mattered).
    class HungSession(FakeSession):
        async def async_get_screen_contents(self):
            await asyncio.sleep(5)
            return await FakeSession.async_get_screen_contents(self)

    saved_read_timeout = spawn.READ_TIMEOUT
    spawn.READ_TIMEOUT = 0.05
    os.environ["RELAY_SPAWN_READY_TIMEOUT"] = "0.3"
    hung = HungSession("HUNG", screens=[_screen("shell_zsh")])
    err2 = io.StringIO()
    start = time.monotonic()
    with redirect_stderr(err2):
        asyncio.run(spawn.spawn_worker("w6", "proj", "p", "/tmp", "worker",
                                       "off", session=hung))
    elapsed = time.monotonic() - start
    spawn.READ_TIMEOUT = saved_read_timeout
    ok &= check("a hung screen read does not stall spawn_worker (< 3s)",
                elapsed < 3)
    ok &= check("...the prompt is still typed",
                any("relay-worker" in t for t in hung.sent))
    ok &= check("...and stderr says Claude was not seen ready",
                "not ready" in err2.getvalue())

    ok &= test_screen_lines_pipeline()
    ok &= test_blank_padding_before_tail()

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


def test_screen_lines_pipeline():
    """`_screen_lines` must run the screen through the same sanitizing
    pipeline the watcher uses (gates.reconstruct_lines, NUL/nbsp mapped to
    space) rather than reading raw cells: a NUL-padded junk row sits right
    above Claude's input row here, breaking the raw pipeline's bracket-
    adjacency check (the junk row is not a border, so the ❯ row no longer
    reads as bracketed) while the sanitized pipeline collapses the junk row
    to nothing and the real border/❯/border framing is intact."""
    ok = True
    lines = _screen("idle_accept_edits")
    idx = lines.index("❯")
    junk = "\x00" * 20
    junky = lines[:idx] + [junk] + lines[idx:]

    class _JunkSession:
        async def async_get_screen_contents(self):
            return _Contents(junky)

    ready = asyncio.run(spawn._wait_ready(_JunkSession(), timeout=0.01))
    ok &= check("the watcher's own pipeline reads the junky screen as ready",
                ready)

    raw_nonblank = [l for l in junky if l.strip()]
    ok &= check("...but a raw, unsanitized read of the same screen would not",
                not swarm.claude_prompt_ready(raw_nonblank))
    return ok


def test_blank_padding_before_tail():
    """Blanks must be dropped BEFORE the 40-line tail is taken, not after.
    A tall terminal window's last 40 raw rows can be mostly blank padding
    below Claude's box - routine right after a fresh spawn, before the
    window has filled with output - so slicing the tail first and dropping
    blanks second can hand claude_prompt_ready an empty (or near-empty)
    list and misread a screen the watcher itself reads as ready."""
    ok = True
    padded = _screen("idle_accept_edits") + [""] * 45
    saved_poll = spawn.READY_POLL
    spawn.READY_POLL = 0
    try:
        ready = asyncio.run(spawn._wait_ready(
            FakeSession(screens=[padded]), timeout=1))
    finally:
        spawn.READY_POLL = saved_poll
    ok &= check("45 trailing blank rows still read as ready "
                "(blanks dropped before the tail, not after)", ready)
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
