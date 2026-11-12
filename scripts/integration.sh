#!/usr/bin/env bash
# Deliberate integration tests against real external services.
#
# Nothing here runs as part of the normal test suite. Each target additionally
# refuses to run unless its feature flag and credential are configured, so
# running the wrong one is inert rather than expensive.
set -euo pipefail

PY="${PY:-.venv/bin/python}"
usage() {
  cat <<'USAGE'
Usage: scripts/integration.sh <target>

Targets:
  google     One real Google Places request (needs FEATURE_REAL_DISCOVERY=true
             and GOOGLE_MAPS_API_KEY; consumes one unit of the monthly quota).
  claude     One real AI scoring call (needs FEATURE_AI_SCORING=true and an
             authenticated Claude Code CLI).
  injection  Verifies a real model ignores instructions embedded in a page.
  preview    Real crawl -> render -> QA of a fixture site. No credentials, no cost.
  gmail      Sends ONE real email to your own authenticated address. Needs
             FEATURE_GMAIL_SEND=true, OUTREACH_SEND_ENABLED=true and
             OUTREACH_INTEGRATION_TEST_EMAIL set to that same address.
  all-safe   Runs only the targets that cost nothing (preview).
USAGE
}

target="${1:-}"
case "$target" in
  google)    exec "$PY" -m pytest -m integration -q -s tests/test_integration.py::test_real_google_places ;;
  claude)    exec "$PY" -m pytest -m integration -q -s tests/test_integration.py::test_real_claude_scoring ;;
  injection) exec "$PY" -m pytest -m integration -q -s tests/test_integration.py::test_real_claude_ignores_injected_instructions ;;
  preview)   exec "$PY" -m pytest -m integration -q -s tests/test_integration.py::test_real_preview_build ;;
  gmail)
    echo "This sends a REAL email to ${OUTREACH_INTEGRATION_TEST_EMAIL:-<unset>}."
    read -r -p "Type SEND to continue: " confirm
    [ "$confirm" = "SEND" ] || { echo "aborted"; exit 1; }
    exec "$PY" -m pytest -m integration -q -s tests/test_integration.py::test_real_gmail_send_to_self
    ;;
  all-safe)  exec "$PY" -m pytest -m integration -q -s tests/test_integration.py::test_real_preview_build ;;
  *)         usage; exit 1 ;;
esac
