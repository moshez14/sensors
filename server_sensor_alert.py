import datetime
import json
import logging
import os
import re
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

HOST = os.getenv("HOST", "localhost")
SERVER_PORT = int(os.getenv("SERVER_PORT", "5800"))
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000/api").rstrip("/")
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/MAI")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def get_notification_sound(action_status: str) -> str:
    if action_status == "initiated":
        return "attention-cam.mp3"
    return "regular-cam.mp3"


def parse_raw_message(raw_message: str) -> Optional[Dict[str, Any]]:
    if not raw_message:
        return None

    message = raw_message.replace("\\n", "\n")
    lines = message.split("\n")
    if len(lines) < 2:
        return None

    first_line = lines[0].strip()
    if ":" in first_line:
        raw_sensor = first_line.split(":", 1)[1].strip()
    else:
        raw_sensor = first_line

    match = re.search(r"(קטע|שער)\s+\d+[\u05d0-\u05ea]*", raw_sensor)
    sensor_name = match.group(0) if match else raw_sensor
    sensor_name = unicodedata.normalize("NFC", sensor_name)

    status_word = lines[1].strip()
    normalized_status = status_word.lower().strip()

    if normalized_status.startswith("alarmed"):
        action_status = "initiated"
    elif normalized_status.startswith("default"):
        action_status = "cancelled"
    else:
        action_status = "ignored"

    return {
        "sensor_name": sensor_name,
        "status": status_word,
        "actionStatus": action_status,
    }


def build_sensor_candidates(sensor_name: str) -> list[str]:
    sensor_name = unicodedata.normalize("NFC", sensor_name.strip())
    candidates = [sensor_name]

    match = re.match(r"^(קטע|שער)\s+(\d+[\u05d0-\u05ea]*)$", sensor_name)
    if match:
        candidates.append(f"{match.group(2)} {match.group(1)}")
        base_number = re.match(r"^(\d+)", match.group(2))
        if base_number and base_number.group(1) != match.group(2):
            candidates.append(f"{match.group(1)} {base_number.group(1)}")
            candidates.append(f"{base_number.group(1)} {match.group(1)}")

    match = re.match(r"^(\d+[\u05d0-\u05ea]*)\s+(קטע|שער)$", sensor_name)
    if match:
        candidates.append(f"{match.group(2)} {match.group(1)}")
        base_number = re.match(r"^(\d+)", match.group(1))
        if base_number and base_number.group(1) != match.group(1):
            candidates.append(f"{base_number.group(1)} {match.group(2)}")
            candidates.append(f"{match.group(2)} {base_number.group(1)}")

    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def run_mongo_eval(js_code: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["mongosh", "--quiet", MONGODB_URI, "--eval", js_code],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        logger.error("Mongo shell execution failed: %s", exc)
        return None

    if result.returncode != 0:
        logger.error("Mongo shell returned %s: %s", result.returncode, result.stderr.strip())
        return None

    return result.stdout.strip()


def resolve_sensor_context(sensor_name: str) -> Optional[Dict[str, Any]]:
    candidates = build_sensor_candidates(sensor_name)
    candidates_json = json.dumps(candidates, ensure_ascii=False)
    js_code = f"""
const names = {candidates_json};
const sensors = db.sensors.find(
  {{
    isDeleted: false,
    $or: [
      {{ name: {{ $in: names }} }},
      {{ nameOnTheMap: {{ $in: names }} }}
    ]
  }},
  {{
    _id: 1,
    name: 1,
    nameOnTheMap: 1,
    companyId: 1,
    clientId: 1,
    stakeHolderType: 1,
    createdBy: 1
  }}
).toArray();

const sensor = sensors.sort((a, b) => {{
  const rank = (doc) => [
    doc.companyId ? 1 : 0,
    doc.clientId ? 1 : 0,
    doc.stakeHolderType ? 1 : 0,
  ];
  const aRank = rank(a);
  const bRank = rank(b);
  for (let i = 0; i < aRank.length; i += 1) {{
    if (aRank[i] !== bRank[i]) {{
      return bRank[i] - aRank[i];
    }}
  }}
  return 0;
}})[0] || null;

print(JSON.stringify(sensor));
"""
    output = run_mongo_eval(js_code)
    if not output:
        return None

    try:
        sensor = json.loads(output)
    except json.JSONDecodeError:
        logger.error("Failed to parse sensor context JSON: %s", output)
        return None

    if not sensor:
        logger.warning("Sensor context not found for %s (candidates=%s)", sensor_name, candidates)
        return None

    return sensor


def log_sensor_alert(sensor_name: str, action_status: str, message: str) -> None:
    sensor_context = resolve_sensor_context(sensor_name)
    if not sensor_context:
        logger.error("Skipping sensor log because sensor context could not be resolved for %s", sensor_name)
        return

    url = f"{API_BASE_URL}/sensor-logs/create"
    notification_sound = get_notification_sound(action_status)
    payload = {
        "sensorId": sensor_context.get("_id"),
        "companyId": sensor_context.get("companyId"),
        "clientId": sensor_context.get("clientId"),
        "stakeHolderType": sensor_context.get("stakeHolderType"),
        "createdBy": sensor_context.get("createdBy"),
        "updatedBy": sensor_context.get("createdBy"),
        "actionStatus": action_status,
        "notificationSound": notification_sound,
        "message": message,
    }
    logger.info("Payload=%s", payload)

    try:
        response = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Error logging sensor alert for %s: %s", sensor_name, exc)


def update_sensor(sensor_name: str, phone_number: str, action_status: str, received_at: str) -> Optional[Dict[str, Any]]:
    sensor_context = resolve_sensor_context(sensor_name)
    if not sensor_context:
        logger.error("Skipping sensor update because sensor context could not be resolved for %s", sensor_name)
        return None

    sensor_id = sensor_context.get("_id")
    updated_by = sensor_context.get("createdBy")
    updated_by_js = f'ObjectId("{updated_by}")' if updated_by else "null"
    js_code = f"""
const result = db.sensors.updateOne(
  {{ _id: ObjectId("{sensor_id}"), isDeleted: false }},
  {{
    $set: {{
      actionStatus: "{action_status}",
      updatedAt: new Date("{received_at}"),
      updatedBy: {updated_by_js}
    }}
  }}
);
print(JSON.stringify({{
  matchedCount: result.matchedCount,
  modifiedCount: result.modifiedCount,
  sensorId: "{sensor_id}",
  phoneNumber: {json.dumps(phone_number)}
}}));
"""
    output = run_mongo_eval(js_code)
    if not output:
        return None

    try:
        parsed_output = json.loads(output)
    except json.JSONDecodeError:
        logger.error("Failed to parse sensor update response: %s", output)
        return None

    if parsed_output.get("matchedCount", 0) == 0:
        logger.error("Sensor %s was not updated because no matching record was found", sensor_name)
        return None

    return parsed_output


@app.route("/", methods=["GET"])
def index():
    return jsonify({"success": True, "message": "SMS Alert Server is running"}), 200


@app.route("/sms", methods=["POST"])
def receive_sms():
    try:
        raw_message = None
        phone_number = None

        incoming_xml = request.form.get("IncomingXML")
        if incoming_xml:
            try:
                root = ET.fromstring(incoming_xml)
                raw_message = root.findtext("Message")
                phone_number = root.findtext("PhoneNumber")
            except ET.ParseError as exc:
                logger.error("Failed to parse XML: %s", exc)
                return jsonify({"status": "error", "message": "Invalid XML format"}), 400

        if not raw_message:
            data = request.get_json(force=True, silent=True)
            if data:
                raw_message = data.get("raw_message")
                phone_number = phone_number or data.get("phone_number")

        if not raw_message:
            return jsonify({"status": "error", "message": "No message content found"}), 400

        logger.info("Received SMS from %s: %s", phone_number, raw_message[:100])

        parsed = parse_raw_message(raw_message)
        if not parsed:
            logger.warning("Could not parse message: %s", raw_message)
            return jsonify({"status": "error", "message": "Could not parse message"}), 400

        logger.info("Sensor name: %s", parsed["sensor_name"])
        logger.info("Status: %s -> ActionStatus: %s", parsed["status"], parsed["actionStatus"])

        update_sensor(
            sensor_name=parsed["sensor_name"],
            phone_number=phone_number,
            action_status=parsed["actionStatus"],
            received_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )

        log_sensor_alert(
            sensor_name=parsed["sensor_name"],
            action_status=parsed["actionStatus"],
            message=raw_message,
        )

        return (
            jsonify(
                {
                    "status": "success",
                    "sensor_name": parsed["sensor_name"],
                    "actionStatus": parsed["actionStatus"],
                    "raw_status": parsed["status"],
                    "phone_number": phone_number,
                }
            ),
            200,
        )

    except Exception as exc:
        logger.exception("Error processing SMS: %s", exc)
        return jsonify({"status": "error", "message": "Internal server error"}), 500


if __name__ == "__main__":
    logger.info("Starting SMS Alert Server on port %s...", SERVER_PORT)
    app.run(host="0.0.0.0", port=SERVER_PORT)
