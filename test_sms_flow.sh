#!/bin/bash
# Sends a test SMS to the sensor alert server's /sms endpoint and verifies
# the resulting actionStatus change AND message-log entry actually land in
# MongoDB (via readDB).
#
# Env overrides:
#   SENSOR_ALERT_URL   default http://localhost:5800
#   MONGO_URI          default mongodb://localhost:27022/MAI
#   SENSOR_NAME        default: auto-picks the first non-deleted sensor in Mongo
#   PHONE_NUMBER       default +972500000000
set -euo pipefail

SENSOR_ALERT_URL="${SENSOR_ALERT_URL:-http://localhost:5800}"
MONGO_URI="${MONGO_URI:-mongodb://localhost:27022/MAI}"
PHONE_NUMBER="${PHONE_NUMBER:-+972500000000}"

mongo_eval() {
  mongosh --quiet "$MONGO_URI" --eval "$1"
}

if [ -z "${SENSOR_NAME:-}" ]; then
  SENSOR_NAME=$(mongo_eval '
    const s = db.sensors.findOne({isDeleted: {$ne: true}}, {name: 1});
    print(s ? s.name : "");
  ')
fi

if [ -z "$SENSOR_NAME" ]; then
  echo "No sensor found in Mongo to test against (and SENSOR_NAME not set). Aborting."
  exit 1
fi

sensor_name_json=$(printf '%s' "$SENSOR_NAME" | jq -Rs .)

SENSOR_ID=$(mongo_eval "
  const s = db.sensors.findOne({name: ${sensor_name_json}, isDeleted: {\$ne: true}}, {_id: 1});
  print(s ? s._id.toString() : '');
")

if [ -z "$SENSOR_ID" ]; then
  echo "Could not resolve sensor _id for '$SENSOR_NAME'. Aborting."
  exit 1
fi

echo "Sensor alert server : $SENSOR_ALERT_URL"
echo "Mongo               : $MONGO_URI"
echo "Test sensor          : '$SENSOR_NAME' ($SENSOR_ID)"
echo

get_action_status() {
  mongo_eval "
    const s = db.sensors.findOne({name: ${sensor_name_json}, isDeleted: {\$ne: true}}, {actionStatus: 1});
    print(s ? (s.actionStatus || 'null') : 'MISSING');
  "
}

count_message_logs() {
  mongo_eval "print(db.sensorlogs.countDocuments({sensorId: ObjectId('$SENSOR_ID')}));"
}

latest_message_log() {
  mongo_eval "
    const l = db.sensorlogs.find({sensorId: ObjectId('$SENSOR_ID')}).sort({\$natural: -1}).limit(1).next();
    print(l ? JSON.stringify({message: l.message, actionStatus: l.actionStatus}) : 'NONE');
  "
}

send_sms() {
  local status_word="$1"
  local raw_message
  raw_message=$(printf 'Alert: %s\n%s' "$SENSOR_NAME" "$status_word")
  curl -s -X POST "$SENSOR_ALERT_URL/sms" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg msg "$raw_message" --arg phone "$PHONE_NUMBER" '{raw_message: $msg, phone_number: $phone}')"
}

check_transition() {
  local status_word="$1" expected="$2"
  echo "--- Sending status word '$status_word' (expect actionStatus='$expected' and a new message log) ---"

  before_status=$(get_action_status)
  before_log_count=$(count_message_logs)
  echo "  before: actionStatus=$before_status, message log count=$before_log_count"

  response=$(send_sms "$status_word")
  echo "  /sms response: $response"

  sleep 1
  after_status=$(get_action_status)
  after_log_count=$(count_message_logs)
  echo "  after:  actionStatus=$after_status, message log count=$after_log_count"

  if [ "$after_status" != "$expected" ]; then
    echo "  FAIL: expected actionStatus '$expected', got '$after_status'"
    exit 1
  fi

  if [ "$after_log_count" -le "$before_log_count" ]; then
    echo "  FAIL: no new message log entry was created for sensor '$SENSOR_NAME'"
    exit 1
  fi

  echo "  latest message log: $(latest_message_log)"
  echo "  PASS"
  echo
}

check_transition "Alarmed" "initiated"

echo "All checks passed: /sms -> readDB -> MongoDB round trip (actionStatus + message log) verified for '$SENSOR_NAME'."
