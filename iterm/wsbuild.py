"""Build a relay workspace in iTerm2.

The only new module that imports iterm2. Four traps are load-bearing here, each
one having cost a debugging cycle in the out-of-repo prototype:

1. iterm2.Window.async_create() returns a window whose current_tab is None
   until async_get_app() has been awaited. Use window.tabs[0] regardless.
2. Typing `cd` into a shell that is still sourcing its rc files loses the
   input, leaving the tab in $HOME. The directory goes in the profile instead.
3. A prompt that writes the title escape sequence (starship) rewrites the tab
   title on every redraw, so async_set_name survives only until the first
   prompt. set_allow_title_setting(False) fixes that - but claude reports its
   live status through that same escape sequence and relay's SESSION column
   displays it, so titles are locked ONLY on tabs relay is not supervising.
4. Anything escaping a coroutine passed to run_until_complete gets a printed
   traceback and sys.exit(1) from the library, which no outer except can
   catch. Callers must catch inside.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db          # noqa: E402
import spawn       # noqa: E402
import wsconfig  # noqa: E402


def _session_profile(workdir: str, lock_title: bool):
    """Per-session profile overrides applied at creation time (traps 2 and 3)."""
    import iterm2

    profile = iterm2.profile.LocalWriteOnlyProfile()
    if workdir:
        profile.set_initial_directory_mode(
            iterm2.profile.InitialWorkingDirectory
            .INITIAL_WORKING_DIRECTORY_CUSTOM)
        profile.set_custom_directory(workdir)
    if lock_title:
        profile.set_allow_title_setting(False)
        profile.set_title_components(
            [iterm2.profile.TitleComponents.SESSION_NAME])
    return profile


def _first_session(handle):
    """A tab's active session, falling back to its first (trap 1)."""
    if handle is None:
        return None
    return handle.current_session or (handle.sessions[0]
                                      if handle.sessions else None)


async def live_tab_names(connection):
    """Every live iTerm tab name, registered or not. None on total failure.

    Named live_tab_names, not live_names, because swarm.live_names() already
    exists and means something narrower: the names of live REGISTERED
    sessions. This one deliberately includes unregistered tabs.

    The restore guard needs unregistered tabs too: a hand-named shell called
    "DRAGEN DOCS" is exactly the collision we must not walk into.
    """
    import iterm2

    names = set()
    try:
        app = await iterm2.async_get_app(connection)
        for window in app.terminal_windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    try:
                        # autoName, not session.name: session.name carries a
                        # live job suffix ("DRAGEN CODE (-zsh)") that never
                        # matches the saved name ("DRAGEN CODE"), which would
                        # make skip_live skip nothing and let build() rebind
                        # a live name to a fresh session - the identity theft
                        # this guard exists to prevent. Matches the autoName
                        # precedent at selftest.py:196. session.name is the
                        # fallback for a session with no autoName at all, not
                        # the primary read.
                        name = (await session.async_get_variable("autoName")
                                or await session.async_get_variable(
                                    "session.name"))
                        if name:
                            names.add(str(name))
                    except Exception:
                        # Trap 4: a session closed mid-enumeration (a real
                        # race) must not take the whole guard down with it.
                        continue
    except Exception:
        # None here means "the enumeration itself failed" - distinct from an
        # empty set, which means "nothing is open" and is a legitimate result.
        # An empty set on failure would make the guard skip NOTHING, the
        # UNSAFE direction: a caller must treat None as a reason to stop, not
        # as license to build blindly on top of an unknown live state.
        return None
    return names


async def build(connection, tabs, warmup: float = 1.5,
                target: str = "new") -> list:
    """Create every tab. Returns a list of failure notes; empty means clean."""
    import iterm2

    app = await iterm2.async_get_app(connection)   # trap 1
    notes: list = []
    pending = []          # (session, cmd) sent only once every tab exists
    conn = None

    groups = wsconfig.group_windows(tabs)
    for index, (_, wintabs) in enumerate(groups):
        window = None
        if target == "current" and index == 0:
            window = app.current_terminal_window

        for tab in wintabs:
            if tab.supervised and (tab.name in db.RESERVED_NAMES
                                   or tab.arm not in db.ARM_REQUEST_MODES):
                # Validate BEFORE creating anything: db.register raises for
                # RESERVED_NAMES and db.set_arm_request raises for a bad arm
                # mode, and either raising after register() succeeded would
                # leave a session registered, unarmed, with no workdir - a
                # live name pointing at a bare shell - with its cmd and panes
                # silently dropped because pending.append sits in the same
                # try block. No window, no tab, no partial registration.
                notes.append(
                    f"{tab.name}: refusing to supervise - reserved name or "
                    f"invalid arm mode {tab.arm!r}")
                continue
            try:
                profile = _session_profile(tab.dir, lock_title=not tab.supervised)
                if window is None:
                    window = await iterm2.Window.async_create(
                        connection, profile_customizations=profile)
                    if window is None:
                        raise RuntimeError("iTerm refused to create a window")
                    handle = window.tabs[0]
                else:
                    handle = await window.async_create_tab(
                        profile_customizations=profile)
                session = _first_session(handle)
                if session is None:
                    raise RuntimeError("iTerm gave no session")

                if tab.is_worker:
                    # The full swarm path, unchanged: it names, registers,
                    # arms, sends cd+claude and the worker prompt itself.
                    await spawn.spawn_worker(
                        tab.name, tab.project, tab.prompt, tab.dir,
                        tab.role, tab.arm, session=session)
                else:
                    await session.async_set_name(tab.name)
                    if tab.supervised:
                        # Register BEFORE the command runs, so the arm request
                        # is already in place when claude boots.
                        if conn is None:
                            conn = db.connect()
                        db.register(conn, tab.name, session.session_id,
                                    tab.role, tab.project)
                        db.set_arm_request(conn, tab.name, tab.arm)
                        db.set_session_context(conn, tab.name, tab.dir, "")
                    if tab.cmd:
                        pending.append((session, tab.cmd))

                current = session
                for pane in tab.panes:
                    current = await current.async_split_pane(
                        vertical=(pane.split == "v"),
                        profile_customizations=_session_profile(
                            pane.dir or tab.dir, lock_title=True))
                    if current is None:
                        raise RuntimeError(f'could not split "{tab.name}"')
                    await current.async_set_name(tab.name)
                    if pane.cmd:
                        pending.append((current, pane.cmd))
            except Exception as exc:                      # noqa: BLE001
                # Trap 4: one bad tab must not take the workspace down, and it
                # must not escape into run_until_complete either.
                notes.append(f"{tab.name}: {type(exc).__name__}: {exc}")

    if pending:
        # One sleep for the whole workspace: the tabs created first have had
        # the entire build as their warm-up by the time we get here.
        await asyncio.sleep(warmup)
        for session, cmd in pending:
            try:
                await session.async_send_text(cmd + "\n")
            except Exception as exc:                      # noqa: BLE001
                notes.append(f"send {cmd!r}: {exc}")
    return notes


async def snapshot_rows(connection, all_windows: bool = False) -> list:
    """Live tabs as plain dicts for wsconfig.snapshot().

    `arm` comes from the persisted `sessions.mode` of a registered session with
    the same name. An unregistered tab's arm level lives only in the running
    watcher and is deliberately not persisted (see watcher.py), so it comes
    back empty - which is correct: the way to make a tab come back armed is to
    give it an `arm` key, which registers it.

    Two rows are skipped outright, never counted as "no name" drops:
    relay's own panel tab (matched by ITERM_SESSION_ID, same trick as
    selftest.py) - a TUI is not a command and would round-trip into a bare
    shell - and any name in db.RESERVED_NAMES, since a saved `arm` key on
    "relay" would make build() refuse it anyway.

    Each row also carries a "split" flag (not one of the four keys
    wsconfig.snapshot() reads, and harmless to it): True when the tab held
    more than one session. A split cannot be captured this way - only the
    tab's own active session is read - so the caller reports the loss rather
    than silently saving a one-pane approximation of a split tab.
    """
    import iterm2

    own_sid = os.environ.get("ITERM_SESSION_ID", "").split(":", 1)[-1]

    app = await iterm2.async_get_app(connection)
    if all_windows:
        windows = list(app.terminal_windows)
    else:
        current = app.current_terminal_window
        windows = [current] if current is not None else []

    conn = None
    rows = []
    for index, window in enumerate(windows, start=1):
        for tab in window.tabs:
            session = _first_session(tab)
            if session is None:
                continue
            if session.session_id == own_sid:
                continue
            # autoName, not session.name: session.name carries the job
            # suffix ("DRAGEN CODE (-zsh)") so a saved name would never match
            # its own live tab again. Same read relay uses at selftest.py:196.
            name = (await session.async_get_variable("autoName")
                    or await session.async_get_variable("session.name"))
            if name and str(name) in db.RESERVED_NAMES:
                continue
            path = await session.async_get_variable("session.path")
            arm = ""
            if name:
                if conn is None:
                    conn = db.connect()
                row = db.get_session(conn, str(name))
                mode = (row["mode"] if row is not None else "") or ""
                arm = mode if mode in wsconfig.ARM_MODES else ""
            rows.append({"name": str(name or ""), "dir": str(path or ""),
                         "arm": arm, "window": index,
                         "split": len(tab.sessions) > 1})
    return rows
