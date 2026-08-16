"""Is this session working, unattended, and getting nowhere?

Pure decision logic, like power.py / titles.py: no Textual, no iTerm2, no
subprocess. The app samples the git tree and the transcript; this decides what
those samples mean, so every case is testable against a fake clock.

Relay reports the cheap failure well - a blocked session costs nothing while it
waits, and it is badged, notified and escalated. The expensive one is
invisible: a session retrying the same failing test for four hours reads
`working` on every tick, so the panel shows a calm fleet. Every existing signal
is a snapshot of what a session is DOING; none measures what it achieved.

Progress here is the git working tree. Not the screen (that infers a loop from
the most fragile surface relay owns) and not turns (a loop increases them).
Tokens are evidence, never a trigger: calibration against real transcripts put
a 25k-per-15-minutes threshold at the MEDIAN, and a retry loop emits short tool
calls over a huge cached context, so it plausibly produces LESS output than a
productive window.

Two rules keep it from lying, and they are the whole reason this is safe to
show:

  shared   - two sessions in one directory is the NORMAL case, and there the
             tree cannot say which of them moved it. No claim.
  attended - while a tab is the selected one its clock is held at zero. If you
             are in the tab you can see the loop yourself; the badge is for the
             tabs you are not reading.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

# Turns completed since the anchor before a stalled tree counts as a loop.
# Below this, the session is sitting inside one long turn, which is not the
# failure this looks for. A floor in turns rather than tokens needs no
# calibration per machine or per model.
MIN_TURNS = 3


@dataclass
class Track:
    fp: str = ""              # tree fingerprint last seen
    since: float = 0.0        # when THAT fingerprint was first seen
    turns_at: int = 0         # cumulative turns at that moment
    out_at: int = 0           # cumulative output tokens at that moment


@dataclass(frozen=True)
class Verdict:
    burning: bool = False
    quiet_for: float = 0.0    # seconds the tree has been unchanged
    turns: int = 0            # turns since the anchor
    spent: int = 0            # output tokens since the anchor
    reason: str = ""          # why NOT claiming: off/no-git/no-usage/
                              # shared/attended/idle. "" when burning.


def _armed(now: float, fp: str, turns, out) -> Track:
    return Track(fp=fp, since=now, turns_at=int(turns or 0),
                 out_at=int(out or 0))


def sample(track: Track, now: float, fp: str, turns, out, working: bool,
           shared: bool, attended: bool, window_min: float
           ) -> Tuple[Track, Verdict]:
    """Advance the track and judge it. Returns (new track, verdict).

    Order matters. `attended` and a changed fingerprint both RE-ARM the anchor,
    because both mean "the clock should start from here". Everything else only
    blocks the claim and leaves the anchor running - in particular `idle`,
    which a healthy session enters between every pair of turns.
    """
    if window_min <= 0:
        return track, Verdict(reason="off")
    if not fp:
        # No git, no read, no claim. Keep the old anchor: the tree may become
        # readable again and the elapsed time was still real.
        return track, Verdict(reason="no-git")
    if turns is None or out is None:
        return track, Verdict(reason="no-usage")

    if attended:
        # Clock held at zero while you are in the tab. Re-arming here (rather
        # than testing recency at judgement time) is what makes the countdown
        # start the moment you leave instead of a window later.
        return _armed(now, fp, turns, out), Verdict(reason="attended")

    if fp != track.fp:
        return _armed(now, fp, turns, out), Verdict()

    quiet = now - track.since
    dt = int(turns) - track.turns_at
    ds = int(out) - track.out_at

    if shared:
        return track, Verdict(quiet_for=quiet, turns=dt, spent=ds,
                              reason="shared")
    if not working:
        # Judged here and NEVER accumulated: detect_state says working during a
        # turn and idle at a ready prompt, so a healthy session alternates
        # every turn. Re-arming on idle would reset this several times a minute
        # and it could never accumulate. The turn floor is what excludes a
        # session that has genuinely stopped.
        return track, Verdict(quiet_for=quiet, turns=dt, spent=ds,
                              reason="idle")

    burning = quiet >= window_min * 60 and dt >= MIN_TURNS
    # No reason either way here: nothing is BLOCKING the claim, the window or
    # the turn floor simply has not been reached yet. reason is for silences
    # the operator would otherwise have to guess at.
    return track, Verdict(burning=burning, quiet_for=quiet, turns=dt,
                          spent=ds)


def _fmt_tokens(n) -> str:
    """Compact, matching usage.fmt_tokens so two panels never disagree about
    how a number is spelled."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1000000:
        return f"{n / 1000.0:.1f}k".replace(".0k", "k")
    return f"{n / 1000000.0:.1f}M".replace(".0M", "M")


def evidence(v: Verdict) -> str:
    """The preview's one-line proof. Says what was measured, never why."""
    mins = int(v.quiet_for // 60)
    return (f"{mins}m unchanged, {v.turns} turns, "
            f"{_fmt_tokens(v.spent)} out")
