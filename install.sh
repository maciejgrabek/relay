#!/bin/bash
# Relay installer. Wires Relay's hooks into a project's (or your global)
# Claude Code settings, idempotently and non-destructively.
#
#   ./install.sh                 # install into ./.claude/settings.local.json (cwd project)
#   ./install.sh --global        # install into ~/.claude/settings.json (all projects)
#   ./install.sh --target <file> # install into a specific settings file
#
# Re-running is safe: it replaces only Relay's own hook entries and merges its
# permission rules, leaving everything else untouched. A timestamped backup of
# the settings file is written before any change.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE="$REPO/hooks/relay-gate.sh"
STATUS="$REPO/hooks/relay-status.sh"
ALERT="${RELAY_ALERT_SOUND:-/System/Library/Sounds/Sosumi.aiff}"
DONE="${RELAY_DONE_SOUND:-/System/Library/Sounds/Glass.aiff}"

command -v jq >/dev/null || { echo "Relay needs jq (brew install jq)"; exit 1; }

TARGET=""
case "${1:-}" in
  --global)  TARGET="$HOME/.claude/settings.json" ;;
  --target)  TARGET="${2:?--target needs a path}" ;;
  "" )       TARGET="$PWD/.claude/settings.local.json" ;;
  -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown arg: $1"; exit 1 ;;
esac

mkdir -p "$(dirname "$TARGET")"
[ -f "$TARGET" ] || echo '{}' > "$TARGET"
jq empty "$TARGET" 2>/dev/null || { echo "ERROR: $TARGET is not valid JSON. Fix or remove it first."; exit 1; }

BACKUP="$TARGET.relay-backup.$(date +%Y%m%d-%H%M%S)"
cp "$TARGET" "$BACKUP"
chmod +x "$REPO"/hooks/*.sh "$REPO"/bin/relay 2>/dev/null || true

# Merge: install Relay hooks, drop any prior Relay hook entries (match on
# repo path so re-install is clean), keep all non-Relay hooks. Also strip junk
# allow-entries that Claude Code may have auto-captured for jq/cp/mv/afplay.
TMP="$(mktemp)"
jq --arg gate "$GATE" --arg status "$STATUS" --arg alert "$ALERT" --arg done "$DONE" --arg repo "$REPO" '
  def strip_relay(arr): [ (arr // [])[]
    | .hooks = [ (.hooks // [])[] | select((.command // "") | contains($repo) | not) ]
    | select((.hooks | length) > 0) ];

  # Preserve existing user hooks, minus any previous Relay entries.
  .hooks.PreToolUse       = strip_relay(.hooks.PreToolUse)
  | .hooks.Stop           = strip_relay(.hooks.Stop)
  | .hooks.Notification   = strip_relay(.hooks.Notification)
  | .hooks.UserPromptSubmit = strip_relay(.hooks.UserPromptSubmit)

  # Add Relay hooks.
  | .hooks.PreToolUse       += [ { "matcher":"Bash", "hooks":[ {"type":"command","command":$gate} ] } ]
  | .hooks.Stop             += [ { "hooks":[ {"type":"command","command":("afplay "+$done)}, {"type":"command","command":($status+" idle")} ] } ]
  | .hooks.Notification     += [ { "hooks":[ {"type":"command","command":("afplay "+$alert)}, {"type":"command","command":($status+" blocked")} ] } ]
  | .hooks.UserPromptSubmit += [ { "hooks":[ {"type":"command","command":($status+" working")} ] } ]

  # Secret-read denies (Relay auto-approves Read when armed, so protect these).
  | .permissions.deny = ((.permissions.deny // []) + ["Read(./.env)","Read(./.env.*)","Read(**/*.pem)","Read(**/id_rsa*)","Read(**/id_ed25519*)"] | unique)

  # Drop dangerous junk allow-rules accidentally captured during setup.
  | .permissions.allow = [ (.permissions.allow // [])[]
      | select(test("^Bash\\((jq |cp settings|mv settings|afplay |chmod \\+x|break\\)$)") | not) ]
' "$TARGET" > "$TMP"

jq empty "$TMP" || { echo "ERROR: merge produced invalid JSON; left $TARGET untouched (backup: $BACKUP)"; rm -f "$TMP"; exit 1; }
mv "$TMP" "$TARGET"

echo "✓ Relay installed into: $TARGET"
echo "  backup: $BACKUP"
echo
echo "Next:"
echo "  1) Add Relay to your PATH:   export PATH=\"$REPO/bin:\$PATH\"   (add to ~/.zshrc)"
echo "  2) Restart any Claude Code sessions in that project (hooks load at startup)."
echo "  3) Run:  relay"
