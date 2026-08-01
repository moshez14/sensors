#!/bin/bash
cd "$(dirname "${BASH_SOURCE[0]}")"
pwd
source venv/bin/activate
which python3
python3 server_sensor_alert.py

