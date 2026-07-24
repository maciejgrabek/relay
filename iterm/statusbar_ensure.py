"""Start the AutoLaunch status-bar provider if it's installed but not running.

WHY THIS EXISTS: the provider (statusbar_autolaunch.py) is an iTerm2 AutoLaunch
script - iTerm2 only starts it at iTerm2 launch. So when `relay update` /
install.sh (re)links the provider AFTER iTerm2 is already up, the symlink is
present but no provider process is running, and the badge slot dies until the
next iTerm2 restart. relay's own registration then steps back (it sees the
symlink and assumes the provider owns the badge), so nobody renders it.

This module heals that without a restart: if the provider is installed but its
heartbeat is dead, request an iTerm2 cookie (osascript) and launch the provider
detached, so it registers `com.relay.arm` and starts serving the badge now.

Both install.sh and relay (watcher._register_statusbar) call ensure(). It is
idempotent and safe:
  - not installed  -> 'absent', do nothing (relay renders in-process).
  - already alive  -> 'alive', do nothing (never double-register -> freeze).
  - installed+dead -> start it (or 'no-cookie' if iTerm2 gave nothing).

The decision itself is the pure statusbar.plan_provider_start; this is only the
live half (cookie + detached launch), kept out of statusbar.py so that module
stays iTerm2-free and unit-testable.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import statusbar  # noqa: E402


def request_cookie(runner=None) -> str:
    """An iTerm2 API auth cookie for a fresh client, via AppleScript. Returns ''
    when iTerm2 isn't running or scripting is refused. Never raises."""
    runner = runner or subprocess.run
    try:
        r = runner(["osascript", "-e",
                    'tell application "iTerm2" to request cookie'],
                   capture_output=True, text=True, timeout=10)
        return (getattr(r, "stdout", "") or "").strip()
    except Exception:
        return ""


def _spawn(script: str, cookie: str) -> None:
    """Launch the provider detached (start_new_session) so it OUTLIVES relay -
    same lifetime the AutoLaunch provider is meant to have. It dies on its own
    when iTerm2 quits (its connection drops), so no lasting duplicate survives a
    later iTerm2 restart + AutoLaunch. Runs under sys.executable, the same
    interpreter we already verified can import iterm2 (see _interpreter_ready)."""
    env = dict(os.environ, ITERM2_COOKIE=cookie)
    subprocess.Popen([sys.executable, script], env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _interpreter_ready() -> bool:
    """True when the interpreter that will run the provider (sys.executable,
    i.e. us) can import iterm2. The provider imports iterm2 and would die at
    once without it - and we DEVNULL its output, so the failure would otherwise
    be silent. Checking here turns that into an actionable 'no-iterm2'."""
    try:
        return importlib.util.find_spec("iterm2") is not None
    except Exception:
        return False


def _confirm_alive(timeout=3.0, interval=0.25, sleep=time.sleep) -> bool:
    """Poll the heartbeat after a launch to CONFIRM the provider actually came
    up, rather than trusting that Popen succeeding means it registered. Returns
    True as soon as the heartbeat is fresh, False if it never appears within
    `timeout` (crash on startup, bad runtime, iTerm2 refused the cookie)."""
    waited = 0.0
    while True:
        if statusbar.provider_alive():
            return True
        if waited >= timeout:
            return False
        sleep(interval)
        waited += interval


def ensure(*, cookie_getter=request_cookie, spawn=_spawn,
           interpreter_ready=_interpreter_ready, confirm=_confirm_alive) -> str:
    """Idempotently make the provider run. Returns a verdict string:
    'absent' | 'alive' | 'no-cookie' | 'no-iterm2' | 'start-failed' |
    'start-unconfirmed' | 'start'. Never raises the launch out."""
    installed = statusbar.provider_installed()
    alive = statusbar.provider_alive()
    # Only pay for an osascript round-trip when a start is actually possible.
    cookie = cookie_getter() if (installed and not alive) else ""
    action = statusbar.plan_provider_start(installed, alive, bool(cookie))
    if action != "start":
        return action
    if not interpreter_ready():
        return "no-iterm2"
    try:
        spawn(statusbar.provider_script_path(), cookie)
    except Exception:
        return "start-failed"
    # Popen returning is not proof the provider registered - confirm liveness.
    return "start" if confirm() else "start-unconfirmed"


_MESSAGES = {
    "absent": "provider not installed (relay renders the badge itself)",
    "alive": "provider already running",
    "no-cookie": "provider not running and iTerm2 gave no cookie (is iTerm2 "
                 "running?) - start it via Scripts > AutoLaunch > "
                 "relay_statusbar.py",
    "no-iterm2": f"cannot start the provider: this Python ({sys.executable}) "
                 "has no 'iterm2' module - pip3 install iterm2",
    "start-failed": "tried to start the provider but the launch failed",
    "start-unconfirmed": "launched the provider but it did not come alive - "
                         "check it runs: python3 " + statusbar.provider_script_path(),
    "start": "started the status-bar provider (badge heals in ~2s)",
}


def main() -> int:
    action = ensure()
    print(f"relay statusbar: {_MESSAGES.get(action, action)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
