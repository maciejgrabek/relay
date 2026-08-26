"""Tests for spawn_worker's `session=` target. No real iTerm2 window is ever
created - a FakeSession stands in, and Connection.async_create is booby-trapped
so a regression that ignores `session=` fails loudly instead of opening a
window on the developer's screen.

Run: python3 iterm/test_spawn.py    or    ./test/run.sh
"""
import asyncio
import os
import sys
import tempfile

os.environ["RELAY_SPAWN_BOOT_DELAY"] = "0"
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


class FakeSession:
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

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
