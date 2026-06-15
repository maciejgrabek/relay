#!/bin/bash
# Relay - record a session's state for the TUI. Invoked by hooks.
#   Usage: relay-status.sh <working|blocked|idle>   (hook JSON on stdin)
# MUST print nothing to stdout: some hook events feed stdout back into context.
STATE="${1:-working}"
DIR="${RELAY_HOME:-$HOME/.relay}/sessions"
mkdir -p "$DIR" 2>/dev/null
INPUT=$(cat)
SID=$(printf '%s' "$INPUT"  | jq -r '.session_id // "unknown"' 2>/dev/null)
CWD=$(printf '%s' "$INPUT"  | jq -r '.cwd // empty'           2>/dev/null)
TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty'     2>/dev/null)
NAME=$(basename "$CWD" 2>/dev/null); [ -z "$NAME" ] && NAME="$SID"
TS=$(date +%s)
jq -n --arg sid "$SID" --arg cwd "$CWD" --arg name "$NAME" \
      --arg state "$STATE" --arg tool "$TOOL" --argjson ts "$TS" \
      '{session_id:$sid,cwd:$cwd,name:$name,state:$state,tool:$tool,ts:$ts}' \
      > "$DIR/$SID.json" 2>/dev/null
exit 0
