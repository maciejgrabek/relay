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
# Same defense for the workspaces file: any suite touching wsconfig.py must
# never write the developer's real ~/.relay/workspaces.toml.
export RELAY_WORKSPACES="$(mktemp -d)/relay-test-workspaces.toml"
# Same defense for the two append-only logs. notify_mac() emits an event on
# every call, and any suite that drives a real execution path calls
# audit.record() - without these, a test run appends to the developer's real
# ~/.relay/events.jsonl and ~/.relay/audit.jsonl.
export RELAY_EVENTS_LOG="$(mktemp -d)/relay-test-events.jsonl"
export RELAY_AUDIT_LOG="$(mktemp -d)/relay-test-audit.jsonl"
# The boot screen intentionally eats the first keypress ("any key skips"), so
# any suite driving a Textual pilot must run with it off.
export RELAY_NO_BOOT=1

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
