#!/bin/bash
# Relay - PreToolUse gate for Bash (self-locating; no hardcoded paths).
#   - Records session state for the TUI.
#   - DISARMED session  -> passthrough: emit {} so Claude Code's normal prompt
#     flow runs. You drive it manually.
#   - ARMED session      -> auto-allow, EXCEPT irreversible-local / remote-
#     mutating commands, which are gated (ask) and ring the alert sound.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/danger.sh"

RELAY_HOME="${RELAY_HOME:-$HOME/.relay}"
DIR="$RELAY_HOME/sessions"
ALERT="${RELAY_ALERT_SOUND:-/System/Library/Sounds/Sosumi.aiff}"

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // "unknown"' 2>/dev/null)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)

printf '%s' "$INPUT" | "$HERE/relay-status.sh" working >/dev/null 2>&1

# Not armed -> hand control back to Claude Code's default permission flow.
[ -f "$DIR/$SID.armed" ] || { echo '{}'; exit 0; }

if relay_is_dangerous "$CMD"; then
  printf '%s' "$INPUT" | "$HERE/relay-status.sh" blocked >/dev/null 2>&1
  [ -f "$ALERT" ] && afplay "$ALERT" &
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"Relay: irreversible-local or remote-mutating command - manual approval required."}}'
else
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
fi
exit 0
