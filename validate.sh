#!/usr/bin/env bash
# One command that shows the guardrail working: unit tests, then the three
# demonstrations a reviewer actually wants to see.
#
#   ./validate.sh          tests + demos
#   ./validate.sh --demo   demos only
set -uo pipefail
cd "$(dirname "$0")"

V="python3 solution/scripts/validate_manifest.py"
BASE="tests/fixtures/manifests/baseline-evidence-a.yaml"
rc_total=0

hr() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
note() { printf '   %s\n' "$1"; }

if [ "${1:-}" != "--demo" ]; then
  hr "Test suite"
  python3 -m unittest discover -s tests -q || rc_total=1
fi

hr "1. The proposed change (Evidence B) — must be rejected"
$V --manifest tests/fixtures/manifests/proposed-evidence-b.yaml --baseline "$BASE"
rc=$?; note "exit code: $rc (expected 1)"; [ "$rc" = 1 ] || rc_total=1

hr "2. The existing CI workflow (Evidence E) — must be rejected"
$V --workflows tests/fixtures/workflows
rc=$?; note "exit code: $rc (expected 1)"; [ "$rc" = 1 ] || rc_total=1

hr "3. The remediated change — must pass"
$V --manifest tests/fixtures/manifests/remediated.yaml --baseline "$BASE"
rc=$?; note "exit code: $rc (expected 0)"; [ "$rc" = 0 ] || rc_total=1

hr "4. Fail-closed: a render that lost the workload"
$V --manifest tests/fixtures/manifests/workload-missing.yaml
rc=$?; note "exit code: $rc (expected 2 — cannot evaluate, so it does not pass)"; [ "$rc" = 2 ] || rc_total=1

hr "5. Exception mechanism: an urgent hotfix, waived and not waived"
note "--- with a valid, in-date exception (BLOCK -> CAVEAT, still needs sign-off)"
$V --manifest tests/fixtures/manifests/hotfix-rollout-capacity.yaml --baseline "$BASE" \
   --exceptions tests/fixtures/exceptions/valid --now 2026-07-28T10:00:00Z | tail -6
note "--- same exception, evaluated after it expires"
$V --manifest tests/fixtures/manifests/hotfix-rollout-capacity.yaml --baseline "$BASE" \
   --exceptions tests/fixtures/exceptions/valid --now 2026-07-28T14:00:00Z | grep -E 'REJECTED|blocking=|recommended'
note "--- an exception that tries to waive a credential finding"
$V --manifest tests/fixtures/manifests/proposed-evidence-b.yaml --baseline "$BASE" \
   --exceptions tests/fixtures/exceptions/non-waivable --now 2026-07-28T10:00:00Z | grep -E 'REJECTED|recommended'

hr "Result"
if [ "$rc_total" = 0 ]; then
  echo "All checks behaved as documented."
else
  echo "Something did not behave as documented — see above." >&2
fi
exit "$rc_total"
