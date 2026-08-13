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


# --- the review loop: a verdict on relay's OWN judgment -----------------------
# relay recap answers "what did relay do". This answers the harder question the
# audit log has always been able to support and nothing ever asked it:
#
#     was relay RIGHT to do it?
#
# Relay's whole argument is supervision quality. Until now the only evidence for
# that was the absence of complaints, which is not evidence. Every number below
# comes from data already on disk since the first day the audit log existed.
#
# The distinction that matters is between an approval the SAFETY GATE made and
# an approval the ARM LEVEL made over the gate's objection. Both land in the log
# as "auto-approved", and they mean opposite things:
#
#   safe permission prompt          -> the gate read the command and cleared it
#   wild/insane/extreme-approve (dangerous command)
#                                   -> the gate said DANGEROUS, the arm level
#                                      approved anyway. This is the arm level
#                                      working as designed, and it is also the
#                                      only list in relay that answers "what did
#                                      I actually authorise by arming that tab?"
#   ...(could not parse / too large / fail safe)
#                                   -> approved WITHOUT the gate being able to
#                                      read the command at all. Not overruled -
#                                      unexamined, which is a different risk.
#
# Nobody is being scolded here: arming insane is a deliberate choice and these
# are its consequences, shown once rather than 128 times in a scrollback nobody
# re-reads.

# The risky verbs danger.sh escalates on, mapped back out of the command text.
# Grouping overrides by EXACT command is near-useless - almost every one is a
# distinct one-off, so a real log produces 133 buckets of "1x" and answers
# nothing. Grouping by the verb answers the question the operator actually has:
# "what kind of thing did I authorise by arming that tab?" -> "ssh 40x,
# sqlite3 22x, curl -X 18x". Kept in the same order of specificity as
# danger.sh's own patterns; a command matching several is counted under each,
# because `ssh ... | psql` is honestly both.
_RISK_VERBS = (
    ("rm -rf", r"\brm\s+-[a-z]*[rf]"), ("dd", r"\bdd\s+if="),
    ("mkfs", r"\bmkfs"), ("chmod -R /", r"chmod\s+-R\s+/"),
    ("chown -R /", r"chown\s+-R\s+/"), ("git push --force", r"push\b.*(--force|-f\b)"),
    ("git reset --hard", r"reset\s+--hard"), ("kubectl delete", r"kubectl\s+delete"),
    ("terraform", r"\bterra(form|grunt)\s+(destroy|apply)"),
    ("aws", r"\baws\b.*\b(delete-|terminate-|rm\b)"),
    ("gcloud", r"\bgcloud\b.*\b(delete|deploy)"), ("psql", r"\bpsql\b.*-c"),
    ("mysql", r"\bmysql\b.*-e"), ("docker rm/kill", r"\bdocker\s+(rm|kill|rmi)\b"),
    ("pkill/killall", r"\b(pkill|killall)\b"), ("kill", r"\bkill\s+(-\S+\s+)*[0-9]"),
    ("ssh", r"\bssh\b"), ("scp", r"\bscp\b"),
    ("curl -X", r"curl\b.*-X\s*(POST|PUT|DELETE|PATCH)"),
    ("wget --method", r"wget\b.*--method="),
    ("relay --arm", r"\brelay\b[^|]*\s--arm\b"),
    ("sqlite3 relay.db", r"\bsqlite3\b[^|]*relay[^|]*\.db"),
    ("pipe into shell", r"\|\s*(sudo\s+)?(ba|z)?sh\b"),
)


def risk_tags(cmd: str) -> list:
    """Which danger.sh-shaped verbs appear in a command. '' -> ['(unreadable)'].

    Best-effort and intentionally simpler than danger.sh: this labels an
    approval that ALREADY happened, it does not decide anything. A command that
    matches nothing here still counted as dangerous at the time - danger.sh saw
    a shape this summary does not model - so it is bucketed as '(other)' rather
    than dropped, which would understate the total.
    """
    import re as _re
    if not (cmd or "").strip():
        return ["(unreadable)"]
    hits = [name for name, pat in _RISK_VERBS
            if _re.search(pat, cmd, _re.I)]
    return hits or ["(other)"]


# Substrings that mark an approval the arm level made over the gate's verdict.
_OVERRIDE_MARK = "dangerous command"
# ...and one the gate could not examine at all. 'fail safe' covers the
# cursor-not-on-option-1 and unparseable cases; 'too large' the off-screen
# header. Matched on substrings because the reason strings are composed
# ("insane-approve (<inner reason>)") rather than enumerated.
_UNVERIFIED_MARKS = ("fail safe", "could not parse", "too large")


def review(entries, since: float = 0.0) -> dict:
    """Aggregate the audit log into a judgment on relay's own decisions.

    Pure, like summarize() above - the CLI reads the log and hands rows here.
    Never raises on odd or partial rows: an audit log is append-only and may
    hold entries written by an older relay with fields this build has never
    seen, and a review that crashes on one bad line is a review nobody gets.
    """
    out = {"approved": 0, "clean": 0, "overridden": 0, "unverified": 0,
           "escalated": 0, "delivered": 0, "pushed": 0,
           "override_cmds": {}, "unverified_cmds": {}, "sessions": {}}
    for e in entries:
        try:
            if float(e.get("ts", 0)) < since:
                continue
            v = e.get("verdict") or ""
            reason = (e.get("reason") or "").lower()
            cmd = (e.get("command") or "").strip()
            sess = (e.get("session") or "?").strip() or "?"
        except Exception:
            continue
        if v == "escalated":
            out["escalated"] += 1
            continue
        if v == "delivered":
            out["delivered"] += 1
            continue
        if v == "extreme-pushed":
            out["pushed"] += 1
            continue
        if v != "auto-approved":
            continue
        out["approved"] += 1
        # Order matters: an override is reported as an override even when its
        # inner reason also mentions a fail-safe, because "the gate said no and
        # was overruled" is the stronger fact about that approval.
        if _OVERRIDE_MARK in reason:
            out["overridden"] += 1
            bucket, key = out["override_cmds"], cmd or "(command unreadable)"
        elif any(m in reason for m in _UNVERIFIED_MARKS):
            out["unverified"] += 1
            bucket, key = out["unverified_cmds"], cmd or "(command unreadable)"
        else:
            out["clean"] += 1
            continue
        for tag in risk_tags(key if key != "(command unreadable)" else ""):
            bucket[tag] = bucket.get(tag, 0) + 1
        out["sessions"][sess] = out["sessions"].get(sess, 0) + 1
    return out


def review_lines(r: dict, top: int = 8) -> list:
    """The review as plain text.

    Leads with the count the operator cannot get anywhere else, and stays
    silent when there is nothing to report: a review that always prints a
    warning block is one that stops being read.
    """
    n = r["approved"]
    out = [f"  approvals: {n}  ({r['clean']} cleared by the safety gate, "
           f"{r['overridden']} approved over it, {r['unverified']} unverified)",
           f"  escalated to you: {r['escalated']}"
           + (f" · delivered {r['delivered']}" if r["delivered"] else "")
           + (f" · extreme pushes {r['pushed']}" if r["pushed"] else "")]
    if n:
        # The rate is the honest headline: 135 overrides reads as alarming
        # until you know it is 5% of 2,700, and it reads as complacent if you
        # only ever see the percentage. Both, always.
        pct = 100.0 * (r["overridden"] + r["unverified"]) / n
        out.append(f"  {pct:.1f}% of approvals did NOT come from the safety "
                   f"gate reading the command")
    if r["overridden"]:
        out.append("")
        out.append(f"  the gate said DANGEROUS and the arm level approved "
                   f"anyway ({r['overridden']}):")
        out.append("    " + " · ".join(
            f"{tag} {c}x" for tag, c in
            sorted(r["override_cmds"].items(), key=lambda kv: -kv[1])[:top]))
    if r["unverified"]:
        out.append("")
        out.append(f"  approved WITHOUT the gate being able to read the "
                   f"command ({r['unverified']}):")
        out.append("    " + " · ".join(
            f"{tag} {c}x" for tag, c in
            sorted(r["unverified_cmds"].items(), key=lambda kv: -kv[1])[:top]))
    if r["sessions"] and (r["overridden"] or r["unverified"]):
        out.append("")
        top_s = sorted(r["sessions"].items(), key=lambda kv: -kv[1])[:5]
        out.append("  by session: "
                   + " · ".join(f"{s} {c}" for s, c in top_s))
    if not (r["overridden"] or r["unverified"]) and n:
        out.append("")
        out.append("  every approval came from the safety gate reading the "
                   "command. Nothing was waved through.")
    return out

