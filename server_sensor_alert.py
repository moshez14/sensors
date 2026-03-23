import datetime
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

HOST = os.getenv("HOST", "localhost")
SERVER_PORT = int(os.getenv("SERVER_PORT", "5800"))
API_BASE_URL = f"http://dev.maifocus.com:5500"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def parse_raw_message(raw_message: str) -> Optional[Dict[str, Any]]:
    """
    Parse raw_message to extract sensor name and action status.

    Expected format (lines separated by real newlines or literal \\n):
        Sensor:<sensor_name>
        <status>
        <datetime>

    actionStatus mapping:
        Alarmed  -> Red
        Default  -> Yellow
        anything else -> Green
    """
    if not raw_message:
        return None

    # Normalise literal \n sequences (from XML/form data) to real newlines
    message = raw_message.replace('\\n', '\n')

    lines = message.split('\n')
    if len(lines) < 2:
        return None

    # Line 0: "Sensor:<sensor_name>" (colon may or may not have a space after it)
    first_line = lines[0].strip()
    if ':' in first_line:
        raw_sensor = first_line.split(':', 1)[1].strip()
    else:
        raw_sensor = first_line

    # Extract "קטע XX" or "שער XX" pattern (word + number, optional trailing letters)
    match = re.search(r'(קטע|שער)\s+\d+[\u05d0-\u05ea]*', raw_sensor)
    #match = re.search(r'(קטע|שער)\s+\d+\S*', raw_sensor)
    print(f"MATCH={match} ")
    #match = re.search(r'^(קטע|שער)\s+\d+', raw_sensor)
    sensor_name = match.group(0) if match else raw_sensor
    import unicodedata

    sensor_name = unicodedata.normalize('NFC', sensor_name)

    # Line 1: status word
    status_word = lines[1].strip()

    if status_word.lower() == 'alarmed':
        action_status = 'initiated'
    elif status_word.lower() == 'default':
        action_status = 'cancelled'
    else:
        action_status = 'ignored'

    return {
        "sensor_name": sensor_name,
        "status": status_word,
        "actionStatus": action_status,
    }


def log_sensor_alert(sensor_name: str, action_status: str, message: str) -> None:
    """Log sensor alert event to sensorLogs via database API."""
    import requests
    import json

    url = f"{API_BASE_URL}/add_sensor_log"
    payload = {
        "sensor_name": sensor_name,
        "actionStatus": action_status,
        "message": message,
    }
    logger.info(f"Payload={payload}")
    try:
        headers = {
            'Content-Type': 'application/json'
        }
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"SENSOR NAME=B{sensor_name}B")
        logger.error(f"Error logging sensor alert for B{sensor_name}B: {e}")


def update_sensor(sensor_name: str, phone_number: str, action_status: str, received_at: str) -> Optional[Dict[str, Any]]:
    """Update sensor document with actionStatus via database API."""
    url = f"{API_BASE_URL}/update_sensor"
    payload = {
        "sensor_name": sensor_name,
        "actionStatus": action_status
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error updating sensor {sensor_name}: {e}")
        return None
    except (ValueError, KeyError) as e:
        logger.error(f"Error parsing update sensor response: {e}")
        return None


@app.route("/", methods=["GET"])
def index():
    """Health check endpoint."""
    return jsonify({
        "success": True,
        "message": "SMS Alert Server is running",
    }), 200


@app.route("/sms", methods=["POST"])
def receive_sms():
    """
    Handle incoming SMS messages.

    Accepts either:
    1. Form POST with IncomingXML field (from SMS gateway)
    2. JSON body with raw_message field
    """
    try:
        raw_message = None
        phone_number = None

        # --- Form data with IncomingXML (SMS gateway format) ---
        incoming_xml = request.form.get("IncomingXML")
        if incoming_xml:
            try:
                root = ET.fromstring(incoming_xml)
                raw_message = root.findtext("Message")
                phone_number = root.findtext("PhoneNumber")
            except ET.ParseError as e:
                logger.error(f"Failed to parse XML: {e}")
                return jsonify({"status": "error", "message": "Invalid XML format"}), 400

        # --- JSON body fallback ---
        if not raw_message:
            data = request.get_json(force=True, silent=True)
            if data:
                raw_message = data.get("raw_message")
                phone_number = phone_number or data.get("phone_number")

        if not raw_message:
            return jsonify({"status": "error", "message": "No message content found"}), 400

        logger.info(f"Received SMS from {phone_number}: {raw_message[:100]}")

        parsed = parse_raw_message(raw_message)
        if not parsed:
            logger.warning(f"Could not parse message: {raw_message}")
            return jsonify({"status": "error", "message": "Could not parse message"}), 400

        logger.info(f"Sensor name: {parsed['sensor_name']}")
        logger.info(f"Status: {parsed['status']} -> ActionStatus: {parsed['actionStatus']}")

        # update_sensor(
        #     sensor_name=parsed["sensor_name"],
        #     phone_number=phone_number,
        #     action_status=parsed["actionStatus"],
        #     received_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
        # )

        log_sensor_alert(
            sensor_name=parsed["sensor_name"],
            action_status=parsed["actionStatus"],
            message=raw_message,
        )

        return jsonify({
            "status": "success",
            "sensor_name": parsed["sensor_name"],
            "actionStatus": parsed["actionStatus"],
            "raw_status": parsed["status"],
            "phone_number": phone_number,
        }), 200

    except Exception as e:
        logger.exception(f"Error processing SMS: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500


if __name__ == "__main__":
    logger.info(f"Starting SMS Alert Server on port {SERVER_PORT}...")
    app.run(host="0.0.0.0", port=SERVER_PORT)
