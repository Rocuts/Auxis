#!/usr/bin/env bash
# Ingest the five fixture PDFs into a deployed instance and report each job's
# fate.
#
# Usage:
#   BASE_URL=https://<deployment> API_KEY=... scripts/seed_remote.sh
#   scripts/seed_remote.sh http://localhost:8000        # positional override
#
# Optional:
#   VERCEL_AUTOMATION_BYPASS_SECRET  sent as x-vercel-protection-bypass, for
#                                    preview deployments behind Deployment
#                                    Protection.
#   POLL_SECONDS                     how long to wait for a job to finish
#                                    (default 180).
#
# SET POLL_SECONDS DELIBERATELY. The default predates the wall-clock
# measurements: the slowest single document measured 346 s sequentially and
# longer under fan-out, so 180 reports a timeout on a run that is merely slow.
# The default is left alone rather than guessed upward — a poll budget is a
# property of the deployment being seeded (`maxDuration`, cron `limit`, the
# provider's rate limit), not of this script. Against the current Vercel
# configuration (`maxDuration` 1800), POLL_SECONDS=1800 is the honest value.
#
# Exit status is the point: non-zero if any upload was rejected or any job
# ended in `failed`, so this is usable as a gate and not only as a demo. A
# job that is still `queued`/`running` when the poll budget runs out is
# reported as a timeout, never quietly counted as success.
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-http://localhost:8000}}"
BASE_URL="${BASE_URL%/}"
API_KEY="${API_KEY:-}"
POLL_SECONDS="${POLL_SECONDS:-180}"
FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../fixtures" && pwd)"

if [[ -z "${API_KEY}" ]]; then
  echo "error: API_KEY is required (POST /documents is authenticated)" >&2
  exit 2
fi

CURL_COMMON=(--silent --show-error --max-time 60 -H "X-API-Key: ${API_KEY}")
if [[ -n "${VERCEL_AUTOMATION_BYPASS_SECRET:-}" ]]; then
  CURL_COMMON+=(-H "x-vercel-protection-bypass: ${VERCEL_AUTOMATION_BYPASS_SECRET}")
  CURL_COMMON+=(-H "x-vercel-set-bypass-cookie: false")
fi

echo "seeding ${BASE_URL}"
failures=0
declare -a job_ids=()
declare -a job_names=()

for pdf in "${FIXTURES_DIR}"/0*.pdf; do
  name="$(basename "${pdf}")"
  # x-filename is the only way to name an uploaded document: the body is raw
  # PDF bytes, so there is no multipart part name to read one from. Without it
  # every row in GET /documents reads `upload.pdf`, which is what the live
  # dataset shows for uploads made before this was added.
  response="$(curl "${CURL_COMMON[@]}" \
    -w '\n%{http_code}' \
    -H 'Content-Type: application/pdf' \
    -H "x-filename: ${name}" \
    --data-binary "@${pdf}" \
    "${BASE_URL}/documents")"
  status="$(tail -n1 <<<"${response}")"
  body="$(sed '$d' <<<"${response}")"

  if [[ "${status}" != "202" ]]; then
    echo "  ✗ ${name}: HTTP ${status} ${body}"
    failures=$((failures + 1))
    continue
  fi
  job_id="$(sed -n 's/.*"job_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"${body}")"
  duplicate="$(sed -n 's/.*"duplicate"[[:space:]]*:[[:space:]]*\([a-z]*\).*/\1/p' <<<"${body}")"
  echo "  → ${name}: 202 job=${job_id} duplicate=${duplicate}"
  job_ids+=("${job_id}")
  job_names+=("${name}")
done

echo "polling ${#job_ids[@]} job(s), budget ${POLL_SECONDS}s"
deadline=$(( $(date +%s) + POLL_SECONDS ))
for i in "${!job_ids[@]}"; do
  job_id="${job_ids[$i]}"
  name="${job_names[$i]}"
  while :; do
    body="$(curl "${CURL_COMMON[@]}" "${BASE_URL}/jobs/${job_id}")"
    status="$(sed -n 's/.*"status"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"${body}")"
    case "${status}" in
      succeeded)
        persisted="$(sed -n 's/.*"records_persisted"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' <<<"${body}")"
        echo "  ✓ ${name}: succeeded, ${persisted:-0} record(s) persisted"
        break
        ;;
      failed)
        echo "  ✗ ${name}: failed — ${body}"
        failures=$((failures + 1))
        break
        ;;
      *)
        if (( $(date +%s) >= deadline )); then
          echo "  ✗ ${name}: still '${status}' when the ${POLL_SECONDS}s budget expired"
          failures=$((failures + 1))
          break
        fi
        sleep 3
        ;;
    esac
  done
done

if (( failures > 0 )); then
  echo "FAILED: ${failures} problem(s)" >&2
  exit 1
fi
echo "OK: all fixtures ingested"
