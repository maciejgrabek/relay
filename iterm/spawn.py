"""relay spawn - open an iTerm2 tab running claude, pre-registered by name.

Ported from synapse-mini with its hard-won lessons: shell warm-up sleep before
typing, `cd && claude` then a boot delay, prompt body as one paste followed by
a STANDALONE \r (Claude's TUI swallows pasted newlines and waits for Enter).

Registration happens BEFORE the prompt is sent, so `relay send <name>` works
the moment this returns. The generated first prompt is minimal on purpose:
the protocol lives in the relay-worker skill, not in pasted boilerplate.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402

BOOT_DELAY = float(os.environ.get("RELAY_SPAWN_BOOT_DELAY", "6.0"))


def _relay_bin_dir() -> str:
    """bin/ of this checkout, so the worker's shell can call `relay`."""
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "bin"))


def first_prompt(name: str, project: str, prompt: str,
                 role: str = "worker") -> str:
    skill = "relay-worker" if role == "worker" else "relay-coordinator"
    p = (f"Invoke the {skill} skill. You are '{name}'"
         + (f" on project '{project}'" if project else "") + ".")
    if prompt:
        p += f" Task: {prompt}"
    return p


async def spawn_worker(name: str, project: str, prompt: str,
                       workdir: str, role: str = "worker",
                       arm: str = "off") -> str:
    import iterm2

    claude_cmd = shutil.which("claude") or "claude"
    connection = await iterm2.Connection.async_create()
    app = await iterm2.async_get_app(connection)
    win = app.current_terminal_window
    if win is None:
        win = await iterm2.Window.async_create(connection)
        tab = win.current_tab
    else:
        tab = await win.async_create_tab()
    session = tab.current_session
    sid = session.session_id           # bare UUID, matches the watcher's key

    # Name the tab so relay's UNIT column and the human both see it.
    try:
        await session.async_set_name(name)
    except Exception:
        pass

    # Register FIRST - addressable before claude even boots; queued messages
    # simply wait until the session is idle at Claude's input box.
    conn = db.connect()
    db.register(conn, name, sid, role, project)
    if arm != "off":
        # The watcher arms the session when it first sees it (the arm state
        # lives in the running TUI, not in this process).
        db.set_arm_request(conn, name, arm)
    db.set_session_context(conn, name, workdir, prompt)

    await asyncio.sleep(0.5)           # shell warm-up
    await session.async_send_text(
        f'export PATH="$PATH":{shlex.quote(_relay_bin_dir())} && '
        f'cd {shlex.quote(workdir)} && {claude_cmd}\n')
    await asyncio.sleep(BOOT_DELAY)    # claude boot
    body = first_prompt(name, project, prompt, role)
    await session.async_send_text(body)
    await asyncio.sleep(0.5)
    await session.async_send_text("\r")
    return sid
