#!/bin/bash
# Verifies the deployed sandbox-sensors docker container is up and healthy,
# then runs the same /sms -> readDB -> MongoDB round trip as test_sms_flow.sh
# against it (rather than any local, non-containerized instance).
#
# Env overrides:
#   CONTAINER_NAME     default sandbox-sensors
#   SENSOR_ALERT_URL   default http://localhost:5805
#   MONGO_URI          default mongodb://localhost:27022/MAI
#   SENSOR_NAME        default: auto-picks the first non-deleted sensor in Mongo
#   PHONE_NUMBER       default +972500000000
set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-sandbox-sensors}"
SENSOR_ALERT_URL="${SENSOR_ALERT_URL:-http://localhost:5805}"
MONGO_URI="${MONGO_URI:-mongodb://localhost:27022/MAI}"

echo "--- Checking container '$CONTAINER_NAME' is running ---"
status=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "MISSING")
if [ "$status" != "running" ]; then
  echo "FAIL: container '$CONTAINER_NAME' is not running (status: $status)"
  exit 1
fi
image=$(docker inspect -f '{{.Config.Image}}' "$CONTAINER_NAME")
echo "  OK: $CONTAINER_NAME is running ($image)"
echo

echo "--- Checking HTTP root endpoint ($SENSOR_ALERT_URL/) ---"
http_code=$(curl -s -o /dev/null -w "%{http_code}" "$SENSOR_ALERT_URL/")
if [ "$http_code" != "200" ]; then
  echo "FAIL: expected HTTP 200 from $SENSOR_ALERT_URL/, got $http_code"
  exit 1
fi
echo "  OK: $SENSOR_ALERT_URL/ returned 200"
echo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "--- Running SMS -> readDB -> MongoDB round trip against $SENSOR_ALERT_URL ---"
echo
SENSOR_ALERT_URL="$SENSOR_ALERT_URL" MONGO_URI="$MONGO_URI" \
  bash "${SCRIPT_DIR}/test_sms_flow.sh"

echo
echo "All checks passed: '$CONTAINER_NAME' container is up and its full SMS flow works."
