#!/bin/bash
# Relay - run the whole test suite: the bash classifier suite plus every Python
# gate/TUI suite. No pytest needed - each Python suite has a __main__ runner.
#
#   ./test/run.sh        run everything
#   ./test/run.sh -v     verbose (forwards -v to the bash classifier suite)
set -uo pipefail

# Defense-in-depth: no test (present or future) should ever be able to write
# the developer's real ~/.relay/config. A throwaway path for the whole suite.
export RELAY_CONFIG="$(mktemp -d)/relay-test-config"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
fail=0

echo "=== classifier (lib/danger.sh) ==="
bash "$HERE/danger_test.sh" "$@" || fail=1

echo
echo "=== iterm gate/TUI suites ==="
for t in "$REPO"/iterm/test_*.py; do
  echo "--- $(basename "$t") ---"
  python3 "$t" || fail=1
done

echo
if [ "$fail" -eq 0 ]; then
  echo "ALL SUITES PASSED"
else
  echo "SOME SUITES FAILED"
fi
exit "$fail"
