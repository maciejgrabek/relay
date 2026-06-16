#!/bin/bash
# Relay uninstaller. Removes the PATH line that install.sh added to your shell
# rc. It touches nothing else and writes a timestamped backup before editing.
#   ./uninstall.sh
set -uo pipefail
MARKER="# relay"

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
exit 0
