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

os.environ["RELAY_SPAWN_BOOT_DELAY"] = "0"
# Default to "never wait" so every spawn_worker call in this file resolves
# _wait_ready instantly unless a test overrides it (the readiness tests
# below do, around their own spawn_worker calls). Without this, a plain
# FakeSession() (screens=None, used by the pre-existing tests) has no way
# to answer async_get_screen_contents, so _wait_ready would fall through
# its except-and-retry loop for the full real default timeout.
os.environ["RELAY_SPAWN_READY_TIMEOUT"] = "0"
os.environ["RELAY_DB"] = os.path.join(tempfile.mkdtemp(), "relay.sqlite3")

sys.path.insert(0, os.path.dirname(__file__))
import db      # noqa: E402
import spawn   # noqa: E402

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
    def __init__(self, sid="FAKE-SID", screens=None):
        self.session_id = sid
        self.names = []
        self.sent = []
        # A list of screens the tab shows on successive polls; the last one
        # repeats. None means "this session cannot report its screen".
        self.screens = screens
        self.polls = 0
        self.polls_at_send = []

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

    # a session that cannot report its screen: type after the boot delay
    blind = BlindSession("BLIND")
    asyncio.run(spawn.spawn_worker("w5", "proj", "p", "/tmp", "worker",
                                   "off", session=blind))
    ok &= check("a screenless session still gets its prompt",
                any("relay-worker" in t for t in blind.sent))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
