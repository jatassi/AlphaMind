#!/usr/bin/env bash
# run_mutation.sh <module_key>
#
# Run scoped cosmic-ray mutation testing for one AlphaMind pure-logic module,
# single-threaded and in-place (no whole-tree copy).  Writes a human-readable
# report to scripts/mutation/reports/<module_key>.txt.
#
# Supported module keys:
#   portfolio_state/computations
#   execution/regt_margin_attribution
#   execution/corporate_actions
#   distillation/q7
#   risk_guardrails/guardrail_evaluation
#   risk_guardrails/breach_behavior
#
# Prerequisites:
#   - Run from the repo root (the directory containing pyproject.toml).
#   - uv must be on PATH; cosmic-ray is fetched ephemerally via `uv run --with`.
#
# Session artifacts (.sqlite files) are written to scripts/mutation/ and are
# git-ignored.  Only the .txt reports are committed.
#
# Example:
#   ./scripts/mutation/run_mutation.sh portfolio_state/computations
set -euo pipefail

MODULE_KEY="${1:-}"

if [[ -z "$MODULE_KEY" ]]; then
    echo "Usage: $0 <module_key>" >&2
    echo "" >&2
    echo "Supported keys:" >&2
    echo "  portfolio_state/computations" >&2
    echo "  execution/regt_margin_attribution" >&2
    echo "  execution/corporate_actions" >&2
    echo "  distillation/q7" >&2
    echo "  risk_guardrails/guardrail_evaluation" >&2
    echo "  risk_guardrails/breach_behavior" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Module registry
# Each entry: MODULE_PATH|TEST_PATH|TIMEOUT
#   MODULE_PATH — path to the package directory passed to cosmic-ray
#   TEST_PATH   — pytest target (path or paths, space-separated) scoped to
#                 the module's own fast/pure tests
#   TIMEOUT     — per-mutation seconds; sized for the suite with 3x headroom
# ---------------------------------------------------------------------------
case "$MODULE_KEY" in
    "portfolio_state/computations")
        MODULE_PATH="src/alphamind/portfolio_state/computations"
        TEST_PATH="tests/portfolio_state/computations"
        TIMEOUT=30
        ;;
    "execution/regt_margin_attribution")
        MODULE_PATH="src/alphamind/execution/regt_margin_attribution"
        TEST_PATH="tests/execution/regt_margin_attribution"
        TIMEOUT=30
        ;;
    "execution/corporate_actions")
        MODULE_PATH="src/alphamind/execution/corporate_actions"
        TEST_PATH="tests/execution/corporate_actions"
        TIMEOUT=60
        ;;
    "distillation/q7")
        MODULE_PATH="src/alphamind/distillation/q7"
        TEST_PATH="tests/distillation/q7/test_compute.py"
        TIMEOUT=30
        ;;
    "risk_guardrails/guardrail_evaluation")
        MODULE_PATH="src/alphamind/risk_guardrails/guardrail_evaluation"
        TEST_PATH="tests/risk_guardrails/guardrail_evaluation"
        TIMEOUT=30
        ;;
    "risk_guardrails/breach_behavior")
        MODULE_PATH="src/alphamind/risk_guardrails/breach_behavior"
        TEST_PATH="tests/risk_guardrails/breach_behavior"
        TIMEOUT=30
        ;;
    *)
        echo "Unknown module key: $MODULE_KEY" >&2
        echo "Run '$0' without arguments to see supported keys." >&2
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRIPTS_DIR="$REPO_ROOT/scripts/mutation"
REPORTS_DIR="$SCRIPTS_DIR/reports"
SAFE_KEY="${MODULE_KEY//\//__}"  # e.g. portfolio_state__computations
SESSION_FILE="$SCRIPTS_DIR/${SAFE_KEY}.sqlite"
CONFIG_FILE="$SCRIPTS_DIR/${SAFE_KEY}.toml"
REPORT_FILE="$REPORTS_DIR/${SAFE_KEY}.txt"

mkdir -p "$REPORTS_DIR"

cd "$REPO_ROOT"

echo "=== cosmic-ray mutation run: $MODULE_KEY ==="
echo "  source : $MODULE_PATH"
echo "  tests  : $TEST_PATH"
echo "  timeout: ${TIMEOUT}s per mutation"
echo "  session: $SESSION_FILE"
echo ""

# ---------------------------------------------------------------------------
# Build per-run config from template
# ---------------------------------------------------------------------------
# The test-command uses `uv run pytest -p no:xdist` to ensure serial
# execution within each worker call (xdist inside a mutation worker would
# be counterproductive), with -x to fail fast on first failure so
# mutations are judged quickly.
TEST_CMD="uv run pytest $TEST_PATH -x -q --tb=no -p no:xdist --no-header"

sed \
    -e "s|MODULE_PATH|${MODULE_PATH}|g" \
    -e "s|TIMEOUT|${TIMEOUT}|g" \
    -e "s|TEST_CMD|${TEST_CMD}|g" \
    "$SCRIPTS_DIR/cr-template.toml" > "$CONFIG_FILE"

echo "Generated config: $CONFIG_FILE"

# ---------------------------------------------------------------------------
# Baseline check: confirm tests pass before mutating
# ---------------------------------------------------------------------------
echo ""
echo "--- Running baseline (unmutated tests) ---"
uv run --with cosmic-ray cosmic-ray baseline "$CONFIG_FILE"
echo "Baseline passed."

# ---------------------------------------------------------------------------
# Init session (work order)
# ---------------------------------------------------------------------------
echo ""
echo "--- Initialising mutation session ---"
uv run --with cosmic-ray cosmic-ray init "$CONFIG_FILE" "$SESSION_FILE" --force

# ---------------------------------------------------------------------------
# Execute mutations (single-threaded via local distributor)
# ---------------------------------------------------------------------------
echo ""
echo "--- Executing mutations (single-threaded, in-place) ---"
uv run --with cosmic-ray cosmic-ray exec "$CONFIG_FILE" "$SESSION_FILE"

# ---------------------------------------------------------------------------
# Generate human-readable report
# ---------------------------------------------------------------------------
echo ""
echo "--- Generating report ---"
{
    echo "Mutation report: $MODULE_KEY"
    echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "Source   : $MODULE_PATH"
    echo "Tests    : $TEST_PATH"
    echo "Timeout  : ${TIMEOUT}s"
    echo "============================================================"
    echo ""
    uv run --with cosmic-ray cr-report "$SESSION_FILE"
    echo ""
    echo "--- Survival rate ---"
    uv run --with cosmic-ray cr-rate "$SESSION_FILE"
} | tee "$REPORT_FILE"

echo ""
echo "Report written to: $REPORT_FILE"

# ---------------------------------------------------------------------------
# Cleanup per-run config (it's derived from the template; not needed)
# ---------------------------------------------------------------------------
rm -f "$CONFIG_FILE"

echo "Done."
