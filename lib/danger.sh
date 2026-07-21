#!/bin/bash
# Relay - command classification. DELIBERATELY SIMPLE.
#
# Philosophy: in "safe" mode Relay escalates ONLY a short list of genuinely
# catastrophic / irreversible / remote-destructive commands, and auto-approves
# everything else. We do NOT try to be clever about command substitution,
# redirects, pipe-into-shell, etc. - that cleverness produced constant false
# escalations on benign things like `echo $(wc -l ...)`. If a tab needs zero
# classification at all, arm it "wild"/"insane"; if a tab is too sensitive for a short
# denylist, leave it manual.
#
# To adjust your risk posture, edit RELAY_DANGER below. That's the whole knob.

# Catastrophic patterns -> escalate to a human even when armed "safe".
# rm only escalates for ROOT, system dirs, or $HOME - NOT /tmp or relative dirs
# (rm -rf build / rm -rf /tmp/x are everyday, approved).
# Note the two swarm self-escalation guards near the end: a session must not
# be able to arm itself or a puppet (relay ... --arm) or reach into relay's
# own state DB (sqlite3 ... relay*.db) - both would grant approval powers a
# safe-armed session is not supposed to have.
RELAY_DANGER='(\bdd\s+if=|\bmkfs|\b:\s*\(\)\s*\{|chmod\s+-R\s+/|chown\s+-R\s+/|>\s*(~|/etc|/usr|/bin|/Users|/var|/dev/sd|/dev/disk)|\bsed\s+-i\b[^|]*\s/(etc|usr|bin|var)|\bgit\s+push\b.*(--force|-f\b)|\bgit\s+reset\s+--hard|\bkubectl\s+delete|\bterraform\s+(destroy|apply)|\bterragrunt\s+(destroy|apply)|\baws\b.*\b(delete-|terminate-|rm\b)|\bgcloud\b.*\b(delete|deploy)|\bpsql\b.*-c|\bmysql\b.*-e|\bdocker\s+(rm|kill|rmi)\b|\b(pkill|killall)\b|\bkill\s+(-[0-9A-Za-z]+\s+)*[0-9]|\bssh\b|\bscp\b|curl\b.*-X\s*(POST|PUT|DELETE|PATCH)|wget\b.*--method=(POST|PUT|DELETE)|\brelay\b[^|]*\s--arm\b|\bsqlite3\b[^|]*relay[^|]*\.db)'

# rm -rf is handled separately (clearer than a mega-regex): escalate when the
# target is an ABSOLUTE path or home (~ / $HOME) - EXCEPT under /tmp - or a
# bare ~ . Relative paths (build, node_modules) and /tmp/* are approved.
RELAY_RM='\brm\s+-[a-z]*[rf][a-z]*\s'

# Piping into a shell (curl ... | sudo bash) - checked on the WHOLE command,
# since splitting on '|' below would otherwise hide it.
RELAY_PIPE_SHELL='\|\s*(sudo\s+)?(ba|z)?sh\b'

# Read-only leading commands - the allowlist for preset=paranoid, where the
# posture flips to DEFAULT-DENY: only these classify safe, everything else
# escalates (closing the make/npm/python leader gaps at the cost of far more
# escalations). Selected via [danger] preset in ~/.relay/config; the watcher
# exports it as RELAY_DANGER_PRESET.
RELAY_READONLY='^[[:space:]]*(ls|cat|head|tail|less|grep|rg|egrep|fgrep|find|fd|wc|echo|printf|pwd|which|whoami|date|env|printenv|stat|file|du|df|tree|sort|uniq|cut|tr|jq|basename|dirname|realpath|shasum|md5|diff|git[[:space:]]+(status|log|diff|show|branch|remote|fetch)\b|sed[[:space:]]+-n\b|awk\b|relay[[:space:]]+(task[[:space:]]+list|msgs|inbox|status|doctor|version)\b)'

# relay_is_dangerous "<command string>" -> exit 0 if dangerous, 1 if safe.
relay_is_dangerous() {
  local cmd="$1" seg
  if [ "${RELAY_DANGER_PRESET:-default}" = "paranoid" ]; then
    # DEFAULT-DENY: every segment must lead with a read-only command.
    while IFS= read -r seg; do
      [ -z "${seg// /}" ] && continue
      echo "$seg" | grep -iqE "$RELAY_READONLY" || return 0
    done < <(echo "$cmd" | tr ';|&\n' '\n')
    return 1
  fi
  echo "$cmd" | grep -iqE "$RELAY_PIPE_SHELL" && return 0
  while IFS= read -r seg; do
    [ -z "${seg// /}" ] && continue
    echo "$seg" | grep -iqE "$RELAY_DANGER" && return 0
    # rm -rf of an absolute/home path (not /tmp) -> dangerous.
    if echo "$seg" | grep -iqE "$RELAY_RM"; then
      echo "$seg" | grep -iqE "$RELAY_RM(-[a-z]*\s+)*(/tmp/|/private/tmp/)" && continue
      echo "$seg" | grep -iqE "$RELAY_RM(-[a-z]*\s+)*(/|~|\\\$HOME)" && return 0
    fi
  done < <(echo "$cmd" | tr ';|&\n' '\n')
  return 1
}
