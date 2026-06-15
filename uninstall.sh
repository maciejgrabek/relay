#!/bin/bash
# Relay uninstaller. Removes Relay's hook entries from a settings file, leaving
# all other hooks and permissions intact. Same target flags as install.sh.
#   ./uninstall.sh [--global | --target <file>]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v jq >/dev/null || { echo "needs jq"; exit 1; }

case "${1:-}" in
  --global) TARGET="$HOME/.claude/settings.json" ;;
  --target) TARGET="${2:?--target needs a path}" ;;
  "" )      TARGET="$PWD/.claude/settings.local.json" ;;
  *) echo "unknown arg: $1"; exit 1 ;;
esac
[ -f "$TARGET" ] || { echo "no settings file at $TARGET"; exit 0; }

BACKUP="$TARGET.relay-backup.$(date +%Y%m%d-%H%M%S)"
cp "$TARGET" "$BACKUP"
TMP="$(mktemp)"
jq --arg repo "$REPO" '
  def strip_relay(arr): [ (arr // [])[]
    | .hooks = [ (.hooks // [])[] | select((.command // "") | contains($repo) | not) ]
    | select((.hooks | length) > 0) ];
  .hooks.PreToolUse       = strip_relay(.hooks.PreToolUse)
  | .hooks.Stop           = strip_relay(.hooks.Stop)
  | .hooks.Notification   = strip_relay(.hooks.Notification)
  | .hooks.UserPromptSubmit = strip_relay(.hooks.UserPromptSubmit)
  | .hooks |= with_entries(select((.value | length) > 0))
' "$TARGET" > "$TMP"
jq empty "$TMP" && mv "$TMP" "$TARGET" || { echo "merge failed; backup: $BACKUP"; rm -f "$TMP"; exit 1; }
echo "✓ Relay removed from $TARGET (backup: $BACKUP)"
echo "  Note: Relay's permission deny-rules were left in place. Remove manually if desired."
echo "  Restart Claude Code sessions to apply."
