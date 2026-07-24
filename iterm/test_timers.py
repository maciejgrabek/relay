"""Tests for the pure session-timer logic. No iTerm2/sqlite imports.

Run: python3 iterm/test_timers.py    or    ./test/run.sh
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import timers  # noqa: E402


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def _t(**kw):
    base = dict(enabled=1, active=1, interval_min=5, mode="idle",
                last_fired_at=1000.0, bound_at=1000.0)
    base.update(kw)
    return base


def run():
    ok = True

    ok &= check("clamp below -> 1", timers.clamp_interval(0) == 1)
    ok &= check("clamp above -> 90", timers.clamp_interval(999) == 90)
    ok &= check("clamp in range", timers.clamp_interval(37) == 37)
    ok &= check("clamp non-int -> 1", timers.clamp_interval("x") == 1)

    ok &= check("sanitize strips newlines",
                "\n" not in timers.sanitize_payload("a\nb\r\nc"))
    ok &= check("sanitize trims", timers.sanitize_payload("  hi  ") == "hi")

    ok &= check("due when interval elapsed",
                timers.is_due(_t(interval_min=5, last_fired_at=1000.0),
                              now=1000.0 + 5 * 60))
    ok &= check("not due before interval",
                not timers.is_due(_t(interval_min=5, last_fired_at=1000.0),
                                  now=1000.0 + 5 * 60 - 1))
    ok &= check("disabled never due",
                not timers.is_due(_t(enabled=0), now=10 ** 9))
    ok &= check("inactive never due",
                not timers.is_due(_t(active=0), now=10 ** 9))
    batch = [_t(interval_min=1, last_fired_at=0.0),
             _t(interval_min=1, last_fired_at=0.0, enabled=0),
             _t(interval_min=90, last_fired_at=0.0)]
    ok &= check("due_timers filters", len(timers.due_timers(batch, now=120.0)) == 1)

    ok &= check("idle mode needs ready",
                not timers.firable(_t(mode="idle"), ready=False, paused=False,
                                   armed=True, require_armed=False))
    ok &= check("idle fires when ready",
                timers.firable(_t(mode="idle"), ready=True, paused=False,
                               armed=False, require_armed=False))
    ok &= check("now mode ignores ready",
                timers.firable(_t(mode="now"), ready=False, paused=False,
                               armed=False, require_armed=False))
    ok &= check("pause freezes everything",
                not timers.firable(_t(mode="now"), ready=True, paused=True,
                                   armed=True, require_armed=False))
    ok &= check("require_armed blocks unarmed",
                not timers.firable(_t(mode="now"), ready=True, paused=False,
                                   armed=False, require_armed=True))
    ok &= check("require_armed allows armed",
                timers.firable(_t(mode="now"), ready=True, paused=False,
                               armed=True, require_armed=True))

    ok &= check("next_due_in counts down",
                timers.next_due_in(_t(interval_min=5, last_fired_at=1000.0),
                                   now=1000.0 + 60) == 4 * 60)

    day = 86400.0
    ok &= check("needs_reconfirm past the window",
                timers.needs_reconfirm(_t(bound_at=0.0), now=8 * day,
                                       reconfirm_days=7))
    ok &= check("no reconfirm within the window",
                not timers.needs_reconfirm(_t(bound_at=0.0), now=6 * day,
                                           reconfirm_days=7))
    ok &= check("reconfirm disabled at 0",
                not timers.needs_reconfirm(_t(bound_at=0.0), now=10 ** 9,
                                           reconfirm_days=0))

    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
