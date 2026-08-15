"""Relay's power assertion - when to hold `caffeinate` and when to let go.

Pure decision logic, like titles.py / statusbar.py: no Textual, no iTerm2, no
subprocess. `tick` returns a DESIRE ("should the child be running") and app.py
reconciles it against the real process, so every transition here is testable
against a fake clock without spawning anything.

Releasing is not sleeping. Relay stops PREVENTING sleep and hands the decision
back to macOS, which already knows from real HID input whether a human is at
the machine. That is why this is safe to decide locally, and why "relay slept
my Mac while I was sitting here" cannot happen.

Three states, two of them released, and the difference between the two is the
whole design:

    HELD  -- c -->  RELEASED (manual)      manual survives work resuming, or a
      ^                   |                session waking at 3am would undo the
      +------- c ---------+                release you made before walking away

    HELD  -- idle N min -->  RELEASED (auto)     auto re-acquires on work, or
      ^                            |             the first quiet stretch would
      +---- any session working ---+             disarm the feature for the run

Nothing here persists across a restart: release_after is the durable
preference, held/manual are this-run intent, the same class as pause and
shadow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class Power:
    release_after: float             # minutes of an idle fleet; 0 = never
    held: bool = True
    manual: bool = False             # the release was the operator's
    idle_since: Optional[float] = None   # None = something is working

    def tick(self, now: float, any_working: bool) -> bool:
        """Advance the clock. Returns whether the assertion should be held."""
        if any_working:
            self.idle_since = None
            # Auto-releases yield to work; manual ones do not.
            if not self.held and not self.manual:
                self.held = True
            return self.held
        if self.idle_since is None:
            self.idle_since = now
        if (self.held and self.release_after > 0
                and now - self.idle_since >= self.release_after * 60):
            self.held = False
            self.manual = False
        return self.held

    def toggle(self, now: float) -> None:
        """The `c` key. Releasing by hand is sticky; taking the assertion back
        restarts the idle clock rather than pinning it, so the timer can fire
        again later in the same run (a permanent hold is release_after = 0)."""
        if self.held:
            self.held = False
            self.manual = True
        else:
            self.held = True
            self.manual = False
            self.idle_since = now

    def status(self, now: float) -> str:
        """Header text, or "" when there is nothing worth saying. Earns space
        only while the timer is armed and running, or once released - a Mac
        that went to sleep must never be a mystery."""
        if not self.held:
            return "☕ released (c)" if self.manual else "☕ released · Mac may sleep"
        if self.release_after <= 0 or self.idle_since is None:
            return ""
        left = self.release_after * 60 - (now - self.idle_since)
        if left <= 0:
            # tick() runs before status() every frame, so this is only
            # reachable if a caller asks out of order. Say nothing rather than
            # promise a release that already happened.
            return ""
        if left >= 60:
            return f"☕ releases in {math.ceil(left / 60)}m"
        return f"☕ releases in {math.ceil(left)}s"
