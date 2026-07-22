"""Pure recap aggregation over audit entries. No I/O, no iTerm2 - the CLI
(relay recap) and the on-quit line both read the audit log and hand the rows
here. Mirrors the statusbar.py split: pure logic here, I/O at the call site."""
import time


def start_of_today() -> float:
    """Local-midnight epoch seconds - the default recap window start."""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def summarize(entries, since: float) -> dict:
    """Count audit verdicts at/after `since`. Returns the headline tallies.
    Never raises on odd or partial rows."""
    out = {"cleared": 0, "woke": 0, "delivered": 0}
    for e in entries:
        try:
            if float(e.get("ts", 0)) < since:
                continue
            v = e.get("verdict")
        except Exception:
            continue
        if v == "auto-approved":
            out["cleared"] += 1
        elif v == "escalated":
            out["woke"] += 1
        elif v == "delivered":
            out["delivered"] += 1
    return out
