#!/usr/bin/env sh
# Regenerate the committed demo transcripts under docs/demo/.
#
# Offline and credential-free: the synthetic mock estate and the fake LLM, exactly what
# CI runs. Every command below is a separate OS process, and the only thing they share
# is the state directory on disk -- which is the point of the approval transcript.
#
#   sh scripts/demo.sh
#
# The state directory is under tmp/, which is gitignored, and is wiped first so the
# transcripts do not depend on whatever a previous run left behind.
set -eu

OUT=docs/demo
STATE=tmp/demo-state
mkdir -p "$OUT"

export MODE=mock
export LLM_BACKEND=fake
export STATE_DIR="$STATE"
# The structured JSON logs go to stderr and repeat what the summary already says, with
# a local timestamp on every line. Warnings and errors still come through.
export LOG_LEVEL=warning

# Show a command the way a reader would type it, then run it as `uv run --quiet`.
# The first argument is the display form: re-joining "$@" would lose the quotes around
# a multi-word --reason.
run() {
    shown=$1
    shift
    printf '$ %s\n' "$shown"
    uv run --quiet costsentinel "$@" 2>&1
    printf '\n'
}

header() {
    printf '# %s\n' "$1"
    printf '# Regenerate with: sh scripts/demo.sh\n'
    printf '# Synthetic mock estate, fake LLM, no credentials, no network.\n'
    printf '# Each $ line runs as `uv run --quiet <command>`, a separate OS process.\n'
    printf '# STATE_DIR=%s, wiped before this transcript.\n\n' "$STATE"
}

APPROVE_REASON="confirmed orphaned; platform team will delete it"
REJECT_REASON="owner confirmed this is a quarter-end batch host"

# --- 1. A gated sweep halts, the process exits, a later process decides -------------
rm -rf "$STATE"
{
    header "A sweep halts for approval, and a different process resumes it"
    printf '# Process 1: scan. Every proposed action is gated, so the sweep writes its\n'
    printf '# report, checkpoints, and the process exits with nine actions waiting.\n'
    run "costsentinel scan --client northwind-energy --summary" \
        scan --client northwind-energy --summary
    printf '# Process 2: nothing in memory survives from process 1 -- only the file.\n'
    run "costsentinel approvals list" approvals list
    printf '# Process 3: approve one item. Approving hands it to a person and records\n'
    printf '# that; CostSentinel has no execution path and changes nothing.\n'
    run "costsentinel approvals decide rec-orphdisk-2923594012 --approve --reason \"$APPROVE_REASON\" --approver ops@example.com" \
        approvals decide rec-orphdisk-2923594012 --approve --reason "$APPROVE_REASON" \
        --approver ops@example.com
    printf '# Process 4: the approved item has left the queue; eight remain.\n'
    run "costsentinel approvals list" approvals list
} > "$OUT/approval-resume.txt"

# --- 2. Declined advice is demoted on the next sweep; no figure moves ----------------
rm -rf "$STATE"
{
    header "Declined advice is demoted on the next sweep, and no figure moves"
    printf '# Sweep 1. Findings are written to episodic memory.\n'
    run "costsentinel scan --client northwind-energy --summary" \
        scan --client northwind-energy --summary
    printf '# The client declines the top finding.\n'
    run "costsentinel decide rec-idlevm-4d7f269994 --reject --reason \"$REJECT_REASON\" --by client@example.com" \
        decide rec-idlevm-4d7f269994 --reject --reason "$REJECT_REASON" --by client@example.com
    printf '# Sweep 2. The declined finding is annotated and ranked lower;\n'
    printf '# every saving is the same figure as in sweep 1.\n'
    run "costsentinel scan --client northwind-energy --summary" \
        scan --client northwind-energy --summary
} > "$OUT/learning-loop.txt"

rm -rf "$STATE"
echo "wrote $OUT/approval-resume.txt and $OUT/learning-loop.txt"
