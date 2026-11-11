#!/bin/bash
# Run the verification suite on macOS or Linux after setup.sh, with the python3 of the virtual
# environment .venv that mise activates:
#     mise exec -- bash dev/verify.sh [report-dir]
# Preserve the check's exit status and all working-tree changes, including changed figures.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
OUT=${1:-${TMPDIR:-/tmp}}
cd "$ROOT"
export MPLBACKEND=Agg
mkdir -p "$OUT"
commit=$(git rev-parse --short=8 HEAD)
report="$OUT/verify-$commit.txt"
log="$OUT/verify-$commit.log"
# A report of an earlier verification of the same commit would otherwise be shown for a run that
# exits before it writes its own report, such as a run that refuses the environment.
rm -f "$report"
runner=(python3 -m agentcontext verify "$report")
if [ "$(uname -s)" = Linux ]; then
    runner+=(--analysis-output-dir "$OUT/analysis")
fi
# Container PID 1 may not reap children killed by the process-group tests.
if [ "$(uname -s)" = Linux ] && command -v tini >/dev/null; then
    runner=(tini -s -- "${runner[@]}")
fi
status=0
"${runner[@]}" > "$log" 2>&1 || status=$?
if [ -f "$report" ]; then
    echo "The verification of $commit exited $status. Its report is $report."
    grep -E "^(PASS|FAIL|ERROR)[: ]" "$report" || true
else
    echo "The verification of $commit exited $status without a report. Its log $log follows."
    cat "$log"
fi
git status --short
exit "$status"
