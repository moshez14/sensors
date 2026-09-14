import json
import logging
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

HOST = os.getenv("HOST", "localhost")
SERVER_PORT = int(os.getenv("SERVER_PORT", "5800"))
READDB_BASE_URL = os.getenv("READDB_BASE_URL", "http://localhost:5500").rstrip("/")

# Forward the original SMS HTTP request exactly as received.
FORWARD_SMS_URL = os.getenv(
    "FORWARD_SMS_URL",
    "https://bsh.maifocus.com/sms"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def forward_sms() -> bool:
    """
    Forward the exact original HTTP request body to the remote server.

    The raw body is forwarded without converting it to JSON or
    reconstructing the IncomingXML field.

    This means the receiving server gets the same request payload
    format as this server received.
    """

    try:
        # Preserve the original Content-Type.
        content_type = request.content_type or ""

        # Get the exact raw request body.
        #
        # cache=True ensures Flask keeps the body available for the
        # remaining local processing.
        raw_body = request.get_data(cache=True)

        headers = {}

        if content_type:
            headers["Content-Type"] = content_type

        logger.info(
            "Forwarding original SMS request to %s "
            "(Content-Type: %s, Size: %s bytes)",
            FORWARD_SMS_URL,
            content_type,
            len(raw_body),
        )

        response = requests.post(
            FORWARD_SMS_URL,
            data=raw_body,
            headers=headers,
            timeout=10,
        )

        response.raise_for_status()

        logger.info(
            "Original SMS request successfully forwarded to %s "
            "(status=%s)",
            FORWARD_SMS_URL,
            response.status_code,
        )

        return True

    except requests.exceptions.RequestException as exc:

        logger.error(
            "Failed to forward original SMS request to %s: %s",
            FORWARD_SMS_URL,
            exc,
        )

        return False


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


def _extract_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        return value.get("$oid")
    if isinstance(value, str):
        return value
    return None


def fetch_all_sensors() -> List[Dict[str, Any]]:
    url = f"{READDB_BASE_URL}/get_sensors"
    try:
        response = requests.get(url, timeout=10)
    except requests.exceptions.RequestException as exc:
        logger.error("Failed to reach readDB get_sensors: %s", exc)
        return []

    if response.status_code == 404:
        return []

    try:
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("readDB get_sensors returned an error: %s", exc)
        return []

    try:
        return response.json().get("sensors", [])
    except ValueError:
        logger.error("Failed to parse readDB get_sensors response: %s", response.text)
        return []


def resolve_sensor_context(sensor_name: str) -> Optional[Dict[str, Any]]:
    candidates = set(build_sensor_candidates(sensor_name))

    matches = [
        sensor
        for sensor in fetch_all_sensors()
        if not sensor.get("isDeleted")
        and (sensor.get("name") in candidates or sensor.get("nameOnTheMap") in candidates)
    ]

    if not matches:
        logger.warning("Sensor context not found for %s (candidates=%s)", sensor_name, sorted(candidates))
        return None

    def rank(doc: Dict[str, Any]) -> tuple:
        return (
            1 if doc.get("companyId") else 0,
            1 if doc.get("clientId") else 0,
            1 if doc.get("stakeHolderType") else 0,
        )

    best = max(matches, key=rank)

    return {
        "_id": _extract_id(best.get("_id")),
        "name": best.get("name"),
        "nameOnTheMap": best.get("nameOnTheMap"),
        "companyId": _extract_id(best.get("companyId")),
        "clientId": _extract_id(best.get("clientId")),
        "stakeHolderType": best.get("stakeHolderType"),
        "createdBy": _extract_id(best.get("createdBy")),
    }


def log_sensor_alert(sensor_name: str, action_status: str, message: str, sensor_id: Optional[str] = None) -> None:
    url = f"{READDB_BASE_URL}/add_sensor_log"
    payload = {
        "sensor_id": sensor_id,
        "sensor_name": sensor_name,
        "actionStatus": action_status,
        "message": message,
    }
    logger.info("Payload=%s", payload)

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Error logging sensor alert for %s: %s", sensor_name, exc)


def update_sensor(sensor_context: Optional[Dict[str, Any]], phone_number: str, action_status: str) -> Optional[Dict[str, Any]]:
    if not sensor_context:
        logger.error("Skipping sensor update because sensor context could not be resolved")
        return None

    url = f"{READDB_BASE_URL}/update_sensor"
    payload = {
        "sensor_id": sensor_context.get("_id"),
        "sensor_name": sensor_context.get("name"),
        "actionStatus": action_status,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Error updating sensor %s via readDB: %s", sensor_context.get("name"), exc)
        return None

    try:
        result = response.json()
    except ValueError:
        logger.error("Failed to parse readDB update_sensor response: %s", response.text)
        return None

    result["sensorId"] = sensor_context.get("_id")
    result["phoneNumber"] = phone_number
    return result


@app.route("/", methods=["GET"])
def index():
    return jsonify({"success": True, "message": "SMS Alert Server is running"}), 200


@app.route("/sms", methods=["POST"])
def receive_sms():
    try:
        # IMPORTANT: read and cache the original request body FIRST,
        # before accessing request.form, which consumes the request
        # stream. This ensures forward_sms() can still forward the
        # exact original body later.
        request.get_data(cache=True)

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
                # Even invalid XML has already been received.
                # Forward the original request before returning.
                forward_sms()
                return jsonify({"status": "error", "message": "Invalid XML format"}), 400

        if not raw_message:
            data = request.get_json(force=True, silent=True)
            if data:
                raw_message = data.get("raw_message")
                phone_number = phone_number or data.get("phone_number")

        # Forward every SMS request exactly as received, regardless of
        # whether it can be parsed successfully.
        forward_sms()

        if not raw_message:
            return jsonify({"status": "error", "message": "No message content found"}), 400

        logger.info("Received SMS from %s: %s", phone_number, raw_message[:100])

        parsed = parse_raw_message(raw_message)
        if not parsed:
            logger.warning("Could not parse message: %s", raw_message)
            return jsonify({"status": "error", "message": "Could not parse message"}), 400

        logger.info("Sensor name: %s", parsed["sensor_name"])
        logger.info("Status: %s -> ActionStatus: %s", parsed["status"], parsed["actionStatus"])

        sensor_context = resolve_sensor_context(parsed["sensor_name"])

        update_sensor(
            sensor_context=sensor_context,
            phone_number=phone_number,
            action_status=parsed["actionStatus"],
        )

        log_sensor_alert(
            sensor_name=sensor_context.get("name") if sensor_context else parsed["sensor_name"],
            action_status=parsed["actionStatus"],
            message=raw_message,
            sensor_id=sensor_context.get("_id") if sensor_context else None,
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
