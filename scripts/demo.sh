#!/usr/bin/env bash
# The safe fixture demo: end-to-end with every external feature flag OFF.
# No credentials, no network beyond loopback, no email, no publishing.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

echo "==> feature flags"
"$PY" -m outreach features

echo
echo "==> initialising the database"
"$PY" -m outreach init-db

echo
echo "==> running the pipeline against local fixture websites (dry run)"
CRAWL_DELAY_SECONDS=0 "$PY" -m outreach run \
  --vertical dentist --city Phoenix --state AZ --dry-run

echo
echo "==> prospects"
"$PY" -m outreach prospects --campaign-id 1

echo
echo "==> funnel"
"$PY" -m outreach stats --campaign-id 1

echo
echo "Done. No email was sent, no paid API was called, nothing was published."
echo "To see generated previews, enable FEATURE_SITE_BUILDING=true and re-run,"
echo "then: $PY -m outreach previews serve"
