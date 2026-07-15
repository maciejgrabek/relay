#!/bin/bash
# Relay uninstaller. Removes the PATH line that install.sh added to your shell
# rc, and the Claude Code skill symlinks install.sh may have created. Writes a
# timestamped backup of any shell rc before editing.
#   ./uninstall.sh
set -uo pipefail
MARKER="# relay"
SKILLS_DST="$HOME/.claude/skills"

removed=0
for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
  [ -f "$RC" ] || continue
  if grep -q "$MARKER\$" "$RC" 2>/dev/null; then
    BACKUP="$RC.relay-backup.$(date +%Y%m%d-%H%M%S)"
    cp "$RC" "$BACKUP"
    grep -v "$MARKER\$" "$RC" > "$RC.tmp" && mv "$RC.tmp" "$RC"
    echo "✓ removed Relay's PATH line from $RC (backup: $BACKUP)"
    removed=1
  fi
done

if [ "$removed" = 0 ]; then
  echo "No Relay PATH line found in ~/.zshrc or ~/.bashrc - nothing to do."
else
  echo "Open a new shell (or re-source your rc) to apply."
fi

skills_removed=0
for s in relay-worker relay-coordinator relay-cli-reference.md; do
  if [ -L "$SKILLS_DST/$s" ]; then
    rm "$SKILLS_DST/$s"
    echo "✓ removed $SKILLS_DST/$s"
    skills_removed=1
  fi
done
[ "$skills_removed" = 0 ] && echo "No Relay skill symlinks found in $SKILLS_DST - nothing to do."

exit 0
